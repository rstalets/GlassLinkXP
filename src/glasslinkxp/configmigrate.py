"""Bring an existing ``config.toml`` up to date with the version installed.

The installer replaces the app but keeps the user's ``config.toml``, so a
config written by an older version has to be reconciled with the settings this
one knows about. The rule is deliberately simple, and it is the *user's* rule
rather than an inferred one:

* a setting this version has and the file does not is **added**, at the value
  ``config.example.toml`` gives it;
* a setting the file has and this version does not is **removed**;
* a setting in both **keeps the user's value**.

The last of those is the accepted cost: changing a default does not reach
anyone who already has a config file, because there is no way to tell a value
somebody chose from the same value copied out of an older example. Retuning a
default therefore needs saying so in the release notes, not just changing it.

``config.example.toml`` is the reference for what this version has, rather
than the dataclasses, because it is already the file that documents every
setting and it is what a fresh install is seeded from -- so "what a new user
would get" and "what an upgrade adds" cannot drift apart.

Two shapes are handled specially, both because a blanket key diff would
destroy something the user meant:

* **Displays.** ``[display.<name>]`` is a set the user owns, not a fixed list
  of settings: the example happens to define pfd and mfd, but somebody may
  have deleted one or added a third. Displays are never added or removed --
  only the settings *within* each display the user has are reconciled, against
  the example's own display as the template.
* **Anything the daemon does not read.** A top-level table that is not one of
  the sections ``config.from_mapping`` looks at -- a hand-written
  ``[[screen]]`` block, somebody's own notes -- is left exactly as it is. It
  is not a setting this version dropped, so "removed" would be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

#: The top-level tables ``config.from_mapping`` reads. Only these are
#: reconciled; see the module docstring for why the rest is left alone.
SECTIONS = ("app", "window_management", "ocr", "color", "publish")
DISPLAY_SECTION = "display"


@dataclass
class Changes:
    """What a reconciliation did, as dotted paths, for reporting."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    kept_untouched: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.added or self.removed)


def reconcile(user: Mapping[str, Any], example: Mapping[str, Any]) -> tuple[dict, Changes]:
    """Merge ``user`` onto what ``example`` says this version has.

    Returns the new document and what changed. Neither input is modified.
    """
    changes = Changes()
    result: dict[str, Any] = {}

    for section in SECTIONS:
        if section not in example:
            continue
        merged = _reconcile_table(
            user.get(section) if isinstance(user.get(section), Mapping) else {},
            example[section],
            section,
            changes,
        )
        result[section] = merged

    displays = user.get(DISPLAY_SECTION)
    template = _display_template(example)
    if isinstance(displays, Mapping) and template is not None:
        result[DISPLAY_SECTION] = {
            name: _reconcile_table(
                entry if isinstance(entry, Mapping) else {},
                template,
                f"{DISPLAY_SECTION}.{name}",
                changes,
            )
            for name, entry in displays.items()
        }
    elif DISPLAY_SECTION in example:
        # No displays of their own to keep: take the example's, whole.
        result[DISPLAY_SECTION] = _copy(example[DISPLAY_SECTION])
        changes.added.append(DISPLAY_SECTION)

    # Everything else the user had, untouched.
    for key, value in user.items():
        if key in result:
            continue
        result[key] = _copy(value)
        changes.kept_untouched.append(key)

    return result, changes


def _display_template(example: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """One display from the example, as the shape every display should have.

    Any of them will do: they carry the same settings as each other, and it is
    the *keys* that are being reconciled, never the values -- a display keeps
    whatever window title and geometry the user calibrated.
    """
    displays = example.get(DISPLAY_SECTION)
    if not isinstance(displays, Mapping):
        return None
    for entry in displays.values():
        if isinstance(entry, Mapping):
            return entry
    return None


def _reconcile_table(user: Mapping[str, Any], example: Mapping[str, Any],
                     path: str, changes: Changes) -> dict[str, Any]:
    """One table: the example's keys, with the user's values where they had one."""
    merged: dict[str, Any] = {}
    for key, example_value in example.items():
        where = f"{path}.{key}"
        user_value = user.get(key, _MISSING)
        if isinstance(example_value, Mapping):
            merged[key] = _reconcile_table(
                user_value if isinstance(user_value, Mapping) else {},
                example_value, where, changes,
            )
            continue
        if user_value is _MISSING:
            merged[key] = _copy(example_value)
            changes.added.append(where)
        else:
            merged[key] = _copy(user_value)

    for key in user:
        if key not in example:
            changes.removed.append(f"{path}.{key}")
    return merged


class _Missing:
    """A sentinel, because None is a value a setting can legitimately have."""


_MISSING = _Missing()


def _copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value
