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


class _RECT(ctypes.Structure):
    """Win32 RECT. One definition, used by every user32 call in this module."""

    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


#: The window class every X-Plane window carries -- the main sim view and each
#: pop-out alike. Windows of other applications do not, so this is what
#: separates "the sim's windows" from the rest of the desktop, and it is the
#: default filter for ``list-windows``.
#:
#: Observed on a running X-Plane 12 rather than documented by Laminar, so a
#: future version could in principle change it. That is why it only ever
#: *filters* a listing -- ``--all`` shows everything, and nothing in the
#: pipeline depends on a window carrying this class.
XPLANE_WINDOW_CLASS = "X-System"


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    width: int
    height: int
    pid: int
    #: Top-left of the *window* in screen coordinates, which is what places it
    #: on a monitor -- while width/height above are the *client* area, which is
    #: what capture sees. The two deliberately measure different rectangles:
    #: each is the number its own job needs, and averaging them into one
    #: would leave neither correct. Negative on a monitor left of the primary.
    x: int = 0
    y: int = 0

    def __str__(self) -> str:
        return (
            f"hwnd=0x{self.hwnd:08X} pid={self.pid:<6} {self.width}x{self.height} "
            f"at {self.x},{self.y} "
            f"class={self.class_name!r} title={self.title!r}"
        )


def is_windows() -> bool:
    return sys.platform == "win32"


