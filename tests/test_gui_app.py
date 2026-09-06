"""The window itself.

Tk needs a display, so these skip where there is not one -- which includes
most CI. They are still worth having: they are what catches a tab that raises
on construction, and a widget layout Tk refuses to manage, neither of which
any amount of testing the modules underneath would find.

Run them headlessly with:  xvfb-run -a python -m pytest tests/test_gui_app.py
"""

from dataclasses import replace

import pytest

from g1000_softkey.color import RED, WHITE
from g1000_softkey.config import StripGeometry
from g1000_softkey.gui import commands, configio, prefs
from g1000_softkey.gui.runner import Failed, Finished, Line, Started

tk = pytest.importorskip("tkinter")


@pytest.fixture
def gui(tmp_path, monkeypatch):
    """A built window, isolated from the real preferences and checkout."""
    from g1000_softkey.gui.app import build

    monkeypatch.setattr(prefs, "prefs_path", lambda: tmp_path / "gui.json")
    monkeypatch.setattr(prefs, "project_root", lambda: tmp_path)
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - depends on the machine
        pytest.skip(f"no display available for Tk ({exc})")
    root.withdraw()
    app = build(root, None)
    yield app
    root.destroy()


def _tab(app, class_name):
    from g1000_softkey.gui import tabs

    widget = app.notebook.tabs()[tabs.tab_index(class_name)]
    return app.root.nametowidget(widget)


# -- construction ----------------------------------------------------------


def test_every_tab_builds(gui):
    from g1000_softkey.gui import tabs

    assert len(gui.notebook.tabs()) == len(tabs.TAB_CLASSES)
    for index in range(len(tabs.TAB_CLASSES)):
        gui.notebook.select(index)
        gui.root.update_idletasks()


def test_tab_titles_are_distinct(gui):
    titles = [gui.notebook.tab(i, "text").strip() for i in range(len(gui.notebook.tabs()))]
    assert len(titles) == len(set(titles))


def test_tabs_are_looked_up_by_class_not_by_position(gui):
    from g1000_softkey.gui import tabs

    for index, cls in enumerate(tabs.TAB_CLASSES):
        assert tabs.tab_index(cls.__name__) == index
    with pytest.raises(KeyError):
        tabs.tab_index("NoSuchTab")


def test_opening_with_no_config_file_falls_back_to_the_defaults(gui):
    assert gui.config_path is None
    assert gui.document["app"]["loop_hz"] == configio.default_document()["app"]["loop_hz"]
    assert "no file" in gui.config_display.get()


def test_opening_a_config_file_reads_it(tmp_path, monkeypatch):
    from g1000_softkey.gui.app import build

    monkeypatch.setattr(prefs, "prefs_path", lambda: tmp_path / "gui.json")
    monkeypatch.setattr(prefs, "project_root", lambda: tmp_path)
    path = tmp_path / "config.toml"
    path.write_text("[app]\nloop_hz = 3.5\n", encoding="utf-8")
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover
        pytest.skip(f"no display available for Tk ({exc})")
    root.withdraw()
    try:
        app = build(root, path)
        assert app.document["app"]["loop_hz"] == 3.5
        assert app.config_argument() == str(path.resolve())
    finally:
        root.destroy()


def test_an_unreadable_config_is_reported_rather_than_fatal(gui, tmp_path):
    broken = tmp_path / "broken.toml"
    broken.write_text("[app\n", encoding="utf-8")
    gui.config_path = broken
    gui.load_config()
    assert "could not be read" in gui.config_display.get()
    assert gui.document  # still usable


# -- the run tab -----------------------------------------------------------


def test_the_board_fills_in_from_the_daemons_own_output(gui):
    run = _tab(gui, "RunTab")
    run.on_event(Line("18:04:11 INFO    g1000_softkey: [pfd] 1:INSET | 2:- | 3:PFD"
                      "  bg: 3=white"))
    board = run._boards["pfd"]
    assert board._boxes[0].cget("text") == "INSET"
    assert board._boxes[1].cget("text") == ""
    from g1000_softkey.gui.widgets import CELL_COLORS

    assert board._boxes[2].cget("background") == CELL_COLORS[WHITE][0]


