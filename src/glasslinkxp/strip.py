"""Softkey strip geometry and per-cell image preprocessing.

Pipeline stage 2: frame -> strip crop -> 12 cells -> binarised cell images
that Tesseract likes (dark text on a white background, ~30px cap height).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import cached_property

import cv2
import numpy as np

from .color import border_ring_mask
from .config import ColorConfig, StripGeometry

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def slice(self) -> tuple[slice, slice]:
        return slice(self.y, self.y + self.h), slice(self.x, self.x + self.w)


def to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def strip_rect(frame_shape: tuple[int, ...], geom: StripGeometry) -> Rect:
    height, width = frame_shape[:2]
    x = int(round(geom.x * width))
    y = int(round(geom.y * height))
    w = max(1, int(round(geom.w * width)))
    h = max(1, int(round(geom.h * height)))
    w = min(w, width - x)
    h = min(h, height - y)
    return Rect(x, y, w, h)


def crop_strip(frame: np.ndarray, geom: StripGeometry) -> np.ndarray:
    rect = strip_rect(frame.shape, geom)
    ys, xs = rect.slice
    return frame[ys, xs]


def cell_rects(frame_shape: tuple[int, ...], geom: StripGeometry) -> list[Rect]:
    """Cell rectangles in *frame* coordinates (for the calibration overlay)."""
    strip = strip_rect(frame_shape, geom)
    cell_w = strip.w / geom.cells
    pad_x = int(round(cell_w * geom.cell_pad_x))
    pad_y = int(round(strip.h * geom.cell_pad_y))
    rects: list[Rect] = []
    for i in range(geom.cells):
        x0 = strip.x + int(round(i * cell_w)) + pad_x
        x1 = strip.x + int(round((i + 1) * cell_w)) - pad_x
        rects.append(Rect(x0, strip.y + pad_y, max(1, x1 - x0), max(1, strip.h - 2 * pad_y)))
    return rects


def split_cells(frame: np.ndarray, geom: StripGeometry) -> list[np.ndarray]:
    """Split the strip into ``geom.cells`` padded cell images (frame slices)."""
    cells = []
    for rect in cell_rects(frame.shape, geom):
        ys, xs = rect.slice
        cells.append(frame[ys, xs])
    return cells


# ---------------------------------------------------------------------------
# Preprocessing
# ---------------------------------------------------------------------------


#: How many pixels in from the boundary still counts as touching it.
#:
#: Zero, and that is a definition rather than a tuned value: ink in the
#: outermost pixel of a crop is ink the crop cut through. It was 2% of the
#: cell width to begin with, until the extents were measured across the
#: offline corpus at a geometry known to be right -- long labels legitimately
#: come within *one* pixel of the edge (CHKLIST, ALERTS, STD BARO all do),
#: while nothing correctly cropped ever reaches the outermost pixel. There is
#: no gap between "close" and "cut" to put a percentage in; there is only the
#: boundary itself.
CLIP_MARGIN = 0


@dataclass(frozen=True)
class CellInk:
    """Everything asked about one cell's ink, from a single pass over it.

    The pipeline asks three questions of every cell it OCRs -- how much ink
    there is, where it reaches, and whether it touches an edge -- and each
    used to start over from the same two operations, a grayscale conversion
    and a median. Three passes for three answers, twelve cells a frame,
    twenty-eight frames a second, on the CPU Tesseract is already competing
    for. Measured, not assumed. On a real 12-cell capture, median of 200 runs:
    0.643 + 0.692 + 0.684 = 2.019 ms per frame separately, against 0.532 ms
    for the shared pass. Repeated offline on a synthetic frame -- a slower
    machine, so the absolute numbers are not comparable, but the same shape:
    0.95 + 1.20 + 1.17 = 3.31 ms against 1.21 ms. At the default loop_hz of
    28 that is CPU handed back to Tesseract, which has no GPU path and is
    competing with X-Plane for it.

    The mask and the ratio are computed on construction because the ratio is
    what every caller starts with. The extent is derived on demand, so a
    blank cell -- discarded on the ratio alone, and the G1000 leaves plenty
    of softkeys blank -- does not pay for an answer nobody reads.
    """

    #: Which pixels deviate from the cell's dominant tone.
    #:
    #: Note what this cannot see: a cell of one uniform tone has no deviation
    #: from itself, so a crop that has landed entirely on a solid region reports
    #: no ink at all rather than reporting a problem.
    mask: np.ndarray
    #: Fraction of the cell's pixels that are ink.
    #:
    #: Works for both a normal (light text on near-black) and a highlighted
    #: (dark text on a light box) cell, unlike a plain "count bright pixels".
    ratio: float

    @cached_property
    def _columns(self) -> np.ndarray:
        return np.flatnonzero(self.mask.any(axis=0))

    @cached_property
    def _rows(self) -> np.ndarray:
        return np.flatnonzero(self.mask.any(axis=1))

    @property
    def extent(self) -> tuple[float, float, float, float] | None:
        """Where the ink reaches, as fractions of the cell: (left, right, top, bottom).

        None when the cell holds no ink at all, which is a different thing from
        ink at the edges and must not be confused with it -- an empty softkey is
        not a clipped one.
        """
        if self._columns.size == 0 or self._rows.size == 0:
            return None
        width = max(1, self.mask.shape[1] - 1)
        height = max(1, self.mask.shape[0] - 1)
        return (
            float(self._columns[0]) / width, float(self._columns[-1]) / width,
            float(self._rows[0]) / height, float(self._rows[-1]) / height,
        )

    @property
    def bounds(self) -> tuple[float, float]:
        """Horizontal extent of the ink, as fractions of the cell width.

        A glyph centred in its cell reports something like (0.3, 0.7). Ink hard
        against 0.0 or 1.0 means the crop is cutting the label off, which starves
        Tesseract of the shape it needs -- a half "0" is not a character, and comes
        back as an empty string rather than a wrong one.
        """
        extent = self.extent
        return (0.0, 0.0) if extent is None else (extent[0], extent[1])

    def clipped_edges(self, margin: int = CLIP_MARGIN) -> tuple[str, ...]:
        """Which edges of the crop have ink in their outermost pixels, if any.

        Vertical as well as horizontal, because ``cell_pad_y`` can cut the tops
        off capitals just as easily as ``cell_pad_x`` can cut the ends off a word,
        and a caller looking only sideways would pass a crop that loses a row of
        every glyph.

        This is a hint and not a measurement of correctness. It has both kinds of
        error: a label drawn hard against the edge of its own cell reports a
        clipping that is really the sim's layout, and a crop that has slipped
        wholesale onto a separator bar or a solid background reports nothing at
        all. It says "look at this one", which is worth having and is not the same
        as saying the calibration is wrong.
        """
        if self._columns.size == 0 or self._rows.size == 0:
            return ()
        height, width = self.mask.shape[:2]
        edges = []
        if self._columns[0] <= margin:
            edges.append("left")
        if self._columns[-1] >= width - 1 - margin:
            edges.append("right")
        if self._rows[0] <= margin:
            edges.append("top")
        if self._rows[-1] >= height - 1 - margin:
            edges.append("bottom")
        return tuple(edges)


def measure_ink(cell: np.ndarray, contrast: int = 40) -> CellInk:
    """One grayscale conversion and one median, for all of :class:`CellInk`.

    Prefer this to the single-question helpers below whenever more than one
    of the answers is wanted from the same cell.
    """
    gray = to_gray(cell)
    dominant = np.median(gray)
    mask = np.abs(gray.astype(np.int16) - dominant) > contrast
    return CellInk(mask=mask, ratio=float(np.mean(mask)))


# The single-question forms. Each is one measure_ink away from the object
# above and is kept for callers that genuinely want one answer -- asking for
# two of them about the same cell measures it twice.


def ink_ratio(cell: np.ndarray, contrast: int = 40) -> float:
    """Fraction of pixels that deviate from the cell's dominant brightness."""
    return measure_ink(cell, contrast).ratio


