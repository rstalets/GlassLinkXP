"""Window management, driven entirely through its injected operations.

Nothing here touches user32, and that is the point: the parts of this feature
that can be wrong in an interesting way are decisions -- which window counts as
missing, which command that means firing, whose monitor the result belongs on
-- and those are all decidable on a machine with no Windows anywhere near it.

What these tests do *not* prove is that SetWindowPos moves an X-Plane pop-out,
that the pop-out commands are named what this thinks they are named, or that
X-Plane's windows really do all carry the ``X-System`` class. Those need a
Windows box with the sim running; see README's *Not verified here*.
"""

from __future__ import annotations

import pytest

from g1000_softkey.capture import Placement, WindowInfo
from g1000_softkey.command import CommandError
from g1000_softkey.config import (
    AppConfig,
    DisplayConfig,
    WindowManagementConfig,
)
from g1000_softkey.windowmgr import POPOUT_COMMANDS, manage_windows

SIM = "X-System"


def window(hwnd, title, *, cls=SIM, width=1280, height=960, x=0, y=0):
    return WindowInfo(hwnd=hwnd, title=title, class_name=cls, width=width,
                      height=height, pid=42, x=x, y=y)


def main_window(hwnd=1, x=0, y=0):
    """X-Plane's main view: same class as a pop-out, and bigger."""
    return window(hwnd, "X-System", width=1920, height=1080, x=x, y=y)


class FakeOps:
    """A desktop: a list of windows, a monitor per window, and a record."""

    def __init__(self, windows=(), monitors=None, default_monitor=(0, 0, 1920, 1080)):
        self.windows = list(windows)
        self.monitors = dict(monitors or {})
        self.default_monitor = default_monitor
        self.placed: list[tuple] = []
        self.lists = 0

    def list(self):
        self.lists += 1
        return list(self.windows)

    def place(self, hwnd, x, y, width, height):
        self.placed.append((hwnd, x, y, width, height))
        # The desktop really changes, so a later look sees the new geometry --
        # which is what makes a second pass over the same windows a no-op.
        self.windows = [
            WindowInfo(hwnd=w.hwnd, title=w.title, class_name=w.class_name,
                       width=width, height=height, pid=w.pid, x=x, y=y)
            if w.hwnd == hwnd else w
            for w in self.windows
        ]
        return Placement(width=width, height=height, x=x, y=y)

    def monitor_bounds(self, hwnd):
        return self.monitors.get(hwnd, self.default_monitor)


class FakeCommander:
    """Records the commands fired, and optionally opens a window for each."""

    def __init__(self, ops=None, opens=None, error=None):
        self.ops = ops
        self.opens = dict(opens or {})
        self.error = error
        self.fired: list[str] = []
        self.closed = False

    def trigger(self, name, duration=0.0):
        self.fired.append(name)
        if self.error:
            raise CommandError(self.error)
        appearing = self.opens.get(name)
        if appearing is not None and self.ops is not None:
            self.ops.windows.append(appearing)

    def close(self):
        self.closed = True


def config(size=(1280, 960), enabled=True, displays=None):
    if displays is None:
        displays = (
            DisplayConfig(key="pfd", window_title="G1000 PFD"),
            DisplayConfig(key="mfd", window_title="G1000 MFD"),
        )
    return AppConfig(
        displays=displays,
        window_management=WindowManagementConfig(enabled=enabled, size=size),
    )


def run(cfg, ops, commander=None):
    """A pass with the sleeping taken out, so the polling costs nothing."""
    return manage_windows(cfg, ops=ops, commander=commander, sleep=lambda _s: None)


# -- when it does nothing --------------------------------------------------


def test_it_does_nothing_at_all_when_it_is_switched_off():
    ops = FakeOps([main_window()])
    report = run(config(enabled=False), ops)

    assert report.skipped
    assert report.managed == frozenset()
    assert ops.placed == []
    assert ops.lists == 0, "a switched-off feature should not even look at the desktop"


def test_a_desktop_with_no_x_plane_on_it_is_left_alone():
    """No windows to work with means the sim is not up yet.

    Firing pop-out commands into a sim that is not running would at best do
    nothing; the honest answer is to say X-Plane is not there.
    """
    ops = FakeOps([window(9, "Notepad", cls="Notepad")])
    commander = FakeCommander(ops)
    report = run(config(), ops, commander)

    assert report.xplane_running is False
    assert commander.fired == []
    assert ops.placed == []


