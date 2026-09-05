"""CLI entry point: run | list-windows | calibrate | dump-cells | bench | synth."""

from __future__ import annotations

import argparse
import logging
import signal
import statistics
import sys
import time
from pathlib import Path

import numpy as np

from . import synth
from .capture import CaptureError, FrameSource, ImageCapture, list_windows, sources_for
from .config import AppConfig, ConfigError, DisplayConfig, load_config
from .ocr import OcrUnavailable, SoftkeyReader
from .pipeline import DisplayPipeline, DisplayResult
from .publish import create_publisher
from .strip import (
    auto_detect_strip,
    crop_strip,
    is_blank,
    overlay_geometry,
    preprocess_cell,
    split_cells,
)

LOG = logging.getLogger("g1000_softkey")


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


def _grab(source: FrameSource, retries: int = 25, delay: float = 0.2) -> np.ndarray:
    """First frame, with a short grace period for WGC to deliver one."""
    for _ in range(retries):
        frame = source.grab()
        if frame is not None:
            return frame
        time.sleep(delay)
    raise CaptureError(f"no frame arrived from {source.name}")


def _values(display: DisplayConfig, result: DisplayResult) -> dict[str, str]:
    return dict(zip(display.dataref_names(), result.labels))


def _format_row(result: DisplayResult) -> str:
    cells = " | ".join(f"{i + 1}:{c.text or '-':<9}" for i, c in enumerate(result.cells))
    return f"[{result.display}] {cells}"


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_list_windows(args: argparse.Namespace, config: AppConfig) -> int:
    windows = list_windows()
    needle = (args.filter or "").casefold()
    shown = [w for w in windows if needle in w.title.casefold()]
    print(f"{len(shown)} of {len(windows)} visible top-level windows")
    for window in sorted(shown, key=lambda w: w.title.lower()):
        print(f"  {window}")
    print("\nPut a distinctive substring of the pop-out title into "
          "[display.pfd].window_title in your config.")
    return 0


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
    import cv2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    sources = _open_sources(config, args.image)
    try:
        for display in config.active_displays:
            frame = _grab(sources[display.key])
            cells = split_cells(frame, display.geometry)
            for index, cell in enumerate(cells, start=1):
                blank = is_blank(cell, config.ocr.blank_ink_ratio)
                cv2.imwrite(str(out / f"{display.key}_{index:02d}_raw.png"), cell)
                cv2.imwrite(
                    str(out / f"{display.key}_{index:02d}_prep.png"),
                    preprocess_cell(cell, config.ocr.upscale, config.ocr.threshold),
                )
                LOG.info("%s cell %2d: %s", display.key, index, "blank" if blank else "has ink")
        print(f"wrote {len(config.active_displays) * 12 * 2} cell PNGs to {out.resolve()}")
    finally:
        for source in sources.values():
            source.close()
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
    print(f"\nUse one as a frame source, e.g.:\n  python -m g1000_softkey.main run "
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
    sources = _open_sources(config, args.image)
    reader = SoftkeyReader(config.ocr)
    names: list[str] = []
    for display in config.active_displays:
        names.extend(display.dataref_names())
    publisher = create_publisher(config.publish, names)
    pipelines = {d.key: DisplayPipeline(d, reader, config) for d in config.active_displays}

    stopping = False

    def _stop(signum, _frame):  # pragma: no cover - signal path
        nonlocal stopping
        LOG.info("caught signal %s, shutting down", signum)
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    period = 1.0 / config.loop_hz
    previous: dict[str, list[str]] = {}
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
                frame = sources[display.key].grab()
                if frame is None:
                    LOG.debug("no frame yet for %s", display.key)
                    continue
                result = pipelines[display.key].process(frame)
                last_results[display.key] = result
                values.update(_values(display, result))
                if previous.get(display.key) != result.labels:
                    previous[display.key] = result.labels
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="g1000-softkey",
        description="OCR the X-Plane G1000 softkey strip into X-Plane datarefs.",
    )
    parser.add_argument("-c", "--config", help="path to config.toml (defaults are used without it)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_image(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--image",
            help="read frames from a PNG file or a directory of PNGs instead of capturing "
                 "a window (offline dev/test; a directory may hold <display>.png per display)",
        )

    run = sub.add_parser("run", help="capture, OCR and publish continuously")
    add_image(run)
    run.add_argument("--once", action="store_true", help="single pass, then exit")
    run.add_argument("--hz", type=float, default=None,
                     help="override app.loop_hz for this run (handy for A/B timing)")
    run.add_argument("--timing", action="store_true",
                     help="log a per-stage latency breakdown whenever labels change")
    run.add_argument("--publisher", choices=["websocket", "webapi", "file", "console"],
                     help="override publish.target from the config")
    run.set_defaults(func=cmd_run)

    windows = sub.add_parser("list-windows", help="list top-level windows (Windows only)")
    windows.add_argument("--filter", help="only show titles containing this substring")
    windows.set_defaults(func=cmd_list_windows)

    calibrate = sub.add_parser("calibrate", help="dump raw/crop/overlay PNGs and suggest geometry")
    add_image(calibrate)
    calibrate.add_argument("--out", default="calibration", help="output directory")
    calibrate.set_defaults(func=cmd_calibrate)

    dump = sub.add_parser("dump-cells", help="write raw + preprocessed images for every cell")
    add_image(dump)
    dump.add_argument("--out", default="cells", help="output directory")
    dump.set_defaults(func=cmd_dump_cells)

    bench = sub.add_parser("bench", help="measure per-stage timings")
    add_image(bench)
    bench.add_argument("-n", "--iterations", type=int, default=50)
    bench.set_defaults(func=cmd_bench)

    synth_cmd = sub.add_parser("synth", help="write synthetic softkey frames for offline testing")
    synth_cmd.add_argument("--out", default="frames", help="output directory")
    synth_cmd.set_defaults(func=cmd_synth)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        config = load_config(args.config)
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
