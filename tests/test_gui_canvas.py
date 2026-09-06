"""The calibration editor as it is actually used: with a mouse.

These drive real Tk events against a mapped window, because the thing worth
checking is not that the arithmetic works -- ``test_gui_geometry.py`` does
that -- but that a press, a drag and a release land where the pointer was.

They need a display, and skip without one:
    xvfb-run -a python -m pytest tests/test_gui_canvas.py
"""

from dataclasses import replace

import pytest

from g1000_softkey.config import StripGeometry
from g1000_softkey.gui import geometry as geo
from g1000_softkey.gui import prefs, tabs
from g1000_softkey.gui.widgets import CELL_COLOR
from g1000_softkey.strip import cell_rects

tk = pytest.importorskip("tkinter")

FRAME_W, FRAME_H = 1280, 800


@pytest.fixture
def editor(tmp_path, monkeypatch):
    """A mapped window on the Calibrate tab, with a picture loaded."""
    from PIL import Image

    from g1000_softkey.gui.app import build

    monkeypatch.setattr(prefs, "prefs_path", lambda: tmp_path / "gui.json")
    monkeypatch.setattr(prefs, "project_root", lambda: tmp_path)
    out = tmp_path / "calibration"
    out.mkdir()
    # Any picture will do: the canvas is being asked where the pointer was,
    # not what the pixels are.
    Image.new("RGB", (FRAME_W, FRAME_H), (40, 40, 40)).save(out / "pfd_raw.png")

    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - depends on the machine
        pytest.skip(f"no display available for Tk ({exc})")
    root.geometry("1200x900+0+0")
    app = build(root, None)
    calibrate = root.nametowidget(app.notebook.tabs()[tabs.tab_index("CalibrateTab")])
    app.notebook.select(tabs.tab_index("CalibrateTab"))
    calibrate.out.set(str(out))
    calibrate.display.set("pfd")
    calibrate.refresh()
    root.update()
    if calibrate.picture.view() is None:  # pragma: no cover - a window manager oddity
        root.destroy()
        pytest.skip("the canvas was never given a size")
    yield app, calibrate
    root.destroy()


def _drag(canvas, root, start, end):
    canvas.event_generate("<Button-1>", x=int(start[0]), y=int(start[1]))
    root.update()
    canvas.event_generate("<B1-Motion>", x=int(end[0]), y=int(end[1]))
    root.update()
    canvas.event_generate("<ButtonRelease-1>", x=int(end[0]), y=int(end[1]))
    root.update()


def _canvas_point(view, frame_x, frame_y):
    return view.to_canvas(frame_x, frame_y)


# -- drawing ---------------------------------------------------------------


def test_drawing_a_box_with_the_mouse_sets_the_geometry(editor):
    app, calibrate = editor
    calibrate._arm_draw()
    view = calibrate.picture.view()
    _drag(calibrate.picture.canvas, app.root,
          _canvas_point(view, 60, 725), _canvas_point(view, 1219, 781))

    x, y, w, h = geo.strip_pixels(calibrate._geometry, FRAME_W, FRAME_H)
    # Within a pixel or two: the picture is scaled to fit, so a canvas pixel
    # is worth rather more than a frame pixel and the click quantises.
    assert x == pytest.approx(60, abs=3)
    assert y == pytest.approx(725, abs=3)
    assert x + w == pytest.approx(1219, abs=3)
    assert y + h == pytest.approx(781, abs=3)


def test_the_numbers_follow_the_mouse(editor):
    app, calibrate = editor
    calibrate._arm_draw()
    view = calibrate.picture.view()
    _drag(calibrate.picture.canvas, app.root,
          _canvas_point(view, 100, 700), _canvas_point(view, 900, 760))
    assert float(calibrate._fields["x"].get()) == pytest.approx(100 / FRAME_W, abs=0.005)
    assert float(calibrate._fields["y"].get()) == pytest.approx(700 / FRAME_H, abs=0.005)


def test_a_box_can_be_drawn_backwards(editor):
    app, calibrate = editor
    calibrate._arm_draw()
    view = calibrate.picture.view()
    _drag(calibrate.picture.canvas, app.root,
          _canvas_point(view, 900, 760), _canvas_point(view, 100, 700))
    x, y, _w, _h = geo.strip_pixels(calibrate._geometry, FRAME_W, FRAME_H)
    assert x == pytest.approx(100, abs=3)
    assert y == pytest.approx(700, abs=3)