def test_a_warning_cell_is_drawn_as_one(gui):
    run = _tab(gui, "RunTab")
    run.on_event(Line("[pfd] 1:WARNING  bg: 1=red"))
    from g1000_softkey.gui.widgets import CELL_COLORS

    assert run._boards["pfd"]._boxes[0].cget("background") == CELL_COLORS[RED][0]


def test_a_display_that_stops_delivering_frames_is_shown_as_such(gui):
    run = _tab(gui, "RunTab")
    run._set_running(True)
    run.on_event(Line("WARNING g1000_softkey: no frames from pfd after 4s. The window must"))
    assert "No frames from pfd" in run.state_text.get()
    run.on_event(Line("INFO g1000_softkey: pfd is delivering frames again"))
    assert run.state_text.get() == "Running"


def test_the_board_clears_when_the_daemon_stops(gui):
    run = _tab(gui, "RunTab")
    run.on_event(Line("[pfd] 1:INSET"))
    run.on_event(Finished(0))
    assert run._boards["pfd"]._boxes[0].cget("text") == "--"
    assert run.state_text.get() == "Stopped"


def test_a_daemon_that_will_not_start_is_reported(gui):
    run = _tab(gui, "RunTab")
    run.on_event(Failed("could not start daemon: no such file"))
    assert "could not start" in gui.status._text.get()
    assert str(run.start_button.cget("state")) == "normal"


def test_the_exact_command_is_shown_above_the_output(gui):
    run = _tab(gui, "RunTab")
    run.on_event(Started(["python", "-m", "g1000_softkey.main", "run"]))
    assert "g1000_softkey.main run" in run.output.contents()


def test_the_run_tab_offers_debug_output(gui):
    """It was reported missing because it was only in the header block."""
    run = _tab(gui, "RunTab")
    boxes = [child for child in run.winfo_children()[0].winfo_children()
             if child.winfo_class() == "TCheckbutton"
             and "Debug" in str(child.cget("text"))]
    assert boxes, "the Run tab has no debug output checkbox"
    assert str(boxes[0].cget("variable")) == str(gui.verbose)


def test_the_two_debug_switches_are_one_setting(gui):
    """Two checkboxes that could disagree about one flag is worse than none."""
    run = _tab(gui, "RunTab")
    gui.verbose.set(True)
    boxes = [child for child in run.winfo_children()[0].winfo_children()
             if child.winfo_class() == "TCheckbutton"
             and "Debug" in str(child.cget("text"))]
    assert gui.root.getvar(str(boxes[0].cget("variable"))) in (1, True, "1")


def test_debug_output_reaches_the_daemons_command_line(gui, monkeypatch):
    started = {}
    monkeypatch.setattr(gui.daemon, "start",
                        lambda command, cwd=None: started.update(command=command))
    run = _tab(gui, "RunTab")
    gui.verbose.set(True)
    run.start()
    assert "-v" in started["command"]

    started.clear()
    gui.verbose.set(False)
    run._set_running(False)
    run.start()
    assert "-v" not in started["command"]


def test_toggling_debug_output_mid_run_says_it_needs_a_restart(gui):
    """Silently doing nothing looks exactly like a checkbox that is broken."""
    run = _tab(gui, "RunTab")
    run._set_running(True)
    gui.verbose.set(True)
    run._verbose_changed()
    assert "Start" in gui.status._text.get()

    run._set_running(False)
    run._verbose_changed()
    assert gui.status._text.get() == "Debug output on."


def test_the_output_pane_does_not_grow_without_limit(gui):
    run = _tab(gui, "RunTab")
    run.output.max_lines = 20
    for index in range(100):
        run.output.append(f"line {index}")
    assert len(run.output.contents().splitlines()) <= 20
    assert "line 99" in run.output.contents()