def test_windows_that_are_already_right_are_not_touched():
    ops = FakeOps([
        main_window(),
        window(2, "G1000 PFD", x=0, y=0),
        window(3, "G1000 MFD", x=0, y=0),
    ])
    commander = FakeCommander(ops)
    report = run(config(), ops, commander)

    assert commander.fired == []
    assert ops.placed == [], "nothing moved, so nothing should have been moved"
    assert [o.action for o in report.outcomes] == ["already", "already"]
    assert report.managed == {"pfd", "mfd"}


def test_a_second_pass_over_its_own_work_changes_nothing():
    """Idempotence is what lets the daemon call this whenever a display starves."""
    ops = FakeOps([main_window(), window(2, "G1000 PFD", width=800, height=600, x=40, y=40)])
    run(config(), ops, FakeCommander(ops))
    assert len(ops.placed) == 1

    run(config(), ops, FakeCommander(ops))
    assert len(ops.placed) == 1, "the second pass found the window already correct"


# -- opening what is not open ----------------------------------------------


def test_a_missing_pop_out_is_popped_out_and_then_placed():
    ops = FakeOps([main_window()])
    commander = FakeCommander(ops, opens={
        POPOUT_COMMANDS["pfd"]: window(2, "G1000 PFD", width=1024, height=768, x=300, y=200),
        POPOUT_COMMANDS["mfd"]: window(3, "G1000 MFD", width=1024, height=768, x=400, y=300),
    })
    report = run(config(), ops, commander)

    assert commander.fired == [POPOUT_COMMANDS["pfd"], POPOUT_COMMANDS["mfd"]]
    assert [o.action for o in report.outcomes] == ["opened", "opened"]
    assert ops.placed == [(2, 0, 0, 1280, 960), (3, 0, 0, 1280, 960)]
    assert report.opened("pfd") and report.opened("mfd")


def test_each_display_gets_its_own_command():
    """n1 is the PFD and n3 the MFD; swapping them would pop out the wrong panel."""
    ops = FakeOps([main_window(), window(3, "G1000 MFD")])
    commander = FakeCommander(ops, opens={POPOUT_COMMANDS["pfd"]: window(2, "G1000 PFD")})
    run(config(), ops, commander)

    assert commander.fired == ["sim/GPS/g1000n1_popout"]


def test_a_window_opened_for_one_display_is_seen_by_the_next():
    """The desktop is re-read after opening one, rather than worked from a stale list.

    A loose ``window_title`` -- "G1000" for both displays, say, which is an easy
    thing to type -- means the window opened for the PFD also matches the MFD.
    Against a list taken before it existed, the MFD is still missing and its
    command goes out too. That is at best pointless, and a pop-out command that
    turns out to toggle would close what was just opened.
    """
    ops = FakeOps([main_window()])
    commander = FakeCommander(ops, opens={POPOUT_COMMANDS["pfd"]: window(2, "G1000 PFD")})
    cfg = config(displays=(
        DisplayConfig(key="pfd", window_title="G1000"),
        DisplayConfig(key="mfd", window_title="G1000"),
    ))
    run(cfg, ops, commander)

    assert commander.fired == [POPOUT_COMMANDS["pfd"]]


def test_a_command_that_cannot_be_sent_is_reported_not_raised():
    ops = FakeOps([main_window()])
    commander = FakeCommander(ops, error="X-Plane's web API is not answering")
    report = run(config(), ops, commander)

    assert [o.action for o in report.outcomes] == ["failed", "failed"]
    assert "not answering" in report.outcomes[0].detail
    assert report.managed == frozenset(), "a failure must not be reported as managed"


def test_a_command_that_opens_nothing_says_so():
    """Accepted, but no window: the configured title is the likely culprit."""
    ops = FakeOps([main_window()])
    commander = FakeCommander(ops)  # fires happily, opens nothing
    report = run(config(), ops, commander)

    assert [o.action for o in report.outcomes] == ["missing", "missing"]
    assert "list-windows" in report.outcomes[0].detail
    assert report.managed == frozenset()


# -- where things get put --------------------------------------------------