def test_a_press_near_the_box_resizes_it_rather_than_starting_a_new_one(editor):
    """Which is why "Draw a new box" has to be a button.

    With a box already on screen there is nowhere left to start a fresh drag:
    near an edge is a resize, inside is a move. That is the right behaviour
    for an editor and it is exactly why the arming button exists.
    """
    app, calibrate = editor
    calibrate.set_geometry(StripGeometry())
    view = calibrate.picture.view()
    x, y, _w, _h = geo.strip_pixels(calibrate._geometry, FRAME_W, FRAME_H)
    _drag(calibrate.picture.canvas, app.root,
          _canvas_point(view, x, y), _canvas_point(view, x + 40, y + 6))

    moved = geo.strip_pixels(calibrate._geometry, FRAME_W, FRAME_H)
    assert moved[0] > x                       # the corner was dragged
    assert moved[0] + moved[2] == pytest.approx(x + _w, abs=3)   # the far edge held


def test_arming_overrides_that(editor):
    app, calibrate = editor
    calibrate.set_geometry(StripGeometry())
    calibrate._arm_draw()
    assert calibrate.picture.armed
    view = calibrate.picture.view()
    x, y, _w, _h = geo.strip_pixels(calibrate._geometry, FRAME_W, FRAME_H)
    _drag(calibrate.picture.canvas, app.root,
          _canvas_point(view, x, y), _canvas_point(view, x + 40, y + 6))
    assert not calibrate.picture.armed
    drawn = geo.strip_pixels(calibrate._geometry, FRAME_W, FRAME_H)
    assert drawn[2] == pytest.approx(40, abs=4)   # a small new box, not a resize


def test_dragging_inside_the_box_slides_it_without_resizing(editor):
    app, calibrate = editor
    calibrate.set_geometry(StripGeometry())
    before = geo.strip_pixels(calibrate._geometry, FRAME_W, FRAME_H)
    view = calibrate.picture.view()
    centre = (before[0] + before[2] / 2, before[1] + before[3] / 2)
    _drag(calibrate.picture.canvas, app.root,
          _canvas_point(view, *centre), _canvas_point(view, centre[0], centre[1] - 20))
    after = geo.strip_pixels(calibrate._geometry, FRAME_W, FRAME_H)
    assert (after[2], after[3]) == (before[2], before[3])
    assert after[1] < before[1]


# -- the coupling ----------------------------------------------------------


def _drawn_cells(canvas_widget):
    """The green rectangles on the canvas, converted back to frame pixels."""
    canvas = canvas_widget.canvas
    view = canvas_widget.view()
    found = []
    for item in canvas.find_all():
        if canvas.type(item) != "rectangle":
            continue
        if canvas.itemcget(item, "outline") != CELL_COLOR:
            continue
        x0, y0, x1, y1 = canvas.coords(item)
        fx0, fy0 = view.to_frame(x0, y0)
        fx1, fy1 = view.to_frame(x1, y1)
        found.append((round(fx0), round(fy0), round(fx1 - fx0), round(fy1 - fy0)))
    return sorted(found)


@pytest.mark.parametrize("geometry", [
    StripGeometry(),
    StripGeometry(x=0.0273, y=0.915, w=0.9461, h=0.0675),
    StripGeometry(x=0.1, y=0.5, w=0.6, h=0.2, cell_pad_x=0.25, cell_pad_y=0.3),
    StripGeometry(x=0.0, y=0.0, w=1.0, h=0.5, cells=6, cell_pad_x=0.0, cell_pad_y=0.0),
])
def test_the_green_boxes_are_the_ones_split_cells_slices(editor, geometry):
    """The whole tab is worthless if these disagree.

    The user would line the boxes up carefully against a picture that was not
    telling the truth about where the reader is going to look. So the canvas
    does not compute cell rectangles; it draws what ``strip.cell_rects``
    returns, and this is the test that says so.
    """
    _app, calibrate = editor
    calibrate.set_geometry(geometry)
    calibrate.app.root.update()

    real = sorted(
        (r.x, r.y, r.w, r.h) for r in cell_rects((FRAME_H, FRAME_W, 3), geometry)
    )
    drawn = _drawn_cells(calibrate.picture)
    assert len(drawn) == geometry.cells
    for got, expected in zip(drawn, real):
        for value, want in zip(got, expected):
            assert value == pytest.approx(want, abs=2)


