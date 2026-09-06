"""The calibration editor's coordinate arithmetic.

Getting this wrong is silent: the boxes would still be drawn, just not where
the reader will actually look, and the user would calibrate carefully against
a picture that was lying to them. So the mapping is checked against
``strip.py`` -- the code that does the real cropping -- rather than against
itself.
"""

import pytest

from g1000_softkey.config import ConfigError, StripGeometry
from g1000_softkey.gui import geometry as geo
from g1000_softkey.strip import cell_rects, strip_rect

FRAME = (1280, 800)


# -- agreement with the daemon ---------------------------------------------


@pytest.mark.parametrize("g", [
    StripGeometry(),
    StripGeometry(x=0.0, y=0.0, w=1.0, h=1.0),
    StripGeometry(x=0.0273, y=0.915, w=0.9461, h=0.0675),
    StripGeometry(x=0.5, y=0.5, w=0.25, h=0.25, cells=6),
])
def test_the_editors_strip_is_the_strip_the_daemon_crops(g):
    width, height = FRAME
    rect = strip_rect((height, width, 3), g)
    assert geo.strip_pixels(g, width, height) == (rect.x, rect.y, rect.w, rect.h)


def test_a_drawn_box_comes_back_as_the_pixels_it_was_drawn_on():
    """What you drag out is what gets cropped, to the pixel."""
    width, height = FRAME
    result = geo.geometry_from_pixels(StripGeometry(), 60, 725, 1219, 781, width, height)
    assert geo.strip_pixels(result, width, height) == (60, 725, 1159, 56)


def test_a_box_drawn_backwards_is_the_same_box():
    width, height = FRAME
    forwards = geo.geometry_from_pixels(StripGeometry(), 60, 725, 1219, 781, width, height)
    backwards = geo.geometry_from_pixels(StripGeometry(), 1219, 781, 60, 725, width, height)
    assert geo.strip_pixels(forwards, *FRAME) == geo.strip_pixels(backwards, *FRAME)


def test_drawing_keeps_the_padding_it_was_given():
    template = StripGeometry(cell_pad_x=0.2, cell_pad_y=0.3, cells=6)
    drawn = geo.geometry_from_pixels(template, 10, 10, 100, 50, *FRAME)
    assert (drawn.cell_pad_x, drawn.cell_pad_y, drawn.cells) == (0.2, 0.3, 6)


# -- clamping ---------------------------------------------------------------


@pytest.mark.parametrize("g", [
    StripGeometry(x=-5.0, y=-5.0),
    StripGeometry(x=0.9, w=0.5),
    StripGeometry(y=0.99, h=0.5),
    StripGeometry(w=-1.0, h=-1.0),
    StripGeometry(cell_pad_x=0.9, cell_pad_y=0.9),
    StripGeometry(x=2.0, y=2.0, w=2.0, h=2.0),
])
def test_anything_clamped_is_something_the_daemon_will_load(g):
    """Clamped rather than refused: this runs on every mouse move, and a
    dialog box per stray drag would be unusable."""
    geo.clamp(g).validate()


def test_clamping_leaves_a_good_geometry_alone():
    good = StripGeometry(x=0.05, y=0.915, w=0.9, h=0.055)
    assert geo.clamp(good) == good


def test_a_box_dragged_off_the_edge_stops_at_the_edge():
    dragged = geo.geometry_from_pixels(StripGeometry(), -400, -400, 4000, 4000, *FRAME)
    dragged.validate()
    assert (dragged.x, dragged.y) == (0.0, 0.0)
    assert dragged.x + dragged.w == pytest.approx(1.0)


def test_the_daemon_would_reject_what_clamping_prevents():
    """The invariants are not this module's invention."""
    with pytest.raises(ConfigError):
        StripGeometry(x=0.9, w=0.5).validate()


# -- nudging ----------------------------------------------------------------


def test_moving_the_left_edge_holds_the_right_one():
    """Otherwise nudging one edge slides the box and undoes the other."""
    width, height = FRAME
    before = geo.strip_pixels(StripGeometry(), width, height)
    after = geo.strip_pixels(
        geo.nudge_edge(StripGeometry(), "left", 3, width, height), width, height
    )
    assert after[0] == before[0] + 3
    assert after[0] + after[2] == before[0] + before[2]


def test_moving_the_top_edge_holds_the_bottom_one():
    width, height = FRAME
    before = geo.strip_pixels(StripGeometry(), width, height)
    after = geo.strip_pixels(
        geo.nudge_edge(StripGeometry(), "top", 4, width, height), width, height
    )
    assert after[1] == before[1] + 4
    assert after[1] + after[3] == before[1] + before[3]


@pytest.mark.parametrize("edge,index", [("right", 0), ("bottom", 1)])
def test_moving_the_far_edges_holds_the_near_ones(edge, index):
    width, height = FRAME
    before = geo.strip_pixels(StripGeometry(), width, height)
    after = geo.strip_pixels(
        geo.nudge_edge(StripGeometry(), edge, -5, width, height), width, height
    )
    assert after[index] == before[index]
    assert after[index + 2] == before[index + 2] - 5


def test_a_nudge_is_a_frame_pixel_not_a_screen_one():
    """The judgement is made in the capture's pixels, so that is the unit."""
    one = geo.nudge_edge(StripGeometry(), "left", 1, 1280, 800)
    assert one.x - StripGeometry().x == pytest.approx(1 / 1280)


