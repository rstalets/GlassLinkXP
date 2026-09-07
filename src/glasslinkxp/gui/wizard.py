"""The setup wizard: what the steps are, and where the user is in them.

No Tk in here. The band that draws this is :class:`widgets.WizardBar`, and the
one instance of it belongs to ``app.py`` -- so the step list, its order, and
the "does this look finished?" checks are all testable without a display.

The wizard is a *bar*, not a tab, and that is the whole design. A tab full of
"go there" buttons sends somebody off to do a step and then has nothing more
to say: they finish, and the way back is to remember which tab they came from
and which of six things they had done. The bar stays on screen while they work
in whichever tab the step needs, so the answer to "where was I?" is always
in front of them.

A check answers "does this look finished?" and never blocks: a "no" becomes a
question. Two of the six steps are the user looking at a picture and deciding
it is right, which nothing here can see, and the two checks that do exist can
be fooled by a config that was hand-edited or that happens to match a default.
A check that cannot tell a finished step from an unusual one, but refuses
anyway, only teaches people to click past it -- the same reason the
calibration editor asks before saving a geometry that looks clipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..config import StripGeometry


@dataclass(frozen=True)
class Step:
    """One step of the walkthrough."""

    key: str
    title: str
    instruction: str
    #: The tab this step is done in, by class name -- resolved through
    #: ``tabs.tab_index`` for the same reason the rest of the GUI does, so
    #: reordering the tab strip cannot leave a step pointing at the wrong one.
    tab: str
    #: Returns "" when the step looks finished, else why it does not. None for
    #: a step whose completion is somebody looking at a picture and deciding
    #: it is right, which is not visible from the configuration.
    check: Callable[[Mapping[str, Any]], str] | None = None


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def _displays(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """The display tables, skipping anything that is not one.

    Defensive because this reads a document the raw-TOML editor can put
    anything into, and a wizard that raises on a malformed config would take
    the window down at exactly the moment somebody is least equipped to
    diagnose it.
    """
    displays = document.get("display")
    if not isinstance(displays, Mapping):
        return {}
    return {
        key: entry for key, entry in displays.items()
        if isinstance(entry, Mapping) and entry.get("enabled", True)
    }


def windows_chosen(document: Mapping[str, Any]) -> str:
    """Every enabled display names a window to capture."""
    missing = sorted(
        key for key, entry in _displays(document).items()
        if not str(entry.get("window_title") or "").strip()
    )
    if missing:
        return f"no window has been chosen for {', '.join(missing)}"
    return ""


def calibrated(document: Mapping[str, Any]) -> str:
    """The strip position has been moved off the built-in default.

    A heuristic, and deliberately a weak one: the default is a starting guess
    at where the strip sits, so a geometry still exactly equal to it means
    nobody has drawn a box -- the odds of dragging one out to four identical
    fractions are nil. It can still be wrong in the harmless direction, asking
    an unnecessary question of somebody who calibrated in a previous session
    and got the default back, which is why it asks rather than refuses.
    """
    default = StripGeometry().as_dict()
    untouched = sorted(
        key for key, entry in _displays(document).items()
        if {**default, **(entry.get("geometry") if isinstance(entry.get("geometry"), Mapping)
                          else {})} == default
    )
    if untouched:
        return (
            f"the strip position for {', '.join(untouched)} is still the built-in "
            "default, so it looks like nothing has been calibrated yet"
        )
    return ""


# ---------------------------------------------------------------------------
# the steps
# ---------------------------------------------------------------------------


STEPS: tuple[Step, ...] = (
    Step(
        "preflight",
        "Start X-Plane, with the aircraft on the ground",
        "X-Plane has to be running, with your aircraft on the ground and the G1000 "
        "switched on. Press Next when it is: that creates your configuration file "
        "and moves on to finding the windows.",
        "StartTab",
    ),
    Step(
        "windows",
        "Find the PFD and MFD windows",
        "Press \"Set up pop-outs\" to open them, then pick each one in the list and "
        "press Apply -- once as pfd, once as mfd.",
        "WindowsTab",
        windows_chosen,
    ),
    Step(
        "calibrate",
        "Draw a box around the softkey strip",
        "Press \"Take a picture\", drag a box roughly around the strip, then work "
        "through the three steps on the right and press Save. This is the step that "
        "decides whether anything reads correctly.",
        "CalibrateTab",
        calibrated,
    ),
    Step(
        "cells",
        "Check what is actually being read",
        "Press \"Read the cells\". Each one should be black text on a white "
        "background. If a cell is clipped, press Back and fix the box; if it looks "
        "right but reads wrong, type what it should say and run the tuner.",
        "CellsTab",
    ),
    Step(
        "watch",
        "Watch the labels, without touching X-Plane",
        "Set Publish to 'console' and press Start. The board fills in with what is "
        "being read; press Stop once you have seen it. Nothing is sent to the sim "
        "in this mode.",
        "RunTab",
    ),
    Step(
        "publish",
        "Send the labels to X-Plane",
        "Set Publish to 'websocket' and press Start. Your Stream Deck buttons can "
        "now read glasslinkxp/softkey/pfd/1:s16. Press Finish when they do.",
        "RunTab",
    ),
)


# ---------------------------------------------------------------------------
# where in them
# ---------------------------------------------------------------------------


def clamp(index: int) -> int:
    """A usable step number, whatever was stored in the preferences."""
    return max(0, min(int(index), len(STEPS) - 1))


def step(index: int) -> Step:
    return STEPS[clamp(index)]


def is_last(index: int) -> bool:
    return clamp(index) == len(STEPS) - 1


def trail(index: int) -> tuple[str, ...]:
    """One of "done" / "current" / "todo" per step, for the progress strip."""
    current = clamp(index)
    return tuple(
        "done" if i < current else "current" if i == current else "todo"
        for i in range(len(STEPS))
    )
