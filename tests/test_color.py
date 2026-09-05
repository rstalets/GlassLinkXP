"""Background-colour classification: the ring measurement and the HSV rules.

These prove the *shape* of the classifier -- four classes, stable under
dimming, unaffected by the glyphs -- against synthetic swatches. They cannot
prove the thresholds, because nothing here has ever seen an X-Plane frame;
that is what the ``dump-colors`` subcommand is for.
"""

import numpy as np
import pytest

from g1000_softkey import synth
from g1000_softkey.color import (
    BLACK,
    RED,
    WHITE,
    YELLOW,
    background_name,
    bgr_to_hsv,
    border_ring_bgr,
    border_ring_mask,
    classify_cell,
    classify_hsv,
    measure_cell,
)
from g1000_softkey.config import ColorConfig, ConfigError, StripGeometry, from_mapping
from g1000_softkey.strip import split_cells

COLOR = ColorConfig()

#: BGR, the same plausible swatches the defaults were set from.
SWATCHES = {
    BLACK: (15, 15, 15),
    WHITE: (235, 235, 235),
    YELLOW: (40, 230, 240),
    RED: (40, 40, 225),
}


def cell(bgr, size=(24, 90), glyph=None):
    """A flat cell of one colour, optionally with a contrasting blob in the middle."""
    image = np.full((size[0], size[1], 3), bgr, dtype=np.uint8)
    if glyph is not None:
        h, w = size
        image[h // 3: 2 * h // 3, w // 3: 2 * w // 3] = glyph
    return image


def scaled(bgr, factor):
    return tuple(int(round(channel * factor)) for channel in bgr)


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("expected,bgr", sorted(SWATCHES.items()))
def test_each_swatch_classifies_as_itself(expected, bgr):
    assert classify_cell(cell(bgr), COLOR) == expected


@pytest.mark.parametrize("factor", [1.0, 0.8, 0.6, 0.45, 0.35])
@pytest.mark.parametrize("expected,bgr", sorted(SWATCHES.items()))
def test_classification_survives_dimming(expected, bgr, factor):
    """Brightness is a separate axis: dimming must not rename a colour.

    Down to 35% -- where red sits at V=79 against a value_max of 60 -- the hue
    and saturation of the coloured swatches barely move, which is the whole
    reason V is tested first and hue last.
    """
    assert classify_cell(cell(scaled(bgr, factor)), COLOR) == expected


def test_hue_and_saturation_barely_move_when_dimmed():
    for bgr in (SWATCHES[YELLOW], SWATCHES[RED]):
        full = bgr_to_hsv(bgr)
        dim = bgr_to_hsv(scaled(bgr, 0.35))
        assert abs(full[0] - dim[0]) <= 2, "hue moved"
        assert abs(full[1] - dim[1]) <= 6, "saturation moved"
        assert dim[2] < full[2] / 2, "value should track the scaling"


def test_a_dimmed_colour_can_only_ever_become_black():
    """The one failure mode brightness is allowed to cause."""
    assert classify_cell(cell(scaled(SWATCHES[RED], 0.15)), COLOR) == BLACK
    assert classify_cell(cell(scaled(SWATCHES[WHITE], 0.15)), COLOR) == BLACK


def test_yellow_is_never_mistaken_for_red_at_any_brightness():
    for factor in [f / 20 for f in range(7, 21)]:
        assert classify_cell(cell(scaled(SWATCHES[YELLOW], factor)), COLOR) != RED
        assert classify_cell(cell(scaled(SWATCHES[RED], factor)), COLOR) != YELLOW


def test_an_unnameable_hue_falls_back_to_the_achromatic_answer():
    """A saturated green (the softkey outline) must not invent a caution."""
    bright_green = (40, 220, 40)
    dark_green = (10, 60, 10)
    assert classify_hsv(bgr_to_hsv(bright_green), COLOR) == WHITE
    assert classify_hsv(bgr_to_hsv(dark_green), COLOR) == BLACK


def test_thresholds_come_from_the_config_not_the_code():
    lenient = ColorConfig(value_max=250)          # everything is dark enough
    assert classify_cell(cell(SWATCHES[WHITE]), lenient) == BLACK
    narrow = ColorConfig(yellow_hue_min=29, yellow_hue_max=30)
    assert classify_cell(cell(SWATCHES[YELLOW]), narrow) != YELLOW


# ---------------------------------------------------------------------------
# the border ring
# ---------------------------------------------------------------------------


def test_the_ring_ignores_the_glyphs():
    """A white blob covering the middle must not shift the black background."""
    inverted = cell(SWATCHES[BLACK], glyph=(255, 255, 255))
    assert classify_cell(inverted, COLOR) == BLACK
    assert classify_cell(cell(SWATCHES[WHITE], glyph=(0, 0, 0)), COLOR) == WHITE


def test_the_ring_is_a_median_so_a_clipped_edge_does_not_move_it():
    """A few pixels of green softkey outline are a minority the median drops."""
    image = cell(SWATCHES[BLACK])
    image[0, :6] = (0, 255, 0)
    image[-1, :6] = (0, 255, 0)
    assert border_ring_bgr(image) == SWATCHES[BLACK]


def test_the_ring_excludes_the_interior_but_covers_the_border():
    mask = border_ring_mask((20, 100), 0.15)
    assert mask[0].all() and mask[-1].all()
    assert mask[:, 0].all() and mask[:, -1].all()
    assert not mask[10, 50]
    assert 0.2 < mask.mean() < 0.8


def test_a_degenerate_cell_selects_everything():
    assert border_ring_mask((2, 3), 0.15).all()
    assert border_ring_bgr(cell(SWATCHES[RED], size=(2, 3))) == SWATCHES[RED]


def test_grayscale_and_bgra_cells_are_accepted():
    gray = np.full((20, 60), 235, dtype=np.uint8)
    assert classify_cell(gray, COLOR) == WHITE
    bgra = np.dstack([cell(SWATCHES[RED]), np.full((24, 90), 255, np.uint8)])
    assert classify_cell(bgra, COLOR) == RED


def test_every_class_has_a_distinct_name():
    names = {background_name(b) for b in (BLACK, WHITE, YELLOW, RED)}
    assert names == {"black", "white", "yellow", "red"}


# ---------------------------------------------------------------------------
# against the synthetic strip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dim", [1.0, 0.6, 0.35])
def test_the_synthetic_alerts_strip_classifies_cell_by_cell(dim):
    frame = synth.render_menu("alerts", dim=dim)
    cells = split_cells(frame, StripGeometry())
    expected = [
        synth.BACKGROUNDS["alerts"].get(i, "black") for i in range(len(cells))
    ]
    got = [background_name(classify_cell(c, COLOR)) for c in cells]
    assert got == expected


def test_measure_cell_reports_what_dump_colors_prints():
    frame = synth.render_menu("alerts")
    yellow_cell = split_cells(frame, StripGeometry())[4]
    bgr, hsv, background = measure_cell(yellow_cell, COLOR)
    assert background == YELLOW
    assert bgr_to_hsv(bgr) == hsv
    assert hsv[1] > COLOR.saturation_max and hsv[2] > COLOR.value_max


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_color_thresholds_load_from_toml():
    config = from_mapping({"color": {"enabled": False, "value_max": 25, "yellow_hue_max": 45}})
    assert config.color.enabled is False
    assert config.color.value_max == 25 and config.color.yellow_hue_max == 45


@pytest.mark.parametrize("section", [
    {"ring_fraction": 0.9},
    {"value_max": 300},
    {"red_hue_max": 200},          # 0..179 in OpenCV, not 0..359
    {"yellow_hue_min": 40, "yellow_hue_max": 20},
])
def test_nonsensical_thresholds_are_rejected(section):
    with pytest.raises(ConfigError):
        from_mapping({"color": section})
