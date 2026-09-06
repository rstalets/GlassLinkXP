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
import re
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Any

from ..color import BACKGROUND_NAMES, BLACK
from . import commands, configio, schema
from .logparse import classify, parse_health, parse_row
from .runner import Event, Failed, Finished, Line, Started
from .widgets import (
    CELL_COLORS,
    HELP_COLOR,
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
        self.publisher = tk.StringVar(value=str(app.prefs.get("publisher") or ""))
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


#: A line of `list-windows` output:
#:   hwnd=0x00010F42 pid=1234   1288x832 class='X-Plane' title='G1000 PFD'
_WINDOW_LINE = re.compile(
    r"hwnd=(?P<hwnd>\S+)\s+pid=(?P<pid>\d+)\s+(?P<size>\d+x\d+)\s+"
    r"class='(?P<cls>.*?)'\s+title='(?P<title>.*)'\s*$"
)


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
            match = _WINDOW_LINE.search(line)
            if match is None:
                continue
            found += 1
            self.tree.insert("", "end", values=(
                match.group("title"), match.group("size"),
                match.group("cls"), match.group("pid"),
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


_FRAME_HEADER = re.compile(r"\[(?P<display>[A-Za-z0-9_.-]+)\]\s+frame\s+(?P<size>\d+x\d+)")
_AUTO_DETECT = re.compile(
    r"auto-detect:\s*x=(?P<x>[\d.]+)\s+y=(?P<y>[\d.]+)\s+w=(?P<w>[\d.]+)\s+h=(?P<h>[\d.]+)"
)

#: The pictures `calibrate` writes, and what each is for.
CALIBRATION_VIEWS: tuple[tuple[str, str, str], ...] = (
    ("overlay", "Boxes on the frame",
     "The window as captured, with the twelve boxes the reader will use drawn on it. "
     "Each box must sit around exactly one label, with none of the bezel or the moving "
     "map inside it. This is the picture to judge the calibration by."),
    ("overlay_auto", "Auto-detected boxes",
     "The same, using the geometry the auto-detect suggested. It finds the dark band "
     "reliably but only approximates the left and right edges, so treat it as a starting "
     "point and then nudge the numbers."),
    ("strip", "The strip alone", "Just the part of the frame that gets read."),
    ("raw", "The whole frame", "Everything that was captured, before any cropping."),
)

GEOMETRY_KEYS = ("x", "y", "w", "h", "cell_pad_x", "cell_pad_y")


class CalibrateTab(Tab):
    """Show the reader where the softkey strip is."""

    tab_title = "Calibrate"

    def __init__(self, app) -> None:
        super().__init__(app)
        self.display = tk.StringVar(value="")
        self.view = tk.StringVar(value="overlay")
        self.out = tk.StringVar(value=str(app.project_root / "calibration"))
        self._suggested: dict[str, dict[str, float]] = {}
        self._fields: dict[str, tk.StringVar] = {}

        controls = ttk.Frame(self)
        controls.pack(fill="x")
        ttk.Button(controls, text="Take a picture", width=15,
                   command=self.calibrate).pack(side="left")
        ttk.Label(controls, text="Display").pack(side="left", padx=(16, 4))
        self.display_box = ttk.Combobox(controls, textvariable=self.display,
                                        state="readonly", width=8)
        self.display_box.pack(side="left")
        self.display_box.bind("<<ComboboxSelected>>", lambda _e: self._show())
        ttk.Label(controls, text="Show").pack(side="left", padx=(16, 4))
        self.view_box = ttk.Combobox(
            controls, textvariable=self.view, state="readonly", width=20,
            values=[title for _key, title, _help in CALIBRATION_VIEWS],
        )
        self.view_box.pack(side="left")
        self.view_box.set(CALIBRATION_VIEWS[0][1])
        self.view_box.bind("<<ComboboxSelected>>", lambda _e: self._show())
        ttk.Button(controls, text="Open folder", width=12,
                   command=self._open_folder).pack(side="right")

        self.view_help = help_label(self, CALIBRATION_VIEWS[0][2], width=900)
        self.view_help.pack(anchor="w", pady=(6, 6))

        middle = ttk.Frame(self)
        middle.pack(fill="both", expand=True)
        self.image = ImageView(middle, "Press \"Take a picture\" to capture the display "
                                       "and see where the reader is looking.")
        self.image.pack(side="left", fill="both", expand=True)

        side = ttk.Frame(middle, padding=(PAD, 0, 0, 0))
        side.pack(side="right", fill="y")
        section_heading(side, "Strip position").pack(anchor="w")
        help_label(side, "Fractions of the window, so a resize does not undo it.",
                   width=240).pack(anchor="w", pady=(0, 6))
        form = ttk.Frame(side)
        form.pack(fill="x")
        for row, key in enumerate(GEOMETRY_KEYS):
            variable = tk.StringVar(value="")
            self._fields[key] = variable
            ttk.Label(form, text=schema.setting("geometry", key).label).grid(
                row=row, column=0, sticky="w", pady=1
            )
            ttk.Entry(form, textvariable=variable, width=10).grid(row=row, column=1, padx=(8, 0))
        self.suggestion = help_label(side, "", width=240)
        self.suggestion.pack(anchor="w", pady=(8, 4))
        self.apply_button = ttk.Button(side, text="Use the suggestion", state="disabled",
                                       command=self._apply_suggestion)
        self.apply_button.pack(fill="x")
        ttk.Button(side, text="Save and take another",
                   command=self._save_and_recalibrate).pack(fill="x", pady=(6, 0))

        self.output = OutputPane(self, height=7)
        self.output.pack(fill="both", expand=True, pady=(10, 0))
        app.on_config_changed(self.refresh)

    def refresh(self) -> None:
        keys = self.app.display_keys()
        self.display_box.configure(values=keys)
        if self.display.get() not in keys:
            self.display.set(keys[0] if keys else "")
        self._load_geometry()
        self._show()

    def _load_geometry(self) -> None:
        key = self.display.get()
        for name, variable in self._fields.items():
            value = configio.get_in(self.app.document, ("display", key, "geometry", name))
            setting = schema.setting("geometry", name)
            variable.set(configio.format_field(setting, value) if value is not None else "")

    def _collect_geometry(self) -> dict[str, float] | None:
        values: dict[str, float] = {}
        for name, variable in self._fields.items():
            try:
                values[name] = configio.parse_field(schema.setting("geometry", name),
                                                    variable.get())
            except configio.ConfigIoError as exc:
                self.app.set_status(str(exc), "error")
                return None
        return values

    def calibrate(self) -> None:
        self._suggested = {}
        self.apply_button.configure(state="disabled")
        self.suggestion.configure(text="")
        self.app.run_task(
            commands.CALIBRATE, {"out": self.out.get()}, self.output, on_finish=self._parse
        )

    def _parse(self, code: int, lines: list[str]) -> None:
        current = ""
        for line in lines:
            header = _FRAME_HEADER.search(line)
            if header:
                current = header.group("display")
                continue
            auto = _AUTO_DETECT.search(line)
            if auto and current:
                self._suggested[current] = {k: float(auto.group(k)) for k in ("x", "y", "w", "h")}
        self._show()
        key = self.display.get()
        if key in self._suggested:
            values = self._suggested[key]
            self.suggestion.configure(
                text="Auto-detect suggests  " + "  ".join(
                    f"{name}={values[name]:.4f}" for name in ("x", "y", "w", "h")
                ) + ". It gets the band right and the left and right edges only roughly, "
                    "so check the picture afterwards."
            )
            self.apply_button.configure(state="normal")
        elif code == 0:
            self.suggestion.configure(
                text="No dark softkey band was found, so there is nothing to suggest. "
                     "Set the numbers by hand against the picture."
            )
        if code == 0:
            self.app.set_status(
                "Calibration pictures written. Check that every box sits around exactly "
                "one label."
            )

    def _apply_suggestion(self) -> None:
        key = self.display.get()
        for name, value in self._suggested.get(key, {}).items():
            self._fields[name].set(repr(round(value, 4)))
        self.app.set_status("Suggestion filled in. Save and take another picture to check it.")

    def _save_and_recalibrate(self) -> None:
        values = self._collect_geometry()
        if values is None:
            return
        key = self.display.get()
        # Edited on a copy and only adopted once it validates. Writing into
        # the live document first would leave the window holding a geometry
        # the daemon will not accept, with nothing on screen saying so.
        document = copy.deepcopy(self.app.document)
        for name, value in values.items():
            configio.set_in(document, ("display", key, "geometry", name), value)
        base = self.app.config_path.parent if self.app.config_path else None
        try:
            configio.validate(document, base_dir=base)
        except Exception as exc:  # noqa: BLE001 - every failure is a message to show
            messagebox.showerror("G1000 softkey labels",
                                 f"That geometry will not load:\n\n{exc}",
                                 parent=self.app.root)
            return
        if self.app.config_path is not None:
            try:
                configio.save(self.app.config_path, document)
            except configio.ConfigIoError as exc:
                self.app.set_status(str(exc), "error")
                return
        self.app.document = document
        self.app.notify_config_changed()
        self.calibrate()

    def _view_key(self) -> str:
        title = self.view_box.get()
        for key, name, text in CALIBRATION_VIEWS:
            if name == title:
                self.view_help.configure(text=text)
                return key
        return "overlay"

    def _show(self) -> None:
        key = self.display.get()
        view = self._view_key()
        path = Path(self.out.get()) / f"{key}_{view}.png"
        self.image.show(path if path.is_file() else None)
        if not path.is_file() and view == "overlay_auto":
            self.image.label.configure(
                text="No auto-detected picture for this display -- the dark softkey band "
                     "was not found, so there was nothing to draw."
            )

    def _open_folder(self) -> None:
        folder = Path(self.out.get())
        if not folder.is_dir():
            self.app.set_status("There is nothing there yet -- take a picture first.", "warning")
            return
        error = open_folder(folder)
        if error:
            self.app.set_status(error, "error")


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------


class CellsTab(Tab):
    """What Tesseract is actually given, cell by cell."""

    tab_title = "Cells"

    def __init__(self, app) -> None:
        super().__init__(app)
        self.display = tk.StringVar(value="")
        self.out = tk.StringVar(value=str(app.project_root / "cells"))
        self._views: list[tuple[ImageView, ImageView, ttk.Label]] = []

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
            "raw crop. If a cell is clipped or has its neighbour's label in it, go back to "
            "Calibrate -- that is a geometry problem and no amount of OCR tuning will fix "
            "it. If a cell looks right but reads wrong, use Tune on it.",
            width=900,
        ).pack(anchor="w", pady=(6, 8))

        self.grid_frame = ttk.Frame(self)
        self.grid_frame.pack(fill="both", expand=True)
        for column in range(6):
            self.grid_frame.columnconfigure(column, weight=1)
        for index in range(12):
            row, column = divmod(index, 6)
            block = ttk.LabelFrame(self.grid_frame, text=f" {index + 1} ")
            block.grid(row=row, column=column, sticky="nsew", padx=3, pady=3)
            prep = ImageView(block, "-")
            prep.configure(width=170, height=52)
            prep.pack_propagate(False)
            prep.pack(fill="x")
            raw = ImageView(block, "-")
            raw.configure(width=170, height=40)
            raw.pack_propagate(False)
            raw.pack(fill="x")
            caption = ttk.Label(block, text="", foreground=HELP_COLOR, anchor="center")
            caption.pack(fill="x")
            ttk.Button(block, text="Tune", width=6,
                       command=lambda i=index: self.tune(i + 1)).pack(pady=(0, 3))
            self._views.append((prep, raw, caption))

        self.output = OutputPane(self, height=8)
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

    def tune(self, cell: int) -> None:
        key = self.display.get()
        path = Path(self.out.get()) / f"{key}_{cell:02d}_raw.png"
        if not path.is_file():
            self.app.set_status("Read the cells first -- there is no picture to tune against.",
                                "warning")
            return
        expected = simpledialog.askstring(
            "Tune cell",
            f"What does cell {cell} actually say?\n\n"
            "Type it exactly as the sim draws it, in capitals -- for example 0, or TMR/REF. "
            "Every combination of upscaling, sharpening and thresholding is then tried "
            "against this one picture, and the ones that read it correctly are listed, "
            "most confident first.",
            parent=self.app.root,
        )
        if not expected:
            return
        self.app.run_task(
            commands.TUNE, {"image": str(path), "expect": expected}, self.output,
            include_image=False,
        )

    def _open_folder(self) -> None:
        folder = Path(self.out.get())
        if not folder.is_dir():
            self.app.set_status("There is nothing there yet -- read the cells first.", "warning")
            return
        error = open_folder(folder)
        if error:
            self.app.set_status(error, "error")


# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------


#: A row of `dump-colors` output:  " 4   250 250 250    0   0 250   white     1"
_COLOR_ROW = re.compile(
    r"^\s*(?P<cell>\d+)\s+(?P<b>\d+)\s+(?P<g>\d+)\s+(?P<r>\d+)\s+"
    r"(?P<h>\d+)\s+(?P<s>\d+)\s+(?P<v>\d+)\s+(?P<name>\S+)\s+(?P<bg>\d+)\s*$"
)
_COLOR_HEADER = re.compile(r"^\[(?P<display>[A-Za-z0-9_.-]+)\]\s+ring")


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
        display = ""
        rows = 0
        for line in lines:
            header = _COLOR_HEADER.search(line)
            if header:
                display = header.group("display")
                continue
            match = _COLOR_ROW.match(line)
            if match is None:
                continue
            rows += 1
            self.tree.insert("", "end", tags=(match.group("name"),), values=(
                display, match.group("cell"),
                f"{match.group('b')} / {match.group('g')} / {match.group('r')}",
                f"{match.group('h')} / {match.group('s')} / {match.group('v')}",
                match.group("name"),
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
    """Teach the daemon which softkey pages exist, and what the glyphs look like."""

    tab_title = "Pages"

    def __init__(self, app) -> None:
        super().__init__(app)
        self.display = tk.StringVar(value="")
        self.page_name = tk.StringVar(value="")
        self.labels = tk.StringVar(value="")
        self._block: list[str] = []

        help_label(
            self,
            "Reading a ten-pixel digit is hard. Recognising which softkey page is showing, "
            "from the labels that did read cleanly, is easy -- and once the page is known, "
            "the hard cells can simply be looked up instead of guessed at. These two tools "
            "build that knowledge from your own display.",
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

        # -- learn ------------------------------------------------------------
        learn = ttk.LabelFrame(self, text="  Learn the glyph shapes  ", padding=PAD)
        learn.pack(fill="x", pady=(10, 0))
        row = ttk.Frame(learn)
        row.pack(fill="x")
        ttk.Label(row, text="This page reads").pack(side="left")
        ttk.Entry(row, textvariable=self.labels).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row, text="Learn it", width=10, command=self.learn).pack(side="left")
        help_label(
            learn,
            "One label per cell, separated by commas, with nothing between the commas for "
            "a blank key -- for example  0,1,2,3,4,5,6,7,IDENT,BKSP,BACK,  which is the "
            "transponder keypad. The binarised, size-normalised shape of each glyph is "
            "stored and compared by distance later, so it survives dimming, highlighting "
            "and a window resize. This is additive: run it on each page you care about. "
            "It is off until you raise 'Shape fallback below' in the settings, because "
            "page lookup above covers the same cells with a stronger signal.",
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
        block: list[str] = []
        for line in lines:
            if line.strip().startswith("[[screen]]"):
                block = [line.rstrip()]
            elif block:
                block.append(line.rstrip())
        # Trailing blank lines only; the command prints the check-these notes
        # after the block and they are worth keeping as a comment in the file.
        while block and not block[-1].strip():
            block.pop()
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

    def learn(self) -> None:
        text = self.labels.get().strip()
        if not text:
            self.app.set_status("Type what the page says first, one label per cell.", "warning")
            return
        self.app.run_task(
            commands.LEARN, {"display": self.display.get(), "labels": text}, self.output
        )


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
        except OSError as exc:
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
            row = self._add_field(fields, row, setting, (*path, setting.key))
        return None

    def _add_field(self, parent: tk.Misc, row: int, setting: schema.Setting,
                   path: tuple[str, ...]) -> int:
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
            widget = ttk.Entry(parent, textvariable=variable)
        widget.grid(row=row, column=1, sticky="ew", pady=(2, 0))
        self._fields.append((path, setting, variable))
        row += 1
        if setting.help:
            note = setting.help
            if setting.optional:
                note += "  (leave empty to leave it unset)"
            help_label(parent, note, width=640).grid(row=row, column=1, sticky="w", pady=(0, 6))
            row += 1
        return row

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
        except OSError as exc:
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