def test_nudging_cannot_push_the_box_out_of_bounds():
    g = StripGeometry()
    for _ in range(5000):
        g = geo.nudge_edge(g, "left", -1, *FRAME)
    g.validate()
    assert g.x == 0.0


def test_nudging_cannot_shrink_the_box_to_nothing():
    g = StripGeometry()
    for _ in range(5000):
        g = geo.nudge_edge(g, "right", -1, *FRAME)
    g.validate()
    assert g.w >= geo.MIN_SPAN


def test_an_unknown_edge_is_a_programming_error():
    with pytest.raises(KeyError):
        geo.nudge_edge(StripGeometry(), "diagonal", 1, *FRAME)


def test_padding_nudges_and_stops_short_of_swallowing_the_cell():
    g = StripGeometry()
    assert geo.nudge_padding(g, "x", 1).cell_pad_x == pytest.approx(g.cell_pad_x + 0.01)
    for _ in range(500):
        g = geo.nudge_padding(g, "x", 1)
    assert g.cell_pad_x <= geo.MAX_PAD
    g.validate()


def test_moving_the_whole_strip_keeps_its_size():
    moved = geo.move(StripGeometry(), 10, -4, *FRAME)
    before = geo.strip_pixels(StripGeometry(), *FRAME)
    after = geo.strip_pixels(moved, *FRAME)
    assert (after[2], after[3]) == (before[2], before[3])
    assert (after[0], after[1]) == (before[0] + 10, before[1] - 4)


# -- the view ---------------------------------------------------------------


def test_a_view_round_trips_a_point():
    view = geo.fit_view((0, 0, 1280, 800), 900, 500)
    x, y = view.to_frame(*view.to_canvas(640, 400))
    assert (x, y) == pytest.approx((640, 400))


def test_a_view_of_a_region_round_trips_a_point():
    view = geo.fit_view((60, 720, 200, 60), 900, 300)
    x, y = view.to_frame(*view.to_canvas(100, 740))
    assert (x, y) == pytest.approx((100, 740))


def test_fitting_preserves_the_aspect_ratio_and_centres():
    view = geo.fit_view((0, 0, 100, 100), 400, 200)
    assert view.scale == pytest.approx(2.0)
    assert view.offset_x == pytest.approx(100.0)
    assert view.offset_y == pytest.approx(0.0)


def test_a_degenerate_canvas_does_not_divide_by_zero():
    view = geo.fit_view((0, 0, 1280, 800), 0, 0)
    assert view.scale > 0
    view.to_frame(0, 0)


# -- handles ----------------------------------------------------------------


def test_there_are_eight_handles_and_they_sit_on_the_rectangle():
    points = geo.handle_points(10, 20, 100, 40)
    assert len(points) == 8
    assert points["nw"] == (10, 20)
    assert points["se"] == (110, 60)
    assert points["n"] == (60, 20)


def test_the_pointer_finds_the_handle_it_is_on():
    rect = (10, 20, 110, 60)
    assert geo.handle_at(10, 20, rect) == "nw"
    assert geo.handle_at(112, 62, rect) == "se"
    assert geo.handle_at(60, 40, rect) is None


def test_dragging_a_corner_leaves_the_opposite_corner_alone():
    width, height = FRAME
    g = geo.geometry_from_pixels(StripGeometry(), 100, 100, 500, 300, width, height)
    dragged = geo.drag_handle(g, "nw", 120, 140, width, height)
    x, y, w, h = geo.strip_pixels(dragged, width, height)
    assert (x, y) == (120, 140)
    assert (x + w, y + h) == (500, 300)


def test_dragging_a_side_handle_moves_only_that_edge():
    width, height = FRAME
    g = geo.geometry_from_pixels(StripGeometry(), 100, 100, 500, 300, width, height)
    dragged = geo.drag_handle(g, "e", 460, 999, width, height)
    x, y, w, h = geo.strip_pixels(dragged, width, height)
    assert (x, y, y + h) == (100, 100, 300)
    assert x + w == 460


def test_dragging_a_corner_past_its_opposite_does_not_invert_the_box():
    width, height = FRAME
    g = geo.geometry_from_pixels(StripGeometry(), 100, 100, 500, 300, width, height)
    dragged = geo.drag_handle(g, "nw", 900, 900, width, height)
    dragged.validate()
    assert dragged.w > 0 and dragged.h > 0


def test_inside_knows_where_the_box_is():
    assert geo.inside(50, 50, (10, 10, 100, 100))
    assert not geo.inside(5, 50, (10, 10, 100, 100))


# -- the coupling that matters ---------------------------------------------


def test_every_step_of_a_calibration_still_agrees_with_split_cells():
    """Draw, nudge every edge, pad -- and the cells are still the real ones."""
    width, height = FRAME
    g = geo.geometry_from_pixels(StripGeometry(), 60, 725, 1219, 781, width, height)
    for edge in ("left", "top", "right", "bottom"):
        g = geo.nudge_edge(g, edge, 2, width, height)
    g = geo.nudge_padding(g, "x", 1)
    g = geo.nudge_padding(g, "y", -1)
    g.validate()

    rects = cell_rects((height, width, 3), g)
    assert len(rects) == g.cells
    strip = geo.strip_pixels(g, width, height)
    for rect in rects:
        assert rect.x >= strip[0] and rect.y >= strip[1]
        assert rect.x + rect.w <= strip[0] + strip[2]
        assert rect.y + rect.h <= strip[1] + strip[3]
