"""CLI entry point: run | gui | list-windows | manage-windows | calibrate | dump-cells
| dump-colors | bench | screen-template | tune | synth."""

from __future__ import annotations

import argparse
import logging
import shutil
import signal
import statistics
import sys
import time
import tomllib
from pathlib import Path

import numpy as np

from . import capture as capture_module
from . import synth, windowmgr
from .capture import (
    XPLANE_WINDOW_CLASS,
    CaptureError,
    FrameSource,
    ImageCapture,
    list_windows,
    sources_for,
)
from .color import BLACK, background_name, measure_cell
from .config import (
    AppConfig,
    ConfigError,
    ConfigNotFound,
    DisplayConfig,
    default_config,
    load_config,
)
from .ocr import OcrUnavailable, SoftkeyReader
from .pipeline import DisplayPipeline, DisplayResult
from .publish import Value, create_publisher
from .strip import (
    auto_detect_strip,
    crop_strip,
    finish_cell,
    is_blank,
    overlay_geometry,
    ring_bright_fraction,
    split_cells,
    threshold_cell,
)

LOG = logging.getLogger("glasslinkxp")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _open_sources(config: AppConfig, image: str | None) -> dict[str, FrameSource]:
    return sources_for(config.active_displays, image)


def _log_report(report: windowmgr.Report, routine: bool) -> None:
    """Say what a management pass did, at a volume that suits how often it runs.

    The pass made on the way up is worth a line per display: it is the record
    of what the daemon did to somebody's screen before it started. A pass made
    later, to put a closed window back, is ``routine`` -- there the displays it
    found already correct are the ones it did not come for, and saying so at
    INFO buries the label lines the log is actually for. Anything that
    *changed*, or failed, is worth a line whenever it happens.
    """
    if report.skipped or not report.xplane_running:
        for line in report.lines():
            (LOG.debug if routine else LOG.info)("window management: %s", line)
        return
    for outcome in report.outcomes:
        if not outcome.ok:
            LOG.warning("window management: %s", outcome)
        elif routine and outcome.action == "already":
            LOG.debug("window management: %s", outcome)
        else:
            LOG.info("window management: %s", outcome)


def _manage_windows(
    config: AppConfig, image: str | None, routine: bool = False
) -> windowmgr.Report:
    """Open, size and place the pop-outs before anything tries to capture them.

    A no-op when reading PNGs: there is no window behind an ``--image`` run, so
    there is nothing to manage and firing pop-out commands at whatever sim
    happens to be running would be a surprise.
    """
    if image is not None:
        return windowmgr.Report(skipped="reading from --image, so no window is managed.")
    report = windowmgr.manage_windows(config)
    _log_report(report, routine)
    return report


#: How long to wait before trying a closed window again, when reopening it did
#: not work. Nothing polls on this: a closed window is acted on the moment the
#: capture reports it. The interval only stops a *failed* reopen -- X-Plane
#: shut down, say -- from enumerating the desktop and firing pop-out commands
#: on every cycle of the loop for as long as the daemon runs.
REOPEN_RETRY_INTERVAL = 5.0


def _reopen_closed(
    config: AppConfig,
    image: str | None,
    display: DisplayConfig,
    sources: dict[str, FrameSource],
) -> None:
    """Put back a pop-out that has been closed, and capture it again.

    Reached only when the capture backend has said the window went away, which
    it reports through ``on_closed``. A WGC session does not survive its
    window, so there is no reconnecting the existing source: the window has to
    exist again and a new source be built on it.

    The window is rebuilt whether this pass reopened it or found it already
    back -- a user who closes a pop-out and immediately reopens it themselves
    leaves a window that needs no managing and a capture that is dead anyway.
    """
    if image is not None or not config.window_management.enabled:
        return
    report = _manage_windows(config, image, routine=True)
    if display.key not in report.managed:
        return  # no window to attach to yet; the retry interval applies
    try:
        rebuilt = sources_for([display], None)[display.key]
    except CaptureError as exc:
        LOG.warning("%s is open again, but could not be captured: %s", display.key, exc)
        return
    try:
        sources[display.key].close()
    except Exception as exc:  # noqa: BLE001 - the old one is being discarded anyway
        LOG.debug("ignoring error closing the old %s capture: %s", display.key, exc)
    sources[display.key] = rebuilt
    LOG.info("%s was closed and has been reopened; capturing it again", display.key)


