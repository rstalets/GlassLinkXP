"""CLI smoke tests -- every subcommand that can run without X-Plane."""

import json

import pytest

from g1000_softkey import synth
from g1000_softkey.color import BLACK, RED, WHITE, YELLOW
from g1000_softkey.config import DisplayConfig, PublishConfig
from g1000_softkey.main import _values, main
from g1000_softkey.ocr import CellResult
from g1000_softkey.pipeline import DisplayResult


@pytest.fixture(scope="module")
def frames(tmp_path_factory):
    path = tmp_path_factory.mktemp("frames")
    synth.write_menus(path)
    return path


def test_synth_writes_frames(tmp_path):
    assert main(["synth", "--out", str(tmp_path)]) == 0
    assert len(list(tmp_path.glob("*.png"))) == len(synth.MENUS)


def test_run_once_with_the_file_publisher(frames, tmp_path, monkeypatch):
    target = tmp_path / "labels.json"
    monkeypatch.setenv("G1000_SOFTKEY_JSON", str(target))
    assert main(["run", "--image", str(frames / "pfd_top.png"), "--publisher", "file", "--once"]) == 0
    labels = json.loads(target.read_text())["labels"]
    assert labels["g1000/softkey/pfd/1"] == "INSET"
    assert labels["g1000/softkey/mfd/12"] == "ALERTS"
    assert labels["g1000/softkey/pfd/2"] == ""


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
    assert values["g1000/softkey/pfd/1"] == "INSET"
    assert values["g1000/softkey/pfd/1/bg"] == BLACK
    assert values["g1000/softkey/pfd/2/bg"] == WHITE
    assert values["g1000/softkey/pfd/3/bg"] == YELLOW
    assert values["g1000/softkey/pfd/4/bg"] == RED
    # A blank cell publishes an empty label and still reports its colour.
    assert values["g1000/softkey/pfd/5"] == ""
    assert values["g1000/softkey/pfd/5/bg"] == YELLOW
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
    assert values["g1000/softkey/pfd/1"] == "STD BARO"
    assert values["g1000/softkey/pfd/2"] == "WARNING"


def test_the_longest_label_fits_the_default_field_width():
    from g1000_softkey.publish import encode_field

    longest = "FLIGHT PLAN"
    width = PublishConfig().field_width
    assert encode_field(longest, width).rstrip(b"\x00").decode() == longest


def test_run_publishes_colours_through_the_file_publisher(frames, tmp_path, monkeypatch):
    target = tmp_path / "labels.json"
    monkeypatch.setenv("G1000_SOFTKEY_JSON", str(target))
    assert main(["run", "--image", str(frames / "alerts.png"),
                 "--publisher", "file", "--once"]) == 0
    payload = json.loads(target.read_text())
    assert payload["numbers"]["g1000/softkey/pfd/5/bg"] == YELLOW
    assert payload["numbers"]["g1000/softkey/pfd/6/bg"] == RED
    assert payload["numbers"]["g1000/softkey/pfd/9/bg"] == WHITE
    assert payload["numbers"]["g1000/softkey/pfd/1/bg"] == BLACK
    assert payload["labels"]["g1000/softkey/pfd/5"] == "CAUTION"


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
