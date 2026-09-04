"""Synthetic G1000 softkey frames.

There is no X-Plane (and no Windows) in CI or on a dev laptop, so the tests,
the benchmark and the calibration walkthrough all run against generated
frames: a dark PFD-ish background with a softkey strip along the bottom,
uppercase labels, empty cells, and one cell drawn in the highlighted
"selected" state.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .config import StripGeometry

LOG = logging.getLogger(__name__)

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)

BAND_COLOR = (10, 10, 10)
BEZEL_COLOR = (4, 4, 6)
LABEL_COLOR = (240, 240, 240)
CYAN_COLOR = (0, 230, 230)
SELECTED_BG = (200, 200, 200)
SELECTED_FG = (10, 10, 10)

#: A few real softkey menu levels, blanks included.
MENUS: dict[str, list[str]] = {
    "pfd_top": ["INSET", "", "PFD", "OBS", "CDI", "DME",
                "XPDR", "IDENT", "TMR/REF", "NRST", "", "ALERTS"],
    "pfd_menu": ["SYN VIS", "DFLTS", "WIND", "DME", "BRG1", "",
                 "HSI FMT", "BRG2", "", "ALT UNIT", "STD BARO", "BACK"],
    "inset": ["OFF", "DCLTR", "TRAFFIC", "TOPO", "TERRAIN", "STRMSCP",
              "NEXRAD", "XM LTNG", "METAR", "", "WX LGND", "BACK"],
    "xpdr": ["STBY", "ON", "ALT", "GROUND", "VFR", "CODE",
             "IDENT", "", "", "", "", "BACK"],
    "mfd_top": ["ENGINE", "MAP", "DCLTR", "", "TRAFFIC", "TOPO",
                "TERRAIN", "NEXRAD", "METAR", "", "CHKLIST", "SHW CHRT"],
}

#: index of the highlighted (selected) softkey per menu
SELECTED: dict[str, int] = {
    "pfd_top": 3, "pfd_menu": 0, "inset": 2, "xpdr": 2, "mfd_top": 5,
}

#: cells drawn in cyan rather than white
CYAN: dict[str, tuple[int, ...]] = {"pfd_menu": (10,), "inset": (7,)}


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    LOG.warning("no TrueType font found, falling back to the PIL bitmap font")
    return ImageFont.load_default()


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_w: int, start: int) -> ImageFont.FreeTypeFont:
    """Shrink the font until the label fits the cell (real G1000 uses a
    condensed face; we approximate by scaling down)."""
    size = start
    while size > 8:
        font = _load_font(size)
        if draw.textlength(text, font=font) <= max_w:
            return font
        size -= 1
    return _load_font(8)


def render_frame(
    labels: list[str],
    geom: StripGeometry | None = None,
    size: tuple[int, int] = (1280, 800),
    selected: int | None = None,
    cyan_indices: tuple[int, ...] = (),
    noise: float = 2.5,
    seed: int = 0,
    font_size: int = 16,
) -> np.ndarray:
    """Render one frame. Returns a BGR uint8 array (OpenCV convention)."""
    geom = geom or StripGeometry()
    width, height = size
    image = Image.new("RGB", size, BEZEL_COLOR)
    draw = ImageDraw.Draw(image)

    # --- fake PFD content above the strip ---------------------------------
    horizon = int(height * 0.40)
    display_bottom = int(geom.y * height)
    draw.rectangle([0, 0, width, horizon], fill=(52, 92, 150))          # sky
    draw.rectangle([0, horizon, width, display_bottom], fill=(112, 82, 58))  # ground
    draw.line([0, horizon, width, horizon], fill=(235, 235, 235), width=2)
    tape_font = _load_font(18)
    draw.rectangle([40, horizon - 120, 150, horizon + 120], fill=(24, 24, 24))
    draw.rectangle([width - 160, horizon - 120, width - 40, horizon + 120], fill=(24, 24, 24))
    draw.text((60, horizon - 10), "120", font=tape_font, fill=(255, 255, 255))
    draw.text((width - 145, horizon - 10), "5500", font=tape_font, fill=(255, 255, 255))

    # --- softkey strip ----------------------------------------------------
    sx = int(round(geom.x * width))
    sy = int(round(geom.y * height))
    sw = int(round(geom.w * width))
    sh = int(round(geom.h * height))
    draw.rectangle([sx, sy, sx + sw, sy + sh], fill=BAND_COLOR)

    cell_w = sw / geom.cells
    for index, label in enumerate(labels[: geom.cells]):
        x0 = sx + int(round(index * cell_w))
        x1 = sx + int(round((index + 1) * cell_w))
        if index == selected:
            draw.rectangle(
                [x0 + 4, sy + 3, x1 - 4, sy + sh - 3], fill=SELECTED_BG
            )
        if not label:
            continue
        font = _fit_font(draw, label, int(cell_w * 0.78), font_size)
        text_w = draw.textlength(label, font=font)
        bbox = font.getbbox(label)
        text_h = bbox[3] - bbox[1]
        tx = x0 + (x1 - x0 - text_w) / 2
        ty = sy + (sh - text_h) / 2 - bbox[1]
        if index == selected:
            color = SELECTED_FG
        elif index in cyan_indices:
            color = CYAN_COLOR
        else:
            color = LABEL_COLOR
        draw.text((tx, ty), label, font=font, fill=color)

    frame = np.asarray(image, dtype=np.uint8)
    if noise > 0:
        rng = np.random.default_rng(seed)
        frame = np.clip(frame.astype(np.float32) + rng.normal(0, noise, frame.shape), 0, 255)
        frame = frame.astype(np.uint8)
    return frame[:, :, ::-1].copy()  # RGB -> BGR


def render_menu(name: str, **kwargs) -> np.ndarray:
    """Render one of the named menus in :data:`MENUS`."""
    if name not in MENUS:
        raise KeyError(f"unknown menu {name!r}; known: {', '.join(sorted(MENUS))}")
    kwargs.setdefault("selected", SELECTED.get(name))
    kwargs.setdefault("cyan_indices", CYAN.get(name, ()))
    return render_frame(MENUS[name], **kwargs)


def write_menus(out_dir: str | Path, **kwargs) -> list[Path]:
    """Write every menu as ``<out_dir>/<menu>.png``. Returns the paths."""
    import cv2

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in MENUS:
        path = out / f"{name}.png"
        cv2.imwrite(str(path), render_menu(name, **kwargs))
        paths.append(path)
    return paths
