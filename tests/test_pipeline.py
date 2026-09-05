"""End-to-end offline pipeline tests: synthetic frame in, labels out."""

import pytest

from g1000_softkey import synth
from g1000_softkey.config import AppConfig, DisplayConfig, StripGeometry
from g1000_softkey.pipeline import DisplayPipeline
from g1000_softkey.strip import auto_detect_strip

MENUS = sorted(synth.MENUS)


def make_pipeline(reader, config, geometry=None, gating=True):
    display = DisplayConfig(key="pfd", geometry=geometry or StripGeometry())
    app = AppConfig(displays=(display,), ocr=config.ocr, change_gating=gating)
    return DisplayPipeline(display, reader, app)


@pytest.mark.parametrize("menu", MENUS)
def test_every_menu_is_read_exactly(menu, reader, config):
    pipeline = make_pipeline(reader, config)
    result = pipeline.process(synth.render_menu(menu))
    assert result.labels == synth.MENUS[menu]
    assert result.ocr_calls == sum(1 for label in synth.MENUS[menu] if label)


def test_blank_cells_are_reported_empty_and_skip_ocr(reader, config):
    pipeline = make_pipeline(reader, config)
    result = pipeline.process(synth.render_menu("xpdr"))
    blanks = [cell for cell in result.cells if cell.blank]
    assert len(blanks) == 4
    assert all(cell.text == "" for cell in blanks)


def test_change_gating_skips_unchanged_cells(reader, config):
    pipeline = make_pipeline(reader, config, gating=True)
    first = pipeline.process(synth.render_menu("pfd_top"))
    assert first.ocr_calls == 10

    second = pipeline.process(synth.render_menu("pfd_top"))
    assert second.ocr_calls == 0
    assert second.labels == first.labels
    assert all(not cell.ocr_ran for cell in second.cells)

    labels = list(synth.MENUS["pfd_top"])
    labels[4] = "BACK"
    changed = synth.render_frame(labels, selected=synth.SELECTED["pfd_top"])
    third = pipeline.process(changed)
    assert third.ocr_calls == 1
    assert third.labels[4] == "BACK"
    assert third.labels[3] == first.labels[3]


def test_gating_can_be_switched_off(reader, config):
    pipeline = make_pipeline(reader, config, gating=False)
    frame = synth.render_menu("inset")
    pipeline.process(frame)
    again = pipeline.process(frame)
    assert again.ocr_calls == sum(1 for label in synth.MENUS["inset"] if label)
    assert again.labels == synth.MENUS["inset"]


def test_timings_are_reported(reader, config):
    pipeline = make_pipeline(reader, config)
    result = pipeline.process(synth.render_menu("mfd_top"))
    assert set(result.timings) == {"split_ms", "gate_ms", "preprocess_ms", "ocr_ms", "screen_ms"}
    assert result.timings["ocr_ms"] > 0


def test_snapping_is_actually_used(reader, config):
    """At least one label per test corpus needs the vocabulary snap; assert
    the raw string is kept alongside the snapped one."""
    pipeline = make_pipeline(reader, config)
    result = pipeline.process(synth.render_menu("pfd_top"))
    tmr = result.cells[8]
    assert tmr.text == "TMR/REF"
    assert tmr.raw != "" and tmr.match_score > 0


def test_auto_detected_geometry_is_usable(reader, config):
    """The coarse auto-detect is only a seed for calibration, but it should
    still get the large majority of cells right."""
    correct = total = 0
    for menu in MENUS:
        frame = synth.render_menu(menu)
        geometry = auto_detect_strip(frame)
        assert geometry is not None
        pipeline = make_pipeline(reader, config, geometry=geometry)
        for got, want in zip(pipeline.process(frame).labels, synth.MENUS[menu]):
            total += 1
            correct += got == want
    assert correct / total >= 0.95, f"only {correct}/{total} cells correct"


def test_frames_from_disk_round_trip(tmp_path, reader, config):
    from g1000_softkey.capture import ImageCapture

    paths = synth.write_menus(tmp_path)
    source = ImageCapture(tmp_path)
    pipeline = make_pipeline(reader, config)
    assert len(paths) == len(synth.MENUS)
    frame = source.grab()
    assert frame is not None
    labels = pipeline.process(frame).labels
    assert any(labels == menu for menu in synth.MENUS.values())
