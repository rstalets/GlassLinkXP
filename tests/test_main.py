"""CLI smoke tests -- every subcommand that can run without X-Plane."""

import json

import pytest

from glasslinkxp import main as main_module
from glasslinkxp import synth
from glasslinkxp.color import BLACK, RED, WHITE, YELLOW
from glasslinkxp.config import DisplayConfig, PublishConfig
from glasslinkxp.main import _values, main
from glasslinkxp.ocr import CellResult
from glasslinkxp.pipeline import DisplayResult


@pytest.fixture(scope="module")
def frames(tmp_path_factory):
    path = tmp_path_factory.mktemp("frames")
    synth.write_menus(path)
    return path


class RecordingPublisher:
    """Everything one pass of ``run`` hands to a publisher, kept for inspection.

    These tests used to read the JSON file the file publisher wrote, because
    it was the one target that could be observed without X-Plane. What they
    are about, though, is what a full ``run --once`` produces -- capture,
    OCR, colour, naming -- not how it is transmitted, so with that target gone
    they watch the values at the publisher boundary instead.
    """

    name = "recording"

    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.closed = False

    def publish(self, values) -> None:
        self.values.update(values)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def published(monkeypatch):
    recorder = RecordingPublisher()
    monkeypatch.setattr(main_module, "create_publisher", lambda *a, **k: recorder)
    return recorder


def test_synth_writes_frames(tmp_path):
    assert main(["synth", "--out", str(tmp_path)]) == 0
    assert len(list(tmp_path.glob("*.png"))) == len(synth.MENUS)


def test_run_once_publishes_the_labels_it_read(frames, published):
    assert main(["run", "--image", str(frames / "pfd_top.png"), "--once"]) == 0
    assert published.values["glasslinkxp/softkey/pfd/1"] == "INSET"
    assert published.values["glasslinkxp/softkey/mfd/12"] == "ALERTS"
    assert published.values["glasslinkxp/softkey/pfd/2"] == ""
    assert published.closed, "run must close the publisher on the way out"


def test_run_with_console_publisher(frames):
    assert main(["run", "--image", str(frames), "--publisher", "console", "--once"]) == 0


def test_calibrate_writes_pngs(frames, tmp_path, capsys):
    assert main(["calibrate", "--image", str(frames / "inset.png"), "--out", str(tmp_path)]) == 0
    names = {p.name for p in tmp_path.glob("*.png")}
    assert {"pfd_raw.png", "pfd_strip.png", "pfd_overlay.png"} <= names
    assert "suggested TOML" in capsys.readouterr().out


def test_dump_cells_writes_every_cell(frames, tmp_path):
    assert main(["dump-cells", "--image", str(frames / "xpdr.png"), "--out", str(tmp_path)]) == 0
    assert len(list(tmp_path.glob("pfd_*_raw.png"))) == 12
    assert len(list(tmp_path.glob("pfd_*_prep.png"))) == 12


def test_tune_reports_success_when_the_queued_cells_already_read_correctly(frames, tmp_path, capsys):
    """A CLI-level smoke test of the whole truth-file -> search -> report path.

    Uses the synthetic corpus, which this project's own docs say never to
    tune *thresholds* against -- but every cell in it already reads correctly
    under the shipped defaults, so this only exercises the plumbing (loading
    the truth file, reading dump-cells' own output, printing the report), not
    a sharpening value, which is exactly what a synthetic frame is good for.
    """
    cells = tmp_path / "cells"
    assert main(["dump-cells", "--image", str(frames / "xpdr.png"), "--out", str(cells)]) == 0

    truth = tmp_path / "truth.toml"
    truth.write_text(
        f'[[case]]\ndir = "{cells.as_posix()}"\ndisplay = "pfd"\n'
        'expect = { 1 = "STBY", 7 = "IDENT" }\n',
        encoding="utf-8",
    )
    assert main(["tune", "--truth", str(truth)]) == 0
    out = capsys.readouterr().out
    assert "2 labelled cell(s)" in out
    assert "2/2 correct" in out
    assert "nothing to fix" in out
    assert "[tuning_result]" in out


def test_tune_returns_2_for_a_truth_file_that_is_not_there():
    assert main(["tune", "--truth", "/nonexistent/truth.toml"]) == 2


def test_tune_returns_2_for_a_case_with_no_expect(tmp_path):
    truth = tmp_path / "truth.toml"
    truth.write_text('[[case]]\ndir = "cells"\n', encoding="utf-8")
    assert main(["tune", "--truth", str(truth)]) == 2


