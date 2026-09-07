"""The window: shared state, the child processes, and the tab strip.

Everything that more than one tab needs lives here -- which config file is
open, whether we are reading a live window or a folder of PNGs, and the two
child processes. The tabs themselves are in ``tabs.py``.

Two children, never one: the daemon (``run``) is long lived and the user
starts and stops it deliberately, while every other command is a one-shot that
finishes on its own. Sharing a single slot between them would mean pressing
"Calibrate" silently killed the running daemon, which is exactly the kind of
surprise a GUI is supposed to remove.
"""

from __future__ import annotations

import sys
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from ..config import ConfigError
from . import commands, configio, prefs, wizard
from .logparse import classify
from .runner import CommandRunner, Event, Failed, Finished, Line, Started
from .widgets import OutputPane, StatusBar, WizardBar, help_label

#: How often the UI drains the child processes' output. 20 Hz: fast enough
#: that the log reads as live next to a 28 Hz daemon, slow enough that the
#: draining itself is not what the main thread spends its time on.
POLL_MS = 50

TITLE = "GlassLinkXP"


class GuiApp:
    """The application. Owns the root window; tabs are given a reference to it."""

    def __init__(self, root: tk.Tk, config_path: str | Path | None = None) -> None:
        self.root = root
        self.prefs = prefs.load()
        self.project_root = prefs.project_root()

        self.config_path: Path | None = prefs.resolve_config_path(
            config_path, self.prefs.get("config_path"), self.project_root
        )
        #: No config file found on launch -- nothing has been calibrated yet.
        #: Read once at startup rather than kept in sync with config_path: it
        #: describes how this session started, not whether a file happens to
        #: be open right now, so the wizard creating one does not turn it off.
        self.first_run = self.config_path is None
        #: Which setup step is showing, and whether the bar is up at all.
        #: Remembered between sessions so closing the window half way through
        #: setup is not the same as starting again.
        self.wizard_step = wizard.clamp(self.prefs.get("wizard_step") or 0)
        self.wizard_active = False
        self.document: dict[str, Any] = {}
        self._config_listeners: list[Callable[[], None]] = []
        self._wizard_listeners: list[Callable[[], None]] = []

        self.daemon = CommandRunner("daemon")
        self.task = CommandRunner("command")
        self._task_pane: OutputPane | None = None
        self._task_lines: list[str] = []
        self._task_done: Callable[[int, list[str]], None] | None = None
        self._daemon_sink: Callable[[Event], None] | None = None
        #: How many times an event handler has raised inside the poll loop,
        #: and the last traceback. Kept because the loop deliberately carries
        #: on afterwards: without a count, "the log went strange for a moment"
        #: leaves nothing behind to ask about.
        self.poll_failures = 0
        self.last_poll_error = ""

        self.image_source = tk.StringVar(value=str(self.prefs.get("image_source") or ""))
        self.verbose = tk.BooleanVar(value=bool(self.prefs.get("verbose")))
        #: Which publisher the Run tab starts the daemon with. Held here, like
        #: the frame source and the debug flag, because it is remembered
        #: between sessions and on_close is what writes the preferences: it was
        #: read at startup and never written back, so the choice was forgotten
        #: every time -- and "console" is a deliberate choice somebody makes
        #: while they are still checking the readings.
        self.publisher = tk.StringVar(value=str(self.prefs.get("publisher") or ""))
        self.config_display = tk.StringVar(value="")

        self._build()
        self.load_config(quiet=True)
        self.root.after(POLL_MS, self._poll)

    # -- layout --------------------------------------------------------------

    def _build(self) -> None:
        self.root.title(TITLE)
        self.root.minsize(940, 640)
        geometry = str(self.prefs.get("window") or "")
        if geometry:
            try:
                self.root.geometry(geometry)
            except tk.TclError:
                pass
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        header = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        header.pack(fill="x")
        header.columnconfigure(1, weight=1)

        ttk.Label(header, text="Configuration").grid(row=0, column=0, sticky="w", padx=(0, 8))
        entry = ttk.Entry(header, textvariable=self.config_display, state="readonly")
        entry.grid(row=0, column=1, sticky="ew")
        buttons = ttk.Frame(header)
        buttons.grid(row=0, column=2, sticky="e", padx=(8, 0))
        ttk.Button(buttons, text="Open...", width=9, command=self.choose_config).pack(side="left")
        ttk.Button(buttons, text="New...", width=9, command=self.new_config).pack(side="left", padx=4)
        ttk.Button(buttons, text="Reload", width=9, command=self.load_config).pack(side="left")

        ttk.Label(header, text="Frame source").grid(row=1, column=0, sticky="w", pady=(8, 0))
        source = ttk.Frame(header)
        source.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(8, 0))
        source.columnconfigure(0, weight=1)
        ttk.Entry(source, textvariable=self.image_source).grid(row=0, column=0, sticky="ew")
        ttk.Button(source, text="PNG...", width=8,
                   command=self.choose_image_file).grid(row=0, column=1, padx=4)
        ttk.Button(source, text="Folder...", width=9,
                   command=self.choose_image_dir).grid(row=0, column=2)
        ttk.Button(source, text="Live capture", width=12,
                   command=lambda: self.image_source.set("")).grid(row=0, column=3, padx=(4, 0))
        help_label(
            header,
            "Leave the frame source empty to capture the real pop-out windows. Point it at a "
            "PNG or a folder of PNGs to run everything from saved pictures instead -- which "
            "is how you can try the whole thing out with no X-Plane running. The Test frames "
            "button on the Tools tab writes a folder to use here.",
            width=900,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        ttk.Checkbutton(
            header,
            text="Debug output for every command (per cell: what was read, what it snapped "
                 "to, confidence). The Run tab has its own copy of this switch.",
            variable=self.verbose,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # Packed before the notebook, and that order is load-bearing: pack
        # gives space to slaves in the order they were packed, so a widget
        # packed after one with expand=True gets whatever that one left --
        # which, for a tab that wants more height than the window has, is
        # nothing. Claiming the status bar's strip along the bottom first
        # guarantees it one no matter how much the notebook overflows by.
        self.status = StatusBar(self.root)
        self.status.pack(fill="x", side="bottom")

        # Built here but not packed: show_wizard_step() puts it in above the
        # notebook, which has to exist first for `before=` to name it.
        self.wizard_bar = WizardBar(
            self.root,
            on_back=self.wizard_back,
            on_next=self.wizard_next,
            on_exit=self.close_wizard,
        )

        self.notebook = ttk.Notebook(self.root, padding=(8, 6))
        self.notebook.pack(fill="both", expand=True)

    def add_tab(self, widget: tk.Widget, title: str) -> None:
        self.notebook.add(widget, text=f" {title} ")

    def select_tab(self, index: int) -> None:
        try:
            self.notebook.select(index)
        except tk.TclError:
            pass

    # -- the setup wizard ----------------------------------------------------

    def on_wizard_changed(self, callback: Callable[[], None]) -> None:
        self._wizard_listeners.append(callback)

    def notify_wizard_changed(self) -> None:
        for callback in list(self._wizard_listeners):
            callback()

    def start_wizard(self, restart: bool = True) -> None:
        """Put the setup bar up, at the first step or where it was left."""
        if restart:
            self.wizard_step = 0
        self.wizard_active = True
        self.wizard_bar.pack(fill="x", padx=10, pady=(2, 0), before=self.notebook)
        self.show_wizard_step()

    def show_wizard_step(self) -> None:
        """Draw the current step, and open the tab it is done in."""
        from . import tabs  # noqa: PLC0415 - avoids a cycle; tabs imports this module

        step = wizard.step(self.wizard_step)
        self.wizard_bar.show(
            index=wizard.clamp(self.wizard_step),
            total=len(wizard.STEPS),
            title=step.title,
            instruction=step.instruction,
            trail=wizard.trail(self.wizard_step),
            last=wizard.is_last(self.wizard_step),
        )
        if step.tab:
            self.select_tab(tabs.tab_index(step.tab))
        self.notify_wizard_changed()

    def wizard_next(self) -> None:
        """Finish this step and move on, asking first if it looks unfinished."""
        step = wizard.step(self.wizard_step)
        if step.key == "preflight" and self.config_path is None:
            if not self.create_config(self.project_root / "config.toml"):
                return
            self.set_status(f"Created {self.config_path}.")
        reason = step.check(self.document) if step.check is not None else ""
        if reason and not messagebox.askokcancel(
            TITLE,
            f"This step does not look finished:\n\n{reason}\n\nMove on to the next "
            "step anyway?",
            parent=self.root, icon="warning", default="cancel",
        ):
            return
        if wizard.is_last(self.wizard_step):
            self.finish_wizard()
            return
        self.wizard_step += 1
        self.show_wizard_step()

    def wizard_back(self) -> None:
        self.wizard_step = wizard.clamp(self.wizard_step - 1)
        self.show_wizard_step()

    def finish_wizard(self) -> None:
        """Setup is done: put the bar away and forget where we were."""
        self.wizard_step = 0
        self._hide_wizard()
        self.set_status("Setup finished. Start it again any time from the Start here tab.")

    def close_wizard(self) -> None:
        """Put the bar away, keeping the step so it can be picked back up."""
        self._hide_wizard()
        self.set_status(
            f"Setup closed at step {wizard.clamp(self.wizard_step) + 1} of "
            f"{len(wizard.STEPS)}. Resume it from the Start here tab."
        )

    def _hide_wizard(self) -> None:
        self.wizard_active = False
        self.wizard_bar.pack_forget()
        self.notify_wizard_changed()

    # -- configuration -------------------------------------------------------

    def on_config_changed(self, callback: Callable[[], None]) -> None:
        self._config_listeners.append(callback)

    def notify_config_changed(self) -> None:
        for callback in list(self._config_listeners):
            callback()

    def load_config(self, quiet: bool = False) -> None:
        """Re-read the config file into the editable document."""
        if self.config_path is None:
            self.document = configio.default_document()
            self.config_display.set("(no file -- built-in defaults; press New... to make one)")
            if not quiet:
                self.set_status("No configuration file open. Using the built-in defaults.",
                                "warning")
            self.notify_config_changed()
            return
        try:
            self.document = configio.read_document(self.config_path)
        except configio.ConfigIoError as exc:
            self.document = configio.default_document()
            self.config_display.set(f"{self.config_path}  (could not be read)")
            self.set_status(str(exc), "error")
            self.notify_config_changed()
            return
        self.config_display.set(str(self.config_path))
        if not quiet:
            self.set_status(f"Loaded {self.config_path}")
        self.notify_config_changed()

    def choose_config(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root, title="Open a configuration file",
            initialdir=str(self.project_root),
            filetypes=[("Configuration files", "*.toml"), ("All files", "*.*")],
        )
        if not path:
            return
        self.config_path = Path(path)
        self.load_config()

    def new_config(self) -> None:
        """Create a config file, seeded from the documented example."""
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Create a configuration file",
            initialdir=str(self.project_root), initialfile="config.toml",
            defaultextension=".toml",
            filetypes=[("Configuration files", "*.toml"), ("All files", "*.*")],
        )
        if not path:
            return
        if self.create_config(Path(path)):
            self.set_status(f"Created {self.config_path}. Set the window titles next.")

    def create_config(self, target: Path) -> bool:
        """Write a new config file at ``target`` and open it. False on failure.

        Shared with the wizard, which creates one without asking where: every
        tab that writes a setting degrades to "this is only set for now" with
        no file open, so a walkthrough that did not make one would spend five
        steps quietly discarding the user's work.
        """
        example = prefs.example_config()
        try:
            if example is not None:
                target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                configio.save(target, configio.default_document(), backup=False)
        except OSError as exc:
            messagebox.showerror(TITLE, f"Could not create {target}:\n{exc}", parent=self.root)
            return False
        self.config_path = target
        self.load_config(quiet=True)
        return True

    def config_argument(self) -> str | None:
        """The ``-c`` value for a child, or None when no file is open."""
        return str(self.config_path.resolve()) if self.config_path else None

    def validate_document(self) -> str:
        """An empty string if the current document is usable, else why not.

        Every failure is a message to show, which is what the Settings tab's
        three call sites of ``configio.validate`` already say for themselves.
        This one used to catch ``ConfigError`` and ``ValueError`` only, and a
        document can raise neither: a config file holding ``loop_hz = "12"``
        -- a string where a number belongs, which the Raw file tab writes
        through untouched -- reaches a comparison inside the daemon's own
        validation and comes back out as a ``TypeError``. That escaped, Tk
        caught it, and Start did nothing at all with no dialog to say why.

        A ``ConfigError`` is a sentence written for the user, so it is shown
        as it is. Anything else is a bug or a mistyped value, and its class is
        worth having in front of whoever is reading the message.
        """
        try:
            base = self.config_path.parent if self.config_path else None
            configio.validate(self.document, base_dir=base)
        except ConfigError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001 - every failure is a message to show
            return f"{type(exc).__name__}: {exc}"
        return ""

    def display_keys(self) -> list[str]:
        displays = self.document.get("display")
        if isinstance(displays, dict) and displays:
            return [key for key in displays]
        return ["pfd", "mfd"]

    # -- file pickers --------------------------------------------------------

    def choose_image_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root, title="Choose a PNG to read frames from",
            initialdir=str(self.project_root),
            filetypes=[("PNG images", "*.png"), ("All files", "*.*")],
        )
        if path:
            self.image_source.set(path)

    def choose_image_dir(self) -> None:
        path = filedialog.askdirectory(
            parent=self.root, title="Choose a folder of PNG frames",
            initialdir=str(self.project_root),
        )
        if path:
            self.image_source.set(path)

    # -- running commands ----------------------------------------------------

    def set_status(self, message: str, level: str = "info") -> None:
        self.status.set(message, level)

    def run_task(
        self,
        spec: commands.CommandSpec,
        values: dict[str, Any] | None = None,
        pane: OutputPane | None = None,
        on_finish: Callable[[int, list[str]], None] | None = None,
        include_image: bool = True,
    ) -> bool:
        """Start a one-shot subcommand. False if it could not be started."""
        if self.task.is_running:
            self.set_status("Another command is still running -- wait for it to finish.",
                            "warning")
            return False
        values = dict(values or {})
        if include_image and any(option.key == "image" for option in spec.options):
            values.setdefault("image", self.image_source.get().strip())
        try:
            argv = commands.build_argv(
                spec, values, config=self.config_argument(), verbose=self.verbose.get()
            )
        except (commands.MissingOption, ValueError) as exc:
            self.set_status(str(exc), "error")
            messagebox.showwarning(TITLE, str(exc), parent=self.root)
            return False
        command = commands.full_command(argv)
        self._task_pane = pane
        self._task_lines = []
        self._task_done = on_finish
        if pane is not None:
            pane.append(commands.quote_command(command), "command")
        self.task.start(command, cwd=self.project_root)
        self.set_status(f"Running {spec.name}...")
        return True

    def start_daemon(self, values: dict[str, Any], sink: Callable[[Event], None]) -> bool:
        if self.daemon.is_running:
            self.set_status("The daemon is already running.", "warning")
            return False
        values = dict(values)
        values.setdefault("image", self.image_source.get().strip())
        try:
            argv = commands.build_argv(
                commands.RUN, values, config=self.config_argument(), verbose=self.verbose.get()
            )
        except (commands.MissingOption, ValueError) as exc:
            self.set_status(str(exc), "error")
            return False
        self._daemon_sink = sink
        self.daemon.start(commands.full_command(argv), cwd=self.project_root)
        return True

    def stop_daemon(self) -> None:
        if not self.daemon.is_running:
            return
        self.set_status("Stopping...")
        self.root.update_idletasks()
        self.daemon.stop()

    def _poll(self) -> None:
        """Drain both children, then ask to be called again -- whatever happened.

        The reschedule is in a ``finally`` rather than on the last line, and
        that is the whole point of this method. Tkinter *catches* an exception
        raised inside an ``after`` callback: it reports it and returns, so the
        callback simply does not finish. A reschedule written at the bottom is
        therefore skipped by any failure above it, and the polling stops for
        good -- while the window carries on looking perfectly healthy. No more
        log lines, no more board, no Finished event to put the buttons back,
        and Stop never learns the daemon has gone. Under ``pythonw.exe``,
        which is how this is started on Windows, the traceback goes to a
        stderr nobody can see, so there is not even a clue.

        One bad line of output reaching one tab's handler is enough to trigger
        it, which makes the failure both easy to hit and impossible to
        diagnose. The loop is kept alive here instead, and the failure is
        said out loud in :meth:`_poll_failed`.
        """
        try:
            self.drain_children()
        except Exception as exc:  # noqa: BLE001 - a handler must not kill the loop
            self._poll_failed(exc)
        finally:
            self.root.after(POLL_MS, self._poll)

    def drain_children(self) -> None:
        """One pass over both children's queues, dispatching what they said.

        Split out from :meth:`_poll` so the polling above contains nothing but
        the keeping-alive, and so a test can drive a drain without waiting on
        the event loop.
        """
        for event in self.daemon.drain():
            if self._daemon_sink is not None:
                self._daemon_sink(event)
        for event in self.task.drain():
            self._task_event(event)

    def _poll_failed(self, exc: BaseException) -> None:
        """Report a handler that raised, rather than swallowing it.

        Kept alive is not the same as kept quiet: a tab that raises on every
        line would otherwise drop output silently, which is the bug this is
        here to prevent, one layer down. The traceback goes to stderr for
        anyone running from a console, and the status bar says so for
        everybody else -- which is the only surface there is under
        ``pythonw.exe``.

        Defensive to the last: this runs from an exception handler, and
        anything it raised itself would land back in Tk's reporter. The
        reschedule would still happen -- it is in the ``finally`` -- but the
        message would be lost as well as the failure.
        """
        self.poll_failures += 1
        self.last_poll_error = traceback.format_exc()
        try:
            sys.stderr.write(self.last_poll_error)
        except Exception:  # noqa: BLE001 - pythonw.exe has no stderr to write to
            pass
        try:
            self.set_status(
                f"Something in the window failed while reading the output: {exc!r}. "
                "Some of it may be missing; the window is still running.",
                "error",
            )
        except Exception:  # noqa: BLE001 - the status bar is not worth a second failure
            pass

    def _task_event(self, event: Event) -> None:
        if isinstance(event, Line):
            self._task_lines.append(event.text)
            if self._task_pane is not None:
                self._task_pane.append(event.text, classify(event.text))
        elif isinstance(event, Failed):
            if self._task_pane is not None:
                self._task_pane.append(event.message, "error")
            self.set_status(event.message, "error")
        elif isinstance(event, Finished):
            if self._task_pane is not None:
                self._task_pane.append(
                    f"-- finished, exit code {event.returncode} --",
                    "plain" if event.returncode == 0 else "error",
                )
            self.set_status(
                "Done." if event.returncode == 0
                else f"That command failed (exit code {event.returncode}). "
                     "The output above says why.",
                "info" if event.returncode == 0 else "error",
            )
            done, self._task_done = self._task_done, None
            if done is not None:
                done(event.returncode, list(self._task_lines))
        elif isinstance(event, Started):
            pass

    # -- shutdown ------------------------------------------------------------

    def on_close(self) -> None:
        if self.daemon.is_running:
            if not messagebox.askokcancel(
                TITLE, "The daemon is still running. Stop it and close?", parent=self.root
            ):
                return
            self.daemon.stop()
        if self.task.is_running:
            self.task.stop(grace=1.0)
        self.prefs.update({
            "config_path": str(self.config_path) if self.config_path else "",
            "image_source": self.image_source.get(),
            "verbose": bool(self.verbose.get()),
            "publisher": self.publisher.get(),
            "window": self.root.winfo_geometry(),
            "tab": self.notebook.index("current") if self.notebook.tabs() else 0,
            "wizard_active": bool(self.wizard_active),
            "wizard_step": wizard.clamp(self.wizard_step),
        })
        prefs.save(self.prefs)
        self.root.destroy()


def build(root: tk.Tk, config_path: str | Path | None = None) -> GuiApp:
    """Create the application and every tab. Kept apart from ``launch`` so a
    test can build the whole window without entering the event loop."""
    from . import tabs  # noqa: PLC0415 - avoids a cycle; tabs import this module's types

    app = GuiApp(root, config_path)
    for tab in tabs.build_tabs(app):
        app.add_tab(tab, tab.tab_title)
    if app.first_run:
        # Nothing configured, so there is nothing this window can usefully do
        # until setup has been through: put the wizard up at step one.
        app.start_wizard()
        app.set_status("Setup: follow the steps in the bar at the top of the window.")
    elif app.prefs.get("wizard_active"):
        # Closed part way through last time. Pick it back up where it was,
        # rather than on whichever tab happened to be open.
        app.start_wizard(restart=False)
    else:
        app.select_tab(int(app.prefs.get("tab") or 0))
    app.notify_config_changed()
    return app