# -- commands --------------------------------------------------------------


def test_the_frame_source_is_passed_to_every_command_that_takes_one(gui, monkeypatch):
    started = {}
    monkeypatch.setattr(gui.task, "start", lambda command, cwd=None: started.update(
        command=command, cwd=cwd))
    gui.image_source.set("/frames")
    assert gui.run_task(commands.CALIBRATE, {"out": "out"})
    assert "--image" in started["command"] and "/frames" in started["command"]
    assert started["cwd"] == gui.project_root


def test_tune_is_not_given_the_shared_frame_source(gui, monkeypatch):
    """Its --image is one cell picture, not the whole frame."""
    started = {}
    monkeypatch.setattr(gui.task, "start", lambda command, cwd=None: started.update(
        command=command))
    gui.image_source.set("/frames")
    gui.run_task(commands.TUNE, {"image": "/cells/pfd_01_raw.png", "expect": "0"},
                 include_image=False)
    assert "/frames" not in started["command"]
    assert "/cells/pfd_01_raw.png" in started["command"]


def test_a_missing_required_option_is_refused_before_spawning(gui, monkeypatch):
    monkeypatch.setattr("tkinter.messagebox.showwarning", lambda *a, **k: None)
    spawned = []
    monkeypatch.setattr(gui.task, "start", lambda *a, **k: spawned.append(a))
    assert gui.run_task(commands.TUNE, {"image": "x.png"}, include_image=False) is False
    assert not spawned


def test_only_one_one_shot_command_runs_at_a_time(gui, monkeypatch):
    monkeypatch.setattr(type(gui.task), "is_running", property(lambda self: True))
    assert gui.run_task(commands.SYNTH, {"out": "frames"}) is False
    assert "still running" in gui.status._text.get()


def test_the_daemon_and_a_one_shot_command_are_separate_processes(gui):
    assert gui.daemon is not gui.task


# -- validating before starting --------------------------------------------


def test_a_string_where_a_number_belongs_does_not_raise_ConfigError(gui):
    """The premise of the test below, taken from the daemon rather than assumed.

    A hand-edited `loop_hz = "12"` reaches a comparison inside `from_mapping`
    and comes back out as a TypeError, which is neither a ConfigError nor a
    ValueError -- so a validator that catches only those two lets it escape.
    """
    from g1000_softkey.config import ConfigError, from_mapping

    with pytest.raises(Exception) as raised:
        from_mapping({"app": {"loop_hz": "12"}})
    assert not isinstance(raised.value, (ConfigError, ValueError))


def test_a_document_the_daemon_cannot_read_at_all_is_a_message_not_a_crash(gui):
    gui.document = {"app": {"loop_hz": "12"}}
    problem = gui.validate_document()
    assert problem
    assert "TypeError" in problem


def test_a_config_error_is_shown_as_the_sentence_it_is(gui):
    gui.document = configio.default_document()
    gui.document["app"]["loop_hz"] = 0
    assert "loop_hz" in gui.validate_document()
    assert "ConfigError" not in gui.validate_document()


def test_start_says_why_rather_than_doing_nothing(gui, monkeypatch):
    """Start went quiet: the dialog is raised from the escaping exception's place."""
    errors = []
    monkeypatch.setattr("tkinter.messagebox.showerror",
                        lambda title, message, **k: errors.append(message))
    started = []
    monkeypatch.setattr(gui.daemon, "start", lambda *a, **k: started.append(a))
    gui.document = {"app": {"loop_hz": "12"}}

    _tab(gui, "RunTab").start()

    assert errors and not started


# -- the poll loop ---------------------------------------------------------
#
# The loop is what makes every other event in this file arrive at all: the
# tests above hand a tab an event directly, so none of them notices if the
# thing that would have delivered it has stopped running. Tkinter *catches*
# an exception raised inside an `after` callback, so a reschedule written at
# the end of the callback is skipped by any failure before it and the polling
# never resumes -- silently, and with no stderr at all under pythonw.exe.


