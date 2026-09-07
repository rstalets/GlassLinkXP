from pathlib import Path

import cv2
import numpy as np
import pytest

from glasslinkxp import synth
from glasslinkxp.config import StripGeometry
from glasslinkxp.strip import (
    sharpen,
    auto_detect_strip,
    cell_rects,
    changed_cells,
    crop_strip,
    ink_ratio,
    is_blank,
    measure_ink,
    to_gray,
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


def _three_separate_passes(cell, contrast=40):
    """The ink measurements as they were written before they shared a pass.

    Copied here on purpose. The point of the refactor was that one grayscale
    conversion and one median can answer all three questions instead of
    three; the point of this copy is that "instead of" has to mean the same
    answers, and a test that called the shipped code twice could not tell.
    """
    gray = to_gray(cell).astype(np.int16)
    dominant = float(np.median(gray))
    ratio = float(np.mean(np.abs(gray - dominant) > contrast))

    mask = np.abs(to_gray(cell).astype(np.int16) - np.median(to_gray(cell))) > contrast
    columns = np.flatnonzero(mask.any(axis=0))
    rows = np.flatnonzero(mask.any(axis=1))
    if columns.size == 0 or rows.size == 0:
        return ratio, (0.0, 0.0), ()
    width = max(1, mask.shape[1] - 1)
    height = max(1, mask.shape[0] - 1)
    bounds = (float(columns[0]) / width, float(columns[-1]) / width)
    edges = []
    if columns[0] <= 0:
        edges.append("left")
    if columns[-1] >= mask.shape[1] - 1:
        edges.append("right")
    if rows[0] <= 0:
        edges.append("top")
    if rows[-1] >= mask.shape[0] - 1:
        edges.append("bottom")
    return ratio, bounds, tuple(edges)


@pytest.mark.parametrize("menu", sorted(synth.MENUS))
@pytest.mark.parametrize("contrast", [20, 40, 80])
def test_the_shared_pass_answers_what_the_three_passes_did(menu, contrast):
    """Every cell of every synthetic frame, at three contrasts."""
    for cell in split_cells(synth.render_menu(menu), GEOM):
        expected = _three_separate_passes(cell, contrast)
        ink = measure_ink(cell, contrast)
        assert (ink.ratio, ink.bounds, ink.clipped_edges()) == expected


def test_the_shared_pass_measures_the_cell_once(monkeypatch):
    """Not an optimisation that can quietly stop being one: three calls to
    to_gray per cell is what this replaced."""
    import glasslinkxp.strip as strip_module

    calls = []
    real = strip_module.to_gray
    monkeypatch.setattr(strip_module, "to_gray", lambda img: (calls.append(1), real(img))[1])

    cell = split_cells(synth.render_menu("pfd_menu"), GEOM)[1]
    ink = measure_ink(cell)
    assert (ink.ratio, ink.bounds, ink.clipped_edges()) == _three_separate_passes(cell)
    assert len(calls) == 1, "one grayscale conversion for all three answers"


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


# ---------------------------------------------------------------------------
# Small-glyph counters: the 0/6 failure
# ---------------------------------------------------------------------------


def _tiny_digit(text, glyph_px=10, blur=0.8, w=59, h=24):
    """A cell at the real captured scale: ~10 px glyph, slightly soft."""
    from PIL import Image, ImageDraw, ImageFont

    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if Path(candidate).is_file():
            font = ImageFont.truetype(candidate, glyph_px)
            break
    else:  # pragma: no cover - depends on the host's fonts
        pytest.skip("no TrueType font available")

    img = Image.new("L", (w, h), 12)
    draw = ImageDraw.Draw(img)
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((w - box[2]) // 2, (h - box[3]) // 2 - 1), text, fill=230, font=font)
    array = np.array(img)
    return cv2.GaussianBlur(array, (0, 0), blur) if blur else array


def _counters(binary):
    """Enclosed background regions -- a surviving counter shows up as one."""
    ink = (binary < 128).astype(np.uint8)
    count, _ = cv2.connectedComponents((1 - ink).astype(np.uint8))
    return count - 2


@pytest.mark.parametrize("digit", ["0", "6", "9"])
def test_sharpening_reopens_small_closed_glyphs(digit):
    """0/6/9 lose their holes when a soft ~10 px glyph is thresholded.

    A filled counter is not a character, so Tesseract returns an empty string
    rather than a wrong digit -- which is how this reached us: "0 is missing".
    """
    cell = _tiny_digit(digit)
    assert _counters(preprocess_cell(cell, sharpen_amount=0.0)) == 0, "expected the bug"
    assert _counters(preprocess_cell(cell)) >= 1, f"{digit} still has no counter"


def test_eight_is_still_beyond_recovery_at_ten_pixels():
    """Documents the limit: two stacked counters in ~10 px do not survive.

    Harmless for the screen this was found on -- transponder codes are octal,
    so the XPDR keypad only ever shows 0-7 -- but if an 8 shows up elsewhere
    and reads empty, the strip needs more pixels, not more sharpening.
    """
    assert _counters(preprocess_cell(_tiny_digit("8"))) == 0


@pytest.mark.parametrize("digit", ["0", "6", "8", "9"])
def test_sharpening_never_removes_counters(digit):
    cell = _tiny_digit(digit)
    assert _counters(preprocess_cell(cell)) >= _counters(
        preprocess_cell(cell, sharpen_amount=0.0)
    )


def test_sharpen_is_a_no_op_when_disabled():
    cell = _tiny_digit("0")
    assert np.array_equal(sharpen(cell, 0.0, 1.4), cell)