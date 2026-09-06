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


# ---------------------------------------------------------------------------
# the calibration editor's canvas
# ---------------------------------------------------------------------------

#: Colours for the boxes drawn over the captured frame. Red for the strip
#: because it is the thing being placed; green for the cells because they are
#: the consequence of that placement, and the eye needs to tell at a glance
#: which one it is dragging.
STRIP_COLOR = "#ff3b30"
CELL_COLOR = "#31d158"
HANDLE_FILL = "#ffffff"

#: How the pointer changes over each resize handle, so it is obvious the box
#: can be grabbed there at all.
HANDLE_CURSORS = {
    "nw": "top_left_corner", "n": "top_side", "ne": "top_right_corner",
    "e": "right_side", "se": "bottom_right_corner", "s": "bottom_side",
    "sw": "bottom_left_corner", "w": "left_side",
}


class GeometryCanvas(ttk.Frame):
    """The captured frame with the strip and cell boxes drawn on it, editable.

    Two instances of this make up the Calibrate tab: one showing the whole
    frame, where the strip is drawn out with the mouse, and one showing only
    the strip and a small margin, magnified, where it is judged. They differ
    only in which part of the frame they are told to show -- every coordinate
    goes through :class:`geometry.View`, so the same drag code serves both.

    The cell boxes are **not** computed here. They come from
    ``strip.cell_rects``, which is the function ``split_cells`` slices with, so
    what is on screen is what the reader will read. Anything else would be a
    calibration editor capable of disagreeing with the thing it calibrates.
    """

    def __init__(
        self,
        parent: tk.Misc,
        on_change: Callable[[object], None] | None = None,
        allow_draw: bool = True,
        zoom_to_strip: bool = False,
        margin: int = 12,
        placeholder: str = "Take a picture first.",
        height: int = 320,
    ) -> None:
        super().__init__(parent)
        self.on_change = on_change
        self.allow_draw = allow_draw
        self.zoom_to_strip = zoom_to_strip
        self.margin = margin
        self.placeholder = placeholder
        #: Which part of the strip the close-up shows: the whole thing, or one
        #: corner. Showing the whole strip is what a close-up obviously ought
        #: to do and is nearly useless for the job -- a 1159 pixel strip in a
        #: 1000 pixel panel comes out *smaller* than life, and the question
        #: being asked is whether an edge is one pixel inside another. Framing
        #: one corner instead gets it to eight times life size, where the
        #: answer is simply visible.
        self.focus = "strip"
        #: Magnification for the close-up. 0 means "fit whatever the focus
        #: covers"; anything else is that many screen pixels per frame pixel.
        #: A control rather than a constant because the right value depends on
        #: how big the pop-out was when the picture was taken, which is not
        #: knowable from here -- the same guess that has cost this project the
        #: most time everywhere else.
        self.zoom = 0.0

        self.canvas = tk.Canvas(self, height=height, highlightthickness=0,
                                background="#20242a", cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)

        self._source_image = None        # the PIL image, loaded once per capture
        self._photo = None               # the PhotoImage Tk is showing
        self._photo_key: tuple | None = None
        self._geometry = None            # a StripGeometry
        self._drag: tuple[str, float, float] | None = None
        self._pending: str | None = None
        #: Set by arm_draw(). A box already on screen swallows presses near it
        #: -- within a few pixels of an edge is a resize, inside it is a move --
        #: so with the default geometry sitting over the strip there is nowhere
        #: left to start a fresh one. This makes "draw a new box" an explicit
        #: thing to ask for rather than a gap to find.
        self._armed = False

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_hover)

    # -- what is being shown -------------------------------------------------

    @property
    def frame_size(self) -> tuple[int, int]:
        if self._source_image is None:
            return (0, 0)
        return (self._source_image.width, self._source_image.height)

    def set_frame(self, path: str | Path | None) -> None:
        """Load the captured PNG this editor works on."""
        self._source_image = None
        self._photo_key = None
        if path is not None and Path(path).is_file():
            try:
                from PIL import Image  # noqa: PLC0415 - optional at import time

                image = Image.open(path)
                image.load()
                self._source_image = image.convert("RGB")
            except Exception:  # noqa: BLE001 - a bad PNG is a message, not a crash
                self._source_image = None
        self.redraw()

    def set_geometry(self, geometry) -> None:
        self._geometry = geometry
        self.redraw()

    def _zoomed_rect(self, x: int, y: int, w: int, h: int,
                     frame_w: int, frame_h: int) -> tuple[int, int, int, int]:
        """A window of the frame at a fixed magnification, around the focus.

        The anchor sits a third of the way in from the near side rather than
        in the middle, so a corner is shown with more of the strip than of
        what lies outside it -- the strip is what the edge is being judged
        against.
        """
        span_w = max(8, int(round(max(self.canvas.winfo_width(), 1) / self.zoom)))
        span_h = max(8, int(round(max(self.canvas.winfo_height(), 1) / self.zoom)))
        if self.focus == "topleft":
            left, top = x - span_w // 3, y - span_h // 3
        elif self.focus == "bottomright":
            left, top = x + w - 2 * span_w // 3, y + h - 2 * span_h // 3
        else:
            left, top = x + w // 2 - span_w // 2, y + h // 2 - span_h // 2
        left = max(0, min(left, max(0, frame_w - span_w)))
        top = max(0, min(top, max(0, frame_h - span_h)))
        return (left, top, min(span_w, frame_w - left), min(span_h, frame_h - top))

    def set_zoom(self, zoom: float) -> None:
        if zoom != self.zoom:
            self.zoom = zoom
            self._photo_key = None
            self.redraw()

    def set_focus(self, focus: str) -> None:
        """Which part of the strip to magnify: strip | topleft | bottomright."""
        if focus != self.focus:
            self.focus = focus
            self._photo_key = None
            self.redraw()

    def arm_draw(self) -> None:
        """The next drag starts a new box, wherever it begins."""
        if not self.allow_draw:
            return
        self._armed = True
        self.canvas.configure(cursor="tcross")

    @property
    def armed(self) -> bool:
        return self._armed

    # -- drawing -------------------------------------------------------------

    def _source_rect(self) -> tuple[int, int, int, int]:
        from . import geometry as geo  # noqa: PLC0415 - avoids a cycle at import time

        width, height = self.frame_size
        if not self.zoom_to_strip or self._geometry is None:
            return (0, 0, width, height)
        x, y, w, h = geo.strip_pixels(self._geometry, width, height)
        # A margin around the strip, so the edge being aimed at has something
        # on the far side of it to be judged against. Without it the strip
        # fills the view and "just inside the edge" has no visible reference.
        margin_x = max(self.margin, int(round(w * 0.02)))
        margin_y = max(self.margin, int(round(h * 0.6)))
        if self.zoom > 0:
            return self._zoomed_rect(x, y, w, h, width, height)
        if self.focus in ("topleft", "bottomright"):
            # Enough of the strip to show a cell or two beside the corner, so
            # the edge has the rest of the strip to be judged against.
            span_x = max(90, int(round(w / max(1, self._geometry.cells) * 1.6)))
            if self.focus == "topleft":
                left, top = x - margin_x, y - margin_y
            else:
                left, top = x + w + margin_x - span_x, y + h + margin_y - (h + 2 * margin_y)
            left = max(0, min(left, width - 1))
            top = max(0, min(top, height - 1))
            right = min(width, left + span_x)
            bottom = min(height, top + h + 2 * margin_y)
            return (left, top, max(1, right - left), max(1, bottom - top))
        left = max(0, x - margin_x)
        top = max(0, y - margin_y)
        right = min(width, x + w + margin_x)
        bottom = min(height, y + h + margin_y)
        return (left, top, max(1, right - left), max(1, bottom - top))

    def view(self):
        """The current mapping between frame pixels and this canvas.

        Computed on demand rather than cached from the last redraw. A cached
        one goes stale the moment the canvas is resized -- redrawing is
        debounced, so for a tenth of a second afterwards every press would be
        mapped through the old scale and land somewhere the user never
        clicked. Recomputing it is a handful of arithmetic operations.
        """
        from . import geometry as geo  # noqa: PLC0415

        if self._source_image is None:
            return None
        return geo.fit_view(
            self._source_rect(),
            max(self.canvas.winfo_width(), 1),
            max(self.canvas.winfo_height(), 1),
        )

    def _on_configure(self, _event: tk.Event) -> None:
        if self._pending is not None:
            self.after_cancel(self._pending)
        self._pending = self.after(80, self.redraw)

    def redraw(self) -> None:
        self._pending = None
        self.canvas.delete("all")
        view = self.view()
        if view is None:
            self.canvas.create_text(
                self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2,
                text=self.placeholder, fill="#9aa3ad", width=420, justify="center",
            )
            return
        self._draw_photo(self._source_rect(), view)
        if self._geometry is not None:
            self._draw_boxes(view)

    def _draw_photo(self, source: tuple[int, int, int, int], view) -> None:
        key = (source, round(view.scale, 4))
        if key != self._photo_key:
            self._photo = _render_region(self._source_image, source, view.scale)
            self._photo_key = key
        if self._photo is not None:
            self.canvas.create_image(view.offset_x, view.offset_y, anchor="nw", image=self._photo)

    def _draw_boxes(self, view) -> None:
        from ..strip import cell_rects  # noqa: PLC0415 - pulls in cv2; not at import time
        from . import geometry as geo  # noqa: PLC0415

        frame_w, frame_h = self.frame_size
        # The cells first, so the strip outline and its handles sit on top of
        # them rather than being hidden behind a cell edge.
        for rect in cell_rects((frame_h, frame_w, 3), self._geometry):
            x0, y0, x1, y1 = view.rect_to_canvas(rect.x, rect.y, rect.w, rect.h)
            self.canvas.create_rectangle(x0, y0, x1, y1, outline=CELL_COLOR, width=1)

        x, y, w, h = geo.strip_pixels(self._geometry, frame_w, frame_h)
        x0, y0, x1, y1 = view.rect_to_canvas(x, y, w, h)
        self.canvas.create_rectangle(x0, y0, x1, y1, outline=STRIP_COLOR, width=2)
        if not self.allow_draw and not self.zoom_to_strip:
            return
        for _name, (px, py) in geo.handle_points(x0, y0, x1 - x0, y1 - y0).items():
            self.canvas.create_rectangle(
                px - 4, py - 4, px + 4, py + 4,
                outline=STRIP_COLOR, fill=HANDLE_FILL, width=1,
            )

    def strip_canvas_rect(self) -> tuple[float, float, float, float] | None:
        from . import geometry as geo  # noqa: PLC0415

        view = self.view()
        if view is None or self._geometry is None:
            return None
        frame_w, frame_h = self.frame_size
        x, y, w, h = geo.strip_pixels(self._geometry, frame_w, frame_h)
        return view.rect_to_canvas(x, y, w, h)

    # -- mouse ---------------------------------------------------------------

    def _on_hover(self, event: tk.Event) -> None:
        from . import geometry as geo  # noqa: PLC0415

        if self._armed:
            return
        rect = self.strip_canvas_rect()
        if rect is None:
            return
        handle = geo.handle_at(event.x, event.y, rect)
        if handle:
            self.canvas.configure(cursor=HANDLE_CURSORS.get(handle, "fleur"))
        elif geo.inside(event.x, event.y, rect):
            self.canvas.configure(cursor="fleur")
        else:
            self.canvas.configure(cursor="crosshair" if self.allow_draw else "")

    def _on_press(self, event: tk.Event) -> None:
        from . import geometry as geo  # noqa: PLC0415

        view = self.view()
        if view is None or self._geometry is None:
            return
        if self._armed:
            self._armed = False
            frame_x, frame_y = view.to_frame(event.x, event.y)
            self._drag = ("draw", frame_x, frame_y)
            return
        rect = self.strip_canvas_rect()
        handle = geo.handle_at(event.x, event.y, rect) if rect else None
        if handle:
            self._drag = (f"handle:{handle}", event.x, event.y)
        elif rect and geo.inside(event.x, event.y, rect):
            self._drag = ("move", event.x, event.y)
        elif self.allow_draw:
            # A fresh box. Starting it means the old one is gone, which is the
            # point: the auto-detect is a seed, not something to nurse.
            frame_x, frame_y = view.to_frame(event.x, event.y)
            self._drag = ("draw", frame_x, frame_y)
        else:
            self._drag = None

    def _on_motion(self, event: tk.Event) -> None:
        from . import geometry as geo  # noqa: PLC0415

        view = self.view()
        if self._drag is None or view is None or self._geometry is None:
            return
        kind, start_x, start_y = self._drag
        frame_w, frame_h = self.frame_size
        frame_x, frame_y = view.to_frame(event.x, event.y)

        if kind == "draw":
            updated = geo.geometry_from_pixels(
                self._geometry, start_x, start_y, frame_x, frame_y, frame_w, frame_h
            )
        elif kind == "move":
            dx = (event.x - start_x) / view.scale
            dy = (event.y - start_y) / view.scale
            updated = geo.move(self._geometry, dx, dy, frame_w, frame_h)
            self._drag = (kind, event.x, event.y)
        else:
            updated = geo.drag_handle(
                self._geometry, kind.split(":", 1)[1], frame_x, frame_y, frame_w, frame_h
            )
        self._emit(updated)

    def _on_release(self, _event: tk.Event) -> None:
        self._drag = None

    def _emit(self, geometry) -> None:
        self._geometry = geometry
        self.redraw()
        if self.on_change is not None:
            self.on_change(geometry)


def _render_region(image, source: tuple[int, int, int, int], scale: float):
    """Crop a region of the frame and scale it for display.

    Enlarged with nearest-neighbour on purpose. The whole judgement being made
    here is where one edge sits relative to another, a few pixels apart, and a
    smooth interpolation would invent a soft boundary that is not in the
    capture -- putting the user's eye on an edge the reader will never see.
    """
    try:
        from PIL import Image, ImageTk  # noqa: PLC0415
    except ImportError:
        return None
    x, y, w, h = source
    region = image.crop((x, y, x + w, y + h))
    target = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
    if target != (region.width, region.height):
        resample = Image.NEAREST if scale >= 1.0 else Image.LANCZOS
        region = region.resize(target, resample)
    return ImageTk.PhotoImage(region)
