"""Opening, sizing and placing the G1000 pop-out windows.

The daemon can only read a softkey strip that is on the screen at a size worth
reading, in a place nothing is sitting on top of. Left to the user that is a
pre-flight checklist -- pop out two windows, size them the way they were sized
last time, drag them clear of the taskbar -- and every step of it fails
silently: a window that was never popped out, or one half under the taskbar,
shows up as OCR that reads nothing rather than as an error that says what is
wrong. So the daemon does it.

Three things, in this order, per display:

1. **Open it.** If no window matches the display's ``window_title``, fire the
   sim command that pops it out and wait for the window to appear.
2. **Size it.** The client area goes to ``window_management.size``, which must
   be 4:3 -- see :class:`~g1000_softkey.config.WindowManagementConfig`.
3. **Place it.** Top-left of the monitor X-Plane is running on. The taskbar
   sits at the bottom of a monitor, so the top-left corner is the part of it
   least likely to have anything over the top of the window.

Only ``pfd`` and ``mfd`` are managed, because those are the displays there are
commands for (see :data:`POPOUT_COMMANDS`). A display configured under any
other name is left entirely alone, and its own ``manage_window_size`` still
applies.

**Testing.** Everything Windows-only is reached through :class:`WindowOps` and
a command client, both injectable, so the decision-making here -- which window
counts as missing, which command that means firing, where the result should be
put -- is covered by ``tests/test_windowmgr.py`` on any platform. What cannot
be covered anywhere but a real Windows box with a real X-Plane is whether
user32 and the sim do what they are asked; see README's *Not verified here*.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .capture import (
    XPLANE_WINDOW_CLASS,
    CaptureError,
    Placement,
    WindowInfo,
    is_windows,
    list_windows,
    monitor_bounds,
    place_window,
)
from .command import CommandClient, CommandError
from .config import AppConfig

LOG = logging.getLogger(__name__)

#: Which sim command pops each display out into its own window.
#:
#: ``_popout`` and not ``_popup``: the popup opens the panel *inside* the
#: X-Plane window, where it is not a top-level window at all, so it can be
#: neither captured nor placed. The two names differ by two characters and do
#: visibly similar things, which is exactly why this says so here.
#:
#: n1 is the pilot's PFD and n3 the MFD, matching the ``g1000n1_``/``g1000n3_``
#: softkey commands the Stream Deck buttons already send.
POPOUT_COMMANDS: dict[str, str] = {
    "pfd": "sim/GPS/g1000n1_popout",
    "mfd": "sim/GPS/g1000n3_popout",
}

#: How long to wait for a window to appear after firing its pop-out command,
#: and how often to look. X-Plane opens the window on its own next frame, so
#: this is generous; it is a bound on how long a *failed* command costs, not an
#: expected wait.
OPEN_TIMEOUT = 5.0
OPEN_POLL = 0.25


class WindowOps(Protocol):
    """The Windows-only operations this module needs, so tests can supply them."""

    def list(self) -> list[WindowInfo]:
        """Every visible top-level window."""

    def place(self, hwnd: int, x: int, y: int, width: int, height: int) -> Placement:
        """Move a window and size its client area; report what was achieved."""

    def monitor_bounds(self, hwnd: int) -> tuple[int, int, int, int]:
        """(left, top, right, bottom) of the monitor a window is on."""


class Win32Ops:
    """:class:`WindowOps` against the real desktop."""

    def list(self) -> list[WindowInfo]:
        return list_windows()

    def place(self, hwnd: int, x: int, y: int, width: int, height: int) -> Placement:
        return place_window(hwnd, x, y, width, height)

    def monitor_bounds(self, hwnd: int) -> tuple[int, int, int, int]:
        return monitor_bounds(hwnd)


@dataclass(frozen=True)
class DisplayOutcome:
    """What happened to one display's window."""

    key: str
    #: opened | placed | already | missing | failed
    action: str
    detail: str
    window: WindowInfo | None = None

    @property
    def ok(self) -> bool:
        return self.action in ("opened", "placed", "already")

    def __str__(self) -> str:
        return f"{self.key}: {self.action} -- {self.detail}"


