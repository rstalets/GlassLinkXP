"""Graphical front end for the G1000 softkey daemon.

The daemon is a command line program and always will be -- it has to run
unattended, next to a flight simulator, and be scriptable. But most of the
people it is for are pilots rather than programmers, and asking them to type

    .\\glasslinkxp -c config.toml calibrate --out calibration

to find out where the softkey strip is, and then to edit a TOML file by hand
to say what they found, is asking a great deal.

So this is a window over the same commands. It does not reimplement any of
them: every button spawns the CLI and shows what it said, which means the GUI
cannot drift into doing something subtly different from what the documentation
describes, and a problem reproduced in the GUI can be reproduced on the
command line -- the exact command is printed above each run's output.
"""

from __future__ import annotations

import sys
from pathlib import Path

TK_MISSING = """\
The graphical interface needs Python's Tk support, which this Python does not
have.

  interpreter: {executable}

On Windows the installer from python.org includes it as standard, and so do
the Python builds uv downloads. If this is a Linux machine, Tk is usually a
separate package -- python3-tk on Debian and Ubuntu, python3-tkinter on
Fedora.

Everything the GUI does can also be done from the command line, which needs
none of this:

    python -m glasslinkxp.main --help
"""


def launch(config_path: str | Path | None = None) -> int:
    """Open the window. Returns a process exit code."""
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        print(TK_MISSING.format(executable=sys.executable or "unknown"), file=sys.stderr)
        return 2

    from .app import build

    root = tk.Tk()
    _use_native_theme(ttk.Style(root))
    build(root, config_path)
    root.mainloop()
    return 0


def _use_native_theme(style) -> None:
    """Prefer the platform's own ttk theme over Tk's 1990s default.

    'vista' on Windows and 'aqua' on macOS are what every other application
    there looks like; 'clam' is the least dated of the ones that exist
    everywhere else. Failing to set a theme is not worth an error -- it only
    means the window looks plain.
    """
    available = set(style.theme_names())
    for name in ("vista", "aqua", "clam", "default"):
        if name in available:
            try:
                style.theme_use(name)
            except Exception:  # noqa: BLE001 - cosmetic only
                continue
            return