def _grab(source: FrameSource, retries: int = 25, delay: float = 0.2) -> np.ndarray:
    """First frame, with a short grace period for WGC to deliver one."""
    for _ in range(retries):
        frame = source.grab()
        if frame is not None:
            return frame
        time.sleep(delay)
    raise CaptureError(f"no frame arrived from {source.name}")


def _values(display: DisplayConfig, result: DisplayResult) -> dict[str, Value]:
    """The dataref writes for one display: a label and a colour per cell.

    The label goes out as the sim draws it, with nothing prepended. What
    colour to draw it in is the Stream Deck's decision to make from the /bg
    dataref, not something to smuggle into the string.
    """
    values: dict[str, Value] = {}
    for name, cell in zip(display.dataref_names(), result.cells):
        values[name] = cell.text
        values[f"{name}/bg"] = cell.background
    return values


def _format_row(result: DisplayResult) -> str:
    cells = " | ".join(f"{i + 1}:{c.text or '-':<9}" for i, c in enumerate(result.cells))
    row = f"[{result.display}] {cells}"
    # Only mentioned when there is something to mention. A normal strip is all
    # black backgrounds, and printing twelve "black"s every time would bury
    # the one cell that is actually highlighted.
    coloured = [
        f"{i + 1}={background_name(c.background)}"
        for i, c in enumerate(result.cells)
        if c.background != BLACK
    ]
    if coloured:
        row += "  bg: " + " ".join(coloured)
    return row


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_list_windows(args: argparse.Namespace, config: AppConfig) -> int:
    # Before listing, not after: the pop-outs this is being run to find are
    # often the ones window management is about to open, and a list taken
    # first would be a list of the windows that existed before the answer.
    _manage_windows(config, None)

    everything = list_windows()
    show_all = getattr(args, "all", False)
    candidates = (
        everything if show_all
        else [w for w in everything if w.class_name == XPLANE_WINDOW_CLASS]
    )
    needle = (args.filter or "").casefold()
    shown = [w for w in candidates if needle in w.title.casefold()]

    print(f"{len(shown)} of {len(everything)} visible top-level windows")
    for window in sorted(shown, key=lambda w: w.title.lower()):
        print(f"  {window}")
    if not show_all:
        # Said even when the filter found plenty: somebody looking for a
        # window that is not here needs to know something was hidden, and the
        # moment they need to know it is while they are looking at the list.
        print(f"\nOnly windows of class {XPLANE_WINDOW_CLASS!r} are shown, which is the "
              "class X-Plane's own windows carry. Add --all to see every window on the "
              "desktop.")
    print("\nPut a distinctive substring of the pop-out title into "
          "[display.pfd].window_title in your config.")
    return 0


def cmd_manage_windows(args: argparse.Namespace, config: AppConfig) -> int:
    """Open, size and place the G1000 pop-outs, and say what was done.

    The same pass ``run`` makes on the way up, on its own, so the setting can
    be tried and its result read without starting the daemon.
    """
    report = windowmgr.manage_windows(config)
    for line in report.lines():
        print(line)
    if report.skipped or not report.xplane_running:
        return 0
    failed = [o for o in report.outcomes if not o.ok]
    return 1 if failed else 0


def cmd_calibrate(args: argparse.Namespace, config: AppConfig) -> int:
    import cv2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    sources = _open_sources(config, args.image)
    try:
        for display in config.active_displays:
            frame = _grab(sources[display.key])
            geom = display.geometry
            cv2.imwrite(str(out / f"{display.key}_raw.png"), frame)
            cv2.imwrite(str(out / f"{display.key}_strip.png"), crop_strip(frame, geom))
            cv2.imwrite(str(out / f"{display.key}_overlay.png"), overlay_geometry(frame, geom))

            detected = auto_detect_strip(frame, cells=geom.cells)
            print(f"\n[{display.key}] frame {frame.shape[1]}x{frame.shape[0]} "
                  f"source={sources[display.key].name}")
            print(f"  configured : x={geom.x:.4f} y={geom.y:.4f} w={geom.w:.4f} h={geom.h:.4f}")
            if detected is None:
                print("  auto-detect: no dark softkey band found; set the geometry by hand "
                      "using the overlay PNG")
            else:
                print(f"  auto-detect: x={detected.x:.4f} y={detected.y:.4f} "
                      f"w={detected.w:.4f} h={detected.h:.4f}")
                print("  suggested TOML:")
                print(f"    [display.{display.key}.geometry]")
                for key in ("x", "y", "w", "h"):
                    print(f"    {key} = {getattr(detected, key):.4f}")
                cv2.imwrite(
                    str(out / f"{display.key}_overlay_auto.png"),
                    overlay_geometry(frame, detected),
                )
        print(f"\nwrote calibration PNGs to {out.resolve()}")
    finally:
        for source in sources.values():
            source.close()
    return 0


