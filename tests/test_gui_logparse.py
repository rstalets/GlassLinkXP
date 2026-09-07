"""The Run tab's board reads the daemon's own log lines.

That coupling is only safe while the two agree, so the round trip is tested
against ``main._format_row`` itself rather than against a copied sample: change
the format and this fails, in the same commit, instead of the board quietly
going blank for a user.
"""

import pytest

from g1000_softkey.color import BACKGROUND_NAMES, BLACK, RED, WHITE, YELLOW
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


def test_a_cell_that_reads_as_a_pipe_does_not_stop_the_board():
    """The row is joined with " | ", and splitting on a bare "|" was safe only
    while the OCR whitelist excluded the character. It is the user's to edit
    and the GUI's own settings form offers it -- and "|" is Tesseract's
    commonest confusion for I and 1, so it is the reading most likely to be
    let through by somebody who added the character to get an I."""
    labels = list(LABELS)
    labels[1] = "|"
    labels[5] = "|"
    row = logparse.parse_row(PREFIX + _format_row(_result(labels=labels)))
    assert row is not None
    assert row.labels == tuple(labels)


def test_a_pipe_inside_a_label_is_part_of_the_label():
    """Split on the bare character and "A|B" came back as two half cells; the
    separator the daemon writes is " | ", spaces included."""
    labels = list(LABELS)
    labels[3] = "A|B"
    row = logparse.parse_row(PREFIX + _format_row(_result(labels=labels)))
    assert row is not None
    assert row.labels == tuple(labels)


def test_a_pipe_in_the_middle_of_a_label_keeps_the_row():
    """Ambiguous in the daemon's format, so the row is kept with the odd label
    rather than dropped: a board that stops is the worse of the two."""
    labels = list(LABELS)
    labels[0] = "A | B"
    row = logparse.parse_row(PREFIX + _format_row(_result(labels=labels)))
    assert row is not None
    assert len(row.labels) == 12
    assert row.labels[0] == "A | B"
    assert row.labels[2] == LABELS[2]


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


# -- the window list -------------------------------------------------------
#
# The Find windows tab reads `list-windows` output, and that parser lived in
# tabs.py with its quotes typed in by hand. WindowInfo.__str__ formats the
# class and title with repr(), which uses double quotes as soon as the string
# contains a single one -- so a window called "Cirrus SR22's PFD" did not
# match, and the user was told "No windows matched" with the line visible in
# the pane above. Tested here the way the label rows are: by formatting a real
# WindowInfo and reading it back, never from a sample typed into the test.


def _window(title="G1000 PFD", class_name="X-Plane"):
    from g1000_softkey.capture import WindowInfo

    return WindowInfo(hwnd=0x10F42, title=title, class_name=class_name,
                      width=1288, height=832, pid=1234)


def _listed(window):
    """The line `cmd_list_windows` prints for a window, indent and all."""
    return f"  {window}"


def test_a_window_round_trips():
    window = _window()
    parsed = logparse.parse_window(_listed(window))
    assert parsed is not None
    assert parsed.title == window.title
    assert parsed.class_name == window.class_name
    assert parsed.pid == window.pid
    assert parsed.width == window.width
    assert parsed.height == window.height
    assert parsed.size == f"{window.width}x{window.height}"
    assert parsed.hwnd == f"0x{window.hwnd:08X}"


@pytest.mark.parametrize("title", [
    "G1000 PFD",
    "Cirrus SR22's PFD",          # repr switches to double quotes for this one
    'a "quoted" window',
    """both ' and " in one title""",
    "C:\\Users\\pilot\\X-Plane 12",
    "Ünïcöde ✈",
    "trailing spaces   ",
    "",
])
def test_every_kind_of_title_survives_the_round_trip(title):
    window = _window(title=title)
    parsed = logparse.parse_window(_listed(window))
    assert parsed is not None, f"{window} did not parse"
    assert parsed.title == title


def test_a_class_name_with_a_quote_survives_too():
    window = _window(class_name="X-Plane's window class")
    parsed = logparse.parse_window(_listed(window))
    assert parsed is not None
    assert parsed.class_name == window.class_name


