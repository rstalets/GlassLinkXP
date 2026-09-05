"""Reusable Tk widgets for the GUI.

Nothing in here knows anything about the daemon; it is the presentation half,
kept separate so ``tabs.py`` reads as "what this tab does" rather than as
several hundred lines of grid() calls.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable

from ..color import BLACK, RED, WHITE, YELLOW

# ---------------------------------------------------------------------------
# palette
# ---------------------------------------------------------------------------

#: Severity colours for the output pane. Chosen to stay legible on the light
#: background ttk gives a Text widget on every platform this may run on.
LEVEL_COLORS = {
    "error": "#b00020",
    "warning": "#8a5300",
    "info": "#1a1a1a",
    "debug": "#6b6b6b",
    "plain": "#1a1a1a",
    "command": "#00507a",
    "note": "#4a4a4a",
}

#: How a softkey cell is drawn for each published background value. These are
#: the GUI's own colours -- readable on a monitor -- not a claim about what
#: X-Plane draws; the daemon publishes which of the four states a cell is in,
#: and this is the GUI showing that state back.
CELL_COLORS = {
    BLACK: ("#101010", "#e8e8e8"),
    WHITE: ("#f2f2f2", "#101010"),
    YELLOW: ("#e5c100", "#101010"),
    RED: ("#c1272d", "#ffffff"),
}
CELL_IDLE = ("#3a3a3a", "#8a8a8a")

HELP_COLOR = "#555555"
WARN_COLOR = "#8a5300"


# ---------------------------------------------------------------------------
# containers
# ---------------------------------------------------------------------------


class ScrollableFrame(ttk.Frame):
    """A frame that scrolls vertically, for forms taller than the window."""

    def __init__(self, parent: tk.Misc, **kwargs) -> None:
        super().__init__(parent, **kwargs)
        self._canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self._scroll = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self.body = ttk.Frame(self._canvas)
        self._window = self._canvas.create_window((0, 0), window=self.body, anchor="nw")
        self._canvas.configure(yscrollcommand=self._scroll.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._scroll.pack(side="right", fill="y")
        self.body.bind("<Configure>", self._on_body)
        self._canvas.bind("<Configure>", self._on_canvas)
        # Bound on enter/leave rather than globally: a bind_all for the wheel
        # would steal scrolling from every other widget in the window.
        self._canvas.bind("<Enter>", lambda _e: self._bind_wheel())
        self._canvas.bind("<Leave>", lambda _e: self._unbind_wheel())

    def _on_body(self, _event: tk.Event) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas(self, event: tk.Event) -> None:
        self._canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self) -> None:
        self._canvas.bind_all("<MouseWheel>", self._wheel)
        self._canvas.bind_all("<Button-4>", self._wheel)
        self._canvas.bind_all("<Button-5>", self._wheel)

    def _unbind_wheel(self) -> None:
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self._canvas.unbind_all(sequence)

    def _wheel(self, event: tk.Event) -> None:
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            delta = -1 if event.delta > 0 else 1
        self._canvas.yview_scroll(delta, "units")


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------


class OutputPane(ttk.Frame):
    """A read-only, colour-tagged log view with a bounded backlog."""

    def __init__(self, parent: tk.Misc, height: int = 14, max_lines: int = 4000) -> None:
        super().__init__(parent)
        self.max_lines = max_lines
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        self.follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Follow output", variable=self.follow).pack(side="left")
        ttk.Button(bar, text="Clear", width=8, command=self.clear).pack(side="right")
        ttk.Button(bar, text="Copy all", width=9, command=self.copy).pack(side="right", padx=4)

        holder = ttk.Frame(self)
        holder.pack(fill="both", expand=True)
        self.text = tk.Text(
            holder, height=height, wrap="none", state="disabled",
            font=tkfont.nametofont("TkFixedFont"), background="#fbfbfb",
            relief="solid", borderwidth=1, padx=6, pady=4,
        )
        y = ttk.Scrollbar(holder, orient="vertical", command=self.text.yview)
        x = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        self.text.pack(side="left", fill="both", expand=True)
        y.pack(side="right", fill="y")
        x.pack(fill="x")
        for name, colour in LEVEL_COLORS.items():
            self.text.tag_configure(name, foreground=colour)
        self.text.tag_configure("command", font=self._bold())

    def _bold(self) -> tkfont.Font:
        bold = tkfont.Font(font=tkfont.nametofont("TkFixedFont"))
        bold.configure(weight="bold")
        return bold

    def append(self, line: str, tag: str = "plain") -> None:
        self.text.configure(state="normal")
        self.text.insert("end", line + "\n", tag)
        # A daemon left running all evening would otherwise grow this widget
        # without limit; Tk keeps the whole backlog in memory and redraws get
        # slower with it.
        excess = int(self.text.index("end-1c").split(".")[0]) - self.max_lines
        if excess > 0:
            self.text.delete("1.0", f"{excess + 1}.0")
        self.text.configure(state="disabled")
        if self.follow.get():
            self.text.see("end")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def contents(self) -> str:
        return self.text.get("1.0", "end-1c")

    def copy(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.contents())


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------


class ImageView(ttk.Frame):
    """Shows a PNG, scaled to fit, with a message when there is nothing to show.

    Pillow is already a dependency of this project (the synthetic frames are
    rendered with it), so it is used for smooth scaling when present. Tk's own
    PhotoImage can only scale by whole-number factors, which is enough to keep
    the tab working if Pillow was built without its Tk bridge.
    """

    def __init__(self, parent: tk.Misc, placeholder: str = "Nothing to show yet") -> None:
        super().__init__(parent)
        self.placeholder = placeholder
        self._path: Path | None = None
        self._image = None  # a reference Tk does not keep for us
        self.label = ttk.Label(self, anchor="center", justify="center",
                               text=placeholder, foreground=HELP_COLOR)
        self.label.pack(fill="both", expand=True)
        self._pending: str | None = None
        self.bind("<Configure>", self._on_resize)

    def show(self, path: str | Path | None) -> None:
        self._path = Path(path) if path else None
        self._render()

    def _on_resize(self, _event: tk.Event) -> None:
        # Debounced: a drag of the window edge fires this continuously, and
        # rescaling a 1280x800 frame on every pixel makes the drag stutter.
        if self._pending is not None:
            self.after_cancel(self._pending)
        self._pending = self.after(120, self._render)

    def _render(self) -> None:
        self._pending = None
        if self._path is None or not self._path.is_file():
            self._image = None
            self.label.configure(image="", text=self.placeholder)
            return
        width = max(self.winfo_width() - 8, 64)
        height = max(self.winfo_height() - 8, 64)
        try:
            self._image = _load_scaled(self._path, width, height)
        except Exception as exc:  # noqa: BLE001 - a bad PNG is a message, not a crash
            self._image = None
            self.label.configure(image="", text=f"could not show {self._path.name}: {exc}")
            return
        self.label.configure(image=self._image, text="")


def _load_scaled(path: Path, width: int, height: int):
    try:
        from PIL import Image, ImageTk  # noqa: PLC0415 - optional at import time
    except ImportError:
        image = tk.PhotoImage(file=str(path))
        factor = max(1, -(-image.width() // max(width, 1)), -(-image.height() // max(height, 1)))
        return image.subsample(factor) if factor > 1 else image
    with Image.open(path) as source:
        source.load()
        scale = min(width / source.width, height / source.height)
        # Only ever shrunk, or enlarged by a whole number: a cell image
        # smoothed up by 1.7x would show interpolation artefacts that are not
        # in the pixels Tesseract was given, which is precisely the confusion
        # this tab exists to remove.
        if scale < 1.0:
            size = (max(1, int(source.width * scale)), max(1, int(source.height * scale)))
            resized = source.resize(size, Image.LANCZOS)
        elif scale >= 2.0:
            factor = int(scale)
            resized = source.resize(
                (source.width * factor, source.height * factor), Image.NEAREST
            )
        else:
            resized = source.copy()
        return ImageTk.PhotoImage(resized)


# ---------------------------------------------------------------------------
# the softkey board
# ---------------------------------------------------------------------------


class LabelBoard(ttk.LabelFrame):
    """The twelve softkeys of one display, as the daemon last reported them."""

    def __init__(self, parent: tk.Misc, display: str, cells: int = 12) -> None:
        super().__init__(parent, text=f"  {display.upper()}  ")
        self.display = display
        self.cells = cells
        self._boxes: list[tk.Label] = []
        self._numbers: list[tk.Label] = []
        grid = ttk.Frame(self)
        grid.pack(fill="both", expand=True, padx=6, pady=6)
        for index in range(cells):
            column = ttk.Frame(grid)
            column.grid(row=0, column=index, sticky="nsew", padx=1)
            grid.columnconfigure(index, weight=1, uniform="cell")
            box = tk.Label(
                column, text="", width=9, height=2, relief="solid", borderwidth=1,
                background=CELL_IDLE[0], foreground=CELL_IDLE[1],
                font=tkfont.nametofont("TkDefaultFont"), wraplength=90,
            )
            box.pack(fill="both", expand=True)
            number = ttk.Label(column, text=str(index + 1), anchor="center",
                               foreground=HELP_COLOR)
            number.pack(fill="x")
            self._boxes.append(box)
            self._numbers.append(number)

    def update_cells(self, labels: tuple[str, ...], backgrounds: tuple[int, ...]) -> None:
        for index, box in enumerate(self._boxes):
            text = labels[index] if index < len(labels) else ""
            background = backgrounds[index] if index < len(backgrounds) else BLACK
            colours = CELL_COLORS.get(background, CELL_COLORS[BLACK])
            box.configure(text=text, background=colours[0], foreground=colours[1])

    def set_idle(self, message: str = "") -> None:
        for box in self._boxes:
            box.configure(text=message, background=CELL_IDLE[0], foreground=CELL_IDLE[1])
            message = ""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def help_label(parent: tk.Misc, text: str, width: int = 560) -> ttk.Label:
    """Explanatory text under a control, in the GUI's quieter voice."""
    label = ttk.Label(parent, text=text, foreground=HELP_COLOR, wraplength=width,
                      justify="left")
    return label


def section_heading(parent: tk.Misc, text: str) -> ttk.Label:
    heading = tkfont.Font(font=tkfont.nametofont("TkDefaultFont"))
    heading.configure(weight="bold")
    return ttk.Label(parent, text=text, font=heading)


class StatusBar(ttk.Frame):
    """One line at the bottom of the window: what just happened."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, relief="sunken", borderwidth=1)
        self._text = tk.StringVar(value="")
        self.label = ttk.Label(self, textvariable=self._text, anchor="w", padding=(6, 2))
        self.label.pack(fill="x")

    def set(self, message: str, level: str = "info") -> None:
        self._text.set(message)
        self.label.configure(foreground=LEVEL_COLORS.get(level, LEVEL_COLORS["info"]))


def browse_button(parent: tk.Misc, command: Callable[[], None], text: str = "Browse...") -> ttk.Button:
    return ttk.Button(parent, text=text, command=command, width=11)