def test_windows_are_snapped_to_the_corner_of_x_planes_monitor():
    """Not the corner of the monitor the pop-out happens to be on already."""
    sim = main_window(hwnd=1)
    popout = window(2, "G1000 PFD", x=-1900, y=100)
    ops = FakeOps(
        [sim, popout],
        monitors={1: (1920, 0, 3840, 1080), 2: (-1920, 0, 0, 1080)},
    )
    run(config(), ops, FakeCommander(ops))

    assert ops.placed == [(2, 1920, 0, 1280, 960)]


def test_the_main_window_is_the_biggest_one_that_is_not_a_pop_out():
    """Every X-Plane window shares a class, so the main view is found by size.

    The pop-outs are excluded first: a pop-out sized larger than a windowed
    main view would otherwise nominate itself as the monitor to use.
    """
    sim = main_window(hwnd=1)
    huge_popout = window(2, "G1000 PFD", width=3000, height=2000)
    ops = FakeOps(
        [huge_popout, sim],
        monitors={1: (0, 0, 1920, 1080), 2: (5000, 0, 8000, 2000)},
    )
    run(config(), ops, FakeCommander(ops))

    assert ops.placed == [(2, 0, 0, 1280, 960)], "placed on the main window's monitor"


def test_with_no_main_window_a_pop_out_is_snapped_on_its_own_monitor():
    """The sim's main view can be missing -- it is on another desktop, say.

    Falling back to the window's own monitor still gets it out from under the
    taskbar, which is most of the point, rather than moving it to a monitor
    nobody has said anything about.
    """
    popout = window(2, "G1000 PFD", cls="Something Else", x=2000, y=500)
    ops = FakeOps([popout], monitors={2: (1920, 0, 3840, 1080)})
    report = run(config(), ops, FakeCommander(ops))

    assert report.xplane_running is True, "a matching pop-out title is enough to proceed"
    assert ops.placed == [(2, 1920, 0, 1280, 960)]


# -- which displays are managed --------------------------------------------


def test_only_the_displays_there_are_commands_for_are_managed():
    ops = FakeOps([main_window(), window(4, "Copilot PFD", width=800, height=600, x=50, y=50)])
    commander = FakeCommander(ops)
    cfg = config(displays=(DisplayConfig(key="copilot", window_title="Copilot PFD"),))
    report = run(cfg, ops, commander)

    assert commander.fired == []
    assert ops.placed == [], "a display with no pop-out command is left entirely alone"
    assert report.outcomes == ()
    assert "copilot" not in report.managed


def test_a_disabled_display_is_not_managed():
    ops = FakeOps([main_window()])
    commander = FakeCommander(ops)
    cfg = config(displays=(
        DisplayConfig(key="pfd", window_title="G1000 PFD", enabled=False),
        DisplayConfig(key="mfd", window_title="G1000 MFD", enabled=False),
    ))
    run(cfg, ops, commander)

    assert commander.fired == []


# -- the size ---------------------------------------------------------------


def test_the_configured_size_is_used_when_it_is_four_by_three():
    ops = FakeOps([main_window(), window(2, "G1000 PFD", width=800, height=600, x=9, y=9)])
    run(config(size=(1600, 1200)), ops, FakeCommander(ops))

    assert ops.placed == [(2, 0, 0, 1600, 1200)]


def test_a_size_that_is_not_four_by_three_falls_back():
    """The G1000 panel is 4:3, and the strip geometry is fractions of the window.

    A 16:9 pop-out therefore moves the softkey strip out from under everybody's
    calibration, which is a silent failure -- so the value is refused rather
    than used.
    """
    ops = FakeOps([main_window(), window(2, "G1000 PFD", width=800, height=600, x=9, y=9)])
    run(config(size=(1920, 1080)), ops, FakeCommander(ops))

    assert ops.placed == [(2, 0, 0, 1280, 960)]


@pytest.mark.parametrize("size", [
    (1920, 1080),   # 16:9
    (1280, 1024),   # 5:4, and close enough to look right in a config file
    (0, 0),
    (-1280, -960),
    (1280,),        # not a pair at all
    "1280x960",
])
def test_every_unusable_size_lands_on_the_default(size):
    assert WindowManagementConfig(size=size).size == (1280, 960)


