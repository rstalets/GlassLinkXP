"""Where the GUI remembers which config file you were using.

Deliberately not stored in ``config.toml``: that file is the daemon's, it is
the thing the user copies between machines and pastes into a bug report, and
which tab the window was last on has no business in it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

APP_DIR_NAME = "g1000-softkey"
FILE_NAME = "gui.json"

DEFAULTS: dict[str, Any] = {
    "config_path": "",
    "image_source": "",
    "verbose": False,
    "publisher": "",
    "window": "",
    "tab": 0,
}


def prefs_dir() -> Path:
    """Per-user settings directory, following the platform's convention."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / APP_DIR_NAME


def prefs_path() -> Path:
    return prefs_dir() / FILE_NAME


def load(path: Path | None = None) -> dict[str, Any]:
    """The saved preferences, merged over the defaults.

    Never raises: a corrupt or unreadable preferences file is not a reason to
    refuse to open the window, so it is discarded and the defaults are used.
    """
    p = path or prefs_path()
    values = dict(DEFAULTS)
    try:
        stored = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return values
    if isinstance(stored, dict):
        values.update({k: v for k, v in stored.items() if k in DEFAULTS})
    return values


def save(values: dict[str, Any], path: Path | None = None) -> None:
    """Store the preferences. Failure is ignored for the same reason."""
    p = path or prefs_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({k: v for k, v in values.items() if k in DEFAULTS}, indent=1),
            encoding="utf-8",
        )
    except OSError:
        pass


def resolve_config_path(explicit: str | os.PathLike[str] | None,
                        stored: str | None,
                        search_from: Path | None = None) -> Path | None:
    """Which config file to open with, or None if there is not one yet.

    In order: what was asked for on the command line, then whatever was open
    last time, then a ``config.toml`` next to the project. None means the user
    has not made one, which the GUI treats as a thing to offer to fix rather
    than an error.
    """
    if explicit:
        return Path(explicit)
    if stored:
        candidate = Path(stored)
        if candidate.is_file():
            return candidate
    root = search_from or project_root()
    candidate = root / "config.toml"
    return candidate if candidate.is_file() else None


def project_root() -> Path:
    """The checkout this package lives in, where config.toml normally sits."""
    return Path(__file__).resolve().parents[2]


def example_config() -> Path | None:
    path = project_root() / "config.example.toml"
    return path if path.is_file() else None