def _ticking_runner(monkeypatch, gui, ticks, events_on_tick=None):
    """Count drains of the daemon, optionally handing out an event on one."""
    def drain(limit=500):
        ticks.append(len(ticks) + 1)
        if events_on_tick and len(ticks) in events_on_tick:
            return [Line("[pfd] 1:INSET")]
        return []

    monkeypatch.setattr(gui.daemon, "drain", drain)
    monkeypatch.setattr(gui.task, "drain", lambda limit=500: [])


def test_the_poll_loop_keeps_running_when_a_handler_raises(gui, monkeypatch):
    import time

    from g1000_softkey.gui import app as app_module

    monkeypatch.setattr(app_module, "POLL_MS", 1)
    ticks = []
    _ticking_runner(monkeypatch, gui, ticks, events_on_tick={3})
    gui._daemon_sink = lambda event: (_ for _ in ()).throw(RuntimeError("handler exploded"))

    # Driven through the loop the window started for itself, and only that
    # one: calling _poll() here as well would leave a second chain of `after`
    # callbacks running, and the survivor would hide the death of the first.
    deadline = time.monotonic() + 5.0
    while len(ticks) < 20 and time.monotonic() < deadline:
        gui.root.update()
        time.sleep(0.001)

    # Before the fix this froze on the tick the handler raised on.
    assert len(ticks) >= 20


def test_a_handler_that_raises_is_said_out_loud_rather_than_swallowed(gui, monkeypatch):
    scheduled = []
    monkeypatch.setattr(gui.root, "after", lambda ms, fn: scheduled.append((ms, fn)))
    monkeypatch.setattr(gui.daemon, "drain", lambda limit=500: [Line("[pfd] 1:INSET")])
    monkeypatch.setattr(gui.task, "drain", lambda limit=500: [])
    gui._daemon_sink = lambda event: (_ for _ in ()).throw(RuntimeError("handler exploded"))

    gui._poll()

    assert gui.poll_failures == 1
    assert "handler exploded" in gui.last_poll_error
    assert "handler exploded" in gui.status._text.get()
    assert scheduled and scheduled[-1][1] == gui._poll


def test_the_next_drain_is_scheduled_even_if_the_reporting_fails(gui, monkeypatch):
    scheduled = []
    monkeypatch.setattr(gui.root, "after", lambda ms, fn: scheduled.append((ms, fn)))
    monkeypatch.setattr(gui.daemon, "drain",
                        lambda limit=500: (_ for _ in ()).throw(RuntimeError("drain exploded")))
    monkeypatch.setattr(gui, "_poll_failed",
                        lambda exc: (_ for _ in ()).throw(RuntimeError("reporting exploded")))

    with pytest.raises(RuntimeError):
        gui._poll()

    assert scheduled and scheduled[-1][1] == gui._poll


# -- settings --------------------------------------------------------------


def test_the_form_shows_a_field_for_every_setting(gui):
    from g1000_softkey.gui import schema

    settings = _tab(gui, "SettingsTab")
    described = sum(len(group.settings) for group in
                    (schema.APP, schema.OCR, schema.COLOR, schema.PUBLISH))
    per_display = len(schema.DISPLAY.settings) + len(schema.GEOMETRY.settings)
    assert len(settings._fields) == described + per_display * len(gui.display_keys())


def test_editing_a_field_and_saving_writes_the_file(gui, tmp_path):
    from g1000_softkey.config import load_config

    path = tmp_path / "config.toml"
    configio.save(path, configio.default_document(), backup=False)
    gui.config_path = path
    gui.load_config(quiet=True)

    settings = _tab(gui, "SettingsTab")
    for field_path, _setting, variable in settings._fields:
        if field_path == ("app", "loop_hz"):
            variable.set("5.5")
        if field_path == ("display", "pfd", "window_title"):
            variable.set("My PFD Window")
    settings.save()

    saved = load_config(path)
    assert saved.loop_hz == 5.5
    assert saved.display("pfd").window_title == "My PFD Window"


