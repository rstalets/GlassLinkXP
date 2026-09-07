"""The setup wizard's steps and its "does this look finished?" checks.

No Tk here: the point of keeping the step list out of the widget is that all
of this is decidable without a display. The bar that draws it is covered in
tests/test_gui_app.py.
"""

from glasslinkxp.config import StripGeometry
from glasslinkxp.gui import configio, wizard


def test_every_step_points_at_a_tab_that_exists():
    """A step names its tab by class name, so a renamed tab fails here.

    The same check the old walkthrough had: it is the only thing standing
    between reordering the tab strip and a wizard that sends people to the
    wrong place.
    """
    from glasslinkxp.gui import tabs

    for step in wizard.STEPS:
        assert 0 <= tabs.tab_index(step.tab) < len(tabs.TAB_CLASSES), step.key


def test_every_step_says_what_to_do():
    for step in wizard.STEPS:
        assert step.title and step.instruction, step.key
    assert len({step.key for step in wizard.STEPS}) == len(wizard.STEPS)


def test_a_stored_step_number_is_always_usable():
    """It comes out of a preferences file a user can edit or corrupt."""
    assert wizard.clamp(-5) == 0
    assert wizard.clamp(0) == 0
    assert wizard.clamp(99) == len(wizard.STEPS) - 1
    assert wizard.step(99) is wizard.STEPS[-1]
    assert wizard.is_last(99) is True
    assert wizard.is_last(0) is False


def test_the_trail_marks_what_is_done_current_and_still_to_do():
    assert wizard.trail(0)[0] == "current"
    assert set(wizard.trail(0)[1:]) == {"todo"}
    trail = wizard.trail(2)
    assert trail[:2] == ("done", "done")
    assert trail[2] == "current"
    assert set(trail[3:]) == {"todo"}


# -- the checks -------------------------------------------------------------


def test_a_display_with_no_window_chosen_is_reported():
    document = configio.default_document()
    document["display"]["pfd"]["window_title"] = ""
    reason = wizard.windows_chosen(document)
    assert "pfd" in reason
    assert "mfd" not in reason


def test_a_disabled_display_is_not_asked_for_a_window():
    document = configio.default_document()
    document["display"]["mfd"]["window_title"] = ""
    document["display"]["mfd"]["enabled"] = False
    assert wizard.windows_chosen(document) == ""


def test_untouched_geometry_reads_as_not_calibrated():
    """The default is a guess at where the strip is, not a calibration."""
    assert "pfd" in wizard.calibrated(configio.default_document())


def test_a_moved_strip_reads_as_calibrated():
    document = configio.default_document()
    for key in ("pfd", "mfd"):
        document["display"][key]["geometry"] = dict(
            StripGeometry(x=0.031, y=0.902, w=0.938, h=0.062).as_dict()
        )
    assert wizard.calibrated(document) == ""


def test_only_the_displays_still_at_the_default_are_named():
    document = configio.default_document()
    document["display"]["pfd"]["geometry"]["y"] = 0.902
    reason = wizard.calibrated(document)
    assert "mfd" in reason
    assert "pfd" not in reason


def test_the_checks_survive_a_document_that_is_not_shaped_like_one():
    """The raw-TOML editor can put anything in here, and a wizard that raised
    would take the window down at the worst possible moment."""
    for document in ({}, {"display": "nonsense"}, {"display": {"pfd": 3}}):
        assert wizard.windows_chosen(document) == ""
        assert wizard.calibrated(document) == ""