@dataclass(frozen=True)
class Report:
    """What a management pass did."""

    outcomes: tuple[DisplayOutcome, ...] = ()
    #: True when at least one X-Plane window was on screen to work with.
    xplane_running: bool = False
    #: Why nothing was done, when nothing was.
    skipped: str = ""

    @property
    def managed(self) -> frozenset[str]:
        """Displays whose window this pass has sized and placed.

        What ``capture.sources_for`` needs: those windows must not then be
        resized a second time by the per-display settings.
        """
        return frozenset(o.key for o in self.outcomes if o.ok)

    def opened(self, key: str) -> bool:
        """Whether this pass created the window for ``key``.

        The one case where an existing capture is certainly attached to a
        window that no longer exists, and so has to be rebuilt rather than
        waited on.
        """
        return any(o.key == key and o.action == "opened" for o in self.outcomes)

    def lines(self) -> list[str]:
        if self.skipped:
            return [self.skipped]
        if not self.xplane_running:
            return [
                "X-Plane does not appear to be running: no window of class "
                f"{XPLANE_WINDOW_CLASS!r} is open, and no configured pop-out title "
                "matched. Start X-Plane first; nothing was changed."
            ]
        return [str(outcome) for outcome in self.outcomes] or [
            "window management is on, but no display is configured under a name it "
            f"manages ({', '.join(sorted(POPOUT_COMMANDS))})."
        ]


def _match(
    windows: list[WindowInfo], title_substring: str, quiet: bool = False
) -> WindowInfo | None:
    """First window whose title contains ``title_substring``, case insensitively.

    The non-raising counterpart of ``capture.find_window``: here a window that
    is not open is the normal case that starts the work, not an error.

    ``quiet`` suppresses the ambiguity warning for the callers that are only
    working out which windows to *exclude*. Without it a config whose two
    titles overlap warns twice per pass -- and a pass runs every ten seconds
    while a display is starved, which is exactly when the log is being read.
    """
    if not title_substring:
        return None
    needle = title_substring.casefold()
    matches = [w for w in windows if needle in w.title.casefold()]
    if len(matches) > 1 and not quiet:
        LOG.warning(
            "%d windows match %r, managing %r", len(matches), title_substring, matches[0].title
        )
    return matches[0] if matches else None


def _anchor(windows: list[WindowInfo], popout_titles: list[str]) -> WindowInfo | None:
    """X-Plane's main window -- the one that says which monitor the sim is on.

    Every X-Plane window carries the same class, pop-outs included, so the main
    view is picked out by elimination: drop the windows that match a configured
    pop-out title, and take the largest of what is left. The main view is
    normally full-screen and a pop-out is normally not, so "largest" separates
    them in every arrangement anyone is likely to be flying.

    None when there is nothing to go on, which leaves each window to be snapped
    to the origin of whichever monitor it is already on.
    """
    taken = {w.hwnd for title in popout_titles
             for w in [_match(windows, title, quiet=True)] if w}
    candidates = [
        w for w in windows
        if w.class_name == XPLANE_WINDOW_CLASS and w.hwnd not in taken
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda w: w.width * w.height)


def _managed_displays(config: AppConfig):
    """The configured displays this module has a pop-out command for."""
    return [d for d in config.active_displays if d.key in POPOUT_COMMANDS]


