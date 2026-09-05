"""Identify which softkey page is showing, and fill in what OCR could not read.

The high-confidence labels on a strip are enough to say *which page* you are
looking at: IDENT, BKSP and BACK in cells 9-11 means the transponder keypad,
and nothing else. The keypad's layout is fixed and known, so once the page is
identified the low-confidence cells do not need to be recognised at all --
they can be looked up.

This is the opposite way round from OCR. Instead of asking "what does this
glyph say?" of a 10-pixel digit, it asks "which page is this?" of the labels
that read cleanly, and takes the rest from the page definition. The hard cells
are exactly the ones a page lookup answers for free.

Rules live in a TOML file so the library grows without touching the code:

    [[screen]]
    name = "xpdr-code"
    display = "pfd"
    match  = { 9 = "IDENT", 10 = "BKSP", 11 = "BACK" }
    labels = { 1 = "0", 2 = "1", 3 = "2" }
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

LOG = logging.getLogger(__name__)


class ScreenError(Exception):
    """A malformed screen definition."""


@dataclass(frozen=True)
class Screen:
    """One softkey page: how to recognise it, and what it should read."""

    name: str
    display: str
    match: dict[int, str] = field(default_factory=dict)
    labels: dict[int, str] = field(default_factory=dict)

    @property
    def specificity(self) -> int:
        """How many cells must agree. More specific pages win a tie."""
        return len(self.match)


def _cells(raw, where: str) -> dict[int, str]:
    out: dict[int, str] = {}
    if not isinstance(raw, dict):
        raise ScreenError(f"{where} must be a table of cell = \"LABEL\", got {raw!r}")
    for key, value in raw.items():
        try:
            index = int(key)
        except (TypeError, ValueError) as exc:
            raise ScreenError(f"{where}: {key!r} is not a cell number") from exc
        if index < 1:
            raise ScreenError(f"{where}: cells are numbered from 1, got {index}")
        out[index] = str(value).strip().upper()
    return out


def load_screens(path: str | Path | None) -> list[Screen]:
    if not path:
        return []
    file = Path(path)
    if not file.is_file():
        LOG.debug("no screen definitions at %s", file)
        return []
    import tomllib

    try:
        raw = tomllib.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScreenError(f"could not read {file}: {exc}") from exc

    screens = []
    for entry in raw.get("screen", []):
        name = str(entry.get("name", "")).strip()
        if not name:
            raise ScreenError(f"{file}: every [[screen]] needs a name")
        display = str(entry.get("display", "")).strip()
        if not display:
            raise ScreenError(f"{file}: screen {name!r} needs a display")
        screen = Screen(
            name=name,
            display=display,
            match=_cells(entry.get("match", {}), f"screen {name!r} match"),
            labels=_cells(entry.get("labels", {}), f"screen {name!r} labels"),
        )
        if not screen.match:
            raise ScreenError(f"screen {name!r} has no match cells, so it would match anything")
        screens.append(screen)
    LOG.info("loaded %d screen definition(s) from %s", len(screens), file)
    return screens


class ScreenLibrary:
    """The known pages, and the lookup that applies them to a strip."""

    def __init__(self, screens: list[Screen] | None = None) -> None:
        self.screens = screens or []

    @classmethod
    def load(cls, path: str | Path | None) -> "ScreenLibrary":
        return cls(load_screens(path))

    def identify(
        self, display: str, labels: dict[int, str], confidences: dict[int, float], floor: float
    ) -> Screen | None:
        """The page whose match cells all agree, read confidently.

        Every match cell must be present, equal, and above ``floor``: an
        identification built on a guess would propagate that guess into every
        cell it fills. Ties go to the more specific page; genuine ambiguity
        returns nothing rather than picking one.
        """
        candidates = []
        for screen in self.screens:
            if screen.display != display:
                continue
            if all(
                labels.get(cell) == expected and confidences.get(cell, 0.0) >= floor
                for cell, expected in screen.match.items()
            ):
                candidates.append(screen)
        if not candidates:
            return None
        candidates.sort(key=lambda s: s.specificity, reverse=True)
        if len(candidates) > 1 and candidates[0].specificity == candidates[1].specificity:
            LOG.warning(
                "screens %s and %s both match equally well; not applying either",
                candidates[0].name, candidates[1].name,
            )
            return None
        return candidates[0]

    def __len__(self) -> int:
        return len(self.screens)