def list_windows(visible_only: bool = True) -> list[WindowInfo]:
    """Enumerate top-level windows so the user can find the pop-out titles.

    Everything, never filtered by class: both callers want the whole desktop.
    ``list-windows`` shows X-Plane's own windows but counts them against the
    total, and window management looks for a pop-out by title whether or not
    it carries the class it is expected to. Filtering here would take that
    choice away from both of them. Pure ctypes/user32 -- no pywin32 needed.
    Windows only.
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
        rect = _RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        outer = _RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(outer))
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
                x=outer.left,
                y=outer.top,
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
            "Run 'python -m glasslinkxp.main list-windows' to see what is open, and check "
            "that the G1000 display is popped out into its own window."
        )
    if len(matches) > 1:
        LOG.warning(
            "%d windows match %r, using %r", len(matches), title_substring, matches[0].title
        )
    return matches[0]


@dataclass(frozen=True)
class Placement:
    """Where a window ended up, and how big its client area ended up.

    Reported back rather than assumed because neither is guaranteed: X-Plane
    enforces a minimum size on pop-outs, and Windows will refuse a position
    that would put a window entirely off every monitor.
    """

    width: int
    height: int
    x: int
    y: int

    def __str__(self) -> str:
        return f"{self.width}x{self.height} at {self.x},{self.y}"


def _set_window(
    hwnd: int, width: int, height: int, x: int | None = None, y: int | None = None
) -> Placement:  # pragma: no cover - Windows only
    """Size a window's *client area* and optionally move the window.

    The client area is what gets captured, and it is smaller than the window by
    the title bar and borders, so the outer size is the target plus whatever
    that frame costs -- measured rather than assumed, since it varies with DPI
    and theme.

    ``x``/``y`` are the *window's* top-left in screen coordinates, not the
    client area's: that is the corner Windows positions a window by, and the
    frame overhead above is exactly the difference between them.
    """
    if not is_windows():
        raise CaptureError("resizing windows requires Windows")

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]

    window_rect, client_rect = _RECT(), _RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(window_rect)):
        raise CaptureError(f"GetWindowRect failed for hwnd 0x{hwnd:08X}")
    if not user32.GetClientRect(hwnd, ctypes.byref(client_rect)):
        raise CaptureError(f"GetClientRect failed for hwnd 0x{hwnd:08X}")

    frame_w = (window_rect.right - window_rect.left) - client_rect.right
    frame_h = (window_rect.bottom - window_rect.top) - client_rect.bottom

    SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0002, 0x0004, 0x0010
    flags = SWP_NOZORDER | SWP_NOACTIVATE
    if x is None or y is None:
        flags |= SWP_NOMOVE
        x = y = 0
    user32.SetWindowPos(
        hwnd, 0, x, y, width + frame_w, height + frame_h, flags,
    )
    user32.GetClientRect(hwnd, ctypes.byref(client_rect))
    user32.GetWindowRect(hwnd, ctypes.byref(window_rect))
    return Placement(
        width=client_rect.right, height=client_rect.bottom,
        x=window_rect.left, y=window_rect.top,
    )


def place_window(hwnd: int, x: int, y: int, width: int, height: int) -> Placement:
    """Move a window to ``x, y`` and size its client area to width x height.

    One call rather than a move and then a resize: two SetWindowPos calls make
    the window visibly jump, and the intermediate position can be one the user
    sees.
    """
    if not is_windows():  # pragma: no cover - Windows only
        raise CaptureError("placing windows requires Windows")
    return _set_window(hwnd, width, height, x, y)  # pragma: no cover - Windows only


def monitor_bounds(hwnd: int) -> tuple[int, int, int, int]:
    """The full bounds of the monitor a window is on, in screen coordinates.

    ``(left, top, right, bottom)`` of the monitor itself, *not* its work area:
    the work area stops short of the taskbar, and the whole point of placing a
    pop-out at the monitor's own origin is to have it start above where the
    taskbar sits rather than be pushed down by it.

    The left/top are not (0, 0) except on the primary monitor -- a second
    monitor's origin is wherever the desktop arrangement puts it, and is
    negative for one placed left of or above the primary.
    """
    if not is_windows():  # pragma: no cover - Windows only
        raise CaptureError("reading monitor bounds requires Windows")
    return _monitor_bounds(hwnd)  # pragma: no cover - Windows only


def _monitor_bounds(hwnd: int) -> tuple[int, int, int, int]:  # pragma: no cover - Windows only
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("rcMonitor", _RECT),
            ("rcWork", _RECT),
            ("dwFlags", ctypes.c_ulong),
        ]

    # An HMONITOR is a pointer. ctypes defaults a return value to c_int, which
    # would truncate it on 64-bit Windows and hand GetMonitorInfoW a handle
    # that is not the one MonitorFromWindow returned.
    user32.MonitorFromWindow.restype = ctypes.c_void_p
    user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]

    MONITOR_DEFAULTTONEAREST = 2
    handle = user32.MonitorFromWindow(ctypes.c_void_p(hwnd), MONITOR_DEFAULTTONEAREST)
    if not handle:
        raise CaptureError(f"MonitorFromWindow found no monitor for hwnd 0x{hwnd:08X}")
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(ctypes.c_void_p(handle), ctypes.byref(info)):
        raise CaptureError(f"GetMonitorInfoW failed for hwnd 0x{hwnd:08X}")
    rect = info.rcMonitor
    return rect.left, rect.top, rect.right, rect.bottom


class _LatestFrame:
    """The one frame slot the capture thread and :meth:`WgcCapture.grab` share.

    Split out of :class:`WgcCapture` because it is the only part of that class
    that can be exercised anywhere but Windows, and because it holds the rule
    that the rest of the daemon depends on: **once the captured window is gone,
    there is no current frame, and the last one is not a substitute.**

    That rule was missing, and it hid the failure it was most needed for. The
    slot kept handing back the final frame from a window the user had closed,
    so the daemon read a frozen picture forever: the labels on the Stream Deck
    stayed as they were at the moment the window went away, the "no frames
    from %s" warning never fired because frames were still arriving, and the
    recovery that reopens a closed pop-out was never reached. A stale frame is
    worse than no frame -- no frame is a condition the daemon can act on.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: Frame | None = None
        self._lost = False
        self._stopping = False

    def put(self, frame: Frame) -> None:
        with self._lock:
            self._frame = frame

    def take(self) -> Frame | None:
        """The newest frame, or None -- including when the window has gone."""
        with self._lock:
            if self._lost or self._frame is None:
                return None
            return self._frame.copy()

    def lose(self) -> None:
        """The captured window closed. Permanent: the session cannot come back."""
        with self._lock:
            self._lost = True

    @property
    def lost(self) -> bool:
        with self._lock:
            return self._lost

    def stop(self) -> None:
        """We are shutting down; the capture thread should stop at its next frame."""
        with self._lock:
            self._stopping = True

    @property
    def stopping(self) -> bool:
        with self._lock:
            return self._stopping


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
        self._buffer = _LatestFrame()
        self._error: str | None = None

        # Resolve the exact title first so we can give a useful error message
        # rather than whatever the Rust layer raises.
        # Note this only *finds* the window. Sizing and placing a pop-out is
        # window management's job and nothing else's -- see windowmgr -- so a
        # capture takes the window as it is.
        window = find_window(window_title)  # pragma: no cover
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
            self._buffer.put(bgr)
            if self._buffer.stopping:
                capture_control.stop()

        @capture.event  # pragma: no cover - Windows only
        def on_closed():  # type: ignore[no-untyped-def]
            LOG.warning("capture target window closed: %s", window_title)
            self._error = "capture window closed"
            # Not just a message: this is what stops grab() serving the last
            # frame of a window that no longer exists, and so what lets the
            # daemon notice and reopen it.
            self._buffer.lose()

        self._control = capture.start_free_threaded()  # pragma: no cover

    @property
    def window_closed(self) -> bool:
        """Whether the captured window has gone. This source is then finished.

        A WGC session does not survive its window, so the way back is a new
        source built on a new window -- see ``main._reopen_closed``.
        """
        return self._buffer.lost

    def grab(self) -> Frame | None:  # pragma: no cover - Windows only
        if self._error:
            LOG.debug("capture backend reported: %s", self._error)
        return self._buffer.take()

    def close(self) -> None:  # pragma: no cover - Windows only
        self._buffer.stop()
        try:
            self._control.stop()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("ignoring error while stopping capture: %s", exc)


def window_lost(source: FrameSource) -> bool:
    """Whether ``source`` has lost the window it was capturing.

    Asked of any source, answered only by :class:`WgcCapture` -- a PNG on disk
    has no window to lose. Read through ``getattr`` rather than added to the
    :class:`FrameSource` protocol so that a source which cannot lose a window
    does not have to carry a method saying so.
    """
    return bool(getattr(source, "window_closed", False))


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


def sources_for(
    displays: Iterable, image_path: str | Path | None
) -> dict[str, FrameSource]:
    """One frame source per display; ``image_path`` overrides WGC everywhere.

    Opening a source neither sizes nor moves anything. That used to depend on
    which display it was for, so this took a set of the displays window
    management had already handled in order to leave those alone -- a
    parameter whose whole job was to stop two settings sizing one window. With
    only one of them left there is nothing to arbitrate.
    """
    sources: dict[str, FrameSource] = {}
    for display in displays:
        if image_path is not None:
            path = Path(image_path)
            per_display = path / f"{display.key}.png"
            sources[display.key] = ImageCapture(per_display if per_display.is_file() else path)
        else:
            sources[display.key] = WgcCapture(display.window_title)
    return sources
