"""Read, edit and write ``config.toml`` on behalf of the Settings tab.

The daemon reads TOML and never writes it; the GUI has to do both. The
standard library gives us the reading half (``tomllib``) and none of the
writing half, and this project does not take a dependency it can avoid, so
there is a small writer here.

It only has to serialise this config's own shapes -- scalars and short arrays
-- so it is a closed problem rather than a general TOML implementation, and
``tests/test_gui_configio.py`` closes it the only way worth trusting: write a
document, load it back through the daemon's own ``load_config`` and compare
against the ``AppConfig`` it started from. A writer that is wrong about
quoting or about floats fails that round trip.

Reading is ``tomllib``'s and writing is ``tomli-w``'s; what is left here is
the shape of a config document and the moving of values in and out of a form.

Writing was a hand-written function here for a while, on the grounds that the
standard library reads TOML but does not write it, and that a library cannot
write the comments that explained each setting. Both halves of that were the
wrong trade. The comments are better off in docs/CONFIGURATION.md, where they
are not at the mercy of somebody pressing Save. And the writer had four ways
of mangling or silently dropping legal TOML -- a quoted table name, a table
inside a table, an array of tables, a value at the top level -- which is the
ordinary fate of a serialiser written for the shapes its author had in mind.

Comments in an existing file are still lost when the form saves, so
:func:`save` keeps the previous version as ``<name>.bak`` and the GUI's raw
editor writes text through untouched for anyone who keeps notes in there.
"""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path
from typing import Any, Mapping

from ..config import AppConfig, StripGeometry, default_config, from_mapping
from . import schema

class ConfigIoError(Exception):
    """A config file could not be read, parsed or written."""


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def read_document(path: str | Path) -> dict[str, Any]:
    """The file as a plain nested dict, ready for the form to edit.

    Both ways of failing to *read* the bytes are a ``ConfigIoError``, because
    the window opens by calling this and catches nothing else. A file saved
    as UTF-16 -- which is what Notepad's old "Unicode" option produces, and
    what a config file edited on a Windows machine can easily end up as --
    raises ``UnicodeDecodeError``, a ``ValueError``, which used to escape:
    the exception happened before the window existed, so there was no window,
    no message, and under ``pythonw.exe`` no console to print to either.
    ``prefs.load`` has always caught ``ValueError`` for the same reason.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigIoError(f"could not read {p}: {exc}") from exc
    except ValueError as exc:  # UnicodeDecodeError, and anything like it
        raise ConfigIoError(
            f"could not read {p}: it is not UTF-8 text ({exc}). TOML files are UTF-8; "
            "if you saved it from Notepad, save it again with the encoding set to UTF-8."
        ) from exc
    return loads(text, source=str(p))


def loads(text: str, source: str = "the configuration") -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigIoError(f"{source}: {exc}") from exc


def default_document() -> dict[str, Any]:
    """A document holding every setting at its built-in default.

    Built from ``default_config()`` rather than from a copy of the example
    file, so it cannot drift from the defaults the daemon actually uses when
    no config file is given at all.
    """
    return document_from_config(default_config())


def document_from_config(config: AppConfig) -> dict[str, Any]:
    document: dict[str, Any] = {
        "app": {
            "loop_hz": config.loop_hz,
            "change_gating": config.change_gating,
            "change_tolerance": config.change_tolerance,
        },
        "display": {},
    }
    for display in config.displays:
        entry: dict[str, Any] = {
            "window_title": display.window_title,
            "enabled": display.enabled,
            "dataref_prefix": display.dataref_prefix,
            "manage_window_size": display.manage_window_size,
            "geometry": dict(display.geometry.as_dict()),
        }
        if display.window_size is not None:
            entry["window_size"] = list(display.window_size)
        document["display"][display.key] = entry
    document["ocr"] = {
        setting.key: _plain(getattr(config.ocr, setting.key))
        for setting in schema.OCR.settings
        if not _is_package_default(config.ocr, setting, "ocr")
    }
    document["color"] = {
        key: getattr(config.color, key)
        for key in (setting.key for setting in schema.COLOR.settings)
    }
    document["publish"] = {
        key: getattr(config.publish, key)
        for key in (setting.key for setting in schema.PUBLISH.settings)
    }
    return document


def _plain(value: Any) -> Any:
    """Tuples back to lists: the config holds tuples so it stays hashable."""
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _is_package_default(section_config: Any, setting: schema.Setting, section: str) -> bool:
    """Whether this value is only the path to the package's own copy of a file.

    Those defaults are absolute paths into whichever checkout is running --
    ``.../g1000_softkey/screens.toml`` -- and a document is what the form
    fills its boxes from and what Save writes back out. Copying one into
    config.toml pins the file to this install, so moving or reinstalling the
    project leaves the daemon pointing at a file that is not there; the user
    never asked for that path and would have no reason to look for it.
    ``config.example.toml`` leaves these keys out for the same reason.

    An absent key is not a missing setting: it is the setting saying "the copy
    that ships with the package", which is what the daemon does with it.
    """
    if not setting.package_default:
        return False
    return getattr(section_config, setting.key) == schema.default_value(section, setting)


# ---------------------------------------------------------------------------
# validating
# ---------------------------------------------------------------------------


def validate(document: Mapping[str, Any], base_dir: Path | None = None) -> AppConfig:
    """Build an :class:`AppConfig`, raising :class:`ConfigError` if it will not.

    Called before writing, so a value that the daemon would reject at startup
    is refused in the dialog where the user can still see what they typed --
    rather than being written out and then failing the next time they press
    Start, in a log pane, in a different tab.
    """
    return from_mapping(document, base_dir=base_dir)


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def save(path: str | Path, document: Mapping[str, Any], backup: bool = True) -> Path | None:
    """Write ``document`` to ``path``. Returns the backup's path, if made."""
    p = Path(path)
    backup_path: Path | None = None
    if backup and p.exists():
        backup_path = p.with_suffix(p.suffix + ".bak")
        try:
            shutil.copy2(p, backup_path)
        except OSError as exc:
            raise ConfigIoError(f"could not back up {p}: {exc}") from exc
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dumps(document), encoding="utf-8")
    except OSError as exc:
        raise ConfigIoError(f"could not write {p}: {exc}") from exc
    return backup_path


