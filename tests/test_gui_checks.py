"""The calibration editor's "does this crop cut the labels?" check."""

import numpy as np
import pytest

from g1000_softkey import synth
from g1000_softkey.config import OcrConfig, StripGeometry
from g1000_softkey.gui import checks
from g1000_softkey.strip import clipped_edges, ink_bounds, ink_extent


@pytest.fixture(scope="module")
def frames(tmp_path_factory):
    path = tmp_path_factory.mktemp("frames")
    return synth.write_menus(path)


@pytest.fixture(scope="module")
def frame(frames):
    """A real synthetic strip, read the way the daemon reads one."""
    return checks.load_frame(next(p for p in frames if p.stem == "pfd_menu"))


def _cell(ink_box=None, size=(40, 80)):
    """A blank cell, optionally with a block of ink at (y0, y1, x0, x1)."""
    cell = np.zeros((*size, 3), dtype=np.uint8)
    if ink_box is not None:
        y0, y1, x0, x1 = ink_box
        cell[y0:y1, x0:x1] = 255
    return cell


# -- the shared check in strip.py ------------------------------------------


def test_ink_in_the_middle_touches_nothing():
    assert clipped_edges(_cell((10, 30, 20, 60))) == ()


@pytest.mark.parametrize("box,expected", [
    ((10, 30, 0, 40), ("left",)),
    ((10, 30, 40, 80), ("right",)),
    ((0, 20, 20, 60), ("top",)),
    ((20, 40, 20, 60), ("bottom",)),
])
def test_the_edge_the_ink_reaches_is_the_edge_reported(box, expected):
    assert clipped_edges(_cell(box)) == expected


def test_ink_on_every_side_reports_every_side():
    cell = _cell()
    cell[:2, :] = cell[-2:, :] = cell[:, :2] = cell[:, -2:] = 255
    assert clipped_edges(cell) == ("left", "right", "top", "bottom")


def test_a_cell_that_is_entirely_one_tone_reports_nothing():
    """Ink is deviation from the cell's dominant tone, so a uniform cell has
    none of it by definition -- whether it is all black or all white.

    That is a false negative and a real limit: a crop that has slipped
    entirely onto a solid region says nothing is wrong. The check finds crops
    that cut a label, not every crop that is in the wrong place, which is why
    it is offered as a hint and why the close-up is still the thing to look
    at.
    """
    solid = _cell()
    solid[:, :] = 255
    assert clipped_edges(solid) == ()
    assert clipped_edges(_cell()) == ()


def test_vertical_clipping_is_seen_at_all():
    """cell_pad_y can cut the tops off capitals as easily as cell_pad_x can
    cut the ends off a word; a horizontal-only check misses half of it."""
    assert "top" in clipped_edges(_cell((0, 20, 20, 60)))


def test_a_cell_with_no_ink_reports_nothing():
    """An empty softkey is not a clipped one, and the G1000 leaves plenty."""
    assert clipped_edges(_cell()) == ()
    assert ink_extent(_cell()) is None


def test_ink_bounds_still_answers_what_it_always_did():
    assert ink_bounds(_cell()) == (0.0, 0.0)
    left, right = ink_bounds(_cell((10, 30, 20, 60)))
    assert 0.2 < left < 0.3 and 0.7 < right < 0.8


# -- the editor's use of it ------------------------------------------------


def test_a_correct_crop_of_the_whole_corpus_says_nothing(frames):
    """The measurement that chose CLIP_MARGIN, kept as a regression.

    At a geometry known to be right, long labels come within one pixel of the
    crop edge -- CHKLIST, ALERTS and STD BARO all do -- so any tolerance
    expressed as a percentage of the cell flags a correct calibration. Ink in
    the outermost pixel does not happen unless the crop cut it. If this starts
    failing, it is that claim that has stopped being true, and the fix is to
    measure again rather than to widen the margin.
    """
    for path in frames:
        clips = checks.check_cells(checks.load_frame(path), StripGeometry())
        assert clips == [], f"{path.stem}: {[str(c) for c in clips]}"


