"""The tabs.

Each tab is a thin wrapper over one CLI subcommand: it collects the arguments,
asks the app to run it, and makes something of the output. The order they
appear in is the order somebody setting this up for the first time needs
them -- find the windows, calibrate, check the cells, then run -- which is
also the order of the calibration walkthrough in README.md.
"""

from __future__ import annotations

import copy
import os
import shutil
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Any

from dataclasses import replace

from ..color import BACKGROUND_NAMES, BLACK
from ..config import StripGeometry
from . import checks, commands, configio, geometry, schema
from .logparse import (
    classify,
    parse_calibration,
    parse_colors,
    parse_health,
    parse_row,
    parse_screen_block,
    parse_tuning_result,
    parse_window,
)
from .runner import Event, Failed, Finished, Line, Started
from .widgets import (
    CELL_COLORS,
    HELP_COLOR,
    WARN_COLOR,
    GeometryCanvas,
    HintEntry,
    ImageView,
    LabelBoard,
    OutputPane,
    ScrollableFrame,
    help_label,
    section_heading,
)

PAD = 8


def open_folder(path: Path) -> str:
    """Show a folder in the platform's file manager. Returns "" or an error."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])  # noqa: S603,S607
        else:
            subprocess.Popen(["xdg-open", str(path)])  # noqa: S603,S607
    except OSError as exc:
        return str(exc)
    return ""


class Tab(ttk.Frame):
    """Base class: holds the app reference and a title for the tab strip."""

    tab_title = "?"

    def __init__(self, app) -> None:
        super().__init__(app.notebook, padding=PAD)
        self.app = app

    def refresh(self) -> None:
        """Called when the configuration document changes."""

    def show_output_folder(self, spec: commands.CommandSpec, values: dict[str, Any],
                           nothing_yet: str) -> None:
        """Open the folder ``spec`` writes into, in the file manager.

        Which folder that is comes from ``spec.output_option`` rather than
        from each tab knowing which of its own boxes holds it -- the tabs had
        a copy of this each, and the declared option was read nowhere.
        """
        where = commands.output_folder(spec, values)
        folder = Path(where) if where else None
        if folder is None or not folder.is_dir():
            self.app.set_status(nothing_yet, "warning")
            return
        error = open_folder(folder)
        if error:
            self.app.set_status(error, "error")


# ---------------------------------------------------------------------------
# Start here
# ---------------------------------------------------------------------------


#: The walkthrough on the first tab. The last item of each step names the
#: tab it sends you to -- by class rather than by position, so reordering
#: the tab strip cannot silently point these buttons at the wrong one.
STEPS: tuple[tuple[str, str, str], ...] = (
    ("Pop the PFD and MFD out into their own windows in X-Plane",
     "In X-Plane, right-click each G1000 display and pop it out. The daemon reads the "
     "labels out of those windows' pixels -- it can read them even when the windows are "
     "behind something else, but they do have to exist and be drawing.", ""),
    ("Tell the daemon which windows those are",
     "The Find windows tab lists everything open. Pick the two pop-outs and apply them "
     "to the PFD and the MFD.", "WindowsTab"),
    ("Line the reader up with the softkey strip",
     "The Calibrate tab takes a picture of each window, draws the twelve boxes it is "
     "about to read on top of it, and offers you a starting geometry. Each box has to sit "
     "around exactly one label. This is the step that matters most: nearly every bad "
     "reading later turns out to be a box in the wrong place.", "CalibrateTab"),
    ("Check what the reader is actually looking at",
     "The Cells tab shows every cell as Tesseract gets it: black text on white, about "
     "30 pixels tall. If a cell is clipped or inverted, fix the geometry rather than the "
     "OCR settings.", "CellsTab"),
    ("Watch the labels before wiring anything up",
     "On the Run tab, set Publish to 'console' and press Start. The board fills in with "
     "what the daemon is reading. Nothing is sent to X-Plane in this mode.", "RunTab"),
    ("Send them to X-Plane",
     "Install the plugin (see README.md), then set Publish to 'websocket' and press Start. "
     "Your Stream Deck buttons read g1000/softkey/pfd/1:s64 and .../1/bg.", "RunTab"),
)


class StartTab(Tab):
    tab_title = "Start here"

    def __init__(self, app) -> None:
        super().__init__(app)
        scroll = ScrollableFrame(self)
        scroll.pack(fill="both", expand=True)
        body = scroll.body

        section_heading(body, "Putting the live G1000 softkey labels on a Stream Deck").pack(
            anchor="w", pady=(0, 4)
        )
        help_label(
            body,
            "X-Plane draws the softkey labels straight to the screen and offers no way to "
            "read them back, so this reads them out of the picture instead and republishes "
            "them as datarefs your Stream Deck can show. That means it has to be shown "
            "exactly where on the screen to look, once, which is what the steps below are for.",
            width=860,
        ).pack(anchor="w", pady=(0, 12))

        for number, (title, text, target) in enumerate(STEPS, start=1):
            block = ttk.Frame(body)
            block.pack(fill="x", pady=(0, 10))
            block.columnconfigure(1, weight=1)
            badge = tk.Label(block, text=str(number), width=3, relief="solid", borderwidth=1,
                             background="#eef3f7")
            badge.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 10))
            section_heading(block, title).grid(row=0, column=1, sticky="w")
            help_label(block, text, width=760).grid(row=1, column=1, sticky="w")
            if target:
                ttk.Button(
                    block, text="Go there", width=10,
                    command=lambda name=target: self.app.select_tab(tab_index(name)),
                ).grid(row=0, column=2, rowspan=2, sticky="e", padx=(10, 0))

        ttk.Separator(body).pack(fill="x", pady=10)
        section_heading(body, "No X-Plane to hand?").pack(anchor="w")
        help_label(
            body,
            "Every tab here works from saved pictures instead of a live window. Press the "
            "button below to write a folder of synthetic softkey frames, and the frame "
            "source at the top of the window will be pointed at them. Nothing else changes: "
            "the same pipeline runs, so you can see what the whole thing does before "
            "installing anything into the simulator.",
            width=860,
        ).pack(anchor="w", pady=(2, 6))
        ttk.Button(body, text="Make test frames and use them",
                   command=self._make_frames).pack(anchor="w")
        self.output = OutputPane(body, height=6)
        self.output.pack(fill="both", expand=True, pady=(10, 0))

    def _make_frames(self) -> None:
        out = self.app.project_root / "frames"
        self.app.run_task(
            commands.SYNTH, {"out": str(out)}, self.output,
            on_finish=lambda code, _lines: self._frames_done(code, out),
        )

    def _frames_done(self, code: int, out: Path) -> None:
        if code != 0:
            return
        self.app.image_source.set(str(out))
        self.app.set_status(
            f"Test frames written to {out}, and the frame source now points at them. "
            "Try the Run tab."
        )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


class RunTab(Tab):
    """Start and stop the daemon, and watch what it is reading."""

    tab_title = "Run"

    def __init__(self, app) -> None:
        super().__init__(app)
        # The app's own variable, not a copy: it is what gets written back to
        # the preferences when the window closes, and two variables that could
        # disagree about one setting would be worse than none -- the same
        # reasoning as the two debug-output checkboxes below.
        self.publisher = app.publisher
        self.hz = tk.StringVar(value="")
        self.once = tk.BooleanVar(value=False)
        self.timing = tk.BooleanVar(value=False)
        self.state_text = tk.StringVar(value="Stopped")
        self._boards: dict[str, LabelBoard] = {}
        self._health: dict[str, bool] = {}
        #: What this tab believes the daemon is doing. Kept rather than asking
        #: the process, because output is drained a poll behind the process
        #: itself: between the child exiting and Finished arriving, the last
        #: few lines are still being read, and they should not repaint a board
        #: the tab is about to clear.
        self._running = False

        controls = ttk.Frame(self)
        controls.pack(fill="x")
        self.start_button = ttk.Button(controls, text="Start", width=10, command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Stop", width=10,
                                      command=self.app.stop_daemon, state="disabled")
        self.stop_button.pack(side="left", padx=(6, 16))

        self.state_label = ttk.Label(controls, textvariable=self.state_text, width=26)
        self.state_label.pack(side="left")

        ttk.Label(controls, text="Publish to").pack(side="left", padx=(16, 4))
        options = ("", *commands.RUN.option("publisher").choices)
        box = ttk.Combobox(controls, textvariable=self.publisher, values=options,
                           state="readonly", width=11)
        box.pack(side="left")
        ttk.Label(controls, text="Rate (Hz)").pack(side="left", padx=(12, 4))
        ttk.Entry(controls, textvariable=self.hz, width=6).pack(side="left")
        ttk.Checkbutton(controls, text="Single pass", variable=self.once).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(controls, text="Stage timings", variable=self.timing).pack(side="left", padx=(8, 0))
        # Bound to the app's own verbose flag rather than a second variable of
        # this tab's: the same setting is offered in the header for every other
        # tab's commands, and two checkboxes that could disagree about one flag
        # would be worse than none. Sharing the Tk variable keeps them in step
        # without either having to know about the other.
        ttk.Checkbutton(controls, text="Debug output", variable=self.app.verbose,
                        command=self._verbose_changed).pack(side="left", padx=(8, 0))

        help_label(
            self,
            "Publish to: leave it empty to use whatever the configuration says. 'console' "
            "reads the labels and prints them without touching X-Plane, which is the safe "
            "thing to watch first. Debug output adds a line per cell saying what was read, "
            "what it snapped to and how confident it was -- it is the first thing to turn "
            "on when a label comes out wrong. The board below fills in from the daemon's "
            "own output, so it shows exactly what is being published, including which "
            "cells the sim has highlighted.",
            width=900,
        ).pack(anchor="w", pady=(6, 8))

        self.boards = ttk.Frame(self)
        self.boards.pack(fill="x")

        self.output = OutputPane(self, height=13)
        self.output.pack(fill="both", expand=True, pady=(10, 0))

        app.on_config_changed(self.refresh)

    # -- boards --------------------------------------------------------------

    def refresh(self) -> None:
        def cell_count(key: str) -> int:
            cells = configio.get_in(self.app.document, ("display", key, "geometry", "cells"), 12)
            return cells if isinstance(cells, int) and cells > 0 else 12

        # Rebuilt on the cell count as well as the names: a strip configured
        # for a different number of keys needs a different number of boxes,
        # and comparing only the names would leave the old ones on screen.
        wanted = [(key, cell_count(key)) for key in self.app.display_keys()]
        if [(k, b.cells) for k, b in self._boards.items()] == wanted:
            return
        for board in self._boards.values():
            board.destroy()
        self._boards = {}
        for key, cells in wanted:
            board = LabelBoard(self.boards, key, cells)
            board.pack(fill="x", pady=(0, 6))
            board.set_idle("--")
            self._boards[key] = board

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        values = {
            "publisher": self.publisher.get(),
            "hz": self.hz.get().strip(),
            "once": self.once.get(),
            "timing": self.timing.get(),
        }
        problem = self.app.validate_document()
        if problem:
            # The daemon would refuse this config on startup anyway; saying so
            # here means the reason is on screen next to the settings that
            # caused it, rather than in a log the user has to go and find.
            messagebox.showerror(
                "G1000 softkey labels",
                f"The configuration will not load:\n\n{problem}\n\nFix it on the Settings tab.",
                parent=self.app.root,
            )
            return
        for board in self._boards.values():
            board.set_idle("...")
        self._health = {}
        if self.app.start_daemon(values, self.on_event):
            self._set_running(True)

    def _verbose_changed(self) -> None:
        """The flag is fixed in the child's argv, so it cannot change mid-run.

        Saying so is the whole point of this callback: silently doing nothing
        would look exactly like a checkbox that does not work, which is how
        this one was reported in the first place.
        """
        state = "on" if self.app.verbose.get() else "off"
        if self._running:
            self.app.set_status(
                f"Debug output {state} -- press Stop and then Start for it to take effect; "
                "it is set on the command line when the daemon starts.",
                "warning",
            )
        else:
            self.app.set_status(f"Debug output {state}.")

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.state_text.set("Running" if running else "Stopped")
        self.state_label.configure(foreground="#14670f" if running else HELP_COLOR)

    def on_event(self, event: Event) -> None:
        if isinstance(event, Started):
            self.output.append(commands.quote_command(event.command), "command")
            self.app.set_status("Daemon started.")
            self._set_running(True)
            return
        if isinstance(event, Failed):
            self.output.append(event.message, "error")
            self.app.set_status(event.message, "error")
            self._set_running(False)
            return
        if isinstance(event, Finished):
            self.output.append(f"-- daemon stopped, exit code {event.returncode} --",
                               "plain" if event.returncode in (0, 130) else "error")
            self._set_running(False)
            for board in self._boards.values():
                board.set_idle("--")
            self.app.set_status(
                "Daemon stopped." if event.returncode in (0, 130)
                else f"The daemon exited with code {event.returncode}. The output says why.",
                "info" if event.returncode in (0, 130) else "error",
            )
            return
        if not isinstance(event, Line):
            return

        self.output.append(event.text, classify(event.text))
        row = parse_row(event.text)
        if row is not None:
            board = self._boards.get(row.display)
            if board is not None:
                board.update_cells(row.labels, row.backgrounds)
            self._health[row.display] = True
            self._update_state()
            return
        health = parse_health(event.text)
        if health is not None:
            display, healthy = health
            self._health[display] = healthy
            self._update_state()

    def _update_state(self) -> None:
        if not self._running:
            return
        starved = sorted(key for key, ok in self._health.items() if not ok)
        if starved:
            self.state_text.set(f"No frames from {', '.join(starved)}")
            self.state_label.configure(foreground="#8a5300")
        else:
            self.state_text.set("Running")
            self.state_label.configure(foreground="#14670f")


# ---------------------------------------------------------------------------
# Find windows
# ---------------------------------------------------------------------------


class WindowsTab(Tab):
    """List the open windows and put one into the config as a display."""

    tab_title = "Find windows"

    def __init__(self, app) -> None:
        super().__init__(app)
        self.filter = tk.StringVar(value="")
        self.target = tk.StringVar(value="")

        controls = ttk.Frame(self)
        controls.pack(fill="x")
        ttk.Button(controls, text="List windows", width=13, command=self.list_windows).pack(side="left")
        ttk.Label(controls, text="Title contains").pack(side="left", padx=(16, 4))
        entry = ttk.Entry(controls, textvariable=self.filter, width=24)
        entry.pack(side="left")
        entry.bind("<Return>", lambda _e: self.list_windows())

        help_label(
            self,
            "Pop the PFD and MFD out in X-Plane first, then list the windows and pick each "
            "one below. Only a distinctive part of the title is stored, matched without "
            "regard to case, so it keeps working if X-Plane adds something to the title. "
            "This needs Windows -- it reads the list from the operating system, and there "
            "is no equivalent to read on Linux or macOS.",
            width=900,
        ).pack(anchor="w", pady=(6, 8))

        columns = ("title", "size", "cls", "pid")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=9)
        for name, heading, width in (
            ("title", "Window title", 420), ("size", "Size", 100),
            ("cls", "Class", 180), ("pid", "Process", 80),
        ):
            self.tree.heading(name, text=heading)
            self.tree.column(name, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _e: self.apply())

        apply_row = ttk.Frame(self)
        apply_row.pack(fill="x", pady=(8, 0))
        ttk.Label(apply_row, text="Use the selected window as").pack(side="left")
        self.display_box = ttk.Combobox(apply_row, textvariable=self.target, state="readonly", width=10)
        self.display_box.pack(side="left", padx=6)
        ttk.Button(apply_row, text="Apply", width=9, command=self.apply).pack(side="left")

        self.output = OutputPane(self, height=7)
        self.output.pack(fill="both", expand=True, pady=(10, 0))
        app.on_config_changed(self.refresh)

    def refresh(self) -> None:
        keys = self.app.display_keys()
        self.display_box.configure(values=keys)
        if self.target.get() not in keys:
            self.target.set(keys[0] if keys else "")

    def list_windows(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.app.run_task(
            commands.LIST_WINDOWS, {"filter": self.filter.get().strip()},
            self.output, on_finish=self._parse,
        )

    def _parse(self, code: int, lines: list[str]) -> None:
        if code != 0:
            return
        found = 0
        for line in lines:
            window = parse_window(line)
            if window is None:
                continue
            found += 1
            self.tree.insert("", "end", values=(
                window.title, window.size, window.class_name, window.pid,
            ))
        self.app.set_status(
            f"{found} window(s) listed. Select one and apply it to a display."
            if found else "No windows matched. Are the displays popped out?",
            "info" if found else "warning",
        )

    def apply(self) -> None:
        selection = self.tree.selection()
        if not selection:
            self.app.set_status("Select a window in the list first.", "warning")
            return
        key = self.target.get()
        if not key:
            self.app.set_status("There is no display to apply it to.", "warning")
            return
        title = self.tree.item(selection[0], "values")[0]
        configio.set_in(self.app.document, ("display", key, "window_title"), title)
        self.app.notify_config_changed()
        saved = self._save()
        self.app.set_status(
            f"{key.upper()} will capture the window whose title contains {title!r}. {saved}"
        )

    def _save(self) -> str:
        if self.app.config_path is None:
            return "There is no configuration file open yet, so this is only set for now -- "\
                   "press New... at the top to make one."
        try:
            backup = configio.save(self.app.config_path, self.app.document)
        except configio.ConfigIoError as exc:
            return f"It could not be saved: {exc}"
        return (f"Saved to {self.app.config_path.name}"
                + (f" (previous version kept as {backup.name})" if backup else "") + ".")


# ---------------------------------------------------------------------------
# Calibrate
# ---------------------------------------------------------------------------


GEOMETRY_KEYS = ("x", "y", "w", "h", "cell_pad_x", "cell_pad_y")

#: The calibration, as three things to get right in order. Each step names the
#: geometry fields it is about and the edges its buttons and the arrow keys
#: move, so the panel, the buttons and the key bindings are all generated from
#: one description instead of three that can disagree.
#:
#: The order is not arbitrary. The top-left corner is placed first because
#: every other number is measured from it: set the width before the left edge
#: and you have to set it again afterwards. Padding is last because it is a
#: fraction *of a cell*, so it only means anything once the strip is right.
CALIBRATION_STEPS: tuple[dict, ...] = (
    {
        "title": "Place the top-left corner",
        "text": "Press \"Draw a new box\" and drag one roughly around the softkey strip, from "
                "any corner to the opposite one. Then nudge the top and left edges "
                "until they sit just INSIDE the strip: inside the dark band, with none of the "
                "bezel caught in the red box. Watch the close-up, not the small picture -- a "
                "pixel is a pixel there.",
        "controls": (
            ("Left edge", "left", "←", "→"),
            ("Top edge", "top", "↑", "↓"),
        ),
        "fields": ("x", "y"),
        "focus": "topleft",
        "zoom": "4x",
        "closeup": "The top-left corner, magnified. The red edges belong just inside the dark band.",
    },
    {
        "title": "Bring in the other two edges",
        "text": "Now set the width and height so the bottom and right edges also sit just "
                "inside the strip. When this step is right all four edges are inside the dark "
                "band: none of the bezel, none of the moving map, and no part of a softkey "
                "label outside the box.",
        "controls": (
            ("Right edge", "right", "←", "→"),
            ("Bottom edge", "bottom", "↑", "↓"),
        ),
        "fields": ("w", "h"),
        "focus": "bottomright",
        "zoom": "4x",
        "closeup": "The bottom-right corner, magnified. Same again: just inside, on both edges.",
    },
    {
        "title": "Trim the cells",
        "text": "The green boxes are what actually gets read -- one per softkey. Trim them "
                "until each box holds its whole label with nothing clipped on any side, and "
                "none of the vertical bars between the cells falls inside a box. A bar caught "
                "in a box reads as a stray character; a clipped glyph reads as nothing at all.",
        "controls": (
            ("Side trim", "pad_x", "−", "+"),
            ("Top/bottom trim", "pad_y", "−", "+"),
        ),
        "fields": ("cell_pad_x", "cell_pad_y"),
        "focus": "strip",
        "zoom": "Fit",
        "closeup": "The whole strip. Every green box should hold one label, whole, and no separator bar.",
    },
)


class CalibrateTab(Tab):
    """Show the reader where the softkey strip is, by drawing it on the picture.

    This used to be a picture and six numbers. The numbers are still here --
    they are what gets saved, and sometimes typing one is the fastest way --
    but nobody can look at ``x = 0.0273`` and say whether it is right. So the
    box is drawn with the mouse and judged in a magnified close-up, and the
    numbers follow along.

    The auto-detect is kept as a seed and nothing more. It finds the dark band
    reliably and the left and right edges only roughly, which is precisely the
    part a person can do in two seconds and an edge detector cannot.
    """

    tab_title = "Calibrate"

    def __init__(self, app) -> None:
        super().__init__(app)
        self.display = tk.StringVar(value="")
        self.out = tk.StringVar(value=str(app.project_root / "calibration"))
        self.step = tk.IntVar(value=0)
        self._suggested: dict[str, dict[str, float]] = {}
        self._fields: dict[str, tk.StringVar] = {}
        self._geometry = StripGeometry()
        #: The captured frame as an array, so the clipping check reads the
        #: same pixels the daemon would. Loaded once per picture, not per check.
        self._frame = None
        self._ocr = None
        self._clips: list[checks.CellClip] = []
        self._clip_check: str | None = None
        #: Whether the displays after the first take their strip position from
        #: it. Kept in the GUI's own preferences rather than in config.toml:
        #: the daemon needs explicit numbers for every display and gains
        #: nothing from knowing where they came from, and a config file that
        #: says what it means is worth more than one that has to be resolved.
        self.follow = tk.BooleanVar(value=True)
        #: Which of the tab's two views is packed. None until the first
        #: refresh, so the mode is set once rather than re-packed on every
        #: configuration change -- re-packing with `before=` reorders the tab.
        self._showing_follow: bool | None = None
        #: Guards the two-way binding between the boxes and the entry boxes.
        #: Without it, updating a field from a drag would fire the field's own
        #: handler, which would re-set the geometry, which would redraw...
        self._syncing = False

        controls = ttk.Frame(self)
        controls.pack(fill="x")
        ttk.Button(controls, text="Take a picture", width=15,
                   command=self.calibrate).pack(side="left")
        ttk.Label(controls, text="Display").pack(side="left", padx=(16, 4))
        self.display_box = ttk.Combobox(controls, textvariable=self.display,
                                        state="readonly", width=8)
        self.display_box.pack(side="left")
        self.display_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        self.draw_button = ttk.Button(controls, text="Draw a new box", width=15,
                                      command=self._arm_draw)
        self.draw_button.pack(side="left", padx=(16, 0))
        self.auto_button = ttk.Button(controls, text="Use auto-detect", state="disabled",
                                      command=self._apply_suggestion)
        self.auto_button.pack(side="left", padx=(6, 0))
        ttk.Button(controls, text="Open folder", width=12,
                   command=self._open_folder).pack(side="right")
        self.follow_box = ttk.Checkbutton(
            controls, variable=self.follow, command=self._follow_changed, text="",
        )

        # Two views of this tab, one packed at a time: the editor, and the
        # panel shown for a display that is simply copying another's numbers.
        self.editor = ttk.Frame(self)
        body = self.editor
        body.pack(fill="both", expand=True, pady=(8, 0))
        self.following_panel = self._build_following_panel()
        pictures = ttk.Frame(body)
        pictures.pack(side="left", fill="both", expand=True)

        self.picture = GeometryCanvas(
            pictures, on_change=self._from_canvas, allow_draw=True, height=300,
            placeholder="Press \"Take a picture\" to capture the display, then drag a box "
                        "around the softkey strip.",
        )
        self.picture.pack(fill="both", expand=True)
        caption_row = ttk.Frame(pictures)
        caption_row.pack(fill="x", pady=(6, 2))
        self.closeup_caption = ttk.Label(caption_row, text="", foreground=HELP_COLOR)
        self.closeup_caption.pack(side="left")
        ttk.Label(caption_row, text="Zoom").pack(side="right", padx=(8, 4))
        self.zoom = tk.StringVar(value="4x")
        zoom_box = ttk.Combobox(caption_row, textvariable=self.zoom, state="readonly", width=5,
                                values=("Fit", "2x", "4x", "8x", "16x"))
        zoom_box.pack(side="right")
        zoom_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_zoom())
        self.closeup = GeometryCanvas(
            pictures, on_change=self._from_canvas, allow_draw=False, zoom_to_strip=True,
            height=240, placeholder="The strip, magnified, once there is a picture to show.",
        )
        self.closeup.pack(fill="x")
        self._apply_zoom()

        side = ttk.Frame(body, padding=(PAD, 0, 0, 0))
        side.pack(side="right", fill="y")
        self._build_steps(side)
        self._build_numbers(side)

        self.clip_warning = ttk.Label(pictures, text="", foreground=WARN_COLOR,
                                      wraplength=760, justify="left")
        self.clip_warning.pack(anchor="w", pady=(4, 0))

        self.output = OutputPane(self, height=6)
        self.output.pack(fill="both", expand=True, pady=(8, 0))

        # Arrow keys nudge whatever the current step is about, which is the
        # fastest way to move one pixel and the only way to do it without
        # taking your eye off the close-up.
        for key, edge, sign in (("Left", 0, -1), ("Right", 0, 1),
                                ("Up", 1, -1), ("Down", 1, 1)):
            self.bind_all(f"<KeyPress-{key}>",
                          lambda e, i=edge, s=sign: self._arrow(e, i, s), add="+")
            self.bind_all(f"<Shift-KeyPress-{key}>",
                          lambda e, i=edge, s=sign: self._arrow(e, i, s, 5), add="+")

        app.on_config_changed(self.refresh)

    # -- following another display -------------------------------------------

    def _build_following_panel(self) -> ttk.Frame:
        frame = ttk.Frame(self, padding=(0, 20, 0, 0))
        self.following_title = section_heading(frame, "")
        self.following_title.pack(anchor="w")
        self.following_text = help_label(frame, "", width=820)
        self.following_text.pack(anchor="w", pady=(6, 10))
        self.following_numbers = ttk.Label(frame, text="", foreground=HELP_COLOR,
                                           font=tkfont.nametofont("TkFixedFont"))
        self.following_numbers.pack(anchor="w", pady=(0, 14))
        buttons = ttk.Frame(frame)
        buttons.pack(anchor="w")
        self.copy_button = ttk.Button(buttons, text="", command=self.save)
        self.copy_button.pack(side="left")
        ttk.Button(buttons, text="Calibrate this display separately",
                   command=self._stop_following).pack(side="left", padx=(8, 0))
        return frame

    def source_display(self) -> str:
        """The display others copy: the first one in the configuration."""
        keys = self.app.display_keys()
        return keys[0] if keys else ""

    def follower_displays(self) -> list[str]:
        return self.app.display_keys()[1:]

    def is_following(self) -> bool:
        """Whether the selected display is currently copying another."""
        return bool(self.follow.get()) and self.display.get() in self.follower_displays()

    def _follow_changed(self) -> None:
        self.app.prefs["follow_first_display"] = bool(self.follow.get())
        self._update_mode()
        source = self.source_display().upper()
        if self.follow.get():
            self.app.set_status(
                f"The other displays will use the {source} strip position. Saving writes it "
                "to all of them."
            )
        else:
            self.app.set_status("Each display is calibrated on its own now.")

    def _stop_following(self) -> None:
        self.follow.set(False)
        self._follow_changed()

    def _update_mode(self) -> None:
        """Show either the editor or the "this one copies another" panel."""
        followers = self.follower_displays()
        if followers:
            self.follow_box.configure(
                text=f"Use the {self.source_display().upper()} strip position "
                     f"for {', '.join(k.upper() for k in followers)}"
            )
            self.follow_box.pack(side="left", padx=(16, 0))
        else:
            self.follow_box.pack_forget()

        following = self.is_following()
        # The editing buttons live in the controls row, which stays visible in
        # both views, so they have to be turned off by hand -- otherwise they
        # act on an editor that is not on screen.
        for button in (self.draw_button, self.auto_button):
            button.configure(state="disabled" if following else "normal")
        if not following and self.display.get() not in self._suggested:
            self.auto_button.configure(state="disabled")
        if following:
            self._show_following()
        if following != self._showing_follow:
            self._showing_follow = following
            if following:
                self.editor.pack_forget()
                self.following_panel.pack(fill="both", expand=True, before=self.output)
            else:
                self.following_panel.pack_forget()
                self.editor.pack(fill="both", expand=True, pady=(8, 0), before=self.output)
        self._update_save_button()

    def _show_following(self) -> None:
        source = self.source_display()
        key = self.display.get()
        geometry = configio.geometry_of(self.app.document, source)
        self.following_title.configure(
            text=f"{key.upper()} is using the {source.upper()} strip position"
        )
        self.following_text.configure(
            text=f"The pop-out windows are usually the same size and the same shape, so the "
                 f"position that works for the {source.upper()} normally works for the "
                 f"{key.upper()} too -- and calibrating one display well is enough work "
                 f"without doing it twice. Calibrate the {source.upper()}, and these numbers "
                 f"are written to {key.upper()} when you save.\n\n"
                 f"If the two windows are not the same size, or the {key.upper()} reads badly "
                 f"while the {source.upper()} reads well, untick the box above (or press the "
                 f"button below) and this display gets its own calibration."
        )
        self.following_numbers.configure(
            text="   ".join(f"{name}={getattr(geometry, name)}" for name in GEOMETRY_KEYS)
        )
        self.copy_button.configure(
            text=f"Copy the {source.upper()} position to {key.upper()} and save"
        )

    def _update_save_button(self) -> None:
        targets = self.save_targets()
        names = " and ".join(k.upper() for k in targets)
        self.save_button.configure(
            text=f"Save to the configuration ({names})" if len(targets) > 1
            else "Save to the configuration"
        )

    def save_targets(self) -> list[str]:
        """Which displays a save from here writes the geometry to.

        With following on that is every display, whichever one is selected:
        there is only one strip position, and the copy button on the panel and
        Save on the editor are then the same action reached from two places.
        """
        key = self.display.get()
        if not key:
            return []
        if not self.follow.get():
            return [key]
        return [self.source_display(), *self.follower_displays()]

    # -- the step panel ------------------------------------------------------

    def _build_steps(self, parent: tk.Misc) -> None:
        self.step_frame = ttk.LabelFrame(parent, text="  Step 1 of 3  ", padding=PAD)
        self.step_frame.pack(fill="x")
        self.step_title = section_heading(self.step_frame, "")
        self.step_title.pack(anchor="w")
        self.step_text = help_label(self.step_frame, "", width=270)
        self.step_text.pack(anchor="w", pady=(2, 8))

        self.step_buttons = ttk.Frame(self.step_frame)
        self.step_buttons.pack(fill="x")

        move = ttk.Frame(self.step_frame)
        move.pack(fill="x", pady=(8, 0))
        self.back_button = ttk.Button(move, text="Back", width=8, command=self.previous_step)
        self.back_button.pack(side="left")
        self.next_button = ttk.Button(move, text="Next", width=8, command=self.next_step)
        self.next_button.pack(side="right")
        help_label(
            self.step_frame,
            "Arrow keys move one pixel, with Shift five. Drag the white squares to resize the "
            "red box, or drag inside it to slide the whole thing.",
            width=270,
        ).pack(anchor="w", pady=(8, 0))
        self._show_step()

    def _show_step(self) -> None:
        index = max(0, min(self.step.get(), len(CALIBRATION_STEPS) - 1))
        step = CALIBRATION_STEPS[index]
        self.step_frame.configure(text=f"  Step {index + 1} of {len(CALIBRATION_STEPS)}  ")
        self.step_title.configure(text=step["title"])
        self.step_text.configure(text=step["text"])
        self.closeup.set_focus(step["focus"])
        # Each step wants a different magnification: a corner is judged a
        # pixel at a time, while the cells are checked by looking at all
        # twelve at once and only then zooming in on the one that looks wrong.
        # Set as the step's default rather than left alone, because carrying
        # 8x from step 2 into step 3 shows two cells out of twelve.
        self.zoom.set(step["zoom"])
        self._apply_zoom()
        self.closeup_caption.configure(text=step["closeup"])
        self.back_button.configure(state="disabled" if index == 0 else "normal")
        self.next_button.configure(
            state="disabled" if index == len(CALIBRATION_STEPS) - 1 else "normal")

        for child in self.step_buttons.winfo_children():
            child.destroy()
        for row, (label, target, minus, plus) in enumerate(step["controls"]):
            ttk.Label(self.step_buttons, text=label).grid(row=row, column=0, sticky="w", pady=2)
            for column, (glyph, sign) in enumerate(((minus, -1), (plus, 1)), start=1):
                # tk.Button rather than ttk: only the plain one autorepeats
                # when held, and holding is how you move an edge ten pixels
                # without clicking ten times.
                tk.Button(
                    self.step_buttons, text=glyph, width=2, repeatdelay=400, repeatinterval=60,
                    command=lambda t=target, s=sign: self._nudge(t, s),
                ).grid(row=row, column=column, padx=2)

    def next_step(self) -> None:
        self.step.set(min(self.step.get() + 1, len(CALIBRATION_STEPS) - 1))
        self._show_step()

    def previous_step(self) -> None:
        self.step.set(max(self.step.get() - 1, 0))
        self._show_step()

    # -- the numbers ---------------------------------------------------------

    def _build_numbers(self, parent: tk.Misc) -> None:
        frame = ttk.LabelFrame(parent, text="  The numbers  ", padding=PAD)
        frame.pack(fill="x", pady=(10, 0))
        help_label(
            frame,
            "Fractions of the window, so a resize does not undo the calibration. Type here if "
            "you would rather, or to copy a setting between displays.",
            width=270,
        ).pack(anchor="w", pady=(0, 6))
        grid = ttk.Frame(frame)
        grid.pack(fill="x")
        for row, key in enumerate(GEOMETRY_KEYS):
            variable = tk.StringVar(value="")
            self._fields[key] = variable
            ttk.Label(grid, text=schema.setting("geometry", key).label).grid(
                row=row, column=0, sticky="w", pady=1
            )
            entry = ttk.Entry(grid, textvariable=variable, width=10)
            entry.grid(row=row, column=1, padx=(8, 0))
            entry.bind("<Return>", lambda _e: self._from_fields())
            entry.bind("<FocusOut>", lambda _e: self._from_fields())

        self.pixels = help_label(frame, "", width=270)
        self.pixels.pack(anchor="w", pady=(8, 0))
        self.save_button = ttk.Button(frame, text="Save to the configuration",
                                      command=self.save)
        self.save_button.pack(fill="x", pady=(8, 0))
        ttk.Button(frame, text="Undo my changes", command=self.refresh).pack(fill="x", pady=(4, 0))

    # -- the model -----------------------------------------------------------

    def set_geometry(self, value: StripGeometry, from_canvas: bool = False) -> None:
        """One place where the working geometry changes, whatever moved it."""
        self._geometry = geometry.clamp(value)
        self._syncing = True
        try:
            for key, variable in self._fields.items():
                variable.set(repr(round(float(getattr(self._geometry, key)), 4)))
            self.picture.set_geometry(self._geometry)
            self.closeup.set_geometry(self._geometry)
        finally:
            self._syncing = False
        self._show_pixels()
        self._schedule_clip_check()

    def _schedule_clip_check(self) -> None:
        """Re-check for clipping once the geometry stops moving.

        Debounced rather than run on every change: reading twelve crops off a
        1280x800 frame costs about five milliseconds, and a drag produces
        changes far faster than that. Waiting for the pause makes it free
        during the drag and immediate once the hand stops.
        """
        if self._clip_check is not None:
            self.after_cancel(self._clip_check)
        self._clip_check = self.after(250, self._check_clipping)

    def _check_clipping(self) -> list[checks.CellClip]:
        self._clip_check = None
        self._clips = checks.check_cells(self._frame, self._geometry, self._ocr)
        self.picture.set_warnings(c.cell for c in self._clips)
        self.closeup.set_warnings(c.cell for c in self._clips)
        self.clip_warning.configure(
            text=(checks.describe(self._clips) + " Amber boxes above. A long label can fill "
                  "its cell honestly, so look before you trim.") if self._clips else ""
        )
        return self._clips

    def _show_pixels(self) -> None:
        """The same numbers in pixels, which is the unit being judged."""
        width, height = self.picture.frame_size
        if not width:
            self.pixels.configure(text="")
            return
        x, y, w, h = geometry.strip_pixels(self._geometry, width, height)
        cell_w = w / max(1, self._geometry.cells)
        self.pixels.configure(
            text=f"On a {width}x{height} frame: the strip is {w}x{h} pixels at ({x}, {y}), "
                 f"and each cell is about {cell_w:.0f} wide before trimming."
        )

    def _from_canvas(self, value: StripGeometry) -> None:
        self.set_geometry(value, from_canvas=True)

    def _from_fields(self) -> None:
        if self._syncing:
            return
        values = {}
        for key, variable in self._fields.items():
            try:
                values[key] = configio.parse_field(schema.setting("geometry", key),
                                                   variable.get())
            except configio.ConfigIoError as exc:
                self.app.set_status(str(exc), "error")
                return
        self.set_geometry(replace(self._geometry, **values))

    def _nudge(self, target: str, sign: int, pixels: float = 1.0) -> None:
        width, height = self.picture.frame_size
        if target in ("pad_x", "pad_y"):
            self.set_geometry(geometry.nudge_padding(self._geometry, target[-1], sign))
        elif width:
            self.set_geometry(
                geometry.nudge_edge(self._geometry, target, sign * pixels, width, height)
            )

    def _arrow(self, event: tk.Event, axis: int, sign: int, pixels: float = 1.0) -> None:
        """Arrow keys, but only when this tab is the one on screen.

        Bound with bind_all because the focus is usually on the canvas or on
        nothing in particular; the guard is what stops them stealing the arrow
        keys from an entry box on another tab.
        """
        if self.app.notebook.select() != str(self):
            return
        if isinstance(event.widget, (ttk.Entry, tk.Entry)):
            return
        index = max(0, min(self.step.get(), len(CALIBRATION_STEPS) - 1))
        controls = CALIBRATION_STEPS[index]["controls"]
        if axis < len(controls):
            self._nudge(controls[axis][1], sign, pixels)

    # -- running the command -------------------------------------------------

    def refresh(self) -> None:
        keys = self.app.display_keys()
        self.display_box.configure(values=keys)
        if self.display.get() not in keys:
            self.display.set(keys[0] if keys else "")
        # The user's own blank thresholds decide which cells the clipping
        # check looks at, so take them from the document rather than from the
        # defaults -- somebody who lowered blank_contrast to keep dim labels
        # wants those cells checked too.
        try:
            self._ocr = configio.validate(self.app.document).ocr
        except Exception:  # noqa: BLE001 - a bad config is the Settings tab's problem
            self._ocr = None
        self._resolve_follow()
        # A follower shows the source's numbers, because those are the ones it
        # will be given; showing its own stale values would be showing
        # something that is about to stop being true.
        shown = self.source_display() if self.is_following() else self.display.get()
        self.set_geometry(configio.geometry_of(self.app.document, shown))
        self._update_mode()
        self._load_picture()
        self.auto_button.configure(
            state="normal" if self.display.get() in self._suggested else "disabled"
        )

    def _resolve_follow(self) -> None:
        """Decide whether the followers are following, if nobody has said.

        An unset preference is answered from the configuration itself: if the
        second display's geometry already matches the first's, it was never
        calibrated separately and linking them changes nothing. If it differs,
        somebody did the work, and the box starts unticked rather than
        offering to overwrite it.
        """
        stored = self.app.prefs.get("follow_first_display")
        if isinstance(stored, bool):
            self.follow.set(stored)
            return
        source = self.source_display()
        followers = self.follower_displays()
        self.follow.set(
            all(configio.same_geometry(self.app.document, source, key) for key in followers)
        )

    def _load_picture(self) -> None:
        raw = Path(self.out.get()) / f"{self.display.get()}_raw.png"
        path = raw if raw.is_file() else None
        self.picture.set_frame(path)
        self.closeup.set_frame(path)
        self._frame = checks.load_frame(path) if path else None
        self.set_geometry(self._geometry)

    def calibrate(self) -> None:
        self._suggested = {}
        self.auto_button.configure(state="disabled")
        self.app.run_task(
            commands.CALIBRATE, {"out": self.out.get()}, self.output, on_finish=self._parse
        )

    def _parse(self, code: int, lines: list[str]) -> None:
        self._suggested = parse_calibration(lines)
        self._load_picture()
        self.auto_button.configure(
            state="normal" if self.display.get() in self._suggested else "disabled"
        )
        if code == 0:
            self.step.set(0)
            self._show_step()
            self.app.set_status(
                "Picture taken. Drag a box around the softkey strip, then work through the "
                "three steps on the right."
            )

    def _apply_zoom(self) -> None:
        choice = self.zoom.get()
        self.closeup.set_zoom(0.0 if choice == "Fit" else float(choice.rstrip("x")))

    def _arm_draw(self) -> None:
        self.picture.arm_draw()
        self.app.set_status(
            "Drag a box around the softkey strip on the picture -- from any corner to the "
            "opposite one. Rough is fine; the three steps are for making it exact."
        )

    def _apply_suggestion(self) -> None:
        found = self._suggested.get(self.display.get())
        if not found:
            return
        self.set_geometry(replace(self._geometry, **found))
        self.app.set_status(
            "Auto-detect applied. It gets the dark band right and the left and right edges "
            "only roughly, so check all four in the close-up."
        )

    def save(self) -> None:
        key = self.display.get()
        targets = self.save_targets()
        if not targets:
            return
        if not self.is_following() and not self._confirm_clipping(key):
            return
        # Edited on a copy and only adopted once it validates. Writing into
        # the live document first would leave the window holding a geometry
        # the daemon will not accept, with nothing on screen saying so.
        document = copy.deepcopy(self.app.document)
        for target in targets:
            configio.set_geometry(document, target, self._geometry)
        base = self.app.config_path.parent if self.app.config_path else None
        try:
            configio.validate(document, base_dir=base)
        except Exception as exc:  # noqa: BLE001 - every failure is a message to show
            messagebox.showerror("G1000 softkey labels",
                                 f"That geometry will not load:\n\n{exc}",
                                 parent=self.app.root)
            return
        if self.app.config_path is None:
            messagebox.showinfo(
                "G1000 softkey labels",
                "There is no configuration file open yet. Press New... at the top of the "
                "window to make one, then save again.",
                parent=self.app.root,
            )
            return
        try:
            backup = configio.save(self.app.config_path, document)
        except configio.ConfigIoError as exc:
            self.app.set_status(str(exc), "error")
            return
        self.app.document = document
        self.app.notify_config_changed()
        self.app.set_status(
            f"Saved the strip position for {' and '.join(t.upper() for t in targets)}"
            + (f" (previous version kept as {backup.name})" if backup else "")
            + ". Check it on the Cells tab, then restart the daemon."
        )

    def _confirm_clipping(self, key: str) -> bool:
        """Ask before saving a geometry whose crops look like they cut labels.

        Asked rather than refused. The check cannot tell a clipped glyph from
        a label that fills its cell, and a warning that blocks the save would
        eventually be worked around by whoever hits the false positive -- at
        which point it has taught them to ignore it. It reports what it saw
        and lets the person who can look at the picture decide.
        """
        if self._frame is None:
            return True
        clips = self._check_clipping()
        if not clips:
            return True
        return bool(messagebox.askokcancel(
            "G1000 softkey labels",
            f"{checks.describe(clips, key)}\n\n{checks.ADVICE}\n\n"
            "Save it anyway?",
            parent=self.app.root, icon="warning", default="cancel",
        ))

    def _open_folder(self) -> None:
        self.show_output_folder(
            commands.CALIBRATE, {"out": self.out.get()},
            "There is nothing there yet -- take a picture first.",
        )


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------


class CellsTab(Tab):
    """What Tesseract is actually given, cell by cell, and the sharpening tuner.

    Tuning used to search against one cell picture at a time, which found a
    setting that fixed the cell it was aimed at and, in one real capture,
    quietly turned a different cell's 6 into a 5. So instead of a Tune button
    per cell, the boxes here are typed into (leave the rest blank) and queued
    with **Add this page** -- across as many pages, and as many displays, as
    the run should be checked against -- before **Run tuning** searches for a
    change that fixes something without breaking anything already queued.
    """

    tab_title = "Cells"

    def __init__(self, app) -> None:
        super().__init__(app)
        self.display = tk.StringVar(value="")
        self.out = tk.StringVar(value=str(app.project_root / "cells"))
        self._views: list[tuple[ImageView, ImageView, ttk.Label]] = []
        self._expect: list[tk.StringVar] = [tk.StringVar() for _ in range(12)]
        #: Pages queued for the tuner: {"dir", "display", "expect"} snapshots
        #: taken by add_tuning_page(), one per press. Plain dicts rather than
        #: tuning.TuningCase so this module never has to import the pipeline
        #: (cv2, Tesseract) that module needs to search them -- the GUI only
        #: ever shells out to that search, the same as everything else here.
        self._tuning_cases: list[dict[str, Any]] = []
        self._suggested: dict[str, Any] | None = None

        controls = ttk.Frame(self)
        controls.pack(fill="x")
        ttk.Button(controls, text="Read the cells", width=15, command=self.dump).pack(side="left")
        ttk.Label(controls, text="Display").pack(side="left", padx=(16, 4))
        self.display_box = ttk.Combobox(controls, textvariable=self.display,
                                        state="readonly", width=8)
        self.display_box.pack(side="left")
        self.display_box.bind("<<ComboboxSelected>>", lambda _e: self._show())
        ttk.Button(controls, text="Open folder", width=12,
                   command=self._open_folder).pack(side="right")

        help_label(
            self,
            "Top row of each pair is the cell as it reaches Tesseract: it should be black "
            "text on a white background, with the letters filling most of the height, and "
            "it should look that way for the highlighted softkey too. Bottom row is the "
            "raw crop. It is shown at the true size Tesseract received, not shrunk to fit -- "
            "a smoothed-down preview of a binary image is how the last closed-counter bug "
            "hid, so scroll rather than trust a blurrier picture. If a cell is clipped or has "
            "its neighbour's label in it, go back to Calibrate -- that is a geometry problem "
            "and no amount of OCR tuning will fix it. If a cell looks right but reads wrong, "
            "type what it should say in the box below it and use the sharpening tuner "
            "underneath.",
            width=900,
        ).pack(anchor="w", pady=(6, 8))

        # Everything below this point -- the grid, the tuner controls and the
        # output pane -- lives inside one scrollable body, rather than being
        # packed directly into the tab. Twelve cells shown at true resolution
        # cannot fit the window's minsize, and neither, on top of that, can
        # the tuner section and the output pane both keep their own minimum
        # size: pack has no way to say "shrink the grid before the output
        # pane", so once the tab's total content exceeds what it is given,
        # *something* is squeezed arbitrarily -- which is exactly how the
        # output pane and the status bar ended up squeezed to nothing before
        # this tab had a scrollbar at all. Scrolling the lot together sidesteps
        # that fight rather than trying to referee it.
        scroll = ScrollableFrame(self)
        scroll.pack(fill="both", expand=True)
        body = scroll.body

        #: Native prep size across the offline corpus is up to ~320x152 (a
        #: ~76x34 raw crop, 4x upscaled, plus an 8px border). Two columns of
        #: boxes that size fit the window's own minsize width with room to
        #: spare and no horizontal scrollbar -- this widget only scrolls
        #: vertically, so a column width tight enough to need one would just
        #: clip the rightmost column instead. More, narrower columns is the
        #: shrink-to-fit this tab used to do and the reason its prep picture
        #: could not be trusted.
        self.grid_frame = ttk.Frame(body)
        self.grid_frame.pack(fill="x")
        columns = 2
        for column in range(columns):
            self.grid_frame.columnconfigure(column, weight=1)
        for row in range(-(-12 // columns)):
            self.grid_frame.rowconfigure(row, weight=1)
        for index in range(12):
            row, column = divmod(index, columns)
            block = ttk.LabelFrame(self.grid_frame, text=f" {index + 1} ")
            block.grid(row=row, column=column, sticky="nsew", padx=3, pady=3)
            # allow_shrink=False on both, and no forced width/height: a fixed
            # box sized for *this* geometry's ~320x152 prep image would have
            # clipped the bottom of a taller one instead, since allow_shrink
            # only refuses to blur an oversized image, it does not resize the
            # box to fit it. Left unconfigured (and propagating, the default),
            # the frame just takes the size of the image once it is shown.
            prep = ImageView(block, "-", allow_shrink=False)
            prep.pack(fill="x")
            raw = ImageView(block, "-", allow_shrink=False)
            raw.pack(fill="x")
            caption = ttk.Label(block, text="", foreground=HELP_COLOR, anchor="center")
            caption.pack(fill="x")
            HintEntry(block, hint="should read", textvariable=self._expect[index],
                      justify="center").pack(fill="x", pady=(0, 3), padx=2)
            self._views.append((prep, raw, caption))

        tuner = ttk.LabelFrame(body, text="  Sharpening tuner  ", padding=PAD)
        tuner.pack(fill="x", pady=(10, 0))
        row = ttk.Frame(tuner)
        row.pack(fill="x")
        ttk.Button(row, text="Add this page", width=14,
                   command=self.add_tuning_page).pack(side="left")
        ttk.Button(row, text="Clear queue", width=11,
                   command=self.clear_tuning_queue).pack(side="left", padx=(6, 0))
        self.queue_label = ttk.Label(row, text="No pages queued yet.")
        self.queue_label.pack(side="left", padx=(12, 0))
        self.save_button = ttk.Button(row, text="Save suggested settings",
                                      command=self.save_tuning_result, state="disabled")
        self.save_button.pack(side="right")
        ttk.Button(row, text="Run tuning", width=11,
                   command=self.run_tuning).pack(side="right", padx=(0, 10))
        help_label(
            tuner,
            "Type what a cell should read above, leave the rest blank, then Add this page -- "
            "it copies out the cells you typed against, so reading a different page afterwards "
            "cannot change what an already-queued page is checked against. Read a different "
            "page (or point Display at the other one) and add that too, to cover more than one "
            "page in the same search. Run tuning then searches sharpening, "
            "upscaling and thresholding settings and keeps only a change that fixes a queued "
            "cell without making any other queued cell -- on any page -- read wrong. Save "
            "suggested settings writes what it found into config.toml.",
            width=900,
        ).pack(anchor="w", pady=(6, 0))

        self.output = OutputPane(body, height=8)
        self.output.pack(fill="both", expand=True, pady=(10, 0))
        app.on_config_changed(self.refresh)

    def refresh(self) -> None:
        keys = self.app.display_keys()
        self.display_box.configure(values=keys)
        if self.display.get() not in keys:
            self.display.set(keys[0] if keys else "")
        self._show()

    def dump(self) -> None:
        self.app.run_task(commands.DUMP_CELLS, {"out": self.out.get()}, self.output,
                          on_finish=lambda _code, _lines: self._show())

    def _show(self) -> None:
        key = self.display.get()
        folder = Path(self.out.get())
        for index, (prep, raw, caption) in enumerate(self._views, start=1):
            prep_path = folder / f"{key}_{index:02d}_prep.png"
            raw_path = folder / f"{key}_{index:02d}_raw.png"
            prep.show(prep_path if prep_path.is_file() else None)
            raw.show(raw_path if raw_path.is_file() else None)
            caption.configure(text="prep / raw" if prep_path.is_file() else "")

    def _open_folder(self) -> None:
        self.show_output_folder(
            commands.DUMP_CELLS, {"out": self.out.get()},
            "There is nothing there yet -- read the cells first.",
        )

    # -- the sharpening tuner --------------------------------------------

    def add_tuning_page(self) -> None:
        key = self.display.get()
        folder = Path(self.out.get())
        expect = {i + 1: v.get().strip().upper() for i, v in enumerate(self._expect)
                 if v.get().strip()}
        if not expect:
            self.app.set_status(
                "Type what at least one cell should read before adding this page.", "warning"
            )
            return
        missing = [i for i in expect if not (folder / f"{key}_{i:02d}_raw.png").is_file()]
        if missing:
            self.app.set_status(
                "Read the cells first -- there is no picture for this display yet.", "warning"
            )
            return
        # Copied out now, into a folder of this page's own: "Read the cells"
        # for a second page overwrites the same out/{key}_{cell:02d}_raw.png
        # files in place (that is what makes it "the" cells folder rather
        # than one per capture), so a case still pointing at out/ would
        # silently start being judged against a *different* page's pixels
        # the moment the next page was captured -- this page's expected
        # labels, that page's cells. Queuing this page has to freeze what it
        # looked like at the moment it was added.
        snapshot = self.app.project_root / "tuning" / "queue" / f"page{len(self._tuning_cases) + 1}"
        snapshot.mkdir(parents=True, exist_ok=True)
        for cell in expect:
            shutil.copy2(folder / f"{key}_{cell:02d}_raw.png", snapshot / f"{key}_{cell:02d}_raw.png")
        self._tuning_cases.append({"dir": str(snapshot), "display": key, "expect": expect})
        for v in self._expect:
            v.set("")
        self._update_queue_label()
        self.app.set_status(
            f"Queued {key} ({len(expect)} cell(s)). Capture a different page and add it too, "
            "or press Run tuning."
        )

    def clear_tuning_queue(self) -> None:
        self._tuning_cases = []
        self._suggested = None
        self.save_button.configure(state="disabled")
        self._update_queue_label()
        shutil.rmtree(self.app.project_root / "tuning" / "queue", ignore_errors=True)

    def _update_queue_label(self) -> None:
        if not self._tuning_cases:
            self.queue_label.configure(text="No pages queued yet.")
            return
        cells = sum(len(case["expect"]) for case in self._tuning_cases)
        pages = len(self._tuning_cases)
        self.queue_label.configure(
            text=f"{pages} page{'s' if pages != 1 else ''} queued, {cells} cell(s)."
        )

    def run_tuning(self) -> None:
        if not self._tuning_cases:
            self.app.set_status("Add at least one page to the queue first.", "warning")
            return
        truth_path = self.app.project_root / "tuning" / "truth.toml"
        try:
            configio.save_text(truth_path, configio.dumps_truth(self._tuning_cases), backup=False)
        except configio.ConfigIoError as exc:
            self.app.set_status(str(exc), "error")
            return
        self._suggested = None
        self.save_button.configure(state="disabled")
        self.app.run_task(
            commands.TUNE, {"truth": str(truth_path)}, self.output,
            include_image=False, on_finish=self._tuning_finished,
        )

    def _tuning_finished(self, _code: int, lines: list[str]) -> None:
        self._suggested = parse_tuning_result(lines)
        self.save_button.configure(state="normal" if self._suggested else "disabled")

    def save_tuning_result(self) -> None:
        if not self._suggested:
            return
        document = copy.deepcopy(self.app.document)
        for key in ("psm", "threshold", "upscale", "sharpen_ladder"):
            if key in self._suggested:
                configio.set_in(document, ("ocr", key), self._suggested[key])
        base = self.app.config_path.parent if self.app.config_path else None
        try:
            configio.validate(document, base_dir=base)
        except Exception as exc:  # noqa: BLE001 - every failure is a message to show
            messagebox.showerror("G1000 softkey labels",
                                 f"Those settings will not load:\n\n{exc}",
                                 parent=self.app.root)
            return
        if self.app.config_path is None:
            messagebox.showinfo(
                "G1000 softkey labels",
                "There is no configuration file open yet. Press New... at the top of the "
                "window to make one, then run tuning again.",
                parent=self.app.root,
            )
            return
        try:
            backup = configio.save(self.app.config_path, document)
        except configio.ConfigIoError as exc:
            self.app.set_status(str(exc), "error")
            return
        self.app.document = document
        self.app.notify_config_changed()
        self.app.set_status(
            "Saved the suggested OCR settings"
            + (f" (previous version kept as {backup.name})" if backup else "")
            + ". Restart the daemon to use them."
        )


# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------


class ColorsTab(Tab):
    """Measure the softkey background colours and check the thresholds."""

    tab_title = "Colours"

    def __init__(self, app) -> None:
        super().__init__(app)
        controls = ttk.Frame(self)
        controls.pack(fill="x")
        ttk.Button(controls, text="Measure colours", width=16,
                   command=self.measure).pack(side="left")
        ttk.Button(controls, text="Colour settings", width=16,
                   command=self._go_to_settings).pack(side="left", padx=6)

        help_label(
            self,
            "The four background states -- normal, selected, caution, warning -- are "
            "published next to each label so a button can show them. The thresholds that "
            "tell them apart shipped as plausible guesses: nobody who wrote them had ever "
            "seen an X-Plane frame. This is the measurement that replaces the guess, so "
            "run it against your own display with one softkey selected, and if a cell is "
            "named wrong, move the threshold that misfired.",
            width=900,
        ).pack(anchor="w", pady=(6, 8))

        columns = ("display", "cell", "bgr", "hsv", "name")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=13)
        for name, heading, width in (
            ("display", "Display", 90), ("cell", "Cell", 60),
            ("bgr", "Blue / Green / Red", 190), ("hsv", "Hue / Sat / Value", 190),
            ("name", "Read as", 120),
        ):
            self.tree.heading(name, text=heading)
            self.tree.column(name, width=width, anchor="w")
        for value, name in BACKGROUND_NAMES.items():
            background, foreground = CELL_COLORS.get(value, CELL_COLORS[BLACK])
            self.tree.tag_configure(name, background=background, foreground=foreground)
        self.tree.pack(fill="both", expand=True)

        self.output = OutputPane(self, height=10)
        self.output.pack(fill="both", expand=True, pady=(10, 0))

    def measure(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.app.run_task(commands.DUMP_COLORS, {}, self.output, on_finish=self._parse)

    def _parse(self, code: int, lines: list[str]) -> None:
        if code != 0:
            return
        measured = parse_colors(lines)
        rows = len(measured)
        for row in measured:
            self.tree.insert("", "end", tags=(row.name,), values=(
                row.display, row.cell,
                " / ".join(str(v) for v in row.bgr),
                " / ".join(str(v) for v in row.hsv),
                row.name,
            ))
        self.app.set_status(
            f"{rows} cell(s) measured. A cell named wrongly means a threshold to move, "
            "not an OCR problem." if rows else "Nothing was measured.",
            "info" if rows else "warning",
        )

    def _go_to_settings(self) -> None:
        self.app.select_tab(tab_index("SettingsTab"))
        self.app.set_status("The colour thresholds are under Reading and colour.")


# ---------------------------------------------------------------------------
# shared: resolving the files the config points at
# ---------------------------------------------------------------------------


def config_path_setting(app, key: str) -> Path:
    """The file ``ocr.<key>`` points at, resolved the way the daemon resolves it.

    ``load_config`` treats a relative path in the config as relative to the
    config file, not to the working directory, and the GUI has to agree with
    it -- otherwise the vocabulary the user edits here is not the one the
    daemon reads.
    """
    from ..config import OcrConfig  # noqa: PLC0415 - only needed for the default

    raw = configio.get_in(app.document, ("ocr", key)) or getattr(OcrConfig(), key)
    path = Path(str(raw))
    if not path.is_absolute() and app.config_path is not None:
        path = (app.config_path.parent / path).resolve()
    return path


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


class PagesTab(Tab):
    """Teach the daemon which softkey pages exist."""

    tab_title = "Pages"

    def __init__(self, app) -> None:
        super().__init__(app)
        self.display = tk.StringVar(value="")
        self.page_name = tk.StringVar(value="")
        self._block: list[str] = []

        help_label(
            self,
            "Reading a ten-pixel digit is hard. Recognising which softkey page is showing, "
            "from the labels that did read cleanly, is easy -- and once the page is known, "
            "the hard cells can simply be looked up instead of guessed at. This tool "
            "builds that knowledge from your own display.",
            width=900,
        ).pack(anchor="w", pady=(0, 10))

        # -- screen template --------------------------------------------------
        template = ttk.LabelFrame(self, text="  Record a page  ", padding=PAD)
        template.pack(fill="x")
        row = ttk.Frame(template)
        row.pack(fill="x")
        ttk.Label(row, text="Display").pack(side="left")
        self.display_box = ttk.Combobox(row, textvariable=self.display, state="readonly", width=8)
        self.display_box.pack(side="left", padx=(4, 16))
        ttk.Label(row, text="Call this page").pack(side="left")
        ttk.Entry(row, textvariable=self.page_name, width=22).pack(side="left", padx=4)
        ttk.Button(row, text="Read it", width=10, command=self.read_page).pack(side="left", padx=8)
        self.append_button = ttk.Button(row, text="Add it to the pages file", state="disabled",
                                        command=self.append_page)
        self.append_button.pack(side="left")
        help_label(
            template,
            "Put the softkey page you want to record on screen, give it a name, and read "
            "it. The cells that read confidently become the ones that identify the page; "
            "anything read below the confidence floor is listed for you to correct first. "
            "Check the block before adding it -- a wrong label recorded here would be "
            "filled into every later frame that matches this page.",
            width=880,
        ).pack(anchor="w", pady=(6, 0))

        self.output = OutputPane(self, height=14)
        self.output.pack(fill="both", expand=True, pady=(10, 0))
        app.on_config_changed(self.refresh)

    def refresh(self) -> None:
        keys = self.app.display_keys()
        self.display_box.configure(values=keys)
        if self.display.get() not in keys:
            self.display.set(keys[0] if keys else "")

    def read_page(self) -> None:
        name = self.page_name.get().strip()
        if not name:
            self.app.set_status("Give the page a name first.", "warning")
            return
        self._block = []
        self.append_button.configure(state="disabled")
        self.app.run_task(
            commands.SCREEN_TEMPLATE,
            {"display": self.display.get(), "name": name},
            self.output, on_finish=self._captured,
        )

    def _captured(self, code: int, lines: list[str]) -> None:
        if code != 0:
            return
        block = parse_screen_block(lines)
        self._block = block
        if block:
            self.append_button.configure(state="normal")
            self.app.set_status(
                "Page read. Check the block above -- especially any cell it flagged -- "
                "then add it to the pages file."
            )

    def append_page(self) -> None:
        if not self._block:
            return
        path = config_path_setting(self.app, "screens_file")
        if not messagebox.askokcancel(
            "G1000 softkey labels",
            f"Add this page to\n{path}\n\nAnything the command flagged as unsure is added "
            "as a comment, not as a label. You can edit the file afterwards.",
            parent=self.app.root,
        ):
            return
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n" + "\n".join(self._block) + "\n")
        except OSError as exc:
            self.app.set_status(f"Could not write {path}: {exc}", "error")
            return
        self.append_button.configure(state="disabled")
        self.app.set_status(f"Added to {path.name}.")


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class VocabularyTab(Tab):
    """Edit labels.txt -- the list of labels a reading is corrected to."""

    tab_title = "Vocabulary"

    def __init__(self, app) -> None:
        super().__init__(app)
        self.path_text = tk.StringVar(value="")

        controls = ttk.Frame(self)
        controls.pack(fill="x")
        ttk.Button(controls, text="Save", width=9, command=self.save).pack(side="left")
        ttk.Button(controls, text="Reload", width=9, command=self.reload).pack(side="left", padx=6)
        ttk.Label(controls, textvariable=self.path_text, foreground=HELP_COLOR).pack(
            side="left", padx=(12, 0)
        )

        help_label(
            self,
            "Every reading is compared against this list and snapped to the closest entry, "
            "which is what turns a misread lNSET into INSET. It is aircraft and version "
            "dependent, so add anything your G1000 shows that is missing -- a label that "
            "is not in here is passed through exactly as it was read rather than being "
            "forced onto the wrong entry. One label per line; lines starting with # are "
            "comments. Save, then restart the daemon to pick it up.",
            width=900,
        ).pack(anchor="w", pady=(6, 8))

        holder = ttk.Frame(self)
        holder.pack(fill="both", expand=True)
        self.text = tk.Text(holder, wrap="none", undo=True, relief="solid", borderwidth=1,
                            padx=6, pady=4)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        app.on_config_changed(self.reload)

    def _path(self) -> Path:
        return config_path_setting(self.app, "labels_file")

    def reload(self) -> None:
        path = self._path()
        self.path_text.set(str(path))
        self.text.delete("1.0", "end")
        try:
            self.text.insert("1.0", path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # ValueError covers UnicodeDecodeError: a file that is not UTF-8
            # is a thing to say in the pane, not an exception into Tk.
            self.text.insert("1.0", f"# could not read {path}: {exc}\n")
        self.text.edit_reset()

    def save(self) -> None:
        path = self._path()
        content = self.text.get("1.0", "end-1c")
        if not content.endswith("\n"):
            content += "\n"
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            self.app.set_status(f"Could not write {path}: {exc}", "error")
            return
        self.app.set_status(
            f"Saved {path.name}. Restart the daemon for it to take effect."
        )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class SettingsTab(Tab):
    """Every setting in the config file, as a form, generated from the schema.

    Laid out from ``schema.GROUPS`` rather than by hand so that a setting
    added to the daemon cannot quietly fail to appear here -- see the coverage
    test in ``tests/test_gui_schema.py``.
    """

    tab_title = "Settings"

    def __init__(self, app) -> None:
        super().__init__(app)
        #: (path in the document, setting, Tk variable)
        self._fields: list[tuple[tuple[str, ...], schema.Setting, tk.Variable]] = []
        self._raw_loaded = ""

        controls = ttk.Frame(self)
        controls.pack(fill="x")
        ttk.Button(controls, text="Save", width=9, command=self.save).pack(side="left")
        ttk.Button(controls, text="Undo changes", width=14, command=self.revert).pack(side="left", padx=6)
        ttk.Button(controls, text="Check", width=9, command=self.check).pack(side="left")
        help_label(
            controls,
            "Saving rewrites the whole file and keeps the previous version as .bak. "
            "Hand-written comments are not preserved by the form -- use the Raw file tab "
            "if you keep notes in there.",
            width=560,
        ).pack(side="left", padx=(12, 0))

        self.inner = ttk.Notebook(self, padding=(4, 6))
        self.inner.pack(fill="both", expand=True, pady=(8, 0))

        self._pages: dict[str, ScrollableFrame] = {}
        for name in ("Loop and displays", "Reading and colour", "Publishing"):
            page = ScrollableFrame(self.inner)
            self.inner.add(page, text=f" {name} ")
            self._pages[name] = page

        raw_page = ttk.Frame(self.inner, padding=PAD)
        self.inner.add(raw_page, text=" Raw file ")
        help_label(
            raw_page,
            "The configuration file exactly as it is on disk. Saving from here writes your "
            "text through unchanged, comments and all -- it is only checked for being valid "
            "TOML first. config.example.toml carries the reasoning behind every default.",
            width=900,
        ).pack(anchor="w", pady=(0, 6))
        raw_controls = ttk.Frame(raw_page)
        raw_controls.pack(fill="x", pady=(0, 6))
        ttk.Button(raw_controls, text="Save this text", width=15,
                   command=self.save_raw).pack(side="left")
        ttk.Button(raw_controls, text="Reload from disk", width=17,
                   command=self.reload_raw).pack(side="left", padx=6)
        holder = ttk.Frame(raw_page)
        holder.pack(fill="both", expand=True)
        self.raw = tk.Text(holder, wrap="none", undo=True, relief="solid", borderwidth=1,
                           padx=6, pady=4)
        raw_scroll = ttk.Scrollbar(holder, orient="vertical", command=self.raw.yview)
        self.raw.configure(yscrollcommand=raw_scroll.set)
        self.raw.pack(side="left", fill="both", expand=True)
        raw_scroll.pack(side="right", fill="y")

        app.on_config_changed(self.refresh)

    # -- building the form ---------------------------------------------------

    def refresh(self) -> None:
        for page in self._pages.values():
            for child in page.body.winfo_children():
                child.destroy()
        self._fields = []

        first = self._pages["Loop and displays"].body
        self._add_group(first, schema.APP, ("app",))
        for key in self.app.display_keys():
            self._add_group(first, schema.DISPLAY, ("display", key),
                            title=f"Display \"{key}\" -- window")
            self._add_group(first, schema.GEOMETRY, ("display", key, "geometry"),
                            title=f"Display \"{key}\" -- softkey strip position")

        second = self._pages["Reading and colour"].body
        self._add_group(second, schema.OCR, ("ocr",))
        self._add_group(second, schema.COLOR, ("color",))

        self._add_group(self._pages["Publishing"].body, schema.PUBLISH, ("publish",))
        self.reload_raw()

    def _add_group(self, parent: tk.Misc, group: schema.Group, path: tuple[str, ...],
                   title: str = "") -> None:
        frame = ttk.LabelFrame(parent, text=f"  {title or group.title}  ", padding=PAD)
        frame.pack(fill="x", expand=True, pady=(0, 10), padx=2)
        help_label(frame, group.blurb, width=820).pack(anchor="w", pady=(0, 8))
        # The fields go in a frame of their own: Tk refuses to manage one
        # container with both pack and grid, and the blurb above is packed.
        fields = ttk.Frame(frame)
        fields.pack(fill="x")
        fields.columnconfigure(1, weight=1)
        row = 0
        for setting in group.settings:
            row = self._add_field(fields, row, setting, (*path, setting.key),
                                  section=group.section)
        return None

    def _add_field(self, parent: tk.Misc, row: int, setting: schema.Setting,
                   path: tuple[str, ...], section: str = "") -> int:
        value = configio.get_in(self.app.document, path)
        if value is None and not setting.optional:
            value = self._default_for(path, setting)

        ttk.Label(parent, text=setting.label).grid(row=row, column=0, sticky="nw",
                                                   padx=(0, 10), pady=(2, 0))
        variable: tk.Variable
        if setting.kind == "bool":
            variable = tk.BooleanVar(value=bool(value))
            widget: tk.Widget = ttk.Checkbutton(parent, variable=variable)
        elif setting.kind == "choice":
            variable = tk.StringVar(value=configio.format_field(setting, value))
            widget = ttk.Combobox(parent, textvariable=variable, values=list(setting.choices),
                                  state="readonly", width=16)
        else:
            variable = tk.StringVar(value=configio.format_field(setting, value))
            # The hint is drawn beside an empty box, never typed into it: a
            # box holding the package's own path is a box whose contents get
            # written into config.toml on the next Save, which pins the
            # configuration to this install. An empty box means "whatever the
            # package ships with", and it has to stay reachable.
            widget = HintEntry(parent, textvariable=variable,
                               hint=self._hint_for(setting, section))
        widget.grid(row=row, column=1, sticky="ew", pady=(2, 0))
        self._fields.append((path, setting, variable))
        row += 1
        if setting.help:
            note = setting.help
            if setting.package_default:
                note += "  (leave empty to use the file that comes with the package)"
            elif setting.optional:
                note += "  (leave empty to leave it unset)"
            help_label(parent, note, width=640).grid(row=row, column=1, sticky="w", pady=(0, 6))
            row += 1
        return row

    def _hint_for(self, setting: schema.Setting, section: str) -> str:
        """What an empty box will actually use, for the hint drawn inside it.

        Only for the settings whose default is a file inside the installed
        package. Those are the ones where an empty box does something
        specific and invisible, and where showing the answer as a *value*
        would write this checkout's path into the user's config file.
        """
        if not (section and setting.package_default):
            return ""
        default = schema.default_value(section, setting)
        return f"{default}  (the one that comes with the package)" if default else ""

    def _default_for(self, path: tuple[str, ...], setting: schema.Setting) -> Any:
        """The built-in default, so an absent key shows what it will actually be."""
        defaults = configio.default_document()
        if path[0] == "display" and len(path) >= 3:
            # Any display's defaults will do -- they are the same for all of
            # them, and the one in this document may not be in the defaults.
            first = next(iter(defaults["display"].values()))
            return configio.get_in({"d": first}, ("d", *path[2:]))
        return configio.get_in(defaults, path)

    # -- saving --------------------------------------------------------------

    def _collect(self) -> dict[str, Any] | None:
        # A deep copy, so a form value the daemon rejects leaves the window's
        # own document exactly as it was rather than half edited.
        document = copy.deepcopy(self.app.document)
        try:
            for path, setting, variable in self._fields:
                raw = variable.get()
                text = "true" if raw is True else "false" if raw is False else str(raw)
                configio.set_in(document, path, configio.parse_field(setting, text))
        except configio.ConfigIoError as exc:
            self.app.set_status(str(exc), "error")
            messagebox.showwarning("G1000 softkey labels", str(exc), parent=self.app.root)
            return None
        return document

    def check(self) -> None:
        document = self._collect()
        if document is None:
            return
        base = self.app.config_path.parent if self.app.config_path else None
        try:
            configio.validate(document, base_dir=base)
        except Exception as exc:  # noqa: BLE001 - every failure is a message to show
            messagebox.showerror("G1000 softkey labels",
                                 f"These settings will not load:\n\n{exc}",
                                 parent=self.app.root)
            self.app.set_status(str(exc), "error")
            return
        self.app.set_status("These settings are valid.")

    def save(self) -> None:
        document = self._collect()
        if document is None:
            return
        base = self.app.config_path.parent if self.app.config_path else None
        try:
            configio.validate(document, base_dir=base)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("G1000 softkey labels",
                                 f"These settings will not load, so they were not saved:\n\n{exc}",
                                 parent=self.app.root)
            return
        if self.app.config_path is None:
            messagebox.showinfo(
                "G1000 softkey labels",
                "There is no configuration file open yet. Press New... at the top of the "
                "window to make one, then save again.",
                parent=self.app.root,
            )
            return
        try:
            backup = configio.save(self.app.config_path, document)
        except configio.ConfigIoError as exc:
            messagebox.showerror("G1000 softkey labels", str(exc), parent=self.app.root)
            return
        self.app.document = document
        self.app.notify_config_changed()
        self.app.set_status(
            f"Saved {self.app.config_path.name}"
            + (f" (previous version kept as {backup.name})" if backup else "")
            + ". Restart the daemon for it to take effect."
        )

    def revert(self) -> None:
        self.app.load_config()

    # -- raw editor ----------------------------------------------------------

    def reload_raw(self) -> None:
        self.raw.delete("1.0", "end")
        if self.app.config_path is None:
            self._raw_loaded = ""
            self.raw.insert("1.0",
                            "# No configuration file open. Press New... at the top of the "
                            "window to make one.\n")
            return
        try:
            self._raw_loaded = self.app.config_path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:  # ValueError: not UTF-8 text
            self._raw_loaded = ""
            self.raw.insert("1.0", f"# could not read {self.app.config_path}: {exc}\n")
            return
        self.raw.insert("1.0", self._raw_loaded)
        self.raw.edit_reset()

    def save_raw(self) -> None:
        if self.app.config_path is None:
            messagebox.showinfo("G1000 softkey labels",
                                "Press New... at the top of the window to make a "
                                "configuration file first.",
                                parent=self.app.root)
            return
        text = self.raw.get("1.0", "end-1c")
        try:
            backup = configio.save_text(self.app.config_path, text)
        except configio.ConfigIoError as exc:
            messagebox.showerror("G1000 softkey labels", str(exc), parent=self.app.root)
            return
        self.app.load_config(quiet=True)
        self.app.set_status(
            f"Saved {self.app.config_path.name}"
            + (f" (previous version kept as {backup.name})" if backup else "")
            + ". Restart the daemon for it to take effect."
        )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class ToolsTab(Tab):
    """The commands that are neither setup nor running: timing and test data."""

    tab_title = "Tools"

    def __init__(self, app) -> None:
        super().__init__(app)
        self.iterations = tk.StringVar(value="50")
        self.frames_out = tk.StringVar(value=str(app.project_root / "frames"))

        bench = ttk.LabelFrame(self, text="  How long does it take?  ", padding=PAD)
        bench.pack(fill="x")
        row = ttk.Frame(bench)
        row.pack(fill="x")
        ttk.Button(row, text="Measure", width=10, command=self.bench).pack(side="left")
        ttk.Label(row, text="Frames").pack(side="left", padx=(16, 4))
        ttk.Entry(row, textvariable=self.iterations, width=6).pack(side="left")
        help_label(
            bench,
            "Times each stage of the pipeline, with and without change gating, and reports "
            "what share of one processor core the daemon would use at the configured rate. "
            "Reading a display fully costs tens of milliseconds; a frame where nothing "
            "moved costs a fraction of one, which is why the rate can be as high as it is.",
            width=880,
        ).pack(anchor="w", pady=(6, 0))

        frames = ttk.LabelFrame(self, text="  Test frames  ", padding=PAD)
        frames.pack(fill="x", pady=(10, 0))
        row = ttk.Frame(frames)
        row.pack(fill="x")
        ttk.Button(row, text="Write frames", width=13, command=self.synth).pack(side="left")
        ttk.Label(row, text="Into").pack(side="left", padx=(16, 4))
        ttk.Entry(row, textvariable=self.frames_out).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Use them", width=11,
                   command=self.use_frames).pack(side="left", padx=(6, 0))
        help_label(
            frames,
            "Synthetic softkey strips for trying the pipeline out with no X-Plane running. "
            "They are drawn with an ordinary system font, not the G1000's, so they are "
            "good for checking that everything is wired up and useless for judging how "
            "well the reading will work on your display.",
            width=880,
        ).pack(anchor="w", pady=(6, 0))

        self.output = OutputPane(self, height=16)
        self.output.pack(fill="both", expand=True, pady=(10, 0))

    def bench(self) -> None:
        self.app.run_task(commands.BENCH, {"iterations": self.iterations.get().strip()},
                          self.output)

    def synth(self) -> None:
        self.app.run_task(commands.SYNTH, {"out": self.frames_out.get()}, self.output,
                          include_image=False)

    def use_frames(self) -> None:
        self.app.image_source.set(self.frames_out.get())
        self.app.set_status("The frame source now points at the test frames.")


#: The tab order.
TAB_CLASSES = (
    StartTab, RunTab, WindowsTab, CalibrateTab, CellsTab,
    ColorsTab, PagesTab, VocabularyTab, SettingsTab, ToolsTab,
)


def tab_index(class_name: str) -> int:
    """Where a tab sits in the strip, looked up by class name.

    The cross-references between tabs -- the walkthrough's "Go there" buttons,
    the Colours tab pointing at the colour settings -- go through this rather
    than through a literal, so reordering TAB_CLASSES moves them with it
    instead of quietly sending the user somewhere else.
    """
    for index, cls in enumerate(TAB_CLASSES):
        if cls.__name__ == class_name:
            return index
    raise KeyError(f"no tab class named {class_name!r}")


def build_tabs(app) -> list[Tab]:
    return [cls(app) for cls in TAB_CLASSES]
