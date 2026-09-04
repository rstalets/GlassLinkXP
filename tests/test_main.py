"""CLI smoke tests -- every subcommand that can run without X-Plane."""

import json

import pytest

from g1000_softkey import synth
from g1000_softkey.main import main


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
