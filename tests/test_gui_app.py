"""The window itself.

Tk needs a display, so these skip where there is not one -- which includes
most CI. They are still worth having: they are what catches a tab that raises
on construction, and a widget layout Tk refuses to manage, neither of which
any amount of testing the modules underneath would find.

Run them headlessly with:  xvfb-run -a python -m pytest tests/test_gui_app.py
"""

from pathlib import Path

import pytest

from g1000_softkey.color import RED, WHITE
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
    calibrate._fields["y"].set("0.99")
    calibrate._save_and_recalibrate()
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
    calibrate._parse(0, ["[pfd] frame 1x1", "  auto-detect: x=0.1 y=0.2 w=0.3 h=0.4"])
    calibrate._apply_suggestion()
    assert calibrate._fields["x"].get() == "0.1"


def test_applying_a_suggestion_and_saving_updates_the_geometry(gui, tmp_path):
    from g1000_softkey.config import load_config

    path = tmp_path / "config.toml"
    configio.save(path, configio.default_document(), backup=False)
    gui.config_path = path
    gui.load_config(quiet=True)
    calibrate = _tab(gui, "CalibrateTab")
    calibrate.display.set("pfd")
    calibrate._fields["x"].set("0.0273")
    calibrate._fields["w"].set("0.9461")
    calibrate._save_and_recalibrate()
    geometry = load_config(path).display("pfd").geometry
    assert (geometry.x, geometry.w) == (0.0273, 0.9461)


def test_a_geometry_off_the_edge_of_the_frame_is_refused(gui, tmp_path, monkeypatch):
    errors = []
    monkeypatch.setattr("tkinter.messagebox.showerror",
                        lambda title, message, **k: errors.append(message))
    path = tmp_path / "config.toml"
    configio.save(path, configio.default_document(), backup=False)
    gui.config_path = path
    gui.load_config(quiet=True)
    calibrate = _tab(gui, "CalibrateTab")
    calibrate.display.set("pfd")
    calibrate._fields["y"].set("0.99")
    calibrate._save_and_recalibrate()
    assert errors


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
