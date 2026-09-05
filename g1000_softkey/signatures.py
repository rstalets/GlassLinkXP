"""Shape signatures for softkey labels, as a fallback when OCR is unsure.

Tesseract decides what a glyph *is*. When the glyph is only ~10 px tall that
decision gets shaky -- a 0 comes back as a 2 at 54% confidence -- but the
pixels themselves are perfectly stable from frame to frame. The softkey
vocabulary is also small and closed, so for the cells OCR is least sure about
we can simply ask "which known label does this look like?" instead.

This is deliberately *not* a hash of the raw cell. Hashing raw pixels is what
brightness and highlight state break. A signature is taken from the binarised,
content-cropped, size-normalised glyph, so it survives a dimmed softkey, a
highlighted one, and a window resize -- and it is compared by distance rather
than equality, so it degrades instead of missing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import cv2
import numpy as np

LOG = logging.getLogger(__name__)

#: Signatures are compared at this size. Small enough to be robust to a pixel
#: of jitter, large enough to keep 0 and 8 apart.
GRID = 16


def signature(prep: np.ndarray) -> np.ndarray:
    """Reduce a preprocessed cell to a GRID x GRID boolean shape."""
    ink = (prep < 128).astype(np.uint8)
    columns = np.flatnonzero(ink.any(axis=0))
    rows = np.flatnonzero(ink.any(axis=1))
    if columns.size and rows.size:
        ink = ink[rows[0]: rows[-1] + 1, columns[0]: columns[-1] + 1]
    resized = cv2.resize(ink * 255, (GRID, GRID), interpolation=cv2.INTER_AREA)
    return resized >= 128


def distance(a: np.ndarray, b: np.ndarray) -> float:
    """Fraction of cells that disagree: 0.0 identical, 1.0 opposite."""
    return float(np.count_nonzero(a != b)) / a.size


class SignatureStore:
    """Known label -> one or more shapes, loaded from and saved to JSON."""

    def __init__(self, entries: dict[str, list[np.ndarray]] | None = None) -> None:
        self.entries: dict[str, list[np.ndarray]] = entries or {}

    # -- persistence ------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "SignatureStore":
        file = Path(path)
        if not file.is_file():
            return cls()
        try:
            raw = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOG.warning("could not read signatures from %s: %s", file, exc)
            return cls()
        entries: dict[str, list[np.ndarray]] = {}
        for label, samples in raw.get("labels", {}).items():
            shapes = []
            for bits in samples:
                if len(bits) != GRID * GRID:
                    LOG.warning("skipping a %s signature of the wrong size", label)
                    continue
                shapes.append(np.array([c == "1" for c in bits]).reshape(GRID, GRID))
            if shapes:
                entries[label] = shapes
        LOG.info("loaded %d label signatures from %s", len(entries), file)
        return cls(entries)

    def save(self, path: str | Path) -> None:
        payload = {
            "grid": GRID,
            "labels": {
                label: ["".join("1" if v else "0" for v in shape.flatten()) for shape in shapes]
                for label, shapes in sorted(self.entries.items())
            },
        }
        file = Path(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        tmp = file.with_suffix(file.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        tmp.replace(file)
        LOG.info("wrote %d label signatures to %s", len(self.entries), file)

    # -- use --------------------------------------------------------------
    def add(self, label: str, shape: np.ndarray, dedupe: float = 0.02) -> bool:
        """Record a shape. Near-duplicates of an existing sample are dropped."""
        samples = self.entries.setdefault(label, [])
        if any(distance(shape, existing) <= dedupe for existing in samples):
            return False
        samples.append(shape)
        return True

    def match(
        self, shape: np.ndarray, max_distance: float = 0.14, margin: float = 0.04
    ) -> tuple[str | None, float]:
        """Nearest label, or (None, distance) when the answer is not clear-cut.

        Two guards, because a confident wrong answer is worse than none: the
        winner must be close in absolute terms, and it must beat the runner-up
        by ``margin``. Without the second, 0 and 8 would take turns.
        """
        if not self.entries:
            return None, 1.0
        ranked = sorted(
            (min(distance(shape, sample) for sample in samples), label)
            for label, samples in self.entries.items()
        )
        best_distance, best_label = ranked[0]
        if best_distance > max_distance:
            return None, best_distance
        for runner_distance, runner_label in ranked[1:]:
            if runner_label != best_label:
                if runner_distance - best_distance < margin:
                    LOG.debug(
                        "signature ambiguous: %s at %.3f vs %s at %.3f",
                        best_label, best_distance, runner_label, runner_distance,
                    )
                    return None, best_distance
                break
        return best_label, best_distance

    def __len__(self) -> int:
        return len(self.entries)