def test_a_value_the_daemon_would_reject_is_not_written(gui, tmp_path, monkeypatch):
    errors = []
    monkeypatch.setattr("tkinter.messagebox.showerror",
                        lambda title, message, **k: errors.append(message))
    path = tmp_path / "config.toml"
    configio.save(path, configio.default_document(), backup=False)
    gui.config_path = path
    gui.load_config(quiet=True)
    before = path.read_text(encoding="utf-8")

    settings = _tab(gui, "SettingsTab")
    for field_path, _setting, variable in settings._fields:
        if field_path == ("app", "loop_hz"):
            variable.set("0")
    settings.save()
    assert errors and path.read_text(encoding="utf-8") == before


def test_a_rejected_save_leaves_the_window_holding_the_old_settings(gui, tmp_path,
                                                                    monkeypatch):
    """Otherwise pressing Start next would use a config nothing had accepted."""
    monkeypatch.setattr("tkinter.messagebox.showerror", lambda *a, **k: None)
    path = tmp_path / "config.toml"
    configio.save(path, configio.default_document(), backup=False)
    gui.config_path = path
    gui.load_config(quiet=True)
    before = gui.document["app"]["loop_hz"]

    settings = _tab(gui, "SettingsTab")
    for field_path, _setting, variable in settings._fields:
        if field_path == ("app", "loop_hz"):
            variable.set("0")
    settings.save()
    assert gui.document["app"]["loop_hz"] == before
    assert gui.validate_document() == ""


def test_a_rejected_geometry_leaves_the_window_holding_the_old_one(gui, tmp_path,
                                                                  monkeypatch):
    monkeypatch.setattr("tkinter.messagebox.showerror", lambda *a, **k: None)
    path = tmp_path / "config.toml"
    configio.save(path, configio.default_document(), backup=False)
    gui.config_path = path
    gui.load_config(quiet=True)
    before = gui.document["display"]["pfd"]["geometry"]["y"]

    calibrate = _tab(gui, "CalibrateTab")
    calibrate.display.set("pfd")
    calibrate._geometry = StripGeometry(y=0.99, h=0.5)
    calibrate.save()
    assert gui.document["display"]["pfd"]["geometry"]["y"] == before


def test_the_board_is_rebuilt_when_the_strip_gains_or_loses_cells(gui):
    run = _tab(gui, "RunTab")
    assert run._boards["pfd"].cells == 12
    configio.set_in(gui.document, ("display", "pfd", "geometry", "cells"), 6)
    gui.notify_config_changed()
    assert run._boards["pfd"].cells == 6