def test_bench_runs(frames, capsys):
    assert main(["bench", "--image", str(frames / "pfd_menu.png"), "-n", "3"]) == 0
    out = capsys.readouterr().out
    assert "change gating ON" in out and "ocr calls/frame" in out


def test_bad_config_path_returns_2(caplog):
    assert main(["-c", "/nonexistent/config.toml", "run", "--once"]) == 2
    assert "not found" in caplog.text


def test_list_windows_off_windows_returns_2(caplog):
    import sys

    if sys.platform == "win32":  # pragma: no cover
        pytest.skip("this asserts the non-Windows path")
    assert main(["list-windows"]) == 2
    assert "Windows" in caplog.text


def test_missing_image_returns_2():
    assert main(["run", "--image", "/nonexistent/frame.png", "--once"]) == 2


# ---------------------------------------------------------------------------
# background colour and the optional inline text colour
# ---------------------------------------------------------------------------


def _result(cells):
    return DisplayResult(display="pfd", cells=cells)


def test_values_publish_a_label_and_a_colour_per_cell():
    display = DisplayConfig(key="pfd")
    result = _result([
        CellResult(index=0, text="INSET", background=BLACK),
        CellResult(index=1, text="STD BARO", background=WHITE),
        CellResult(index=2, text="CAUTION", background=YELLOW),
        CellResult(index=3, text="WARNING", background=RED),
        CellResult(index=4, text="", background=YELLOW, blank=True),
    ])
    values = _values(display, result)
    assert values["glasslinkxp/softkey/pfd/1"] == "INSET"
    assert values["glasslinkxp/softkey/pfd/1/bg"] == BLACK
    assert values["glasslinkxp/softkey/pfd/2/bg"] == WHITE
    assert values["glasslinkxp/softkey/pfd/3/bg"] == YELLOW
    assert values["glasslinkxp/softkey/pfd/4/bg"] == RED
    # A blank cell publishes an empty label and still reports its colour.
    assert values["glasslinkxp/softkey/pfd/5"] == ""
    assert values["glasslinkxp/softkey/pfd/5/bg"] == YELLOW
    assert all(isinstance(v, int) for k, v in values.items() if k.endswith("/bg"))


def test_labels_go_out_exactly_as_the_sim_draws_them():
    """Nothing is prepended to a label -- no markup, no colour hint.

    What colour to draw the text is the Stream Deck's decision, made from the
    /bg dataref. A daemon that smuggled a prefix into the string would render
    as literal characters on any client that did not expect it.
    """
    display = DisplayConfig(key="pfd")
    result = _result([
        CellResult(index=0, text="STD BARO", background=WHITE),
        CellResult(index=1, text="WARNING", background=RED),
    ])
    values = _values(display, result)
    assert values["glasslinkxp/softkey/pfd/1"] == "STD BARO"
    assert values["glasslinkxp/softkey/pfd/2"] == "WARNING"


def test_the_longest_label_fits_the_default_field_width():
    from glasslinkxp.publish import encode_field

    longest = "FLIGHT PLAN"
    width = PublishConfig().field_width
    assert encode_field(longest, width).rstrip(b"\x00").decode() == longest


def test_run_publishes_the_cell_colours_alongside_the_labels(frames, published):
    assert main(["run", "--image", str(frames / "alerts.png"), "--once"]) == 0
    assert published.values["glasslinkxp/softkey/pfd/5/bg"] == YELLOW
    assert published.values["glasslinkxp/softkey/pfd/6/bg"] == RED
    assert published.values["glasslinkxp/softkey/pfd/9/bg"] == WHITE
    assert published.values["glasslinkxp/softkey/pfd/1/bg"] == BLACK
    assert published.values["glasslinkxp/softkey/pfd/5"] == "CAUTION"