def test_a_size_written_with_a_decimal_point_is_taken_as_whole_pixels():
    """TOML tells floats and integers apart; windows are measured in pixels.

    ``[1280.0, 960.0]`` is a perfectly ordinary thing to end up with, and it
    means the same size as ``[1280, 960]``.
    """
    assert WindowManagementConfig(size=(1280.0, 960.0)).size == (1280, 960)


@pytest.mark.parametrize("size", [(1024, 768), (1280, 960), (1600, 1200), (2048, 1536)])
def test_four_by_three_sizes_are_kept_as_given(size):
    assert WindowManagementConfig(size=size).size == size


def test_a_size_given_as_a_list_survives_being_frozen():
    """TOML hands back a list; the config has to hold something hashable."""
    assert WindowManagementConfig(size=[1600, 1200]).size == (1600, 1200)


# -- the report the rest of the daemon reads -------------------------------


def test_the_report_names_the_displays_whose_size_it_now_owns():
    """``capture.sources_for`` uses this to not resize the same window again."""
    ops = FakeOps([main_window(), window(2, "G1000 PFD"), window(3, "G1000 MFD", x=80)])
    report = run(config(), ops, FakeCommander(ops))

    assert report.managed == {"pfd", "mfd"}


def test_every_outcome_says_something_a_user_could_act_on():
    ops = FakeOps([main_window(), window(2, "G1000 PFD", width=640, height=480, x=7)])
    commander = FakeCommander(ops)
    report = run(config(), ops, commander)

    lines = report.lines()
    assert lines and all(line.strip() for line in lines)
    assert any("pfd" in line for line in lines)


def test_a_reopened_window_that_needs_no_move_still_counts_as_opened():
    """Otherwise the daemon never rebuilds the capture that went stale.

    X-Plane reopens a pop-out at the geometry it last had, which after any
    earlier pass is exactly the geometry being asked for -- so "opened it, and
    it needed no moving" is the likely path through here, not a corner. The
    daemon rebuilds a display's capture only when this pass *opened* its
    window; reported as merely "already" the right size, the capture still
    attached to the window the user closed would never be replaced and the
    display would stay starved for as long as the daemon ran.
    """
    ops = FakeOps([main_window()])
    ready = window(2, "G1000 PFD", width=1280, height=960, x=0, y=0)
    commander = FakeCommander(ops, opens={POPOUT_COMMANDS["pfd"]: ready})
    cfg = config(displays=(DisplayConfig(key="pfd", window_title="G1000 PFD"),))
    report = run(cfg, ops, commander)

    assert ops.placed == [], "it was already right, so nothing should have moved"
    assert report.opened("pfd"), "but the daemon still has to know it was opened"
    assert report.outcomes[0].action == "opened"


def test_a_window_nobody_opened_is_still_reported_as_already_right():
    ops = FakeOps([main_window(), window(2, "G1000 PFD", width=1280, height=960, x=0, y=0)])
    cfg = config(displays=(DisplayConfig(key="pfd", window_title="G1000 PFD"),))
    report = run(cfg, ops, FakeCommander(ops))

    assert report.outcomes[0].action == "already"
    assert not report.opened("pfd")


@pytest.mark.parametrize("sim_window_present", [True, False], ids=["with-main", "no-main"])
def test_overlapping_titles_are_only_warned_about_once_per_pass(caplog, sim_window_present):
    """Only the lookup that picks a window complains; the other two are quiet.

    A pass runs every ten seconds for as long as a display is starved, which
    is exactly when somebody is reading the log, and the same ambiguity
    reported three times reads as three problems.

    Both cases matter and only one of them used to work: with X-Plane's own
    window on the desktop the "is the sim running" probe never reaches the
    title lookup, so a test that only covered that case passed on a
    short-circuit rather than on the behaviour.
    """
    desktop = [window(2, "G1000 PFD", x=5), window(3, "G1000 PFD spare", x=6)]
    if sim_window_present:
        desktop.insert(0, main_window())
    ops = FakeOps(desktop)
    cfg = config(displays=(DisplayConfig(key="pfd", window_title="G1000 PFD"),))
    with caplog.at_level("WARNING"):
        report = run(cfg, ops, FakeCommander(ops))

    assert report.xplane_running is True
    assert sum("windows match" in r.message for r in caplog.records) == 1