@pytest.mark.parametrize("line", [
    "",
    "12 of 40 visible top-level windows",
    "18:04:11 INFO    g1000_softkey: [pfd] 1:INSET",
    "hwnd=0x1 pid=2 3x4 class='X' title=unquoted",
])
def test_lines_that_are_not_windows_are_ignored(line):
    assert logparse.parse_window(line) is None


# -- the one-shot commands' output -----------------------------------------
#
# The Calibrate, Colours and Pages tabs each read a command's printed output.
# Those parsers used to live in tabs.py with a sample of the format typed into
# a comment beside them, which is how the window-list parser came to disagree
# with the producer for two years' worth of aircraft names. Here they are fed
# from the commands themselves: the tests run the real subcommand over the
# synthetic frames and parse exactly what it printed.


@pytest.fixture(scope="module")
def frames(tmp_path_factory):
    from g1000_softkey import synth

    path = tmp_path_factory.mktemp("frames")
    synth.write_menus(path)
    return path


def _run(capsys, argv):
    """Run a real subcommand and hand back the lines it printed."""
    from g1000_softkey.main import main

    assert main(argv) == 0, f"{argv} failed"
    return capsys.readouterr().out.splitlines()


def test_the_suggested_geometry_is_read_from_calibrate(frames, tmp_path, capsys):
    lines = _run(capsys, ["calibrate", "--image", str(frames / "pfd_menu.png"),
                          "--out", str(tmp_path)])
    suggested = logparse.parse_calibration(lines)

    assert "pfd" in suggested, "\n".join(lines)
    for key in logparse.DETECTED_KEYS:
        assert 0.0 <= suggested["pfd"][key] <= 1.0


def test_a_display_whose_strip_was_not_found_is_not_suggested():
    """calibrate prints a sentence instead of numbers, and a button offering
    to apply nothing is worse than a button that stays off."""
    assert logparse.parse_calibration([
        "[pfd] frame 1288x832 source=image:x.png",
        "  auto-detect: no dark softkey band found; set the geometry by hand",
    ]) == {}


def test_the_colour_table_is_read_from_dump_colors(frames, capsys):
    lines = _run(capsys, ["dump-colors", "--image", str(frames / "alerts.png")])
    rows = logparse.parse_colors(lines)

    assert len(rows) == 24, "\n".join(lines)  # both displays, twelve cells each
    assert {row.display for row in rows} == {"pfd", "mfd"}
    assert [row.cell for row in rows[:12]] == list(range(1, 13))
    for row in rows:
        assert row.name in BACKGROUND_NAMES.values()
        assert row.background in BACKGROUND_NAMES
        assert len(row.bgr) == 3 and len(row.hsv) == 3


def test_the_colours_read_back_are_the_ones_the_command_measured(frames, tmp_path, capsys):
    """The JSON the command writes for a bug report is the same measurement,
    so the two have to agree cell for cell."""
    import json

    out = tmp_path / "colors.json"
    lines = _run(capsys, ["dump-colors", "--image", str(frames / "alerts.png"),
                          "--json", str(out)])
    rows = logparse.parse_colors(lines)
    records = json.loads(out.read_text(encoding="utf-8"))

    assert len(rows) == len(records)
    for row, record in zip(rows, records):
        assert (row.display, row.cell) == (record["display"], record["cell"])
        assert list(row.bgr) == record["bgr"]
        assert list(row.hsv) == record["hsv"]
        assert row.background == record["background"]
        assert row.name == record["name"]


def test_the_screen_block_is_read_from_screen_template(frames, capsys):
    import tomllib

    lines = _run(capsys, ["screen-template", "--image", str(frames / "xpdr.png"),
                          "--display", "pfd", "--name", "xpdr-code"])
    block = logparse.parse_screen_block(lines)

    assert block, "\n".join(lines)
    assert block[0].strip() == "[[screen]]"
    assert block[-1].strip(), "trailing blank lines belong to the file, not the block"
    # What the Pages tab appends has to be a page the daemon can read back.
    parsed = tomllib.loads("\n".join(block))
    assert parsed["screen"][0]["name"] == "xpdr-code"
    assert parsed["screen"][0]["display"] == "pfd"


def test_output_with_no_block_in_it_gives_nothing():
    assert logparse.parse_screen_block(["reading pfd...", "nothing to say"]) == []