def test_the_close_up_draws_the_same_cells_as_the_picture(editor):
    _app, calibrate = editor
    calibrate.set_geometry(StripGeometry())
    calibrate.app.root.update()
    assert len(_drawn_cells(calibrate.closeup)) == StripGeometry().cells


# -- the steps -------------------------------------------------------------


def test_every_step_moves_something_that_exists(editor):
    """The buttons, the arrow keys and the panel all come from one table."""
    _app, calibrate = editor
    for step in tabs.CALIBRATION_STEPS:
        assert step["focus"] in ("strip", "topleft", "bottomright")
        assert step["zoom"] in ("Fit", "2x", "4x", "8x", "16x")
        for _label, target, _minus, _plus in step["controls"]:
            before = calibrate._geometry
            calibrate._nudge(target, 1)
            assert calibrate._geometry != before, target
        for field in step["fields"]:
            assert field in calibrate._fields


def test_the_close_up_follows_the_step(editor):
    _app, calibrate = editor
    calibrate.step.set(0)
    calibrate._show_step()
    assert calibrate.closeup.focus == "topleft"
    calibrate.next_step()
    assert calibrate.closeup.focus == "bottomright"
    calibrate.next_step()
    assert calibrate.closeup.focus == "strip"


def test_the_steps_stop_at_both_ends(editor):
    _app, calibrate = editor
    calibrate.step.set(0)
    calibrate.previous_step()
    assert calibrate.step.get() == 0
    for _ in range(10):
        calibrate.next_step()
    assert calibrate.step.get() == len(tabs.CALIBRATION_STEPS) - 1


def test_the_arrow_keys_nudge_whatever_the_step_is_about(editor):
    app, calibrate = editor
    calibrate.step.set(0)
    calibrate._show_step()
    before = geo.strip_pixels(calibrate._geometry, FRAME_W, FRAME_H)
    event = type("E", (), {"widget": calibrate.picture.canvas})()
    calibrate._arrow(event, 0, 1)          # Right: the left edge, on step 1
    after = geo.strip_pixels(calibrate._geometry, FRAME_W, FRAME_H)
    assert after[0] == before[0] + 1
    assert app.notebook.select() == str(calibrate)


def test_the_arrow_keys_keep_out_of_entry_boxes(editor):
    """bind_all reaches every widget, including the ones being typed in."""
    from tkinter import ttk

    _app, calibrate = editor
    before = calibrate._geometry
    entry = ttk.Entry(calibrate)
    calibrate._arrow(type("E", (), {"widget": entry})(), 0, 1)
    assert calibrate._geometry == before


def test_zoom_changes_how_much_of_the_frame_the_close_up_shows(editor):
    _app, calibrate = editor
    calibrate.set_geometry(StripGeometry())
    calibrate.zoom.set("2x")
    calibrate._apply_zoom()
    wide = calibrate.closeup._source_rect()
    calibrate.zoom.set("8x")
    calibrate._apply_zoom()
    close = calibrate.closeup._source_rect()
    assert close[2] < wide[2]


# -- the clipping warning --------------------------------------------------


def _real_frame(calibrate, tmp_path):
    """Swap the blank test picture for a synthetic strip with real labels."""
    from g1000_softkey import synth
    from g1000_softkey.gui import checks

    frames = tmp_path / "frames"
    frames.mkdir(exist_ok=True)
    synth.write_menus(frames)
    calibrate._frame = checks.load_frame(frames / "pfd_menu.png")
    return calibrate


def test_an_over_trimmed_crop_warns_and_names_the_cells(editor, tmp_path):
    calibrate = _real_frame(editor[1], tmp_path)
    calibrate.set_geometry(replace(StripGeometry(), cell_pad_x=0.30, cell_pad_y=0.35))
    clips = calibrate._check_clipping()
    assert len(clips) >= 6
    assert "may be clipped" in calibrate.clip_warning.cget("text")