def ink_mask(cell: np.ndarray, contrast: int = 40) -> np.ndarray:
    """Which pixels deviate from the cell's dominant tone."""
    return measure_ink(cell, contrast).mask


def ink_extent(cell: np.ndarray, contrast: int = 40) -> tuple[float, float, float, float] | None:
    """Where the ink reaches, as fractions of the cell, or None if there is none."""
    return measure_ink(cell, contrast).extent


def ink_bounds(cell: np.ndarray, contrast: int = 40) -> tuple[float, float]:
    """Horizontal extent of the ink, as fractions of the cell width."""
    return measure_ink(cell, contrast).bounds


def clipped_edges(
    cell: np.ndarray, contrast: int = 40, margin: int = CLIP_MARGIN
) -> tuple[str, ...]:
    """Which edges of the crop have ink in their outermost pixels, if any."""
    return measure_ink(cell, contrast).clipped_edges(margin)


def is_blank(cell: np.ndarray, min_ink_ratio: float = 0.004, contrast: int = 40) -> bool:
    return measure_ink(cell, contrast).ratio < min_ink_ratio


def sharpen(gray: np.ndarray, amount: float, radius: float) -> np.ndarray:
    """Unsharp mask, applied at native resolution before any upscaling.

    The softkey glyphs are only ~10 px tall and arrive slightly soft from the
    capture. Blurring at that size closes the counters of 0, 6, 8 and 9, and a
    filled counter is not a character at all -- Tesseract returns an empty
    string rather than a wrong digit, which is exactly how the bug shows up.
    Restoring the edges before thresholding keeps the holes open.
    """
    if amount <= 0:
        return gray
    blurred = cv2.GaussianBlur(gray, (0, 0), radius)
    return cv2.addWeighted(gray, 1.0 + amount, blurred, -amount, 0)


