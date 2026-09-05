"""Softkey cell background colour: measure it, name it, derive a text colour.

The G1000 says things with the background of a softkey cell that it never says
with the glyphs: a selected key is drawn inverted (white box, black text), a
caution is yellow, a warning is red. None of that survives the OCR stage --
``preprocess_cell()`` binarises to black-on-white on purpose, and normalises
the polarity away precisely so an inverted cell reads the same as a normal
one. So the colour has to be taken from the cell *before* that, from the same
BGR slices ``split_cells()`` already returns.

Two decisions here are worth stating plainly, because both were arrived at by
ruling out the obvious alternative:

**Measure on a border ring, not the whole cell.** Labels are centred, so the
outermost few pixels of a cell are essentially never glyph, whatever the cell
is doing. A whole-cell statistic has to assume the glyphs are the minority
class to find the background -- which is the assumption ``preprocess_cell()``
makes and the one an inverted cell is most likely to break when the highlight
box is smaller than the crop. The ring needs no such assumption and behaves
identically on an inverted cell. The ring is summarised with a per-channel
**median**: a mean would be dragged by the handful of clipped pixels where the
green softkey outline clips the ring.

**Classify in HSV, achromatic axes first.** Nearest-neighbour in RGB puts a
dimmed red closer to black than to red, because it measures brightness and
colour on the same axis. Hue and saturation barely move when the display is
dimmed -- scaling the red and yellow swatches to 35% brightness moves H and S
by about a unit each while V tracks the scaling -- so testing V, then S, then
H means brightness can only ever push a cell into BLACK, and can never turn a
yellow into a red.
"""

from __future__ import annotations

import cv2
import numpy as np

from .config import ColorConfig

#: The four background states, published as an int per cell.
BLACK = 0
WHITE = 1
YELLOW = 2
RED = 3

BACKGROUND_NAMES = {BLACK: "black", WHITE: "white", YELLOW: "yellow", RED: "red"}

#: Text colour per background. This is a **legibility rule** for the Stream
#: Deck face, not a measurement of the G1000's own font colour: whatever the
#: sim draws, a button whose face we have coloured needs text that can be read
#: against it. Measuring the real font colour is a different (and much harder)
#: problem -- the glyphs are ~10 px of anti-aliased, sometimes cyan text -- and
#: it is not what the Stream Deck needs.
TEXT_COLORS = {
    BLACK: "#FFFFFF",
    WHITE: "#000000",
    YELLOW: "#000000",
    RED: "#000000",
}

#: Where an unnameable hue falls back to black rather than white. Only reached
#: when a cell is chromatic but matches neither the red nor the yellow window
#: -- a green softkey outline filling the ring, or a miscalibrated crop sitting
#: on the blue sky. Calling that "yellow" would invent a caution the sim never
#: showed, so it degrades to the achromatic answer instead, which at worst
#: picks the less legible of two text colours.
FALLBACK_VALUE_MIDPOINT = 128


def as_bgr(cell: np.ndarray) -> np.ndarray:
    """Normalise a cell to 3-channel BGR (capture delivers BGRA on Windows)."""
    if cell.ndim == 2:
        return cv2.cvtColor(cell, cv2.COLOR_GRAY2BGR)
    if cell.shape[2] == 4:
        return cell[:, :, :3]
    return cell


def border_ring_mask(shape: tuple[int, ...], fraction: float) -> np.ndarray:
    """Boolean mask selecting the outermost ``fraction`` of a cell.

    A mask rather than four edge slices so the corners are counted once.
    Degenerate cells (a crop only a couple of pixels tall) select everything,
    which is the right answer for them: there is no interior to exclude.
    """
    height, width = shape[:2]
    ty = max(1, int(round(height * fraction)))
    tx = max(1, int(round(width * fraction)))
    mask = np.ones((height, width), dtype=bool)
    if height > 2 * ty and width > 2 * tx:
        mask[ty:height - ty, tx:width - tx] = False
    return mask


def border_ring_bgr(cell: np.ndarray, fraction: float = 0.15) -> tuple[int, int, int]:
    """Per-channel median BGR of the cell's border ring."""
    bgr = as_bgr(cell)
    if bgr.size == 0:
        return (0, 0, 0)
    ring = bgr[border_ring_mask(bgr.shape, fraction)]
    median = np.median(ring.reshape(-1, 3), axis=0)
    return tuple(int(round(v)) for v in median)  # type: ignore[return-value]


def bgr_to_hsv(bgr: tuple[int, int, int]) -> tuple[int, int, int]:
    """One BGR triple to OpenCV HSV (H 0-179, S 0-255, V 0-255)."""
    pixel = np.array([[list(bgr)]], dtype=np.uint8)
    h, s, v = cv2.cvtColor(pixel, cv2.COLOR_BGR2HSV)[0][0]
    return int(h), int(s), int(v)


def classify_hsv(hsv: tuple[int, int, int], config: ColorConfig) -> int:
    """HSV -> one of BLACK / WHITE / YELLOW / RED. Order matters (see module docstring)."""
    hue, saturation, value = hsv
    if value <= config.value_max:
        return BLACK
    if saturation <= config.saturation_max:
        return WHITE
    if hue <= config.red_hue_max or hue >= config.red_hue_wrap_min:
        return RED
    if config.yellow_hue_min <= hue <= config.yellow_hue_max:
        return YELLOW
    return WHITE if value >= FALLBACK_VALUE_MIDPOINT else BLACK


def classify_cell(cell: np.ndarray, config: ColorConfig) -> int:
    """Background classification for one BGR cell slice."""
    return classify_hsv(bgr_to_hsv(border_ring_bgr(cell, config.ring_fraction)), config)


def measure_cell(cell: np.ndarray, config: ColorConfig) -> tuple[tuple[int, int, int], tuple[int, int, int], int]:
    """(border-ring BGR, its HSV, classification) -- what ``dump-colors`` prints."""
    bgr = border_ring_bgr(cell, config.ring_fraction)
    hsv = bgr_to_hsv(bgr)
    return bgr, hsv, classify_hsv(hsv, config)


def background_name(background: int) -> str:
    return BACKGROUND_NAMES.get(background, "?")


def text_color(background: int) -> str:
    """Legible text colour for a background classification, as ``#RRGGBB``."""
    return TEXT_COLORS.get(background, "#FFFFFF")


#: PilotsDeck's inline text-colour marker. A displayed string beginning with
#: "[[#RRGGBB" overrides the button's configured font colour for that update.
TEXT_COLOR_MARKER = "[["


def text_color_prefix(background: int) -> str:
    """The 9-character PilotsDeck prefix for a background, e.g. ``[[#000000``.

    Kept next to the colours it wraps so the one place that knows this markup
    is the one place that knows the colours; ``publish.embed_text_color``
    decides whether a label ever gets it.
    """
    return TEXT_COLOR_MARKER + text_color(background)
