"""Run a CLI subcommand as a child process and stream its output back.

Nothing here imports Tk. The GUI polls :meth:`CommandRunner.drain` from a Tk
``after`` callback, so every widget update still happens on the main thread --
touching a Tk widget from the reader thread is the classic way to make a
Tkinter program hang, and keeping the thread boundary at a queue makes that
impossible rather than merely unlikely.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

# Windows creation flags, spelled out so this module imports on any platform.
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class Started:
    command: list[str]


@dataclass(frozen=True)
class Line:
    text: str


@dataclass(frozen=True)
class Finished:
    returncode: int


@dataclass(frozen=True)
class Failed:
    message: str


Event = Started | Line | Finished | Failed


def child_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a child, forced to unbuffered UTF-8.

    Unbuffered because a block-buffered pipe would hold ``print`` output until
    the child exits, which for the daemon is never -- the log pane would sit
    empty while the daemon ran perfectly. UTF-8 because the child's default
    console encoding on Windows is a code page that cannot represent every
    character a label or a file path might contain.
    """
    env = dict(os.environ if base is None else base)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class CommandRunner:
    """One child process at a time, with its output on a queue.

    A runner is reusable: :meth:`start` after the previous child has exited
    begins a new one. Starting while a child is still alive is a programming
    error, not a queue-up.
    """

    def __init__(self, name: str = "command") -> None:
        self.name = name
        self.events: queue.Queue[Event] = queue.Queue()
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self.command: list[str] = []

    # -- state ---------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    # -- lifecycle -----------------------------------------------------------

    def start(self, command: Sequence[str], cwd: str | Path | None = None) -> None:
        with self._lock:
            if self.is_running:
                raise RuntimeError(f"{self.name} is already running")
            self.command = list(command)
            kwargs: dict = {}
            if sys.platform == "win32":
                # A new process group is what makes a targeted CTRL_BREAK
                # possible in `stop`; without it the event would go to this
                # process too and take the GUI down with the daemon.
                kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            else:
                # Same idea: its own session, so the signal can be sent to the
                # child's group and reach anything it spawned.
                kwargs["start_new_session"] = True
            try:
                self._process = subprocess.Popen(  # noqa: S603 - argv built by commands.py
                    self.command,
                    cwd=str(cwd) if cwd else None,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    # Merged rather than a second pipe: the daemon prints to
                    # stdout and logs to stderr, and two pipes would interleave
                    # them by whichever thread woke first. One pipe keeps the
                    # order the child wrote them in.
                    stderr=subprocess.STDOUT,
                    env=child_environment(),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    **kwargs,
                )
            except OSError as exc:
                self._process = None
                self.events.put(Failed(f"could not start {self.name}: {exc}"))
                return
            self.events.put(Started(list(self.command)))
            self._reader = threading.Thread(
                target=self._pump, args=(self._process,), name=f"{self.name}-reader", daemon=True
            )
            self._reader.start()

    def _pump(self, process: subprocess.Popen[str]) -> None:
        stream = process.stdout
        assert stream is not None
        try:
            for line in stream:
                self.events.put(Line(line.rstrip("\r\n")))
        except (ValueError, OSError):  # pipe closed under us by stop()
            pass
        finally:
            returncode = process.wait()
            self.events.put(Finished(returncode))

    def stop(self, grace: float = 4.0, poll: float = 0.05) -> None:
        """Ask the child to stop, then insist.

        The first step is the signal the daemon actually handles, so it shuts
        down through its own ``finally`` block -- closing the WebSocket, the
        Tesseract API and the capture sources -- rather than being cut off
        mid-publish. Only if that is ignored does this escalate.
        """
        process = self._process
        if process is None or process.poll() is not None:
            return
        for step in (self._interrupt, process.terminate, process.kill):
            try:
                step()
            except (OSError, PermissionError):
                pass
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    return
                time.sleep(poll)

    def _interrupt(self) -> None:
        process = self._process
        if process is None:
            return
        if sys.platform == "win32":
            # CTRL_BREAK rather than CTRL_C: with CREATE_NEW_PROCESS_GROUP the
            # child ignores Ctrl-C by default, and CTRL_C_EVENT cannot be
            # aimed at a single group anyway. The daemon handles SIGBREAK for
            # exactly this reason.
            process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGINT)

    def wait(self, timeout: float | None = None) -> int | None:
        process = self._process
        if process is None:
            return None
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    # -- output --------------------------------------------------------------

    def drain(self, limit: int = 500) -> list[Event]:
        """Everything the child has said since the last call.

        Bounded because a chatty child at 28 Hz can out-produce a 20 Hz UI
        poll, and a drain that emptied an unbounded queue would let the GUI
        spend the whole frame appending text. The remainder is simply read on
        the next poll.
        """
        events: list[Event] = []
        for _ in range(limit):
            try:
                events.append(self.events.get_nowait())
            except queue.Empty:
                break
        return events
