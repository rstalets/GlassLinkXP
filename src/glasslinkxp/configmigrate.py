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

#: Settings the example leaves out *on purpose*, so "absent from the example"
#: does not mean "this version dropped it". These are the ones whose default
#: is a file inside the package (see ``configio._is_package_default``): the
#: example omits them so that a config file does not name a path into one
#: install. Without this, every update removed the ``ocr.labels_file`` the
#: vocabulary migration had just written, and re-added it, reporting a
#: change that had not happened.
#: ``tests/test_gui_schema.py`` keeps this in step with the schema.
KEPT_THOUGH_ABSENT = frozenset({"ocr.labels_file", "ocr.screens_file"})


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
        if key in example:
            continue
        where = f"{path}.{key}"
        if where in KEPT_THOUGH_ABSENT:
            merged[key] = _copy(user[key])
        else:
            changes.removed.append(where)
    return merged


# ---------------------------------------------------------------------------
# the vocabulary
# ---------------------------------------------------------------------------


@dataclass
class VocabularyChanges:
    """What a vocabulary merge did."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.added or self.removed)


def reconcile_labels(user_text: str, shipped_text: str,
                     previous_shipped_text: str | None) -> tuple[str, VocabularyChanges]:
    """Merge the vocabulary this version ships into the user's own copy.

    ``labels.txt`` is meant to be edited -- the softkey set depends on the
    aircraft and the X-Plane version -- so an update must not simply overwrite
    it, and must not simply leave it either, or a label added upstream never
    reaches anyone who has run the app once.

    Three inputs, not two, and that is the whole point. Given only the user's
    file and the new shipped one, a label in theirs and not in ours is
    ambiguous: it is either one they added or one we retired, and guessing
    wrong either deletes their work or keeps ours forever. ``previous_shipped``
    -- a copy of the vocabulary this app last shipped, kept beside the user's
    file for exactly this -- settles it:

    * shipped now and not shipped before, and not already theirs -> **added**;
    * shipped before but not now, and in their file -> **removed** (it came
      from us, so it is ours to withdraw);
    * anything else in their file is theirs, and is left alone.

    With no previous snapshot -- a file from before this mechanism existed --
    nothing can be attributed, so it only ever adds, and never removes.

    The user's file is edited as *lines*, not rewritten from a set: their
    comments, their groupings and their order are theirs too.
    """
    from .ocr import parse_labels  # noqa: PLC0415 - one reader of the format

    user = parse_labels(user_text)
    shipped = parse_labels(shipped_text)
    previously_shipped = (
        parse_labels(previous_shipped_text) if previous_shipped_text is not None else None
    )

    have = set(user)
    changes = VocabularyChanges()

    if previously_shipped is None:
        # Nothing to attribute anything to: add what they are missing, and
        # never take anything away.
        changes.added = [label for label in shipped if label not in have]
    else:
        was_shipped = set(previously_shipped)
        # New upstream, and not something they already have or deleted on
        # purpose -- a label they removed was shipped before, so it is not new.
        changes.added = [
            label for label in shipped if label not in have and label not in was_shipped
        ]
        withdrawn = was_shipped - set(shipped)
        changes.removed = [label for label in user if label in withdrawn]

    if not changes.any:
        return user_text, changes

    lines = user_text.splitlines()
    removed = set(changes.removed)
    kept = [line for line in lines if line.split("#", 1)[0].strip().upper() not in removed]

    if changes.added:
        if kept and kept[-1].strip():
            kept.append("")
        kept.append("# --- added by a GlassLinkXP update ---")
        kept.extend(changes.added)

    return "\n".join(kept) + "\n", changes


class _Missing:
    """A sentinel, because None is a value a setting can legitimately have."""


_MISSING = _Missing()


def _copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value