def cmd_dump_cells(args: argparse.Namespace, config: AppConfig) -> int:
    """Write each cell's raw crop and the picture Tesseract is actually given.

    ``_prep.png`` is the *first rung of the configured ladder* -- the same
    image ``run`` hands to Tesseract first -- and not
    ``preprocess_cell``'s own defaults. It was the defaults once, which meant
    the picture in the Cells tab was sharpened at 1.2/1.4 while the shipped
    ladder starts at no sharpening at all: a diagnostic image of a variant the
    daemon never produces. Finding the closed-counter bug took a picture of a
    preprocessed cell; a picture of the wrong preprocessed cell is worse than
    none, because it is believed.

    The printed line carries the polarity decision for the same reason. It is
    a threshold on a measured quantity (see ``strip._background_is_white``),
    and until now the only way to see it was to notice that a dumped cell had
    come out white-on-black.
    """
    import cv2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    first_amount, first_radius = (config.ocr.sharpen_ladder or ((0.0, 0.0),))[0]
    sources = _open_sources(config, args.image)
    try:
        for display in config.active_displays:
            frame = _grab(sources[display.key])
            cells = split_cells(frame, display.geometry)
            for index, cell in enumerate(cells, start=1):
                blank = is_blank(cell, config.ocr.blank_ink_ratio)
                binary = threshold_cell(
                    cell, config.ocr.upscale, config.ocr.threshold, first_amount, first_radius,
                )
                bright = ring_bright_fraction(binary)
                cv2.imwrite(str(out / f"{display.key}_{index:02d}_raw.png"), cell)
                cv2.imwrite(
                    str(out / f"{display.key}_{index:02d}_prep.png"),
                    finish_cell(binary, "auto"),
                )
                LOG.info(
                    "%s cell %2d: %s, ring bright %.2f -> read as %s",
                    display.key, index, "blank" if blank else "has ink", bright,
                    "dark text on a light box" if bright > 0.5 else "light text on a dark box",
                )
        print(f"wrote {len(config.active_displays) * 12 * 2} cell PNGs to {out.resolve()}")
    finally:
        for source in sources.values():
            source.close()
    return 0


