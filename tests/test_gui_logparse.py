"""The Run tab's board reads the daemon's own log lines.

That coupling is only safe while the two agree, so the round trip is tested
against ``main._format_row`` itself rather than against a copied sample: change
the format and this fails, in the same commit, instead of the board quietly
going blank for a user.
"""

import pytest

from g1000_softkey.color import BLACK, RED, WHITE, YELLOW
from g1000_softkey.gui import logparse
from g1000_softkey.main import _format_row
from g1000_softkey.ocr import CellResult
from g1000_softkey.pipeline import DisplayResult

LABELS = ["INSET", "", "PFD", "OBS", "CDI", "DME", "XPDR", "IDENT",
          "TMR/REF", "NRST", "ALERTS", "FLIGHT PLAN"]

#: The daemon's own logging format, so the parser is exercised behind the
#: prefix it actually arrives with rather than on a bare message.
PREFIX = "18:04:11 INFO    g1000_softkey: "


def _result(labels=None, backgrounds=None, display="pfd"):
    labels = labels if labels is not None else LABELS
    backgrounds = backgrounds if backgrounds is not None else [BLACK] * len(labels)
    return DisplayResult(
        display=display,
        cells=[CellResult(index=i, text=t, background=b)
               for i, (t, b) in enumerate(zip(labels, backgrounds))],
    )


def test_a_plain_row_round_trips():
    row = logparse.parse_row(PREFIX + _format_row(_result()))
    assert row is not None
    assert row.display == "pfd"
    assert row.labels == tuple(LABELS)
    assert row.backgrounds == (BLACK,) * 12


def test_backgrounds_round_trip():
    backgrounds = [BLACK] * 12
    backgrounds[3] = WHITE
    backgrounds[6] = YELLOW
    backgrounds[11] = RED
    row = logparse.parse_row(PREFIX + _format_row(_result(backgrounds=backgrounds)))
    assert row.backgrounds == tuple(backgrounds)
    assert row.cell(4) == ("OBS", WHITE)
    assert row.cell(12) == ("FLIGHT PLAN", RED)


def test_a_blank_cell_comes_back_as_an_empty_string():
    row = logparse.parse_row(PREFIX + _format_row(_result(labels=[""] * 12)))
    assert row.labels == ("",) * 12


def test_a_shorter_strip_round_trips():
    labels = ["A", "B", "C"]
    row = logparse.parse_row(PREFIX + _format_row(_result(labels=labels)))
    assert row.labels == tuple(labels)


def test_the_mfd_row_is_told_apart_from_the_pfd_row():
    row = logparse.parse_row(PREFIX + _format_row(_result(display="mfd")))
    assert row.display == "mfd"


def test_a_label_with_a_slash_survives():
    labels = ["TMR/REF"] + [""] * 11
    row = logparse.parse_row(PREFIX + _format_row(_result(labels=labels)))
    assert row.labels[0] == "TMR/REF"


@pytest.mark.parametrize("line", [
    "",
    "18:04:11 INFO    g1000_softkey: running at 12.0 Hz, gating=True, publisher=console",
    "18:04:11 WARNING g1000_softkey: loop overran: 190 ms > 83 ms budget",
    "wrote 48 cell PNGs to /tmp/cells",
    "[pfd] not a row at all",
])
def test_lines_that_are_not_rows_are_ignored(line):
    assert logparse.parse_row(line) is None


def test_a_row_with_a_gap_in_the_numbering_is_refused():
    """Half a board is worse than none -- it would show stale labels as live."""
    assert logparse.parse_row("[pfd] 1:A | 3:B") is None


def test_an_unknown_background_name_falls_back_to_black():
    assert logparse.parse_row("[pfd] 1:A  bg: 1=chartreuse").backgrounds == (BLACK,)


def test_every_background_the_daemon_can_name_is_understood():
    from g1000_softkey.color import BACKGROUND_NAMES

    for value, name in BACKGROUND_NAMES.items():
        assert logparse.BACKGROUND_VALUES[name] == value


@pytest.mark.parametrize("line,expected", [
    ("18:04:11 ERROR   g1000_softkey: nope", "error"),
    ("18:04:11 WARNING g1000_softkey: hmm", "warning"),
    ("18:04:11 INFO    g1000_softkey: fine", "info"),
    ("18:04:11 DEBUG   g1000_softkey: detail", "debug"),
    ("wrote 48 cell PNGs", "plain"),
])
def test_severity_is_read_off_the_line(line, expected):
    assert logparse.classify(line) == expected


def test_a_starving_display_is_noticed():
    line = ("18:04:11 WARNING g1000_softkey: no frames from pfd after 3s. The window must "
            "exist and be rendering")
    assert logparse.parse_health(line) == ("pfd", False)


def test_a_recovering_display_is_noticed():
    assert logparse.parse_health(
        "18:04:11 INFO    g1000_softkey: pfd is delivering frames again"
    ) == ("pfd", True)


def test_an_ordinary_line_says_nothing_about_health():
    assert logparse.parse_health("18:04:11 INFO    g1000_softkey: [pfd] 1:INSET") is None
