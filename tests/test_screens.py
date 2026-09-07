"""Identifying a softkey page from its confident labels."""

import pytest

from glasslinkxp.screens import Screen, ScreenError, ScreenLibrary, load_screens

XPDR = Screen(
    name="xpdr-code",
    display="pfd",
    match={9: "IDENT", 10: "BKSP", 11: "BACK"},
    labels={1: "0", 2: "1", 3: "2"},
)

#: The live reading this feature was built for: cell 1 wrong at 54%.
LIVE_LABELS = {1: "2", 2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7",
               9: "IDENT", 10: "BKSP", 11: "BACK", 12: ""}
LIVE_CONF = {1: 54.0, 2: 91.0, 3: 96.0, 4: 96.0, 5: 91.0, 6: 96.0, 7: 89.0, 8: 22.0,
             9: 89.0, 10: 92.0, 11: 95.0, 12: 0.0}


def test_identifies_the_page_from_its_match_cells():
    library = ScreenLibrary([XPDR])
    assert library.identify("pfd", LIVE_LABELS, LIVE_CONF, 85.0) is XPDR


def test_does_not_identify_a_page_from_a_shaky_match_cell():
    """An identification built on a guess propagates that guess everywhere."""
    confidences = {**LIVE_CONF, 9: 40.0}
    assert ScreenLibrary([XPDR]).identify("pfd", LIVE_LABELS, confidences, 85.0) is None


def test_does_not_identify_a_page_when_a_match_cell_reads_differently():
    labels = {**LIVE_LABELS, 10: "NRST"}
    assert ScreenLibrary([XPDR]).identify("pfd", labels, LIVE_CONF, 85.0) is None


def test_does_not_apply_a_page_belonging_to_another_display():
    assert ScreenLibrary([XPDR]).identify("mfd", LIVE_LABELS, LIVE_CONF, 85.0) is None


def test_the_more_specific_page_wins():
    loose = Screen(name="loose", display="pfd", match={9: "IDENT"}, labels={1: "X"})
    library = ScreenLibrary([loose, XPDR])
    assert library.identify("pfd", LIVE_LABELS, LIVE_CONF, 85.0) is XPDR


def test_an_equally_specific_tie_applies_neither():
    """Guessing between two pages would silently corrupt a whole strip."""
    twin = Screen(name="twin", display="pfd", match=dict(XPDR.match), labels={1: "9"})
    assert ScreenLibrary([twin, XPDR]).identify("pfd", LIVE_LABELS, LIVE_CONF, 85.0) is None


def test_empty_library_identifies_nothing():
    assert ScreenLibrary().identify("pfd", LIVE_LABELS, LIVE_CONF, 85.0) is None


# -- loading ---------------------------------------------------------------


def _write(tmp_path, text):
    path = tmp_path / "screens.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_a_definition(tmp_path):
    path = _write(tmp_path, """
[[screen]]
name = "xpdr-code"
display = "pfd"
match = { 9 = "IDENT", 10 = "BKSP" }
labels = { 1 = "0", 2 = "1" }
""")
    screens = load_screens(path)
    assert len(screens) == 1
    assert screens[0].match == {9: "IDENT", 10: "BKSP"}
    assert screens[0].labels == {1: "0", 2: "1"}


def test_labels_are_upper_cased_so_config_casing_does_not_matter(tmp_path):
    path = _write(tmp_path, """
[[screen]]
name = "x"
display = "pfd"
match = { 9 = "ident" }
labels = { 1 = "back" }
""")
    screens = load_screens(path)
    assert screens[0].match[9] == "IDENT"
    assert screens[0].labels[1] == "BACK"


def test_a_page_with_no_match_cells_is_rejected(tmp_path):
    """It would match every strip and overwrite all of them."""
    path = _write(tmp_path, """
[[screen]]
name = "greedy"
display = "pfd"
labels = { 1 = "0" }
""")
    with pytest.raises(ScreenError, match="no match cells"):
        load_screens(path)


def test_a_page_without_a_name_is_rejected(tmp_path):
    path = _write(tmp_path, '[[screen]]\ndisplay = "pfd"\nmatch = { 9 = "IDENT" }\n')
    with pytest.raises(ScreenError, match="needs a name"):
        load_screens(path)


def test_a_non_numeric_cell_is_rejected(tmp_path):
    path = _write(tmp_path, """
[[screen]]
name = "x"
display = "pfd"
match = { nine = "IDENT" }
""")
    with pytest.raises(ScreenError, match="not a cell number"):
        load_screens(path)


def test_missing_file_is_not_an_error(tmp_path):
    assert load_screens(tmp_path / "absent.toml") == []
    assert load_screens(None) == []


def test_the_shipped_definitions_load():
    from glasslinkxp.config import OcrConfig

    library = ScreenLibrary.load(OcrConfig().screens_file)
    assert len(library) >= 1
    assert library.identify("pfd", LIVE_LABELS, LIVE_CONF, 85.0) is not None