def cmd_dump_colors(args: argparse.Namespace, config: AppConfig) -> int:
    """Print each cell's border-ring BGR/HSV and how it classifies.

    This exists because the classification thresholds in ``[color]`` were set
    from *plausible* G1000 swatches, not from a capture -- nobody on this side
    of the project has ever seen an X-Plane frame. Guessing at pixel values
    that could not be observed is what has cost this project the most time so
    far: two rounds of preprocessing were tuned against synthetic glyphs and
    made the live display worse. So before trusting the defaults, run this
    against a real capture with one softkey selected and, if you can find one,
    a caution or a warning showing, and move the thresholds to fit what it
    prints. ``--json`` dumps the same numbers for a bug report.
    """
    import json

    sources = _open_sources(config, args.image)
    records: list[dict] = []
    try:
        for display in config.active_displays:
            frame = _grab(sources[display.key])
            cells = split_cells(frame, display.geometry)
            print(f"\n[{display.key}]  ring = outer {config.color.ring_fraction:.0%} of each cell")
            print(f"{'cell':>4}  {'B':>4}{'G':>4}{'R':>4}   {'H':>4}{'S':>4}{'V':>4}   "
                  f"{'class':<7} {'bg':>3}")
            for index, cell in enumerate(cells, start=1):
                bgr, hsv, background = measure_cell(cell, config.color)
                print(
                    f"{index:>4}  {bgr[0]:>4}{bgr[1]:>4}{bgr[2]:>4}   "
                    f"{hsv[0]:>4}{hsv[1]:>4}{hsv[2]:>4}   "
                    f"{background_name(background):<7} {background:>3}"
                )
                records.append({
                    "display": display.key, "cell": index,
                    "bgr": list(bgr), "hsv": list(hsv),
                    "background": background, "name": background_name(background),
                })
    finally:
        for source in sources.values():
            source.close()

    print(
        "\nThresholds in [color], applied in this order:\n"
        f"  V <= {config.color.value_max:<3} -> black\n"
        f"  S <= {config.color.saturation_max:<3} -> white\n"
        f"  H <= {config.color.red_hue_max} or H >= {config.color.red_hue_wrap_min} -> red\n"
        f"  H in {config.color.yellow_hue_min}..{config.color.yellow_hue_max} -> yellow\n"
        "V is tested first on purpose: hue and saturation barely move when the display is\n"
        "dimmed, so brightness can only push a cell into black and can never turn a yellow\n"
        "into a red. If a cell above is named wrong, move the threshold that misfired --\n"
        "these numbers are the measurement, the defaults are only a guess."
    )
    if getattr(args, "json", None):
        Path(args.json).write_text(json.dumps(records, indent=1), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


def cmd_bench(args: argparse.Namespace, config: AppConfig) -> int:
    sources = _open_sources(config, args.image)
    reader = SoftkeyReader(config.ocr)
    try:
        for display in config.active_displays:
            source = sources[display.key]
            frame = _grab(source)
            pipeline = DisplayPipeline(display, reader, config)

            for gating in (False, True):
                object.__setattr__(config, "change_gating", gating)
                pipeline.reset()
                pipeline.process(frame)  # warm up / prime the gate cache
                samples: list[dict[str, float]] = []
                totals: list[float] = []
                calls: list[int] = []
                for _ in range(args.iterations):
                    start = time.perf_counter()
                    grabbed = source.grab()
                    capture_ms = (time.perf_counter() - start) * 1000.0
                    result = pipeline.process(grabbed if grabbed is not None else frame)
                    total = (time.perf_counter() - start) * 1000.0
                    stage = dict(result.timings)
                    stage["capture_ms"] = capture_ms
                    stage["pipeline_ms"] = total - capture_ms
                    samples.append(stage)
                    totals.append(total)
                    calls.append(result.ocr_calls)

                label = "change gating ON " if gating else "change gating OFF"
                print(f"\n[{display.key}] {label} n={args.iterations} source={source.name}")
                for stage_name in (
                    "capture_ms", "split_ms", "gate_ms", "preprocess_ms", "ocr_ms", "pipeline_ms"
                ):
                    values = [s.get(stage_name, 0.0) for s in samples]
                    print(f"  {stage_name:<14} mean={statistics.mean(values):7.2f} "
                          f"median={statistics.median(values):7.2f} max={max(values):7.2f}")
                print(f"  {'total_ms':<14} mean={statistics.mean(totals):7.2f} "
                      f"median={statistics.median(totals):7.2f} max={max(totals):7.2f}")
                if isinstance(source, ImageCapture):
                    print("  note: capture_ms here is PNG decode, not Windows Graphics Capture")
                print(f"  ocr calls/frame mean={statistics.mean(calls):.2f}  "
                      f"throughput={1000.0 / statistics.mean(totals):.1f} fps  "
                      f"duty cycle at {config.loop_hz:g} Hz="
                      f"{statistics.mean(totals) * config.loop_hz / 10.0:.1f}% of one core")
    finally:
        reader.close()
        for source in sources.values():
            source.close()
    return 0


def cmd_synth(args: argparse.Namespace, config: AppConfig) -> int:
    paths = synth.write_menus(args.out)
    for path in paths:
        print(path)
    print(f"\nUse one as a frame source, e.g.:\n  python -m glasslinkxp.main run "
          f"--image {paths[0]} --publisher console")
    return 0


def cmd_run(args: argparse.Namespace, config: AppConfig) -> int:
    if args.publisher:
        object.__setattr__(config.publish, "target", args.publisher)
    if getattr(args, "hz", None):
        if args.hz <= 0:
            LOG.error("--hz must be > 0")
            return 2
        object.__setattr__(config, "loop_hz", args.hz)
    # Before the sources are opened: WgcCapture resolves its window in its
    # constructor and fails if it is not there, so a pop-out that has to be
    # opened has to be opened before that, not after.
    _manage_windows(config, args.image)
    sources = _open_sources(config, args.image)
    reader = SoftkeyReader(config.ocr)
    names: list[str] = []
    for display in config.active_displays:
        names.extend(display.all_dataref_names())
    publisher = create_publisher(config.publish, names)
    pipelines = {d.key: DisplayPipeline(d, reader, config) for d in config.active_displays}

    stopping = False

    def _stop(signum, _frame):  # pragma: no cover - signal path
        nonlocal stopping
        LOG.info("caught signal %s, shutting down", signum)
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    # SIGBREAK is the Windows one, and it is here for the GUI: a child started
    # in its own process group ignores Ctrl-C by default, so CTRL_BREAK_EVENT
    # is the only console signal that can be aimed at this process alone. That
    # is what the Stop button sends, and handling it here is what makes Stop a
    # clean shutdown -- through the `finally` below, closing the publisher, the
    # Tesseract API and the capture sources -- rather than a kill.
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _stop)

    period = 1.0 / config.loop_hz
    previous: dict[str, tuple[list[str], list[int]]] = {}
    starved: dict[str, float] = {}
    warned_starved: set[str] = set()
    next_reopen: dict[str, float] = {}
    LOG.info(
        "running at %.1f Hz, gating=%s, publisher=%s",
        config.loop_hz, config.change_gating, publisher.name,
    )
    try:
        while not stopping:
            cycle_start = time.perf_counter()
            values: dict[str, str] = {}
            last_results = {}
            changed_this_cycle = False
            for display in config.active_displays:
                # The capture backend says when its window has gone, so acting
                # on that is immediate and needs nothing to poll: a pop-out
                # closed mid-flight is back within a cycle. The interval only
                # paces a reopen that did not work.
                if (capture_module.window_lost(sources[display.key])
                        and time.monotonic() >= next_reopen.get(display.key, 0.0)):
                    next_reopen[display.key] = time.monotonic() + REOPEN_RETRY_INTERVAL
                    _reopen_closed(config, args.image, display, sources)
                frame = sources[display.key].grab()
                if frame is None:
                    # A display that never delivers is a setup problem, not a
                    # transient. Say so once, loudly, instead of a debug line
                    # per cycle that scrolls the real output away.
                    first = starved.setdefault(display.key, time.monotonic())
                    waited = time.monotonic() - first
                    if waited > 3.0 and display.key not in warned_starved:
                        warned_starved.add(display.key)
                        LOG.warning(
                            "no frames from %s after %.0fs. The window must exist and be "
                            "rendering: check it is still popped out, not minimised, and "
                            "that window_title %r still matches. 'list-windows' shows what "
                            "is open.",
                            display.key, waited, display.window_title,
                        )
                    continue
                if display.key in starved:
                    del starved[display.key]
                    if display.key in warned_starved:
                        warned_starved.discard(display.key)
                        LOG.info("%s is delivering frames again", display.key)
                result = pipelines[display.key].process(frame)
                last_results[display.key] = result
                values.update(_values(display, result))
                # Compared against labels *and* backgrounds: a softkey
                # becoming selected changes only the colour, and a log line
                # that ignores that reports nothing happened while the
                # published datarefs change underneath it.
                snapshot = (result.labels, result.backgrounds)
                if previous.get(display.key) != snapshot:
                    previous[display.key] = snapshot
                    changed_this_cycle = True
                    LOG.info("%s", _format_row(result))
            publish_ms = 0.0
            if values:
                t0 = time.perf_counter()
                publisher.publish(values)
                publish_ms = (time.perf_counter() - t0) * 1000.0
            work_ms = (time.perf_counter() - cycle_start) * 1000.0
            if changed_this_cycle:
                # Only report cycles that actually did something; steady-state
                # cycles are gated down to a fraction of a millisecond.
                stages = {}
                for key, res in last_results.items():
                    for stage, ms in res.timings.items():
                        stages[stage] = stages.get(stage, 0.0) + ms
                if getattr(args, "timing", False):
                    detail = "  ".join(f"{k}={v:.1f}" for k, v in sorted(stages.items()))
                    LOG.info(
                        "cycle %.0f ms (work) + %.0f ms (sleep budget)  publish=%.1f  %s",
                        work_ms, max(0.0, period * 1000 - work_ms), publish_ms, detail,
                    )
                elif work_ms > period * 1000:
                    LOG.debug("cycle %.0f ms, publish %.0f ms", work_ms, publish_ms)
            if args.once:
                break
            elapsed = time.perf_counter() - cycle_start
            if elapsed < period:
                time.sleep(period - elapsed)
            elif elapsed > period * 2:
                LOG.warning("loop overran: %.0f ms > %.0f ms budget", elapsed * 1000, period * 1000)
    finally:
        publisher.close()
        reader.close()
        for source in sources.values():
            source.close()
    return 0


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def cmd_tune(args: argparse.Namespace, config: AppConfig) -> int:
    """Search sharpening-ladder settings across every labelled cell given.

    A setting that reads one cell correctly is not an improvement: the ladder
    it goes into runs against all twelve cells of every frame, and a rung
    strong enough to open up a 0 has, in practice, turned a 6 into a 5. So
    this is fed one or more captured pages -- each a ``dump-cells`` output
    folder plus what some of its cells should read -- and only keeps a
    candidate that fixes something without making a cell that already read
    correctly read wrong.

        glasslinkxp dump-cells --out cells_page1
        # edit cells_page1's captures into a truth file, then:
        glasslinkxp tune --truth truth.toml

    See ``TuningCase`` in ``tuning.py`` for the truth file's shape.
    """
    from .tuning import TuningError, format_report, load_truth, run_tuning

    try:
        cases = load_truth(args.truth)
    except TuningError as exc:
        LOG.error("%s", exc)
        return 2

    LOG.info("tuning against %d case(s): %s", len(cases), ", ".join(c.label for c in cases))
    try:
        result = run_tuning(cases, config.ocr, color=config.color, progress=LOG.info)
    except TuningError as exc:
        LOG.error("%s", exc)
        return 2

    print(format_report(result))
    wrong_at_baseline = sum(1 for o in result.baseline.outcomes if not o.ok)
    if wrong_at_baseline and not result.best.fixed:
        return 1
    return 0


