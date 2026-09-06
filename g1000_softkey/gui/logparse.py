"""Read the daemon's own log lines back into structured labels.

The Run tab shows a live board of the twelve softkeys, and the numbers on it
come from parsing the lines ``cmd_run`` already logs. That is a coupling: this
module reads a format that ``main._format_row`` writes, and nothing in the
compiler or the type checker connects the two.

It is deliberate all the same. The alternative -- a second, machine-readable
output mode on ``run`` -- is a second thing to keep correct, and a GUI showing
labels from a path the CLI does not use is a GUI that can agree with itself
while disagreeing with the daemon. What the GUI shows is exactly what the
daemon said.

The coupling is made safe by ``tests/test_gui_logparse.py``, which builds a
``DisplayResult``, formats it with ``main._format_row`` and parses it back
here. Change the format and that test fails, which is the point: the failure
lands on whoever changed it, in the same commit, instead of on a user
wondering why the board went blank.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from ..color import BACKGROUND_NAMES, BLACK

#: name -> int, the reverse of color.BACKGROUND_NAMES.
BACKGROUND_VALUES = {name: value for value, name in BACKGROUND_NAMES.items()}

#: "[pfd] 1:INSET     | 2:-  ..." possibly behind a logging prefix. Anchored on
#: the bracketed display key followed by "1:", which a timestamp cannot fake.
_ROW = re.compile(r"\[(?P<display>[A-Za-z0-9_.-]+)\]\s+(?P<body>1:.*)$")
_CELL = re.compile(r"^(?P<index>\d+):(?P<text>.*)$")
#: What separates one cell from the next: `_format_row` joins with " | ", and
#: the spaces are the load-bearing part. Splitting on a bare "|" was safe only
#: because DEFAULT_WHITELIST leaves the character out -- but ocr.whitelist is
#: the user's to edit, is offered in the GUI's own settings form, and "|" is
#: Tesseract's commonest confusion for I and 1. One cell reading "|" made this
#: whole function return None, and the board stopped updating without a word.
_SEPARATOR = re.compile(r"\s\|\s")
_BG = re.compile(r"^(?P<index>\d+)=(?P<name>[a-z?]+)$")

#: What `_format_row` prints for a cell with no label. A real label cannot be
#: a bare "-": the OCR whitelist allows the character, but a softkey drawn as
#: nothing but a dash is not a thing the G1000 shows, and treating it as blank
#: is the reading that is right on every frame this project has seen.
BLANK_MARKER = "-"


@dataclass(frozen=True)
class LabelRow:
    """One display's twelve cells, as the daemon last reported them."""

    display: str
    labels: tuple[str, ...]
    backgrounds: tuple[int, ...]

    def cell(self, index: int) -> tuple[str, int]:
        """``index`` is 1-based, as it is on the panel and in the datarefs."""
        position = index - 1
        if not 0 <= position < len(self.labels):
            return "", BLACK
        return self.labels[position], self.backgrounds[position]


def parse_row(line: str) -> LabelRow | None:
    """A label row out of one log line, or None if it is not one."""
    match = _ROW.search(line)
    if match is None:
        return None
    body = match.group("body")

    backgrounds_text = ""
    if "  bg:" in body:
        body, _, backgrounds_text = body.partition("  bg:")

    labels: list[str] = []
    for chunk in _SEPARATOR.split(body):
        cell = _CELL.match(chunk.strip())
        if cell is None:
            # Not "<n>:" at all, so it is not a cell: it is the tail of the
            # previous label, which happened to contain the separator itself.
            # Rejoined rather than refused -- the row is ambiguous at that
            # point and one odd label is worth more than a board that stops.
            if not labels:
                return None
            labels[-1] = f"{labels[-1]} | {chunk.strip()}".strip()
            continue
        # Cells are printed in order and padded to a fixed width; a gap would
        # mean the format changed, and half a row is worse than none.
        if int(cell.group("index")) != len(labels) + 1:
            return None
        text = cell.group("text").strip()
        labels.append("" if text == BLANK_MARKER else text)
    if not labels:
        return None

    backgrounds = [BLACK] * len(labels)
    for chunk in backgrounds_text.split():
        entry = _BG.match(chunk)
        if entry is None:
            continue
        index = int(entry.group("index")) - 1
        if 0 <= index < len(backgrounds):
            backgrounds[index] = BACKGROUND_VALUES.get(entry.group("name"), BLACK)

    return LabelRow(match.group("display"), tuple(labels), tuple(backgrounds))