def save_text(path: str | Path, text: str, backup: bool = True) -> Path | None:
    """Write raw TOML text, so a hand-edited file keeps its own comments."""
    p = Path(path)
    loads(text, source=str(p))  # refuse to write something that will not parse
    backup_path: Path | None = None
    if backup and p.exists():
        backup_path = p.with_suffix(p.suffix + ".bak")
        try:
            shutil.copy2(p, backup_path)
        except OSError as exc:
            raise ConfigIoError(f"could not back up {p}: {exc}") from exc
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise ConfigIoError(f"could not write {p}: {exc}") from exc
    return backup_path


HEADER = """\
# G1000 softkey daemon configuration.
#
# Written by the g1000 GUI. What each setting means, and why its default is
# what it is, lives in docs/CONFIGURATION.md -- saving from the Settings form
# rewrites this file and does not keep comments, so the reasoning is kept
# somewhere that survives. The previous version is saved beside this one as
# config.toml.bak, and the GUI's Raw file tab writes your text through exactly
# as you typed it if you would rather keep notes in here.

"""


def strip_unset(document: Mapping[str, Any]) -> dict[str, Any]:
    """A copy with the ``None`` values removed.

    ``None`` is how the form says a setting is not set, and TOML has no way to
    write that -- an absent key *is* the unset state, which is what the daemon
    reads it as. Done on a copy: the document the window is holding still has
    the key, because the form needs somewhere to put an empty box.
    """
    out: dict[str, Any] = {}
    for key, value in document.items():
        if value is None:
            continue
        out[key] = strip_unset(value) if isinstance(value, Mapping) else value
    return out


def dumps(document: Mapping[str, Any]) -> str:
    """Serialise a config document to TOML.

    The serialising is ``tomli-w``'s. It was a hand-written function here
    once, because the standard library reads TOML and does not write it and
    because a library cannot write the comments that explained each setting.
    Both halves of that turned out to be the wrong trade: the comments moved
    to docs/CONFIGURATION.md, where they are not at the mercy of a save, and
    the hand-written writer had four ways of mangling or silently dropping
    perfectly legal TOML -- a quoted table name, a nested table, an array of
    tables, a value at the top level. A dependency that already handles all of
    them is the right amount of code to own for this: none.
    """
    try:
        import tomli_w  # noqa: PLC0415 - a clear message beats an ImportError
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise ConfigIoError(
            "tomli-w is needed to save the configuration and is not installed. "
            "Run: pip install tomli-w   (or re-run the installer). The Raw file "
            "tab can still save, since it writes your text out unchanged."
        ) from exc

    try:
        body = tomli_w.dumps(strip_unset(document))
    except (TypeError, ValueError) as exc:
        raise ConfigIoError(f"this configuration cannot be written as TOML: {exc}") from exc
    return HEADER + body


