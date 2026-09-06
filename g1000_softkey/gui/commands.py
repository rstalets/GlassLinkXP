"""Every CLI subcommand the GUI can launch, described as data.

The GUI does not call ``cmd_run`` and friends in-process. It spawns
``python -m g1000_softkey.main ...`` and reads the output back, for three
reasons:

* ``cmd_run`` installs SIGINT/SIGTERM handlers, and ``signal.signal`` only
  works on the main thread -- which Tk owns.
* Stopping is then a signal rather than a flag the run loop has to poll, so
  the daemon shuts down through the same path Ctrl-C uses and the ``finally``
  block that closes the publisher, the Tesseract API and the capture sources
  still runs.
* An OCR or capture crash takes the child down, not the window the user is
  looking at.

So the GUI's job is to build an argv, and the risk is that it builds one the
CLI does not accept -- a flag renamed in ``main.py`` would turn every button
in a tab into an error dialog. The commands are therefore declared here once,
and ``tests/test_gui_commands.py`` feeds every argv this module can produce
through the real ``build_parser()``. A flag that no longer exists fails the
test suite instead of the user.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class Option:
    """One argument of a subcommand, and how to present it in the GUI."""

    key: str
    #: the CLI flag, e.g. "--image"
    flag: str
    #: flag   -- passed on its own when true
    #: value  -- passed as "--flag value"
    kind: str = "value"
    label: str = ""
    help: str = ""
    default: Any = None
    #: fixed set of accepted values; mirrors argparse's `choices`
    choices: tuple[str, ...] = ()
    required: bool = False

    def __post_init__(self) -> None:
        if self.kind not in ("flag", "value"):
            raise ValueError(f"{self.key}: kind must be 'flag' or 'value', got {self.kind!r}")
        if not self.label:
            object.__setattr__(self, "label", self.key.replace("_", " "))


@dataclass(frozen=True)
class CommandSpec:
    """A subcommand of ``g1000_softkey.main``."""

    name: str
    title: str
    summary: str
    options: tuple[Option, ...] = ()
    #: True for `run`: it does not exit on its own and needs a Stop button.
    long_running: bool = False
    #: True where the command talks to Windows APIs and cannot work elsewhere
    #: without --image. The GUI says so rather than letting it fail obscurely.
    needs_windows: bool = False
    #: Directory-producing commands: the key of the option naming the folder
    #: they write into, so the GUI can offer to show what was written without
    #: each tab knowing which of its own fields that is. Read by
    #: :func:`output_folder`.
    output_option: str = ""

    def option(self, key: str) -> Option:
        for option in self.options:
            if option.key == key:
                return option
        raise KeyError(f"{self.name} has no option {key!r}")

    def __post_init__(self) -> None:
        if self.output_option:
            self.option(self.output_option)  # KeyError here beats a dead button


# The --image option appears on most commands and always means the same thing.
IMAGE = Option(
    key="image",
    flag="--image",
    label="Frame source",
    help="Read frames from a PNG file or a directory of PNGs instead of capturing a "
         "window. This is how the whole pipeline runs with no X-Plane and no Windows.",
)


RUN = CommandSpec(
    name="run",
    title="Run",
    summary="Capture, read the softkey strip and publish the labels, continuously.",
    long_running=True,
    needs_windows=True,
    options=(
        IMAGE,
        Option("publisher", "--publisher", label="Publish to",
               choices=("websocket", "webapi", "console"),
               help="Where the labels go. 'console' just prints them, which is the safe "
                    "thing to watch first; 'websocket' is the normal X-Plane path."),
        Option("hz", "--hz", label="Rate (Hz)",
               help="Override app.loop_hz for this run only."),
        Option("once", "--once", kind="flag", label="Single pass then stop",
               help="Process one frame and exit. Useful for a quick check."),
        Option("timing", "--timing", kind="flag", label="Log stage timings",
               help="Print a per-stage latency breakdown whenever the labels change."),
    ),
)

LIST_WINDOWS = CommandSpec(
    name="list-windows",
    title="Find windows",
    summary="List the open windows, so the pop-out titles can be copied into the config.",
    needs_windows=True,
    options=(
        Option("filter", "--filter", label="Title contains",
               help="Only show window titles containing this text."),
    ),
)

CALIBRATE = CommandSpec(
    name="calibrate",
    title="Calibrate",
    summary="Write raw/crop/overlay PNGs for each display and suggest a strip geometry.",
    needs_windows=True,
    output_option="out",
    options=(
        IMAGE,
        Option("out", "--out", label="Output folder", default="calibration",
               help="Where the calibration PNGs are written."),
    ),
)

DUMP_CELLS = CommandSpec(
    name="dump-cells",
    title="Cells",
    summary="Write the raw and preprocessed image of every cell -- what Tesseract sees.",
    needs_windows=True,
    output_option="out",
    options=(
        IMAGE,
        Option("out", "--out", label="Output folder", default="cells",
               help="Where the cell PNGs are written."),
    ),
)

DUMP_COLORS = CommandSpec(
    name="dump-colors",
    title="Colours",
    summary="Measure each cell's background colour and show how it classified.",
    needs_windows=True,
    options=(
        IMAGE,
        Option("json", "--json", label="Also write JSON to",
               help="Write the same measurements to a JSON file, for a bug report."),
    ),
)

BENCH = CommandSpec(
    name="bench",
    title="Benchmark",
    summary="Measure how long each pipeline stage takes, with and without change gating.",
    needs_windows=True,
    options=(
        IMAGE,
        Option("iterations", "-n", label="Iterations", default=50,
               help="How many frames to time."),
    ),
)

SCREEN_TEMPLATE = CommandSpec(
    name="screen-template",
    title="Screen template",
    summary="Read the display and print a [[screen]] block ready to paste into screens.toml.",
    needs_windows=True,
    options=(
        IMAGE,
        Option("display", "--display", label="Display", default="pfd",
               help="Which display to read."),
        Option("name", "--name", label="Page name", default="unnamed-page",
               help="A name for this softkey page, e.g. 'xpdr-code'."),
    ),
)

TUNE = CommandSpec(
    name="tune",
    title="Tune a cell",
    summary="Search preprocessing settings against one cell image that reads wrongly.",
    options=(
        Option("image", "--image", label="Cell image", required=True,
               help="A *_raw.png written by dump-cells."),
        Option("expect", "--expect", label="Should read", required=True,
               help="What that cell actually says, e.g. 0"),
    ),
)

SYNTH = CommandSpec(
    name="synth",
    title="Test frames",
    summary="Write synthetic softkey frames, so the rest of the GUI works with no X-Plane.",
    output_option="out",
    options=(
        Option("out", "--out", label="Output folder", default="frames",
               help="Where the PNG frames are written."),
    ),
)

COMMANDS: tuple[CommandSpec, ...] = (
    RUN, LIST_WINDOWS, CALIBRATE, DUMP_CELLS, DUMP_COLORS,
    BENCH, SCREEN_TEMPLATE, TUNE, SYNTH,
)

BY_NAME: dict[str, CommandSpec] = {spec.name: spec for spec in COMMANDS}


def output_folder(spec: CommandSpec, values: Mapping[str, Any] | None = None) -> str:
    """The folder ``spec`` writes into, given what the tab has in its fields.

    Falls back to the option's own default, which is what the child would use
    if the field were left empty, so the "Open folder" button and the command
    cannot end up looking in different places. "" for a command that writes no
    folder -- there is nothing to show, and the caller should not offer to.
    """
    if not spec.output_option:
        return ""
    option = spec.option(spec.output_option)
    raw = (values or {}).get(option.key)
    text = "" if raw is None else str(raw).strip()
    return text or ("" if option.default is None else str(option.default))


class MissingOption(ValueError):
    """A required option was left empty in the GUI."""


def build_argv(
    spec: CommandSpec,
    values: Mapping[str, Any] | None = None,
    *,
    config: str | os.PathLike[str] | None = None,
    verbose: bool = False,
) -> list[str]:
    """Turn a spec plus the GUI's field values into an argv for ``main()``.

    ``-c`` and ``-v`` go first, matching the form written throughout the docs
    (``g1000 -c config.toml run``); ``main.py`` accepts them on either side.
    """
    argv: list[str] = []
    if config:
        argv += ["-c", str(config)]
    if verbose:
        argv.append("-v")
    argv.append(spec.name)

    values = values or {}
    for option in spec.options:
        raw = values.get(option.key, None)
        if option.kind == "flag":
            if raw:
                argv.append(option.flag)
            continue
        text = "" if raw is None else str(raw).strip()
        if not text:
            if option.required:
                raise MissingOption(f"{spec.title}: {option.label} is required")
            continue
        if option.choices and text not in option.choices:
            raise ValueError(
                f"{spec.title}: {option.label} must be one of "
                f"{', '.join(option.choices)}, got {text!r}"
            )
        argv += [option.flag, text]
    return argv


def child_interpreter(executable: str | None = None) -> str:
    """The interpreter to spawn children with.

    Under ``pythonw.exe`` -- which is how the GUI is started on Windows, so it
    comes up without a console window behind it -- prefer the console
    ``python.exe`` beside it for the children. Both work, but a console build
    is the one every other part of this project is documented and tested
    against, and it is what appears in the command line the GUI shows the user
    so they can reproduce a failure by hand.
    """
    executable = executable or sys.executable
    if not executable:  # frozen or embedded interpreter: nothing better to offer
        return "python"
    path = Path(executable)
    name = path.name
    if name.lower().startswith("pythonw"):
        sibling = path.with_name("python" + path.suffix)
        if sibling.exists():
            return str(sibling)
    return str(path)


def full_command(argv: list[str], executable: str | None = None) -> list[str]:
    """``argv`` prefixed with the interpreter and ``-m g1000_softkey.main``."""
    return [child_interpreter(executable), "-m", "g1000_softkey.main", *argv]


def quote_command(command: list[str]) -> str:
    """The command as something the user could paste into a shell.

    Shown in the GUI above every run, because a GUI that hides what it did
    leaves the user unable to ask for help about it.
    """
    parts = []
    for item in command:
        parts.append(f'"{item}"' if (" " in item or not item) else item)
    return " ".join(parts)