def test_over_trimming_the_whole_corpus_is_noticed(frames):
    for path in frames:
        clips = checks.check_cells(
            checks.load_frame(path), StripGeometry(cell_pad_x=0.30, cell_pad_y=0.35)
        )
        assert len(clips) >= 5, f"{path.stem}: {[str(c) for c in clips]}"


def test_over_trimming_a_real_strip_is_noticed(frame):
    clips = checks.check_cells(frame, StripGeometry(cell_pad_x=0.30, cell_pad_y=0.35))
    assert len(clips) >= 6
    assert all(c.edges for c in clips)


def test_the_blank_softkeys_are_not_reported(frame):
    """pfd_menu leaves cells empty; every one of them would otherwise read as
    ink touching every edge, and the warning would be permanent noise."""
    clips = checks.check_cells(frame, StripGeometry(cell_pad_x=0.30, cell_pad_y=0.35))
    reported = {c.cell for c in clips}
    blank = {6, 9}
    assert not (reported & blank), sorted(reported & blank)


def test_a_label_one_pixel_from_the_edge_is_not_reported():
    """The case that made a percentage tolerance unusable."""
    cell = _cell((10, 30, 1, 79))
    assert clipped_edges(cell) == ()
    assert clipped_edges(_cell((10, 30, 0, 79))) == ("left",)


def test_the_users_own_blank_threshold_is_used(frame):
    """Somebody who lowered blank_contrast to keep dim labels wants those
    cells checked too."""
    strict = OcrConfig(blank_ink_ratio=0.9)
    assert checks.check_cells(frame, StripGeometry(cell_pad_x=0.3), strict) == []


def test_a_frame_is_read_exactly_as_the_capture_path_reads_it(tmp_path):
    """The docstring says "byte for byte the one the capture path produces",
    and the flag underneath it said otherwise: IMREAD_UNCHANGED keeps whatever
    the file has, so a PNG with an alpha channel arrived with four channels
    where the reader would have three. Asked of ImageCapture rather than
    asserted about channel counts."""
    import cv2

    from g1000_softkey.capture import ImageCapture

    with_alpha = np.zeros((30, 60, 4), dtype=np.uint8)
    with_alpha[..., :3] = 200
    with_alpha[..., 3] = 128
    path = tmp_path / "frame.png"
    cv2.imwrite(str(path), with_alpha)

    source = ImageCapture(path)
    try:
        theirs = source.grab()
    finally:
        source.close()
    ours = checks.load_frame(path)

    assert ours is not None and theirs is not None
    assert ours.shape == theirs.shape
    assert ours.dtype == theirs.dtype
    assert np.array_equal(ours, theirs)


def test_a_missing_picture_is_not_an_error(tmp_path):
    assert checks.load_frame(tmp_path / "nope.png") is None
    assert checks.check_cells(None, StripGeometry()) == []


def test_a_degenerate_crop_does_not_raise(frame):
    checks.check_cells(frame, StripGeometry(cell_pad_x=0.45, cell_pad_y=0.45))


# -- what it says ----------------------------------------------------------


def test_nothing_to_say_says_nothing():
    assert checks.describe([]) == ""


def test_one_cell_reads_as_one_cell():
    assert checks.describe([checks.CellClip(4, ("left",))]) == "1 cell may be clipped: cell 4 (left)."


def test_the_display_is_named_when_it_is_known():
    text = checks.describe([checks.CellClip(4, ("left",))], "pfd")
    assert "on PFD" in text


def test_a_long_list_is_summarised_rather_than_recited():
    clips = [checks.CellClip(n, ("left",)) for n in range(1, 13)]
    text = checks.describe(clips)
    assert text.startswith("12 cells may be clipped")
    assert "and 6 more" in text
    assert "cell 12" not in text


def test_the_advice_names_the_control_for_each_edge():
    for word in ("side trim", "top/bottom trim"):
        assert word in checks.ADVICE.lower()
    # It has to say it is a hint; a warning read as a verdict gets worked around.
    assert "hint" in checks.ADVICE.lower()
