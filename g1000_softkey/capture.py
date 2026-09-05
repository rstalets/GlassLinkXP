"""Frame sources.

Two backends behind one tiny interface:

* :class:`WgcCapture`  -- Windows Graphics Capture (production). Imported
  lazily so this module imports fine on Linux/macOS.
* :class:`ImageCapture` -- a PNG file or a directory of PNGs (offline dev,
  tests, benchmarking).

Frames are ``numpy.uint8`` arrays shaped ``(H, W, 3)`` in **BGR** order
(OpenCV convention). Everything downstream of this module is platform
independent.
"""

from __future__ import annotations

import ctypes
import logging
import platform
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

import numpy as np

LOG = logging.getLogger(__name__)

Frame = np.ndarray


class CaptureError(Exception):
    """Capture could not be set up (bad platform, missing dep, no window)."""


class FrameSource(Protocol):
    """Anything that can hand out the most recent frame of a display."""

    name: str

    def grab(self) -> Frame | None:
        """Return the newest frame, or None if none has arrived yet."""

    def close(self) -> None:
        ...


# ---------------------------------------------------------------------------
# Offline backend
# ---------------------------------------------------------------------------


class ImageCapture:
    """Serve frames from a PNG file or a directory of PNGs.

    A directory is cycled in sorted order, one image per :meth:`grab`, which
    makes it easy to replay a sequence of softkey menus offline.
    """

    def __init__(self, path: str | Path, loop: bool = True) -> None:
        import cv2  # local import keeps module import cheap

        self._cv2 = cv2
        self.path = Path(path)
        self.loop = loop
        self.name = f"image:{self.path}"
        if self.path.is_dir():
            self._files = sorted(
                p for p in self.path.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"}
            )
            if not self._files:
                raise CaptureError(f"no images found in {self.path}")
        elif self.path.is_file():
            self._files = [self.path]
        else:
            raise CaptureError(f"image path does not exist: {self.path}")
        self._index = 0

    def grab(self) -> Frame | None:
        if self._index >= len(self._files):
            if not self.loop:
                return None
            self._index = 0
        target = self._files[self._index]
        self._index += 1
        frame = self._cv2.imread(str(target), self._cv2.IMREAD_COLOR)
        if frame is None:
            raise CaptureError(f"could not decode image: {target}")
        return frame

    def close(self) -> None:  # nothing to release
        return None


# ---------------------------------------------------------------------------
# Windows backend
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    width: int
    height: int
    pid: int

    def __str__(self) -> str:
        return (
            f"hwnd=0x{self.hwnd:08X} pid={self.pid:<6} {self.width}x{self.height} "
            f"class={self.class_name!r} title={self.title!r}"
        )


def is_windows() -> bool:
    return sys.platform == "win32"


def list_windows(visible_only: bool = True) -> list[WindowInfo]:
    """Enumerate top-level windows so the user can find the pop-out titles.

    Pure ctypes/user32 -- no pywin32 needed. Windows only.
    """
    if not is_windows():
        raise CaptureError(
            f"list-windows only works on Windows (running on {platform.system()}). "
            "Use --image to feed the pipeline from PNG files instead."
        )

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    enum_proc = ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
        ctypes.c_bool, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
    )
    results: list[WindowInfo] = []

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    def callback(hwnd, _lparam):  # pragma: no cover - Windows only
        if visible_only and not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        rect = RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        results.append(
            WindowInfo(
                hwnd=int(hwnd),
                title=buf.value,
                class_name=cls.value,
                width=rect.right - rect.left,
                height=rect.bottom - rect.top,
                pid=int(pid.value),
            )
        )
        return True

    user32.EnumWindows(enum_proc(callback), None)  # pragma: no cover
    return results  # pragma: no cover


def find_window(title_substring: str) -> WindowInfo:  # pragma: no cover - Windows only
    """First visible window whose title contains ``title_substring`` (ci)."""
    needle = title_substring.casefold()
    matches = [w for w in list_windows() if needle in w.title.casefold()]
    if not matches:
        raise CaptureError(
            f"no visible window title contains {title_substring!r}. "
            "Run 'python -m g1000_softkey.main list-windows' to see what is open, and check "
            "that the G1000 display is popped out into its own window."
        )
    if len(matches) > 1:
        LOG.warning(
            "%d windows match %r, using %r", len(matches), title_substring, matches[0].title
        )
    return matches[0]


def resize_window(hwnd: int, width: int, height: int) -> tuple[int, int]:
    """Resize a window so its *client area* is width x height.

    The client area is what gets captured, and it is smaller than the window by
    the title bar and borders, so the outer size is the target plus whatever
    that frame costs -- measured rather than assumed, since it varies with DPI
    and theme.

    Returns the client size actually achieved. X-Plane enforces a minimum on
    pop-out windows, so a request below that comes back larger than asked.
    """
    if not is_windows():  # pragma: no cover - Windows only
        raise CaptureError("resizing windows requires Windows")

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    window_rect, client_rect = RECT(), RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(window_rect)):
        raise CaptureError(f"GetWindowRect failed for hwnd 0x{hwnd:08X}")
    if not user32.GetClientRect(hwnd, ctypes.byref(client_rect)):
        raise CaptureError(f"GetClientRect failed for hwnd 0x{hwnd:08X}")

    frame_w = (window_rect.right - window_rect.left) - client_rect.right
    frame_h = (window_rect.bottom - window_rect.top) - client_rect.bottom

    SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0002, 0x0004, 0x0010
    user32.SetWindowPos(
        hwnd, 0, 0, 0, width + frame_w, height + frame_h,
        SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE,
    )
    user32.GetClientRect(hwnd, ctypes.byref(client_rect))
    return client_rect.right, client_rect.bottom