def cmd_screen_template(args: argparse.Namespace, config: AppConfig) -> int:
    """Print a [[screen]] block for whatever is on screen right now.

    Authoring a page by hand means typing twelve labels correctly; this reads
    them off the display instead. Cells that read confidently become the match
    set, everything else is listed for you to correct -- which is the right way
    round, because the confident cells are exactly the ones that should
    identify the page.
    """
    display = config.display(args.display)
    if display is None:
        LOG.error("no display %r in the config", args.display)
        return 2

    reader = SoftkeyReader(config.ocr)
    sources = _open_sources(config, args.image)
    try:
        frame = _grab(sources[display.key])
        result = DisplayPipeline(display, reader, config).process(frame)
    finally:
        reader.close()
        for source in sources.values():
            source.close()

    floor = config.ocr.screen_match_confidence
    confident = {c.index + 1: c.text for c in result.cells
                 if c.text and not c.blank and c.confidence >= floor}
    shaky = {c.index + 1: (c.text, c.confidence) for c in result.cells
             if not c.blank and c.confidence < floor}

    print(f"\n[[screen]]")
    print(f'name = "{args.name}"')
    print(f'display = "{display.key}"')
    if confident:
        print("match = { " + ", ".join(f'{k} = "{v}"' for k, v in sorted(confident.items())) + " }")
    else:
        print("# nothing read confidently enough to identify this page")
    labels = {c.index + 1: c.text for c in result.cells if c.text and not c.blank}
    print("labels = { " + ", ".join(f'{k} = "{v}"' for k, v in sorted(labels.items())) + " }")

    if shaky:
        print("\n# CHECK THESE -- read below "
              f"{floor:.0f}% and may be wrong:")
        for cell, (text, confidence) in sorted(shaky.items()):
            print(f"#   cell {cell}: {text!r} at {confidence:.0f}%")
        print("# Correct them in `labels` above, and drop them from `match`.")
    print()
    return 0


