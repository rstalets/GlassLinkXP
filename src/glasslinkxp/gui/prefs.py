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

APP_DIR_NAME = "glasslinkxp"
FILE_NAME = "gui.json"

DEFAULTS: dict[str, Any] = {
    "config_path": "",
    "image_source": "",
    "verbose": False,
    "publisher": "",
    "window": "",
    "tab": 0,
    #: Whether the setup bar was up when the window last closed, and which
    #: step it was on. Kept so that closing the window half way through setup
    #: is not the same as abandoning it.
    "wizard_active": False,
    "wizard_step": 0,
    #: Whether the displays after the first one take their strip position
    #: from it. None means the user has not said, and the answer is worked
    #: out from whether the geometries already match -- so a configuration
    #: written before this existed, with a second display calibrated
    #: separately, is not silently overwritten the first time the GUI opens it.
    "follow_first_display": None,
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
    """Where the user's own files live: config.toml, calibration/, frames/.

    The installed app's root -- one level above ``src/``, where the venv and
    the launcher scripts sit too -- not the package directory itself, so a
    user's working files land somewhere they would think to look rather than
    buried inside the installed code.
    """
    return Path(__file__).resolve().parents[3]


def example_config() -> Path | None:
    path = project_root() / "src" / "config.example.toml"
    return path if path.is_file() else None