def test_the_warned_cells_are_the_amber_ones(editor, tmp_path):
    calibrate = _real_frame(editor[1], tmp_path)
    calibrate.set_geometry(replace(StripGeometry(), cell_pad_x=0.30, cell_pad_y=0.35))
    clips = calibrate._check_clipping()
    assert calibrate.picture._warned == frozenset(c.cell for c in clips)
    assert calibrate.closeup._warned == calibrate.picture._warned


def test_a_reasonable_crop_says_nothing(editor, tmp_path):
    calibrate = _real_frame(editor[1], tmp_path)
    calibrate.set_geometry(StripGeometry())
    calibrate._check_clipping()
    assert calibrate.clip_warning.cget("text") == ""
    assert calibrate.picture._warned == frozenset()


def test_saving_a_clipped_geometry_asks_first(editor, tmp_path, monkeypatch):
    from g1000_softkey.gui import configio

    app, calibrate = editor
    _real_frame(calibrate, tmp_path)
    path = tmp_path / "config.toml"
    configio.save(path, configio.default_document(), backup=False)
    app.config_path = path
    app.load_config(quiet=True)
    _real_frame(calibrate, tmp_path)

    asked = {}
    monkeypatch.setattr("tkinter.messagebox.askokcancel",
                        lambda title, message, **k: asked.setdefault("message", message) and False)
    calibrate.display.set("pfd")
    calibrate.set_geometry(replace(StripGeometry(), cell_pad_x=0.30, cell_pad_y=0.35))
    calibrate.save()
    assert "may be clipped" in asked.get("message", "")
    # Cancelled, so nothing was written.
    assert app.document["display"]["pfd"]["geometry"]["cell_pad_x"] != 0.30


def test_saying_yes_saves_it_anyway(editor, tmp_path, monkeypatch):
    """Asked, not refused: the check cannot tell a clipped glyph from a label
    that fills its cell, and a block would train people to work around it."""
    from g1000_softkey.config import load_config
    from g1000_softkey.gui import configio

    app, calibrate = editor
    path = tmp_path / "config.toml"
    configio.save(path, configio.default_document(), backup=False)
    app.config_path = path
    app.load_config(quiet=True)
    _real_frame(calibrate, tmp_path)

    monkeypatch.setattr("tkinter.messagebox.askokcancel", lambda *a, **k: True)
    calibrate.display.set("pfd")
    calibrate.set_geometry(replace(StripGeometry(), cell_pad_x=0.30, cell_pad_y=0.35))
    calibrate.save()
    assert load_config(path).display("pfd").geometry.cell_pad_x == 0.30


def test_a_clean_save_is_not_interrupted(editor, tmp_path, monkeypatch):
    from g1000_softkey.config import load_config
    from g1000_softkey.gui import configio

    app, calibrate = editor
    path = tmp_path / "config.toml"
    configio.save(path, configio.default_document(), backup=False)
    app.config_path = path
    app.load_config(quiet=True)
    _real_frame(calibrate, tmp_path)

    monkeypatch.setattr("tkinter.messagebox.askokcancel",
                        lambda *a, **k: pytest.fail("asked about a crop that is fine"))
    calibrate.display.set("pfd")
    calibrate.set_geometry(replace(StripGeometry(), x=0.05, y=0.915, w=0.9, h=0.055))
    calibrate.save()
    assert load_config(path).display("pfd").geometry.w == 0.9


def test_the_check_is_debounced_rather_than_run_on_every_drag(editor):
    """Reading twelve crops costs milliseconds; a drag moves faster than that."""
    _app, calibrate = editor
    calibrate.set_geometry(replace(StripGeometry(), x=0.06))
    assert calibrate._clip_check is not None      # scheduled, not run


def test_the_editor_says_what_the_numbers_mean_in_pixels(editor):
    _app, calibrate = editor
    calibrate.set_geometry(replace(StripGeometry(), x=0.05, y=0.915, w=0.9, h=0.055))
    assert "1280x800" in calibrate.pixels.cget("text")
    assert "1152x44" in calibrate.pixels.cget("text")