def cmd_gui(args: argparse.Namespace, config: AppConfig) -> int:
    """Open the graphical interface.

    The GUI does not use the AppConfig loaded here: it edits the config *file*
    and spawns this same CLI for everything it does, so what it needs is the
    path. It is the one command that can usefully run without a config file at
    all -- making one is among the things it is for.
    """
    from .gui import launch

    return launch(getattr(args, "config", None))


def cmd_migrate_config(args: argparse.Namespace, config: AppConfig) -> int:
    """Bring an existing config.toml up to date with this version's settings.

    Run by the installer after it has put a newer GlassLinkXP over an older
    one. Like ``gui`` it works on the config *file* rather than on the
    AppConfig loaded from it -- there may not be one, which is not an error
    here: a first install has nothing to migrate and says so.
    """
    import tomli_w

    from . import configmigrate
    from .config import PACKAGE_DIR

    path = Path(getattr(args, "config", None) or "config.toml")
    example_path = Path(args.example) if args.example else PACKAGE_DIR.parent / "config.example.toml"
    if not path.is_file():
        print(f"no configuration at {path} -- nothing to migrate")
        return 0
    if not example_path.is_file():
        LOG.error("cannot find the settings this version has: %s", example_path)
        return 2

    user = tomllib.loads(path.read_text(encoding="utf-8"))
    example = tomllib.loads(example_path.read_text(encoding="utf-8"))
    merged, changes = configmigrate.reconcile(user, example)

    if changes.any:
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
        path.write_text(tomli_w.dumps(merged), encoding="utf-8")

        for added in changes.added:
            print(f"  added   {added}")
        for removed in changes.removed:
            print(f"  removed {removed}")
        for kept in changes.kept_untouched:
            print(f"  kept    [{kept}] (not a setting this version reads -- left alone)")
        print(f"updated {path}; the previous version is {backup.name}")
    else:
        print(f"{path} is already up to date ({example_path.name})")

    return _migrate_vocabulary(path, merged)