def manage_windows(
    config: AppConfig,
    ops: WindowOps | None = None,
    commander: CommandClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Report:
    """Open, size and place the G1000 pop-outs. Never raises.

    Idempotent, and cheap when there is nothing to do: a pass over an already
    correct pair of windows enumerates the desktop once and touches nothing.
    That is what lets the daemon call it again whenever a display stops
    delivering frames.
    """
    settings = config.window_management
    if not settings.enabled:
        return Report(skipped="window management is off in the config.")
    # Only when nobody supplied the operations: a caller that brought its own
    # has already said how windows are to be reached, which is what lets the
    # tests drive all of this on a machine with no user32 at all.
    if ops is None and not is_windows():
        return Report(
            skipped="window management needs Windows: the pop-outs are placed through "
                    "user32, and there is no equivalent to place them with here."
        )

    ops = ops or Win32Ops()
    displays = _managed_displays(config)
    try:
        windows = ops.list()
    except CaptureError as exc:
        return Report(skipped=f"could not list windows: {exc}")

    # A matching pop-out title counts as well as the class, so a build whose
    # windows are not classed as expected still gets sized and placed -- only
    # the automatic opening depends on recognising the sim's own window. Quiet,
    # because this is asking whether anything is there at all, not choosing
    # which window to manage; the lookup that does that warns for itself.
    running = any(w.class_name == XPLANE_WINDOW_CLASS for w in windows) or any(
        _match(windows, d.window_title, quiet=True) for d in displays
    )
    if not running:
        return Report(xplane_running=False)

    anchor = _anchor(windows, [d.window_title for d in displays])
    owned: list[CommandClient] = []
    outcomes: list[DisplayOutcome] = []
    try:
        for display in displays:
            window = _match(windows, display.window_title)
            opened = False
            if window is None:
                if commander is None:
                    commander = CommandClient(config.publish)
                    owned.append(commander)
                outcome, window = _open(display, commander, ops, sleep)
                if window is None:
                    outcomes.append(outcome)
                    continue
                opened = True
                # The desktop changed: later displays must see the new window
                # so it is not matched as missing and popped out twice.
                try:
                    windows = ops.list()
                except CaptureError:  # pragma: no cover - defensive
                    windows = windows + [window]
            outcomes.append(_settle(display.key, window, settings.size, anchor, ops, opened))
    finally:
        for client in owned:
            client.close()
    return Report(outcomes=tuple(outcomes), xplane_running=True)


def _open(
    display, commander: CommandClient, ops: WindowOps, sleep: Callable[[float], None]
) -> tuple[DisplayOutcome, WindowInfo | None]:
    """Fire the pop-out command and wait for the window to turn up."""
    command = POPOUT_COMMANDS[display.key]
    LOG.info("%s is not open; firing %s", display.key, command)
    try:
        commander.trigger(command)
    except CommandError as exc:
        return DisplayOutcome(display.key, "failed", str(exc)), None

    # Counted rather than clock-driven so that the waiting is entirely in the
    # injected sleep: a test can hand in a no-op and get the same number of
    # attempts in no time at all, instead of the loop spinning against a
    # wall-clock deadline it cannot move.
    for _ in range(max(1, round(OPEN_TIMEOUT / OPEN_POLL))):
        sleep(OPEN_POLL)
        try:
            window = _match(ops.list(), display.window_title)
        except CaptureError as exc:  # pragma: no cover - defensive
            return DisplayOutcome(display.key, "failed", f"could not list windows: {exc}"), None
        if window is not None:
            return DisplayOutcome(display.key, "opened", f"opened with {command}", window), window
    return (
        DisplayOutcome(
            display.key, "missing",
            f"{command} was accepted but no window titled like "
            f"{display.window_title!r} appeared within {OPEN_TIMEOUT:.0f}s. If the "
            "pop-out did open, its title is not what the config expects -- check it "
            "with 'list-windows'.",
        ),
        None,
    )


def _settle(
    key: str,
    window: WindowInfo,
    size: tuple[int, int],
    anchor: WindowInfo | None,
    ops: WindowOps,
    opened: bool,
) -> DisplayOutcome:
    """Put one window at the right size in the right corner."""
    width, height = size
    try:
        left, top, _right, _bottom = ops.monitor_bounds((anchor or window).hwnd)
    except CaptureError as exc:
        return DisplayOutcome(key, "failed", f"could not find the monitor: {exc}", window)

    if (window.width, window.height, window.x, window.y) == (width, height, left, top):
        # Nothing to move, and saying so is worth a line: it is the difference
        # between "this is already right" and "this was never looked at".
        #
        # But "opened" still wins when this pass created the window, and that
        # is not a nicety. X-Plane reopens a pop-out at the geometry it last
        # had -- which, after any previous pass, is exactly the geometry being
        # asked for here -- so the freshly opened window needing no move is the
        # *likely* case, not a corner one. Reporting it as "already" would tell
        # the daemon nothing was opened, and the capture still attached to the
        # window the user closed would never be rebuilt: the display would stay
        # starved for as long as the daemon ran.
        return DisplayOutcome(
            key,
            "opened" if opened else "already",
            f"{'opened, and already' if opened else 'already'} "
            f"{width}x{height} at {left},{top}",
            window,
        )

    try:
        placed = ops.place(window.hwnd, left, top, width, height)
    except CaptureError as exc:
        return DisplayOutcome(key, "failed", f"could not place the window: {exc}", window)

    detail = f"{'opened, ' if opened else ''}now {placed}"
    if (placed.width, placed.height) != (width, height):
        detail += (
            f", not the {width}x{height} asked for -- X-Plane enforces a minimum size "
            "on pop-out windows, so the strip geometry should be re-checked against "
            "what it settled at"
        )
    if (placed.x, placed.y) != (left, top):
        detail += f", not the {left},{top} asked for"
    return DisplayOutcome(
        key, "opened" if opened else "placed", detail,
        WindowInfo(
            hwnd=window.hwnd, title=window.title, class_name=window.class_name,
            width=placed.width, height=placed.height, pid=window.pid,
            x=placed.x, y=placed.y,
        ),
    )