class WgcCapture:
    """Windows Graphics Capture backend (``windows-capture`` PyPI package).

    WGC keeps delivering frames for windows that are occluded or in the
    background, which BitBlt/PrintWindow do not do for GPU-composited windows
    like X-Plane's. The library is callback driven, so a background thread
    pushes frames into a single-slot buffer that :meth:`grab` reads.
    """

    def __init__(
        self,
        window_title: str,
        cursor_capture: bool = False,
        draw_border: bool = False,
        target_size: tuple[int, int] | None = None,
    ) -> None:
        if not is_windows():
            raise CaptureError(
                f"WGC capture requires Windows (running on {platform.system()}). "
                "Use --image <png-or-dir> for offline runs."
            )
        try:  # imported lazily: the package is Windows-only
            from windows_capture import WindowsCapture  # type: ignore
        except ImportError as exc:  # pragma: no cover - Windows only
            raise CaptureError(
                "the 'windows-capture' package is not installed: pip install windows-capture"
            ) from exc

        self.window_title = window_title
        self.name = f"wgc:{window_title}"
        self._lock = threading.Lock()
        self._frame: Frame | None = None
        self._closed = False
        self._error: str | None = None

        # Resolve the exact title first so we can give a useful error message
        # rather than whatever the Rust layer raises.
        window = find_window(window_title)  # pragma: no cover
        if target_size is not None:  # pragma: no cover - Windows only
            want_w, want_h = target_size
            if (window.width, window.height) != (want_w, want_h):
                LOG.info(
                    "resizing %r from %dx%d to %dx%d",
                    window.title, window.width, window.height, want_w, want_h,
                )
                try:
                    got_w, got_h = resize_window(window.hwnd, want_w, want_h)
                except CaptureError as exc:
                    LOG.warning("could not resize %r: %s", window.title, exc)
                else:
                    if (got_w, got_h) != (want_w, want_h):
                        LOG.warning(
                            "%r settled at %dx%d, not %dx%d -- X-Plane enforces a "
                            "minimum size on pop-out windows",
                            window.title, got_w, got_h, want_w, want_h,
                        )
                    window = find_window(window_title)
        LOG.info("capturing %s", window)  # pragma: no cover

        capture = WindowsCapture(  # pragma: no cover - Windows only
            cursor_capture=cursor_capture,
            draw_border=draw_border,
            monitor_index=None,
            window_name=window.title,
        )

        @capture.event  # pragma: no cover - Windows only
        def on_frame_arrived(frame, capture_control):  # type: ignore[no-untyped-def]
            try:
                buffer = np.asarray(frame.frame_buffer)  # BGRA
                bgr = np.ascontiguousarray(buffer[:, :, :3])
            except Exception as exc:  # noqa: BLE001 - never kill the capture thread
                self._error = f"frame conversion failed: {exc}"
                return
            with self._lock:
                self._frame = bgr
                if self._closed:
                    capture_control.stop()

        @capture.event  # pragma: no cover - Windows only
        def on_closed():  # type: ignore[no-untyped-def]
            LOG.warning("capture target window closed: %s", window_title)
            self._error = "capture window closed"

        self._control = capture.start_free_threaded()  # pragma: no cover

    def grab(self) -> Frame | None:  # pragma: no cover - Windows only
        if self._error:
            LOG.debug("capture backend reported: %s", self._error)
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def close(self) -> None:  # pragma: no cover - Windows only
        with self._lock:
            self._closed = True
        try:
            self._control.stop()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("ignoring error while stopping capture: %s", exc)


def create_source(spec: str) -> FrameSource:
    """Build a frame source from a CLI spec.

    ``wgc:<window title substring>`` or ``image:<path>``.
    """
    kind, _, value = spec.partition(":")
    if kind == "image":
        return ImageCapture(value)
    if kind == "wgc":
        return WgcCapture(value)
    raise CaptureError(f"unknown frame source {spec!r} (expected 'wgc:<title>' or 'image:<path>')")


def sources_for(displays: Iterable, image_path: str | Path | None) -> dict[str, FrameSource]:
    """One frame source per display; ``image_path`` overrides WGC everywhere."""
    sources: dict[str, FrameSource] = {}
    for display in displays:
        if image_path is not None:
            path = Path(image_path)
            per_display = path / f"{display.key}.png"
            sources[display.key] = ImageCapture(per_display if per_display.is_file() else path)
        else:
            sources[display.key] = WgcCapture(
                display.window_title, target_size=getattr(display, "window_size", None)
            )
    return sources