def _migrate_vocabulary(config_path: Path, document: dict) -> int:
    """Give the user their own labels.txt, and merge this version's into it.

    The shipped vocabulary lives inside the package, which an update replaces
    wholesale -- so a vocabulary edited in the Vocabulary tab, at its default
    location, was being destroyed by the next update. The user's copy belongs
    beside their config.toml, where the installer keeps it, and the config
    points at it by a *relative* path so the file stays portable.
    """
    import tomli_w

    from . import configmigrate
    from .config import PACKAGE_DIR

    shipped = PACKAGE_DIR / "labels.txt"
    if not shipped.is_file():  # pragma: no cover - it ships with the package
        LOG.error("the vocabulary this version ships is missing: %s", shipped)
        return 2

    root = config_path.parent
    configured = str((document.get("ocr") or {}).get("labels_file") or "")
    if configured and Path(configured).name != "labels.txt":
        print(f"vocabulary: using your own file ({configured}) -- left alone")
        return 0

    user_labels = root / "labels.txt"
    snapshot = root / "labels.shipped.txt"

    if not user_labels.is_file():
        shutil.copy2(shipped, user_labels)
        shutil.copy2(shipped, snapshot)
        _point_config_at_labels(config_path, document, tomli_w)
        print(f"vocabulary: your own copy is now {user_labels} (edit it in the Vocabulary tab)")
        return 0

    merged, changes = configmigrate.reconcile_labels(
        user_labels.read_text(encoding="utf-8"),
        shipped.read_text(encoding="utf-8"),
        snapshot.read_text(encoding="utf-8") if snapshot.is_file() else None,
    )
    if changes.any:
        shutil.copy2(user_labels, user_labels.with_suffix(".txt.bak"))
        user_labels.write_text(merged, encoding="utf-8")
        for label in changes.added:
            print(f"  vocabulary added   {label}")
        for label in changes.removed:
            print(f"  vocabulary removed {label}")
        print(f"updated {user_labels}; the previous version is {user_labels.name}.bak")
    else:
        print(f"vocabulary: {user_labels} is already up to date")
    shutil.copy2(shipped, snapshot)
    _point_config_at_labels(config_path, document, tomli_w)
    return 0