def test_the_raw_editor_shows_the_file_as_it_is_on_disk(gui, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("# my notes\n[app]\nloop_hz = 2.0\n", encoding="utf-8")
    gui.config_path = path
    gui.load_config(quiet=True)
    assert "# my notes" in _tab(gui, "SettingsTab").raw.get("1.0", "end-1c")


# -- the other tabs --------------------------------------------------------


def test_the_window_list_is_parsed_and_can_be_applied(gui):
    windows = _tab(gui, "WindowsTab")
    windows._parse(0, [
        "2 of 40 visible top-level windows",
        "  hwnd=0x00010F42 pid=1234   1288x832 class='X-Plane' title='G1000 PFD (Cessna)'",
        "  hwnd=0x00010F43 pid=1234   1288x832 class='X-Plane' title='G1000 MFD (Cessna)'",
    ])
    rows = windows.tree.get_children()
    assert len(rows) == 2
    windows.tree.selection_set(rows[0])
    windows.target.set("pfd")
    windows.apply()
    assert gui.document["display"]["pfd"]["window_title"] == "G1000 PFD (Cessna)"


def test_the_calibration_suggestion_is_read_from_the_output(gui):
    calibrate = _tab(gui, "CalibrateTab")
    calibrate._parse(0, [
        "[pfd] frame 1280x800 source=image",
        "  configured : x=0.0500 y=0.9150 w=0.9000 h=0.0550",
        "  auto-detect: x=0.0273 y=0.9150 w=0.9461 h=0.0675",
        "[mfd] frame 1280x800 source=image",
        "  auto-detect: no dark softkey band found",
    ])
    assert calibrate._suggested["pfd"]["w"] == 0.9461
    assert "mfd" not in calibrate._suggested
    calibrate.display.set("pfd")
    calibrate._apply_suggestion()
    assert calibrate._fields["x"].get() == "0.0273"
    assert calibrate._geometry.w == 0.9461


def test_the_auto_detect_button_is_dead_until_there_is_a_suggestion(gui):
    calibrate = _tab(gui, "CalibrateTab")
    assert str(calibrate.auto_button.cget("state")) == "disabled"
    calibrate.display.set("pfd")
    calibrate._parse(0, ["[pfd] frame 1280x800",
                         "  auto-detect: x=0.1 y=0.2 w=0.3 h=0.4"])
    assert str(calibrate.auto_button.cget("state")) == "normal"


def test_typing_a_number_moves_the_boxes(gui):
    calibrate = _tab(gui, "CalibrateTab")
    calibrate._fields["x"].set("0.25")
    calibrate._from_fields()
    assert calibrate._geometry.x == 0.25
    assert calibrate.picture._geometry.x == 0.25
    assert calibrate.closeup._geometry.x == 0.25


def test_moving_the_boxes_updates_the_numbers(gui):
    calibrate = _tab(gui, "CalibrateTab")
    calibrate.set_geometry(replace(calibrate._geometry, y=0.5))
    assert calibrate._fields["y"].get() == "0.5"


def test_a_number_that_is_not_a_number_is_refused_not_swallowed(gui):
    calibrate = _tab(gui, "CalibrateTab")
    before = calibrate._geometry
    calibrate._fields["x"].set("left a bit")
    calibrate._from_fields()
    assert calibrate._geometry == before
    assert "not a number" in gui.status._text.get()


def test_saving_writes_the_geometry_that_is_on_screen(gui, tmp_path):
    from g1000_softkey.config import load_config

    path = tmp_path / "config.toml"
    configio.save(path, configio.default_document(), backup=False)
    gui.config_path = path
    gui.load_config(quiet=True)
    calibrate = _tab(gui, "CalibrateTab")
    calibrate.display.set("pfd")
    calibrate.set_geometry(replace(calibrate._geometry, x=0.0273, w=0.9461))
    calibrate.save()
    geometry = load_config(path).display("pfd").geometry
    assert (geometry.x, geometry.w) == (0.0273, 0.9461)


def test_a_geometry_off_the_edge_of_the_frame_is_refused(gui, tmp_path, monkeypatch):
    """Clamping should make this unreachable; the check is the backstop."""
    errors = []
    monkeypatch.setattr("tkinter.messagebox.showerror",
                        lambda title, message, **k: errors.append(message))
    path = tmp_path / "config.toml"
    configio.save(path, configio.default_document(), backup=False)
    gui.config_path = path
    gui.load_config(quiet=True)
    calibrate = _tab(gui, "CalibrateTab")
    calibrate.display.set("pfd")
    calibrate._geometry = StripGeometry(y=0.99, h=0.5)
    calibrate.save()
    assert errors


# -- one calibration for both displays -------------------------------------


@pytest.fixture
def calibrated(gui, tmp_path):
    """The Calibrate tab, with a real config file open."""
    path = tmp_path / "config.toml"
    configio.save(path, configio.default_document(), backup=False)
    gui.config_path = path
    gui.load_config(quiet=True)
    return gui, _tab(gui, "CalibrateTab"), path


def test_a_fresh_configuration_links_the_displays(calibrated):
    _gui, calibrate, _path = calibrated
    assert calibrate.follow.get() is True
    assert calibrate.source_display() == "pfd"
    assert calibrate.follower_displays() == ["mfd"]


def test_the_follower_shows_a_panel_instead_of_the_editor(calibrated):
    _gui, calibrate, _path = calibrated
    calibrate.display.set("mfd")
    calibrate.refresh()
    assert calibrate.is_following()
    assert calibrate.following_panel.winfo_manager() == "pack"
    assert calibrate.editor.winfo_manager() == ""
    assert "PFD" in calibrate.following_title.cget("text")


def test_the_source_display_still_gets_the_editor(calibrated):
    _gui, calibrate, _path = calibrated
    calibrate.display.set("pfd")
    calibrate.refresh()
    assert not calibrate.is_following()
    assert calibrate.editor.winfo_manager() == "pack"


def test_saving_the_source_writes_the_follower_too(calibrated):
    from g1000_softkey.config import load_config

    _gui, calibrate, path = calibrated
    calibrate.display.set("pfd")
    calibrate.refresh()
    assert calibrate.save_targets() == ["pfd", "mfd"]
    calibrate.set_geometry(replace(StripGeometry(), x=0.0273, w=0.9461))
    calibrate.save()

    config = load_config(path)
    assert config.display("pfd").geometry == config.display("mfd").geometry
    assert config.display("mfd").geometry.x == 0.0273


def test_the_save_button_says_where_it_is_going(calibrated):
    _gui, calibrate, _path = calibrated
    calibrate.display.set("pfd")
    calibrate.refresh()
    assert "PFD and MFD" in calibrate.save_button.cget("text")
    calibrate._stop_following()
    assert calibrate.save_button.cget("text") == "Save to the configuration"


def test_unticking_gives_the_follower_its_own_calibration(calibrated):
    from g1000_softkey.config import load_config

    _gui, calibrate, path = calibrated
    calibrate.display.set("pfd")
    calibrate.refresh()
    calibrate.set_geometry(replace(StripGeometry(), x=0.0273))
    calibrate.save()

    calibrate.display.set("mfd")
    calibrate.refresh()
    calibrate._stop_following()
    assert not calibrate.is_following()
    assert calibrate.editor.winfo_manager() == "pack"
    assert calibrate.save_targets() == ["mfd"]

    calibrate.set_geometry(replace(StripGeometry(), x=0.10))
    calibrate.save()
    config = load_config(path)
    assert config.display("pfd").geometry.x == 0.0273
    assert config.display("mfd").geometry.x == 0.10


def test_the_choice_is_remembered(calibrated):
    gui, calibrate, _path = calibrated
    calibrate._stop_following()
    assert gui.prefs["follow_first_display"] is False
    calibrate.follow.set(True)
    calibrate._follow_changed()
    assert gui.prefs["follow_first_display"] is True


def test_a_configuration_already_calibrated_apart_is_not_linked(gui, tmp_path):
    """The safety net: with no stored choice, the numbers decide.

    Somebody who calibrated their MFD separately before this existed must not
    have that work quietly overwritten the first time they open the window.
    """
    document = configio.default_document()
    configio.set_geometry(document, "mfd", StripGeometry(x=0.2, y=0.8, w=0.5, h=0.1))
    path = tmp_path / "config.toml"
    configio.save(path, document, backup=False)
    gui.prefs["follow_first_display"] = None
    gui.config_path = path
    gui.load_config(quiet=True)

    calibrate = _tab(gui, "CalibrateTab")
    assert calibrate.follow.get() is False
    calibrate.display.set("mfd")
    calibrate.refresh()
    assert not calibrate.is_following()


def test_a_stored_choice_beats_the_guess(gui, tmp_path):
    document = configio.default_document()
    configio.set_geometry(document, "mfd", StripGeometry(x=0.2, w=0.5))
    path = tmp_path / "config.toml"
    configio.save(path, document, backup=False)
    gui.prefs["follow_first_display"] = True
    gui.config_path = path
    gui.load_config(quiet=True)
    assert _tab(gui, "CalibrateTab").follow.get() is True


def test_the_editing_buttons_are_off_while_following(calibrated):
    """They would act on an editor that is not on screen."""
    _gui, calibrate, _path = calibrated
    calibrate.display.set("mfd")
    calibrate.refresh()
    assert str(calibrate.draw_button.cget("state")) == "disabled"
    calibrate.display.set("pfd")
    calibrate.refresh()
    assert str(calibrate.draw_button.cget("state")) == "normal"


def test_a_single_display_configuration_offers_no_checkbox(gui, tmp_path):
    document = configio.default_document()
    del document["display"]["mfd"]
    path = tmp_path / "config.toml"
    configio.save(path, document, backup=False)
    gui.config_path = path
    gui.load_config(quiet=True)
    calibrate = _tab(gui, "CalibrateTab")
    assert calibrate.follower_displays() == []
    assert calibrate.follow_box.winfo_manager() == ""
    assert calibrate.save_targets() == ["pfd"]


def test_the_colour_table_is_parsed(gui):
    colors = _tab(gui, "ColorsTab")
    colors._parse(0, [
        "[pfd]  ring = outer 15% of each cell",
        " cell     B   G   R      H   S   V   class    bg",
        "    1    10  10  10      0   0  10   black      0",
        "    2   250 250 250      0   0 250   white      1",
    ])
    rows = colors.tree.get_children()
    assert len(rows) == 2
    assert colors.tree.item(rows[1], "values")[4] == "white"


def test_a_recorded_page_can_be_appended_to_the_pages_file(gui, tmp_path, monkeypatch):
    monkeypatch.setattr("tkinter.messagebox.askokcancel", lambda *a, **k: True)
    pages = _tab(gui, "PagesTab")
    screens = tmp_path / "screens.toml"
    screens.write_text("# pages\n", encoding="utf-8")
    configio.set_in(gui.document, ("ocr", "screens_file"), str(screens))
    pages._captured(0, [
        "some preamble that is not part of the block",
        "[[screen]]",
        'name = "xpdr-code"',
        'display = "pfd"',
        'labels = { 1 = "0", 2 = "1" }',
        "",
    ])
    assert pages._block[0] == "[[screen]]"
    pages.append_page()
    text = screens.read_text(encoding="utf-8")
    assert 'name = "xpdr-code"' in text
    assert "preamble" not in text
    # It appends: whatever was already recorded stays.
    assert text.startswith("# pages")


def test_the_vocabulary_tab_edits_the_file_the_daemon_reads(gui, tmp_path):
    labels = tmp_path / "labels.txt"
    labels.write_text("INSET\n", encoding="utf-8")
    configio.set_in(gui.document, ("ocr", "labels_file"), str(labels))
    vocabulary = _tab(gui, "VocabularyTab")
    vocabulary.reload()
    assert vocabulary.text.get("1.0", "end-1c").strip() == "INSET"
    vocabulary.text.insert("end", "NEW LABEL\n")
    vocabulary.save()
    assert "NEW LABEL" in labels.read_text(encoding="utf-8")


def test_a_relative_path_in_the_config_resolves_beside_the_config(gui, tmp_path):
    from g1000_softkey.gui.tabs import config_path_setting

    gui.config_path = tmp_path / "config.toml"
    configio.set_in(gui.document, ("ocr", "labels_file"), "my-labels.txt")
    assert config_path_setting(gui, "labels_file") == tmp_path / "my-labels.txt"


def test_the_walkthrough_only_points_at_tabs_that_exist(gui):
    from g1000_softkey.gui import tabs

    for _title, _text, target in tabs.STEPS:
        if target:
            assert 0 <= tabs.tab_index(target) < len(tabs.TAB_CLASSES)