# ---------------------------------------------------------------------------
# `list-windows`
# ---------------------------------------------------------------------------

#: A quoted string as ``repr`` writes one -- either quote character, with its
#: own escapes inside. Which one repr picks depends on the *contents*: it
#: prefers single quotes and switches to double as soon as the string holds a
#: single quote of its own. That is the whole reason this parser is here.
_QUOTED = r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\""

#: One line of ``list-windows`` output, as ``capture.WindowInfo.__str__``
#: writes it:
#:
#:     hwnd=0x00010F42 pid=1234   1288x832 class='X-Plane' title='G1000 PFD'
#:
#: The class and title come from ``repr()``, so a window called
#: ``Cirrus SR22's PFD`` is printed in double quotes instead. A pattern with a
#: single quote typed into it therefore skipped exactly those windows, and the
#: Find windows tab told the user "No windows matched" with the line they were
#: looking for visible in the pane above it.
_WINDOW = re.compile(
    rf"hwnd=(?P<hwnd>\S+)\s+pid=(?P<pid>\d+)\s+(?P<width>\d+)x(?P<height>\d+)\s+"
    rf"class=(?P<cls>{_QUOTED})\s+title=(?P<title>{_QUOTED})\s*$"
)


@dataclass(frozen=True)
class WindowLine:
    """One window the daemon listed."""

    hwnd: str
    pid: int
    width: int
    height: int
    class_name: str
    title: str

    @property
    def size(self) -> str:
        return f"{self.width}x{self.height}"


def parse_window(line: str) -> WindowLine | None:
    """One listed window out of a log line, or None if it is not one.

    The quoted halves are turned back into strings by ``ast.literal_eval``,
    which is the exact inverse of the ``repr`` that wrote them -- so a title
    containing a quote, a backslash or a tab arrives as the title, rather than
    as the source text that spells it.
    """
    match = _WINDOW.search(line)
    if match is None:
        return None
    try:
        class_name = ast.literal_eval(match.group("cls"))
        title = ast.literal_eval(match.group("title"))
    except (SyntaxError, ValueError):  # not a repr after all
        return None
    if not isinstance(class_name, str) or not isinstance(title, str):
        return None
    return WindowLine(
        hwnd=match.group("hwnd"),
        pid=int(match.group("pid")),
        width=int(match.group("width")),
        height=int(match.group("height")),
        class_name=class_name,
        title=title,
    )


#: Log levels as the daemon's format string writes them, longest first so
#: "WARNING" is not shadowed by a prefix match on a shorter level.
_LEVELS = ("CRITICAL", "WARNING", "ERROR", "DEBUG", "INFO")


def classify(line: str) -> str:
    """Severity of a log line, for colouring the output pane.

    Returns one of error / warning / debug / info / plain. "plain" is a line
    with no level in it at all, which is most command output: the subcommands
    print their results rather than logging them.
    """
    for level in _LEVELS:
        if re.search(rf"\b{level}\b", line):
            return {
                "CRITICAL": "error", "ERROR": "error", "WARNING": "warning",
                "DEBUG": "debug", "INFO": "info",
            }[level]
    return "plain"


#: Lines the daemon logs when a display is not delivering frames. The Run tab
#: turns these into a visible state rather than a line that scrolls away,
#: because "nothing is happening" is the failure a new user hits first.
_STARVED = re.compile(r"no frames from (?P<display>\S+) after")
_RECOVERED = re.compile(r"(?P<display>\S+) is delivering frames again")


def parse_health(line: str) -> tuple[str, bool] | None:
    """(display, healthy) when a line reports a display starting or stopping.

    None when the line says nothing about it.
    """
    starved = _STARVED.search(line)
    if starved:
        return starved.group("display"), False
    recovered = _RECOVERED.search(line)
    if recovered:
        return recovered.group("display"), True
    return None
