import numpy as np
import pytest

from g1000_softkey import synth
from g1000_softkey.config import StripGeometry
from g1000_softkey.strip import (
    auto_detect_strip,
    cell_rects,
    changed_cells,
    crop_strip,
    ink_ratio,
    is_blank,
    overlay_geometry,
    preprocess_cell,
    snapshot_cells,
    split_cells,
    strip_rect,
)

GEOM = StripGeometry()


@pytest.fixture(scope="module")
def frame():
    return synth.render_menu("pfd_menu")


def test_strip_rect_is_fractional(frame):
    rect = strip_rect(frame.shape, GEOM)
    assert rect.x == round(GEOM.x * frame.shape[1])
    assert rect.y == round(GEOM.y * frame.shape[0])
    assert rect.w == round(GEOM.w * frame.shape[1])


def test_crop_and_split(frame):
    strip = crop_strip(frame, GEOM)
    assert strip.shape[0] == round(GEOM.h * frame.shape[0])
    cells = split_cells(frame, GEOM)
    assert len(cells) == 12
    widths = {cell.shape[1] for cell in cells}
    assert max(widths) - min(widths) <= 2  # evenly spaced
    rects = cell_rects(frame.shape, GEOM)
    assert rects[0].x < rects[-1].x
    assert all(r.w > 0 and r.h > 0 for r in rects)


def test_blank_cells_are_detected(frame):
    cells = split_cells(frame, GEOM)
    blanks = [i for i, cell in enumerate(cells) if is_blank(cell)]
    expected = [i for i, label in enumerate(synth.MENUS["pfd_menu"]) if not label]
    assert blanks == expected


def test_ink_ratio_orders_blank_below_labelled(frame):
    cells = split_cells(frame, GEOM)
    assert ink_ratio(cells[5]) < ink_ratio(cells[1])  # blank vs 'DFLTS'


def test_preprocess_gives_dark_text_on_light_paper(frame):
    """Both a normal and a highlighted (selected) cell must come out with a
    white background -- that is the whole point of per-cell thresholding."""
    cells = split_cells(frame, GEOM)
    for index in (0, 1, 11):  # 0 is the selected/highlighted cell
        binary = preprocess_cell(cells[index], upscale=3.0)
        assert set(np.unique(binary)) <= {0, 255}
        assert binary.mean() > 160, f"cell {index} came out inverted"
        assert binary.shape[0] > cells[index].shape[0]  # upscaled


def test_preprocess_rejects_unknown_method(frame):
    cell = split_cells(frame, GEOM)[0]
    with pytest.raises(ValueError):
        preprocess_cell(cell, method="magic")


def test_change_gating_flags_only_changed_cells():
    before = split_cells(synth.render_menu("pfd_top"), GEOM)
    after_frame = synth.render_menu("pfd_top")
    after = split_cells(after_frame, GEOM)
    assert changed_cells(None, snapshot_cells(after)) == [True] * 12

    same = changed_cells(snapshot_cells(before), snapshot_cells(after), tolerance=6)
    assert not any(same), "identical frames must not be flagged as changed"

    labels = list(synth.MENUS["pfd_top"])
    labels[4] = "BACK"
    changed_frame = synth.render_frame(labels, selected=synth.SELECTED["pfd_top"])
    flags = changed_cells(
        snapshot_cells(before), snapshot_cells(split_cells(changed_frame, GEOM)), tolerance=6
    )
    assert flags[4] is True
    assert sum(flags) == 1


def test_auto_detect_finds_the_band(frame):
    detected = auto_detect_strip(frame)
    assert detected is not None
    true_top = GEOM.y * frame.shape[0]
    true_bottom = (GEOM.y + GEOM.h) * frame.shape[0]
    top = detected.y * frame.shape[0]
    bottom = (detected.y + detected.h) * frame.shape[0]
    assert abs(top - true_top) <= 4, "detected band starts far from the real strip"
    assert bottom >= true_bottom - 4
    assert detected.x <= GEOM.x + 0.05


def test_auto_detect_returns_none_without_a_band():
    blank = np.full((400, 600, 3), 200, np.uint8)
    assert auto_detect_strip(blank) is None


def test_overlay_draws_without_touching_the_original(frame):
    annotated = overlay_geometry(frame, GEOM)
    assert annotated.shape == frame.shape
    assert not np.array_equal(annotated, frame)
