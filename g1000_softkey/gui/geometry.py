"""Mapping between the picture on screen, the frame's pixels, and the config.

The calibration editor has three coordinate systems in play at once:

* **fractions** -- what the config stores, so that a resized pop-out does not
  invalidate the calibration;
* **frame pixels** -- what the capture actually contains, and the unit the
  user is really thinking in when they say "a pixel or two inside the edge";
* **canvas pixels** -- where the mouse is, on an image that has been scaled to
  fit a window and may be showing only part of the frame.

All the conversion lives here, with no Tk, because getting it wrong is
silent: the boxes would still be drawn, just not where the reader will
actually look. Every function is total -- clamped rather than raising -- since
these run on mouse motion and a dialog box per stray drag would be unusable.

What this module deliberately does **not** do is work out where the cells are.
That is ``strip.cell_rects``, the same function ``split_cells`` slices with,
and the canvas draws exactly what it returns. A second implementation here
would be a calibration editor that can disagree with the thing being
calibrated.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..config import StripGeometry

#: Smallest strip the editor will let you draw or nudge down to, as a
#: fraction of the frame. Not zero: a strip of no width crops to nothing, the
#: cell rectangles collapse, and the picture stops explaining what went wrong.
MIN_SPAN = 0.01

#: Padding is a fraction of one cell taken off *each* side, so half a cell is
#: the point where nothing is left. Stop short of it for the same reason.
MAX_PAD = 0.45


@dataclass(frozen=True)
class View:
    """One displayed region of the frame, and where it landed on the canvas.

    ``source_*`` is the part of the frame being shown -- the whole thing for
    the main picture, the strip and a margin for the close-up.
    """

    source_x: int
    source_y: int
    source_w: int
    source_h: int
    scale: float
    offset_x: float
    offset_y: float

    def to_canvas(self, frame_x: float, frame_y: float) -> tuple[float, float]:
        return (
            self.offset_x + (frame_x - self.source_x) * self.scale,
            self.offset_y + (frame_y - self.source_y) * self.scale,
        )

    def to_frame(self, canvas_x: float, canvas_y: float) -> tuple[float, float]:
        return (
            self.source_x + (canvas_x - self.offset_x) / self.scale,
            self.source_y + (canvas_y - self.offset_y) / self.scale,
        )

    def rect_to_canvas(self, x: float, y: float, w: float, h: float) -> tuple[float, float, float, float]:
        """A frame-pixel rectangle as canvas (x0, y0, x1, y1)."""
        x0, y0 = self.to_canvas(x, y)
        x1, y1 = self.to_canvas(x + w, y + h)
        return (x0, y0, x1, y1)


def fit_view(
    source: tuple[int, int, int, int],
    canvas_w: int,
    canvas_h: int,
    max_scale: float = 64.0,
) -> View:
    """Fit a frame region into a canvas, centred, preserving aspect ratio."""
    sx, sy, sw, sh = source
    sw = max(1, int(sw))
    sh = max(1, int(sh))
    canvas_w = max(1, int(canvas_w))
    canvas_h = max(1, int(canvas_h))
    scale = min(canvas_w / sw, canvas_h / sh, max_scale)
    scale = max(scale, 1e-6)
    return View(
        source_x=int(sx), source_y=int(sy), source_w=sw, source_h=sh, scale=scale,
        offset_x=(canvas_w - sw * scale) / 2.0,
        offset_y=(canvas_h - sh * scale) / 2.0,
    )


# ---------------------------------------------------------------------------
# fractions <-> pixels
# ---------------------------------------------------------------------------


def clamp(geometry: StripGeometry) -> StripGeometry:
    """The nearest geometry ``StripGeometry.validate`` will accept.

    Clamped rather than rejected because this runs on every mouse move: a drag
    that wanders off the edge of the picture should stop at the edge, not
    raise. The invariants are the same ones ``config.py`` enforces when the
    daemon loads the file, so anything this returns can be saved.
    """
    x = _between(geometry.x, 0.0, 1.0 - MIN_SPAN)
    y = _between(geometry.y, 0.0, 1.0 - MIN_SPAN)
    w = _between(geometry.w, MIN_SPAN, 1.0 - x)
    h = _between(geometry.h, MIN_SPAN, 1.0 - y)
    return replace(
        geometry,
        x=x, y=y, w=w, h=h,
        cell_pad_x=_between(geometry.cell_pad_x, 0.0, MAX_PAD),
        cell_pad_y=_between(geometry.cell_pad_y, 0.0, MAX_PAD),
        cells=max(1, geometry.cells),
    )


def _between(value: float, low: float, high: float) -> float:
    if high < low:
        return low
    return low if value < low else high if value > high else value


def strip_pixels(geometry: StripGeometry, frame_w: int, frame_h: int) -> tuple[int, int, int, int]:
    """The strip as frame pixels: (x, y, w, h).

    Rounded the same way ``strip.strip_rect`` rounds it, so the editor's red
    box lands on the pixels the crop will actually take.
    """
    x = int(round(geometry.x * frame_w))
    y = int(round(geometry.y * frame_h))
    w = max(1, int(round(geometry.w * frame_w)))
    h = max(1, int(round(geometry.h * frame_h)))
    return (x, y, w, h)


def geometry_from_pixels(
    template: StripGeometry,
    x0: float, y0: float, x1: float, y1: float,
    frame_w: int, frame_h: int,
) -> StripGeometry:
    """A geometry from a rectangle dragged out in frame pixels.

    Corners in any order -- a box is usually drawn from whichever corner the
    hand started at, and demanding top-left first would just be a rule to
    trip over.
    """
    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    frame_w = max(1, frame_w)
    frame_h = max(1, frame_h)
    return clamp(replace(
        template,
        x=left / frame_w,
        y=top / frame_h,
        w=max(1.0, right - left) / frame_w,
        h=max(1.0, bottom - top) / frame_h,
    ))


# ---------------------------------------------------------------------------
# nudging
# ---------------------------------------------------------------------------

#: Which fraction each nudgeable edge moves, and whether moving that edge
#: should hold the opposite one still. Dragging the right edge must not drag
#: the left one with it, which is what changing `w` alone achieves -- but
#: moving the *left* edge means changing `x`, and `w` has to absorb it or the
#: box slides instead of resizing.
EDGES = {
    "left": ("x", True),
    "top": ("y", True),
    "right": ("w", False),
    "bottom": ("h", False),
}


def nudge_edge(
    geometry: StripGeometry, edge: str, pixels: float, frame_w: int, frame_h: int
) -> StripGeometry:
    """Move one edge by a number of *frame pixels*, keeping the others still.

    Pixels rather than fractions because that is the unit the judgement is
    made in -- "just inside the edge of the strip" is a statement about
    pixels, and asking someone to convert it into a change in the fourth
    decimal place of a fraction is asking them to do arithmetic to press a
    button.
    """
    if edge not in EDGES:
        raise KeyError(f"no edge {edge!r} (have {', '.join(sorted(EDGES))})")
    field, anchored = EDGES[edge]
    span = frame_w if field in ("x", "w") else frame_h
    delta = pixels / max(1, span)
    if not anchored:
        return clamp(replace(geometry, **{field: getattr(geometry, field) + delta}))
    # Moving the left or top edge: the opposite edge stays where it is, so the
    # span shrinks by exactly what the origin gained.
    origin = getattr(geometry, field) + delta
    span_field = "w" if field == "x" else "h"
    return clamp(replace(geometry, **{
        field: origin,
        span_field: getattr(geometry, span_field) - delta,
    }))


def nudge_padding(geometry: StripGeometry, axis: str, steps: float, step: float = 0.01) -> StripGeometry:
    """Move a padding fraction. ``axis`` is "x" or "y"."""
    field = {"x": "cell_pad_x", "y": "cell_pad_y"}[axis]
    return clamp(replace(geometry, **{field: getattr(geometry, field) + steps * step}))


def move(geometry: StripGeometry, dx_pixels: float, dy_pixels: float,
         frame_w: int, frame_h: int) -> StripGeometry:
    """Slide the whole strip without resizing it."""
    return clamp(replace(
        geometry,
        x=geometry.x + dx_pixels / max(1, frame_w),
        y=geometry.y + dy_pixels / max(1, frame_h),
    ))


# ---------------------------------------------------------------------------
# resize handles
# ---------------------------------------------------------------------------

#: The eight handles, as (horizontal, vertical) anchors. "" means that axis is
#: not affected -- a side handle only moves one edge.
HANDLES: dict[str, tuple[str, str]] = {
    "nw": ("left", "top"),
    "n": ("", "top"),
    "ne": ("right", "top"),
    "e": ("right", ""),
    "se": ("right", "bottom"),
    "s": ("", "bottom"),
    "sw": ("left", "bottom"),
    "w": ("left", ""),
}


def handle_points(x: float, y: float, w: float, h: float) -> dict[str, tuple[float, float]]:
    """Where each handle sits on a rectangle, in whatever units it is given."""
    mid_x, mid_y = x + w / 2.0, y + h / 2.0
    right, bottom = x + w, y + h
    return {
        "nw": (x, y), "n": (mid_x, y), "ne": (right, y), "e": (right, mid_y),
        "se": (right, bottom), "s": (mid_x, bottom), "sw": (x, bottom), "w": (x, mid_y),
    }


def handle_at(
    canvas_x: float, canvas_y: float,
    rect: tuple[float, float, float, float],
    tolerance: float = 7.0,
) -> str | None:
    """Which handle the pointer is on, if any. ``rect`` is (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = rect
    points = handle_points(x0, y0, x1 - x0, y1 - y0)
    best, best_distance = None, tolerance
    for name, (px, py) in points.items():
        distance = max(abs(px - canvas_x), abs(py - canvas_y))
        if distance <= best_distance:
            best, best_distance = name, distance
    return best


def drag_handle(
    geometry: StripGeometry, handle: str,
    frame_x: float, frame_y: float,
    frame_w: int, frame_h: int,
) -> StripGeometry:
    """Put ``handle`` at a frame-pixel position, leaving the other edges alone."""
    horizontal, vertical = HANDLES[handle]
    x, y, w, h = strip_pixels(geometry, frame_w, frame_h)
    left, top, right, bottom = x, y, x + w, y + h
    if horizontal == "left":
        left = min(frame_x, right - 1)
    elif horizontal == "right":
        right = max(frame_x, left + 1)
    if vertical == "top":
        top = min(frame_y, bottom - 1)
    elif vertical == "bottom":
        bottom = max(frame_y, top + 1)
    return geometry_from_pixels(geometry, left, top, right, bottom, frame_w, frame_h)


def inside(canvas_x: float, canvas_y: float, rect: tuple[float, float, float, float]) -> bool:
    x0, y0, x1, y1 = rect
    return x0 <= canvas_x <= x1 and y0 <= canvas_y <= y1