def test_dump_colors_prints_the_measurements(frames, tmp_path, capsys):
    out = tmp_path / "colors.json"
    assert main(["dump-colors", "--image", str(frames / "alerts.png"),
                 "--json", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "yellow" in printed and "red" in printed
    assert "Thresholds in [color]" in printed
    records = json.loads(out.read_text())
    assert len(records) == 24  # both displays, 12 cells each
    yellow = next(r for r in records if r["display"] == "pfd" and r["cell"] == 5)
    assert yellow["name"] == "yellow" and yellow["background"] == YELLOW
    assert len(yellow["bgr"]) == 3 and len(yellow["hsv"]) == 3


def test_gui_opens_the_window(monkeypatch):
    """The subcommand exists and hands the config path to the GUI."""
    seen = {}

    def fake_launch(config_path=None):
        seen["path"] = config_path
        return 0

    monkeypatch.setattr("glasslinkxp.gui.launch", fake_launch)
    assert main(["-c", "some/config.toml", "gui"]) == 0
    assert seen["path"] == "some/config.toml"


def test_gui_still_opens_when_the_config_file_is_not_there_yet(monkeypatch, tmp_path):
    """Making that file is one of the things the GUI is for, so a missing one
    is not the error it is for every other subcommand."""
    monkeypatch.setattr("glasslinkxp.gui.launch", lambda config_path=None: 0)
    assert main(["-c", str(tmp_path / "not-yet.toml"), "gui"]) == 0


def test_gui_refuses_a_config_file_that_is_broken_rather_than_absent(monkeypatch, tmp_path):
    """A file that exists and is wrong must not be treated as one that is not there.

    The tolerance above was written as `except ConfigError`, which is also
    raised for a TOML syntax error and for every validation failure. So
    `gui -c config.toml` on a file with one bad value opened the window on the
    built-in defaults, said nothing, and the first Save wrote those defaults
    over a file that only needed one number corrected.
    """
    opened = []
    monkeypatch.setattr("glasslinkxp.gui.launch",
                        lambda config_path=None: opened.append(config_path) or 0)

    broken = tmp_path / "broken.toml"
    broken.write_text("[color]\nvalue_max = 300\n", encoding="utf-8")
    assert main(["-c", str(broken), "gui"]) == 2

    unparseable = tmp_path / "unparseable.toml"
    unparseable.write_text("[app\nloop_hz =", encoding="utf-8")
    assert main(["-c", str(unparseable), "gui"]) == 2

    assert not opened, "the window must not open on defaults over a real file"


def test_every_other_command_still_refuses_a_missing_config(tmp_path):
    assert main(["-c", str(tmp_path / "nope.toml"), "synth", "--out", str(tmp_path)]) == 2


# -- reopening a pop-out the user closed ------------------------------------
#
# The capture backend reports its window closing, and with window management
# on that is enough to act on: reopen the window and build a new capture on
# it. There is no polling for this -- the signal arrives on its own, and the
# only interval involved paces a reopen that failed.


class _LostSource:
    """A capture whose window has gone, which is what WgcCapture reports."""

    def __init__(self, name="wgc:gone", window_closed=True):
        self.name = name
        self.window_closed = window_closed
        self.closed = False

    def grab(self):
        return None

    def close(self):
        self.closed = True


def _report(managed=("pfd",), opened=()):
    from glasslinkxp.windowmgr import DisplayOutcome, Report

    return Report(
        outcomes=tuple(
            DisplayOutcome(key, "opened" if key in opened else "already", "detail")
            for key in managed
        ),
        xplane_running=True,
    )


@pytest.fixture
def reopening(monkeypatch):
    """Window management and source building, both stood in for."""
    calls = {"managed": 0, "built": []}

    def fake_manage(config, *a, **kw):
        calls["managed"] += 1
        return calls["report"]

    def fake_sources_for(displays, image, managed=frozenset()):
        calls["built"].append([d.key for d in displays])
        return {d.key: _LostSource(name=f"wgc:new-{d.key}", window_closed=False)
                for d in displays}

    calls["report"] = _report()
    monkeypatch.setattr(main_module.windowmgr, "manage_windows", fake_manage)
    monkeypatch.setattr(main_module, "sources_for", fake_sources_for)
    return calls


def _config(enabled=True):
    from glasslinkxp.config import AppConfig, WindowManagementConfig

    return AppConfig(
        displays=(DisplayConfig(key="pfd", window_title="G1000 PFD"),),
        window_management=WindowManagementConfig(enabled=enabled),
    )


def test_a_closed_window_is_reopened_and_captured_again(reopening):
    config = _config()
    old = _LostSource()
    sources = {"pfd": old}

    main_module._reopen_closed(config, None, config.display("pfd"), sources)

    assert reopening["managed"] == 1
    assert sources["pfd"] is not old, "a dead capture cannot be reconnected"
    assert sources["pfd"].name == "wgc:new-pfd"
    assert old.closed, "and the old one has to be released"


def test_a_window_the_user_reopened_themselves_is_recaptured_too(reopening):
    """Management finds it already there, so nothing is 'opened' -- but the
    capture attached to the window that was closed is dead regardless."""
    reopening["report"] = _report(managed=("pfd",), opened=())
    config = _config()
    sources = {"pfd": _LostSource()}

    main_module._reopen_closed(config, None, config.display("pfd"), sources)

    assert sources["pfd"].name == "wgc:new-pfd"


def test_nothing_is_rebuilt_when_the_window_could_not_be_brought_back(reopening):
    """X-Plane has gone too, say. The retry interval covers trying again."""
    reopening["report"] = _report(managed=())
    config = _config()
    old = _LostSource()
    sources = {"pfd": old}

    main_module._reopen_closed(config, None, config.display("pfd"), sources)

    assert sources["pfd"] is old
    assert not old.closed
    assert reopening["built"] == []


def test_window_management_switched_off_reopens_nothing(reopening):
    config = _config(enabled=False)
    old = _LostSource()
    sources = {"pfd": old}

    main_module._reopen_closed(config, None, config.display("pfd"), sources)

    assert sources["pfd"] is old
    assert reopening["managed"] == 0, "not even a look at the desktop"


def test_an_image_run_reopens_nothing(reopening):
    """There is no window behind --image, and no sim to fire commands at."""
    config = _config()
    old = _LostSource()
    sources = {"pfd": old}

    main_module._reopen_closed(config, "frames/pfd.png", config.display("pfd"), sources)

    assert sources["pfd"] is old
    assert reopening["managed"] == 0


def test_the_run_loop_acts_on_a_closed_window_without_being_asked_to_look(monkeypatch):
    """The wiring, not the policy: a lost source reaches _reopen_closed.

    Checked through `main` rather than by reading the loop, because what makes
    this feature work is that the check sits on the path every cycle takes --
    a helper nothing calls would pass every test written about the helper.
    """
    seen = []
    monkeypatch.setattr(
        main_module, "_reopen_closed",
        lambda config, image, display, sources: seen.append(display.key),
    )
    monkeypatch.setattr(
        main_module, "_open_sources",
        lambda config, image, managed=frozenset(): {
            d.key: _LostSource(name=f"wgc:{d.key}") for d in config.active_displays
        },
    )

    assert main(["run", "--once", "--publisher", "console"]) == 0
    assert seen == ["pfd", "mfd"]


def test_a_healthy_source_is_left_alone_by_the_run_loop(monkeypatch):
    seen = []
    monkeypatch.setattr(
        main_module, "_reopen_closed",
        lambda config, image, display, sources: seen.append(display.key),
    )
    monkeypatch.setattr(
        main_module, "_open_sources",
        lambda config, image, managed=frozenset(): {
            d.key: _LostSource(name=f"wgc:{d.key}", window_closed=False)
            for d in config.active_displays
        },
    )

    assert main(["run", "--once", "--publisher", "console"]) == 0
    assert seen == []


def test_dump_cells_writes_the_picture_the_daemon_actually_uses(tmp_path, monkeypatch):
    """The prep image is a diagnostic that gets believed, so it has to be the
    daemon's first variant and not an approximation of it.

    It has been wrong twice. First it used ``preprocess_cell``'s own default
    sharpening, which no shipped ladder rung produces. Then it fell back to
    reading polarity off the binarised ring while the pipeline read it off the
    colour classifier -- so a cell the daemon inverted correctly was dumped
    un-inverted, and the picture said the opposite of the truth. This pins it
    to the real thing rather than to a description of it.
    """
    import cv2

    from glasslinkxp import synth
    from glasslinkxp.color import background_is_light
    from glasslinkxp.config import AppConfig, ColorConfig, DisplayConfig, OcrConfig, StripGeometry
    from glasslinkxp.main import build_parser, cmd_dump_cells
    from glasslinkxp.pipeline import SharpenLadder
    from glasslinkxp.strip import split_cells

    frame_dir = tmp_path / "frames"
    frame_dir.mkdir()
    frame = synth.render_menu("pfd_top")
    cv2.imwrite(str(frame_dir / "pfd.png"), frame)

    display = DisplayConfig(key="pfd", geometry=StripGeometry())
    config = AppConfig(displays=(display,), ocr=OcrConfig(), color=ColorConfig())
    out = tmp_path / "cells"
    args = build_parser().parse_args(
        ["dump-cells", "--out", str(out), "--image", str(frame_dir / "pfd.png")]
    )
    assert cmd_dump_cells(args, config) == 0

    for index, cell in enumerate(split_cells(frame, display.geometry), start=1):
        expected = next(iter(SharpenLadder(
            cell, config.ocr, light_background=background_is_light(cell, config.color)
        )))
        written = cv2.imread(str(out / f"pfd_{index:02d}_prep.png"), cv2.IMREAD_GRAYSCALE)
        assert written is not None, f"cell {index} was not written"
        assert written.shape == expected.shape, f"cell {index} is not the daemon's first variant"
        assert (written == expected).all(), f"cell {index} differs from what run() would OCR"
