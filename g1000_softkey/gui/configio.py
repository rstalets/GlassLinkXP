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

One thing it cannot do is preserve comments. A user who has hand-annotated
their config and then saves from the form would lose those notes, so
:func:`save` keeps the previous file as ``<name>.bak`` first, and the GUI's
raw editor writes text through untouched for anyone who would rather keep
their own file exactly as they wrote it.
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path
from typing import Any, Mapping

from ..config import AppConfig, StripGeometry, default_config, from_mapping
from . import schema

#: Sections in the order they are written, matching config.example.toml so a
#: file saved by the GUI and one copied from the example read the same way.
SECTION_ORDER = ("app", "display", "ocr", "color", "publish")


class ConfigIoError(Exception):
    """A config file could not be read, parsed or written."""


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def read_document(path: str | Path) -> dict[str, Any]:
    """The file as a plain nested dict, ready for the form to edit."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigIoError(f"could not read {p}: {exc}") from exc
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
        key: _plain(getattr(config.ocr, key))
        for key in (setting.key for setting in schema.OCR.settings)
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
# Written by the g1000 GUI. Editing it by hand is fine -- the GUI reads
# whatever is here -- but note that saving from the Settings form rewrites the
# whole file and does not keep hand-written comments. The previous version is
# saved beside it as config.toml.bak, and the GUI's raw editor writes your
# text through unchanged if you would rather keep your own notes in here.
#
# config.example.toml carries the full reasoning behind every default.
"""


def dumps(document: Mapping[str, Any]) -> str:
    """Serialise a config document to TOML, with a one-line note per setting."""
    out: list[str] = [HEADER]
    for section in SECTION_ORDER:
        body = document.get(section)
        if body is None:
            continue
        if section == "display":
            out.append(_dump_displays(body))
        else:
            out.append(_dump_table(section, section, body))
    # Anything the GUI does not know about is kept rather than dropped: an
    # unrecognised table is more likely to be a newer setting than a mistake,
    # and silently deleting a user's file content is not a thing to do.
    for key, value in document.items():
        if key not in SECTION_ORDER and isinstance(value, Mapping):
            out.append(_dump_table(key, key, value))
    return "\n".join(out).rstrip() + "\n"


def _dump_displays(displays: Mapping[str, Any]) -> str:
    blocks: list[str] = []
    for key, entry in displays.items():
        if not isinstance(entry, Mapping):
            continue
        geometry = entry.get("geometry")
        scalars = {k: v for k, v in entry.items() if k != "geometry"}
        blocks.append(_dump_table(f"display.{key}", "display", scalars,
                                  blurb=schema.DISPLAY.blurb))
        if isinstance(geometry, Mapping):
            blocks.append(_dump_table(f"display.{key}.geometry", "geometry", geometry,
                                      blurb=schema.GEOMETRY.blurb))
    return "\n".join(blocks)


def _dump_table(header: str, section: str, body: Mapping[str, Any], blurb: str = "") -> str:
    group = schema.BY_SECTION.get(section)
    lines: list[str] = []
    text = blurb or (group.blurb if group else "")
    if text:
        lines += _comment(text)
    lines.append(f"[{header}]")
    for key, value in body.items():
        if isinstance(value, Mapping):  # nested tables are emitted by the caller
            continue
        note = _note(section, key)
        if note:
            lines += _comment(note)
        if value is None:
            # An optional setting left unset. Written as a comment rather than
            # omitted silently, so the file still lists everything there is to
            # set -- the file is documentation as much as it is configuration.
            lines.append(f"# {key} is not set")
            continue
        lines.append(f"{key} = {_value(section, key, value)}")
    return "\n".join(lines) + "\n"


def _note(section: str, key: str) -> str:
    group = schema.BY_SECTION.get(section)
    if group is None:
        return ""
    for item in group.settings:
        if item.key == key:
            return item.help
    return ""


def _comment(text: str, width: int = 74) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = "#"
    for word in words:
        if len(current) + 1 + len(word) > width and current != "#":
            lines.append(current)
            current = "#"
        current += " " + word
    if current != "#":
        lines.append(current)
    return lines


def _value(section: str, key: str, value: Any) -> str:
    kind = schema.kind_of(section, key)
    if kind == "float" and isinstance(value, (int, float)) and not isinstance(value, bool):
        # Always with a decimal point. TOML types 12 as an integer, and while
        # the daemon would cope, a config that reads `loop_hz = 12` when the
        # setting is a rate invites the next reader to wonder which it is.
        return repr(float(value))
    return _scalar(value)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_scalar(item) for item in value) + "]"
    raise ConfigIoError(f"cannot write {value!r} ({type(value).__name__}) to TOML")


_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\f": "\\f",
            "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _string(value: str) -> str:
    out = ["\""]
    for char in value:
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    out.append("\"")
    return "".join(out)


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
        return _scalar(_plain(value))
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
