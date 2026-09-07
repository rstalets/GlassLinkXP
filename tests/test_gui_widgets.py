"""ImageView / _load_scaled: the piece that decides whether the Cells tab
shows the true pixels Tesseract received or a smoothed-down guess at them.

Needs a display, and skips without one:
    xvfb-run -a python -m pytest tests/test_gui_widgets.py
"""

from __future__ import annotations

import numpy as np
import pytest

tk = pytest.importorskip("tkinter")

from glasslinkxp.gui.widgets import ImageView, StatusBar, _load_scaled  # noqa: E402
from glasslinkxp.version import NO_VERSION, __version__, display_version  # noqa: E402


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - depends on the machine
        pytest.skip(f"no display available for Tk ({exc})")
    r.withdraw()
    yield r
    r.destroy()


def _write_binary_png(path, width, height):
    import cv2

    image = np.full((height, width), 255, dtype=np.uint8)
    image[height // 3: 2 * height // 3, width // 3: 2 * width // 3] = 0
    cv2.imwrite(str(path), image)
    return image.shape[1], image.shape[0]


# ---------------------------------------------------------------------------
# _load_scaled
# ---------------------------------------------------------------------------


def test_load_scaled_shrinks_a_big_image_into_a_small_box(root, tmp_path):
    path = tmp_path / "big.png"
    w, h = _write_binary_png(path, 320, 152)
    image = _load_scaled(path, 100, 60, allow_shrink=True)
    assert image.width() < w and image.height() < h


def test_load_scaled_with_shrink_disallowed_keeps_native_size(root, tmp_path):
    """This is the whole point of allow_shrink=False: a box far smaller than
    the picture must not come back LANCZOS-smoothed to fit it -- the offline
    corpus's hardest bug (a closed counter filled in during thresholding) was
    only found once someone saw a picture of the preprocessed cell, and a
    blurred one would have hidden it just as well as none at all."""
    path = tmp_path / "big.png"
    w, h = _write_binary_png(path, 320, 152)
    image = _load_scaled(path, 100, 60, allow_shrink=False)
    assert (image.width(), image.height()) == (w, h)


def test_load_scaled_still_upscales_by_whole_numbers_when_shrink_is_disallowed(root, tmp_path):
    """allow_shrink only removes the *shrink* branch; a small picture in a
    generous box is still enlarged the same way as everywhere else in the
    GUI, because that direction never introduces pixels Tesseract did not
    see."""
    path = tmp_path / "small.png"
    w, h = _write_binary_png(path, 20, 10)
    factor = int(min(100 / w, 60 / h))
    image = _load_scaled(path, 100, 60, allow_shrink=False)
    assert image.width() == w * factor and image.height() == h * factor


# ---------------------------------------------------------------------------
# ImageView
# ---------------------------------------------------------------------------


def test_imageview_default_shrinks_to_its_box(root, tmp_path):
    path = tmp_path / "big.png"
    _write_binary_png(path, 320, 152)
    view = ImageView(root)
    view.configure(width=100, height=60)
    view.pack_propagate(False)
    view.pack()
    view.show(path)
    root.update_idletasks()
    view._render()
    assert view._image.width() <= 100


def test_imageview_with_shrink_disallowed_never_blurs_a_cell_smaller_than_its_box(root, tmp_path):
    path = tmp_path / "big.png"
    w, h = _write_binary_png(path, 320, 152)
    view = ImageView(root, allow_shrink=False)
    view.configure(width=100, height=60)
    view.pack_propagate(False)
    view.pack()
    view.show(path)
    root.update_idletasks()
    view._render()
    assert (view._image.width(), view._image.height()) == (w, h)


# ---------------------------------------------------------------------------
# the status bar's corner
# ---------------------------------------------------------------------------

def test_the_status_bar_shows_which_build_this_is(root):
    """A screenshot carries no log, and a screenshot is how the window gets
    reported. Same string the CLI logs, from the same file."""
    bar = StatusBar(root)
    assert bar.version.cget("text") == display_version(__version__)


def test_a_long_status_message_does_not_push_the_version_off(root):
    """The version is packed first for this reason: a widget packed after one
    with expand=True gets what that one left over, which is nothing."""
    bar = StatusBar(root)
    bar.pack(fill="x")
    bar.set("something went wrong: " + "and then some more detail " * 20, "error")
    root.update_idletasks()
    assert bar.version.winfo_reqwidth() > 0
    assert bar.version.winfo_reqwidth() == StatusBar(root).version.winfo_reqwidth()


def test_a_build_with_no_version_file_says_so_in_the_corner(root):
    """`vNO_VERSION` would read like something somebody tagged."""
    bar = StatusBar(root, version=display_version(NO_VERSION))
    assert bar.version.cget("text") == NO_VERSION