# ---------------------------------------------------------------------------
# form values
# ---------------------------------------------------------------------------


def parse_field(setting: schema.Setting, text: str) -> Any:
    """One form field's text back into a config value.

    The composite kinds ("toml") are parsed by ``tomllib`` rather than by a
    syntax invented here: the field holds exactly what would be in the file,
    so the user sees one notation instead of two and the error messages come
    from a real parser.
    """
    text = text.strip()
    if setting.kind == "bool":
        return text.lower() in ("1", "true", "yes", "on")
    if not text:
        if setting.optional:
            return None
        raise ConfigIoError(f"{setting.label} cannot be empty")
    if setting.kind == "int":
        try:
            return int(text, 10)
        except ValueError:
            raise ConfigIoError(f"{setting.label}: {text!r} is not a whole number") from None
    if setting.kind == "float":
        try:
            return float(text)
        except ValueError:
            raise ConfigIoError(f"{setting.label}: {text!r} is not a number") from None
    if setting.kind == "toml":
        parsed = loads(f"value = {text}", source=setting.label)
        return parsed["value"]
    return text


def format_field(setting: schema.Setting, value: Any) -> str:
    """A config value as the text to put in the form field."""
    if value is None:
        return ""
    if setting.kind == "bool":
        return "true" if value else "false"
    if setting.kind == "toml":
        return _toml_literal(value)
    if setting.kind == "float":
        return repr(float(value))
    return str(value)


# ---------------------------------------------------------------------------
# strip geometry, shared between displays
# ---------------------------------------------------------------------------

#: The keys a strip geometry is made of, in the order the form shows them.
GEOMETRY_FIELDS = tuple(StripGeometry.__dataclass_fields__)


def geometry_of(document: Mapping[str, Any], display: str) -> StripGeometry:
    """One display's strip geometry, with the built-in defaults filled in."""
    stored = get_in(document, ("display", display, "geometry"), {})
    if not isinstance(stored, Mapping):
        return StripGeometry()
    return StripGeometry(**{k: v for k, v in stored.items() if k in GEOMETRY_FIELDS})


def same_geometry(document: Mapping[str, Any], one: str, other: str) -> bool:
    """Whether two displays are already reading the same part of their window.

    Used to work out, for a configuration file written before the GUI offered
    to link them, whether the second display was in fact being calibrated
    separately. Getting that wrong in the permissive direction would silently
    overwrite somebody's second calibration, so it is asked of the numbers
    rather than assumed.
    """
    return geometry_of(document, one) == geometry_of(document, other)


def set_geometry(document: dict[str, Any], display: str, geometry: StripGeometry) -> None:
    """Write a strip geometry into a document, field by field."""
    for field in GEOMETRY_FIELDS:
        set_in(document, ("display", display, "geometry", field),
               _plain(getattr(geometry, field)))


def _toml_literal(value: Any) -> str:
    """One value as the TOML text a form field holds, on a single line.

    ``json.dumps`` rather than the TOML writer, which spreads a nested array
    over five lines -- correct in a file, useless in a one-line entry box. For
    the values these fields hold (numbers, strings, booleans and arrays of
    them) JSON's notation and TOML's are the same text, and
    ``test_gui_configio.py`` checks that by reading every one of them back
    with ``tomllib``. Anything outside that overlap has no business in a
    single-line box in the first place.
    """
    return json.dumps(_plain(value))


def get_in(document: Mapping[str, Any], path: tuple[str, ...], default: Any = None) -> Any:
    node: Any = document
    for step in path:
        if not isinstance(node, Mapping) or step not in node:
            return default
        node = node[step]
    return node


def set_in(document: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    """Set a nested key, creating tables on the way. ``None`` removes the key."""
    node = document
    for step in path[:-1]:
        nxt = node.get(step)
        if not isinstance(nxt, dict):
            nxt = {}
            node[step] = nxt
        node = nxt
    if value is None:
        node.pop(path[-1], None)
    else:
        node[path[-1]] = value