def _point_config_at_labels(config_path: Path, document: dict, tomli_w) -> None:
    """Make config.toml name the user's copy, relatively, if it does not yet.

    Relative because ``from_mapping`` resolves it against the config file's
    own directory: an absolute path would pin the config to one install, which
    is the thing ``labels_file`` is documented as avoiding.
    """
    ocr = document.setdefault("ocr", {})
    if ocr.get("labels_file") == "labels.txt":
        return
    ocr["labels_file"] = "labels.txt"
    config_path.write_text(tomli_w.dumps(document), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    # -c and -v live on a shared parent so they are accepted both before and
    # after the subcommand: `main -v run` and `main run -v` are equally natural
    # to type, and argparse subparsers do not inherit the top-level flags.
    # SUPPRESS keeps an absent flag from overwriting one given on the other side.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-c", "--config", default=argparse.SUPPRESS,
        help="path to config.toml (defaults are used without it)",
    )
    common.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
        help="debug logging: per-cell raw OCR text, confidence and match score",
    )

    parser = argparse.ArgumentParser(
        prog="glasslinkxp",
        description="OCR the X-Plane G1000 softkey strip into X-Plane datarefs.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_image(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--image",
            help="read frames from a PNG file or a directory of PNGs instead of capturing "
                 "a window (offline dev/test; a directory may hold <display>.png per display)",
        )

    run = sub.add_parser("run", help="capture, OCR and publish continuously", parents=[common])
    add_image(run)
    run.add_argument("--once", action="store_true", help="single pass, then exit")
    run.add_argument("--hz", type=float, default=None,
                     help="override app.loop_hz for this run (handy for A/B timing)")
    run.add_argument("--timing", action="store_true",
                     help="log a per-stage latency breakdown whenever labels change")
    run.add_argument("--publisher", choices=["websocket", "webapi", "console"],
                     help="override publish.target from the config")
    run.set_defaults(func=cmd_run)

    gui = sub.add_parser(
        "gui", help="open the graphical interface (start here if you are not sure)",
        parents=[common],
    )
    # Alone among the subcommands, this one still runs when -c names a file
    # that is not there: creating that file is one of the things it does. It
    # gets no more licence than that -- a file that exists and is broken is an
    # error for `gui` exactly as for the rest.
    gui.set_defaults(func=cmd_gui, tolerate_missing_config=True)

    windows = sub.add_parser("list-windows", help="list top-level windows (Windows only)", parents=[common])
    windows.add_argument("--filter", help="only show titles containing this substring")
    windows.add_argument(
        "--all", action="store_true",
        help=f"show every window, not just the ones of class {XPLANE_WINDOW_CLASS!r} "
             "that X-Plane's own windows carry",
    )
    windows.set_defaults(func=cmd_list_windows)

    manage = sub.add_parser(
        "manage-windows",
        help="open, size and place the G1000 pop-outs (Windows only)",
        parents=[common],
    )
    manage.set_defaults(func=cmd_manage_windows)

    calibrate = sub.add_parser("calibrate", help="dump raw/crop/overlay PNGs and suggest geometry", parents=[common])
    add_image(calibrate)
    calibrate.add_argument("--out", default="calibration", help="output directory")
    calibrate.set_defaults(func=cmd_calibrate)

    dump = sub.add_parser("dump-cells", help="write raw + preprocessed images for every cell", parents=[common])
    add_image(dump)
    dump.add_argument("--out", default="cells", help="output directory")
    dump.set_defaults(func=cmd_dump_cells)

    colors = sub.add_parser(
        "dump-colors",
        help="print each cell's border-ring BGR/HSV and its colour classification",
        parents=[common],
    )
    add_image(colors)
    colors.add_argument("--json", help="also write the measurements to this JSON file")
    colors.set_defaults(func=cmd_dump_colors)

    bench = sub.add_parser("bench", help="measure per-stage timings", parents=[common])
    add_image(bench)
    bench.add_argument("-n", "--iterations", type=int, default=50)
    bench.set_defaults(func=cmd_bench)

    template = sub.add_parser(
        "screen-template", help="print a [[screen]] block from the current display",
        parents=[common],
    )
    add_image(template)
    template.add_argument("--display", default="pfd")
    template.add_argument("--name", default="unnamed-page", help="a name for this page")
    template.set_defaults(func=cmd_screen_template)

    tune = sub.add_parser(
        "tune", help="search sharpening-ladder settings across one or more captured pages",
        parents=[common],
    )
    tune.add_argument(
        "--truth", required=True,
        help="a TOML file listing one or more [[case]] pages (a dump-cells folder, a display, "
             "and what some of its cells should read) -- see tuning.load_truth",
    )
    tune.set_defaults(func=cmd_tune)

    synth_cmd = sub.add_parser("synth", help="write synthetic softkey frames for offline testing", parents=[common])
    synth_cmd.add_argument("--out", default="frames", help="output directory")
    synth_cmd.set_defaults(func=cmd_synth)

    migrate = sub.add_parser(
        "migrate-config",
        help="add settings this version has to an existing config.toml, and drop ones it no longer has",
        parents=[common],
    )
    migrate.add_argument(
        "--example", default=None,
        help="the settings this version has (default: the config.example.toml beside the package)",
    )
    # Like `gui`, this works on the config file rather than on a loaded
    # AppConfig, and a missing one is not an error: a first install has
    # nothing to migrate.
    migrate.set_defaults(func=cmd_migrate_config, tolerate_missing_config=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    try:
        try:
            config = load_config(getattr(args, "config", None))
        except ConfigNotFound:
            # Only a file that is *not there* is tolerated, and only for `gui`.
            # A file that exists but does not parse or does not validate is
            # still an error here: opening the window on the built-in defaults
            # would hide the mistake and then overwrite the file with the
            # defaults on the first Save.
            if not getattr(args, "tolerate_missing_config", False):
                raise
            config = default_config()
        return args.func(args, config)
    except (ConfigError, CaptureError, OcrUnavailable, ValueError) as exc:
        LOG.error("%s", exc)
        return 2
    except FileNotFoundError as exc:
        LOG.error("%s", exc)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