#: How much of a cell counts as its border ring when deciding polarity.
#:
#: The colour stage's ``ring_fraction``, deliberately the same number rather
#: than one of its own: the two stages ask the same question -- what is this
#: cell's background -- of the same pixels, and a second value for one
#: affordance is a value that drifts. Sourced from the dataclass default so
#: there is one place to change it.
RING_FRACTION: float = ColorConfig.ring_fraction


#: The polarities :func:`preprocess_cell` will finish a thresholded cell in.
#:
#: ``"auto"`` is :func:`_background_is_white`'s answer and is what the first
#: pass of the ladder uses; ``"opposite"`` is the other one. There is
#: deliberately no way to name a polarity absolutely: the caller that needs
#: the second option needs it *because* the first was wrong, and "the other
#: one" is the only description of it that cannot itself be wrong.
POLARITIES = ("auto", "opposite")


def threshold_cell(
    cell: np.ndarray,
    upscale: float = 4.0,
    method: str = "otsu",
    sharpen_amount: float = 1.2,
    sharpen_radius: float = 1.4,
) -> np.ndarray:
    """Sharpen, upscale and binarise one cell, without deciding its polarity.

    Split out from :func:`preprocess_cell` because this is the expensive half
    -- an unsharp mask, a 4x resize and a threshold -- and both polarities of
    a given rung share it exactly. The ladder thresholds once per rung and
    finishes the result twice.

    Thresholding is done *per cell* rather than once for the whole strip
    because the selected softkey is drawn with a bright highlight box behind
    it; a single global threshold either loses that cell or blows out the
    others.
    """
    gray = to_gray(cell)
    gray = sharpen(gray, sharpen_amount, sharpen_radius)
    if upscale and abs(upscale - 1.0) > 1e-6:
        gray = cv2.resize(gray, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)

    if method == "adaptive":
        block = max(3, (min(gray.shape) // 2) | 1)
        return cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, 5
        )
    if method == "otsu":
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return binary
    raise ValueError(f"unknown threshold method {method!r} (use 'otsu' or 'adaptive')")


def finish_cell(
    binary: np.ndarray,
    polarity: str = "auto",
    border: int = 8,
    ring_fraction: float = RING_FRACTION,
    light_background: bool | None = None,
) -> np.ndarray:
    """Normalise a thresholded cell to dark glyphs on light paper.

    ``polarity`` selects between :func:`_background_is_white`'s answer and the
    other one; see :data:`POLARITIES`. Cheap on purpose -- a ``bitwise_not``,
    a crop and a border -- so that trying both costs a fraction of a rung
    rather than a whole one.

    ``light_background`` is the polarity, already measured from the *raw*
    cell -- normally :func:`color.background_is_light`, which the pipeline has
    computed for every cell of every frame anyway. Pass it. The fallback when
    it is ``None`` reads the binarised ring instead, and that reading degrades
    as the crop tightens in a way the raw one does not; it is here for callers
    that have no colour configuration, not as an equal option.

    :func:`_crop_to_content` runs *after* the polarity is settled, which is
    where it has always run and where it is safe. Moving it earlier, so that
    the fallback ring reading would see the label's own background rather than
    the surround around a highlight box, looked harmless and was not: its
    guard -- four or more rows and columns more than half bright -- is not
    specific to a box. A full-width word at a tight vertical crop satisfies it
    too, and a hit was then being read as "this cell has a light background",
    overriding the classifier that had just said otherwise. A cell whose
    polarity was measured correctly came out the wrong way up anyway. The
    shape of a crop is not evidence about polarity when a real measurement is
    in hand, and it is only used as a hint on the fallback path where there is
    nothing better.
    """
    if polarity not in POLARITIES:
        raise ValueError(f"unknown polarity {polarity!r} (use one of {POLARITIES})")

    if light_background is not None:
        light = light_background
    else:
        # No classification to go on. Read the binarised ring instead, and
        # crop any dark frame away first so the ring is the label's own
        # background rather than the surround around a highlight box; a crop
        # that fires here is itself weak evidence of such a box. Weak evidence
        # is worth having only when there is no strong evidence, which is why
        # this whole branch sits under the `is None`.
        probe = _crop_to_content(binary)
        light = (
            True if probe.shape != binary.shape
            else _background_is_white(probe, ring_fraction)
        )

    flip = not light
    if polarity == "opposite":
        flip = not flip
    if flip:
        # The bright class is the text, not the paper -> flip so that
        # Tesseract gets dark glyphs on a light background.
        binary = cv2.bitwise_not(binary)

    # Now that the cell is dark-on-light, a dark frame around a highlight box
    # is the only thing left that looks like one, and a plain word's border is
    # white so this returns it untouched.
    binary = _crop_to_content(binary)

    if border > 0:
        binary = cv2.copyMakeBorder(
            binary, border, border, border, border, cv2.BORDER_CONSTANT, value=255
        )
    return binary


def preprocess_cell(
    cell: np.ndarray,
    upscale: float = 4.0,
    method: str = "otsu",
    border: int = 8,
    sharpen_amount: float = 1.2,
    sharpen_radius: float = 1.4,
    polarity: str = "auto",
    color_config: "ColorConfig | None" = None,
) -> np.ndarray:
    """Return a binarised, OCR-ready cell: black text on a white background.

    ``color_config`` is what lets the polarity be measured on the raw cell
    rather than on the thresholded one; without it this falls back to the
    binarised ring. See :func:`finish_cell`.
    """
    binary = threshold_cell(cell, upscale, method, sharpen_amount, sharpen_radius)
    light = None
    if color_config is not None:
        from .color import background_is_light

        light = background_is_light(cell, color_config)
    return finish_cell(binary, polarity, border, light_background=light)


def ring_bright_fraction(binary: np.ndarray, ring_fraction: float = RING_FRACTION) -> float:
    """What fraction of the cell's border ring is the bright class.

    The number :func:`_background_is_white` decides on, exposed so that a
    human can look at it: this is a threshold on a measured quantity, and
    every other one in this project has a diagnostic that prints the real
    value from a real capture. ``dump-cells`` prints this one.
    """
    ring = binary[border_ring_mask(binary.shape, ring_fraction)]
    if ring.size == 0:
        return float(np.mean(binary > 0)) if binary.size else 1.0
    return float(np.mean(ring > 0))


#: How bright a cell's ring has to be before it counts as a light background.
#:
#: Not the midpoint, and that asymmetry is the point. Light-on-dark is the
#: rule and a light background is the exception -- a selected key, a caution,
#: a warning -- so the exception is what has to prove itself, and an ambiguous
#: ring means black rather than a coin toss. ``color.py`` already works this
#: way for the same pixels: its ``value_max`` is tested first "so brightness
#: can only ever push a cell into black".
#:
#: Where the number sits, and the limit past which no number works.
#:
#: On a roomy crop the two populations are nowhere near each other: over the
#: 58 non-blank cells of the offline corpus, light-on-dark rings run 0.00-0.08
#: and light-background rings 0.91-1.00, so anything from about 0.1 to 0.9
#: separates them equally well and the value is chosen for which way it fails.
#:
#: On a tight one they close. A ring band is only clean while the glyph stays
#: out of it, and nothing keeps it out: a tall glyph reaches the top and
#: bottom bands, a full-width word reaches the left and right ones, and the
#: contamination is one-directional -- it lifts a dark cell's fraction and
#: lowers a light cell's. Rendered words at one geometry, varying only how
#: much of the cell height the glyph fills:
#:
#:     glyph fills 55% of the cell    dark <= 0.09    light >= 0.91
#:     glyph fills 80%                dark <= 0.77    light >= 0.84
#:     glyph fills 90%                dark <= 0.67    light >= 0.71
#:
#: At 90% they overlap once the light side's own contamination is counted, and
#: judging each edge band separately and taking the worst does not rescue it
#: (dark <= 0.59 against light >= 0.56 -- worse, because the light side loses
#: more). There is no threshold there, on any band or combination of them.
#:
#: So this bar is not a fix for a crop that tight, and must not be tuned as
#: though it were. What it does is decide which way the answer falls while it
#: is still ambiguous, and it falls to black because black is the rule and a
#: light background -- a selected key, a caution, a warning -- is the
#: exception. Past the limit the ring stops being the answer and becomes only
#: the polarity tried *first*; what settles it there is the vocabulary, via
#: the opposite-polarity rung and ``SoftkeyReader._rank``.
LIGHT_BACKGROUND_RING = 0.75


def _background_is_white(binary: np.ndarray, ring_fraction: float = RING_FRACTION) -> bool:
    """Is the bright class the background rather than the glyphs?

    Decided on the cell's **border ring**, for the reason ``color.py`` sets
    out at length and had already worked out for the colour stage: labels are
    centred, so the outermost few pixels of a cell are essentially never
    glyph, whatever the cell is doing. The ring is therefore background by
    construction, and a majority test over it needs no assumption at all.

    It used to be decided on the *centre* of the cell instead, on the
    assumption that the glyphs are the minority there. That is a real
    assumption and it fails, which cost this project a bug report: what
    crosses the line is the ink coverage of the middle band, which depends on
    how tight the crop is *and on which letters the word is made of*. TERRAIN
    and NEXRAD came out of preprocessing still white-on-black off a live MFD
    while DCLTR-1, a longer word in the same cells at the same geometry, read
    perfectly -- because D, C, L, T, R, hyphen and 1 are thin open shapes and
    E, R, A, N, X and D are not. There was never a crop tight enough to
    predict from, and the fix was not a better threshold on that quantity but
    a different quantity. ``color.py``'s own docstring named this failure
    ("the assumption ``preprocess_cell()`` makes") before anything hit it;
    the ring measurement was simply never wired through to here.

    The ring is still only a first choice, not a proof: ``POLARITIES`` gives
    the ladder the other one to fall back to. Do not put the centre test back.
    """
    return ring_bright_fraction(binary, ring_fraction) > LIGHT_BACKGROUND_RING


def _crop_to_content(binary: np.ndarray, margin: int = 2) -> np.ndarray:
    """Drop a dark frame around a highlight box.

    When the selected softkey's highlight box is smaller than the cell, the
    normalised cell is a white box with black text sitting inside a black
    surround. Tesseract reads that far better once the surround is cut away.
    Cells without such a frame are returned untouched.
    """
    white = binary > 127
    border = np.concatenate([
        white[:margin, :].ravel(), white[-margin:, :].ravel(),
        white[:, :margin].ravel(), white[:, -margin:].ravel(),
    ])
    if border.mean() > 0.5:  # no dark frame
        return binary
    rows = np.flatnonzero(white.mean(axis=1) > 0.5)
    cols = np.flatnonzero(white.mean(axis=0) > 0.5)
    if rows.size < 4 or cols.size < 4:
        return binary
    return binary[rows[0]: rows[-1] + 1, cols[0]: cols[-1] + 1]


# ---------------------------------------------------------------------------
# Change gating
# ---------------------------------------------------------------------------


def changed_cells(
    previous: list[np.ndarray] | None,
    current: list[np.ndarray],
    tolerance: int = 6,
    min_pixels: int = 4,
) -> list[bool]:
    """Per-cell "has this changed since last frame" mask.

    A plain numpy comparison used as a cache check -- softkeys change rarely,
    so in steady state this skips all 12 OCR calls.
    """
    if previous is None or len(previous) != len(current):
        return [True] * len(current)
    flags: list[bool] = []
    for prev, cur in zip(previous, current):
        if prev.shape != cur.shape:
            flags.append(True)
            continue
        diff = np.abs(prev.astype(np.int16) - cur.astype(np.int16))
        flags.append(bool(np.count_nonzero(diff > tolerance) > min_pixels))
    return flags


def snapshot_cells(cells: list[np.ndarray]) -> list[np.ndarray]:
    """Grayscale copies of the cells, kept for the next frame's comparison."""
    return [to_gray(cell).copy() for cell in cells]


# ---------------------------------------------------------------------------
# Coarse auto-detection (seeds the fractional geometry for calibration)
# ---------------------------------------------------------------------------


def auto_detect_strip(
    frame: np.ndarray,
    cells: int = 12,
    search_fraction: float = 0.35,
    dark_level: int = 70,
    bright_level: int = 130,
) -> StripGeometry | None:
    """Find the dark softkey band at the bottom of the frame.

    Deliberately coarse: it produces a *starting point* for calibration, not
    a final answer. Returns None when no plausible band is found.
    """
    gray = to_gray(frame)
    height, width = gray.shape
    top = int(height * (1.0 - search_fraction))
    region = gray[top:]
    if region.size == 0:
        return None

    # Rows that cut through a lot of glyphs can rise above `dark_level` and
    # split the band in two, so small gaps are closed before the run search.
    dark_rows = region.mean(axis=1) < dark_level
    dark_rows = _close_gaps(dark_rows, max_gap=max(3, int(round(height * 0.02))))
    run = _longest_run(dark_rows)
    if run is None:
        LOG.debug("auto-detect: no dark band in the bottom %.0f%%", search_fraction * 100)
        return None
    r0, r1 = run  # inclusive/exclusive within `region`

    band = region[r0:r1]
    bright = band > bright_level
    rows_with_text = np.flatnonzero(bright.sum(axis=1) > 0)
    cols_with_text = np.flatnonzero(bright.sum(axis=0) > 0)
    if rows_with_text.size == 0 or cols_with_text.size == 0:
        LOG.debug("auto-detect: dark band found but it holds no bright glyphs")
        return None

    text_h = rows_with_text[-1] - rows_with_text[0] + 1
    pad_y = max(2, int(round(text_h * 0.30)))
    # Clamp to the dark run: padding must not spill into whatever is drawn
    # above the strip, or blank cells pick up terrain pixels and OCR noise.
    y0 = max(r0, r0 + int(rows_with_text[0]) - pad_y)
    y1 = min(r1, r0 + int(rows_with_text[-1]) + 1 + pad_y)

    # Horizontal: labels only cover the middle of each cell, so widen by
    # roughly half a cell on each side.
    span = int(cols_with_text[-1] - cols_with_text[0] + 1)
    pad_x = max(2, int(round(span / cells * 0.5)))
    x0 = max(0, int(cols_with_text[0]) - pad_x)
    x1 = min(width, int(cols_with_text[-1]) + 1 + pad_x)

    geom = StripGeometry(
        x=x0 / width,
        y=(top + y0) / height,
        w=(x1 - x0) / width,
        h=(y1 - y0) / height,
        cells=cells,
    )
    geom.validate()
    return geom


def _close_gaps(mask: np.ndarray, max_gap: int) -> np.ndarray:
    """Fill runs of False shorter than ``max_gap`` that sit between Trues."""
    out = mask.copy()
    start: int | None = None
    for i, value in enumerate(mask):
        if not value and start is None:
            start = i
        elif value and start is not None:
            if start > 0 and i - start <= max_gap:
                out[start:i] = True
            start = None
    return out


def _longest_run(mask: np.ndarray) -> tuple[int, int] | None:
    """Longest run of True in a 1-D boolean mask, as [start, end)."""
    best: tuple[int, int] | None = None
    start: int | None = None
    for i, value in enumerate(mask):
        if value and start is None:
            start = i
        elif not value and start is not None:
            if best is None or i - start > best[1] - best[0]:
                best = (start, i)
            start = None
    if start is not None:
        end = int(mask.size)
        if best is None or end - start > best[1] - best[0]:
            best = (start, end)
    return best


def overlay_geometry(frame: np.ndarray, geom: StripGeometry) -> np.ndarray:
    """Copy of the frame with the strip crop and cell boundaries drawn on."""
    canvas = frame.copy()
    if canvas.ndim == 2:
        canvas = cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR)
    rect = strip_rect(frame.shape, geom)
    cv2.rectangle(canvas, (rect.x, rect.y), (rect.x + rect.w, rect.y + rect.h), (0, 0, 255), 1)
    for i, cell in enumerate(cell_rects(frame.shape, geom)):
        cv2.rectangle(canvas, (cell.x, cell.y), (cell.x + cell.w, cell.y + cell.h), (0, 255, 0), 1)
        cv2.putText(
            canvas, str(i + 1), (cell.x + 2, max(10, cell.y - 3)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 255, 255), 1, cv2.LINE_AA,
        )
    return canvas
