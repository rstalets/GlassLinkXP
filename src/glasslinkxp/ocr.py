"""OCR: a persistent Tesseract instance plus vocabulary snapping.

Two things matter for speed and accuracy here:

* the Tesseract API object is created **once** and reused (a binding that
  shells out to tesseract.exe and reloads the model per call is far too slow
  for video rates);
* the raw OCR string is snapped to the nearest known G1000 label, which turns
  ``DCLTP`` into ``DCLTR`` and ``lNSET`` into ``INSET`` for the price of a
  ``difflib`` call. Both the raw and the snapped string are reported so the
  user can see what Tesseract actually produced.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol, Sequence

import numpy as np

from .config import OcrConfig

LOG = logging.getLogger(__name__)

TESSDATA_CANDIDATES = (
    "/usr/share/tesseract-ocr/5/tessdata",
    "/usr/share/tesseract-ocr/4.00/tessdata",
    "/usr/share/tessdata",
    "/usr/local/share/tessdata",
    "/opt/homebrew/share/tessdata",
    r"C:\Program Files\Tesseract-OCR\tessdata",
)


class OcrUnavailable(Exception):
    """No usable Tesseract binding / language data."""


@dataclass
class CellResult:
    """One softkey cell after preprocessing + OCR + snapping."""

    index: int
    text: str = ""          # snapped label, this is what gets published
    raw: str = ""           # what Tesseract actually returned (normalised)
    confidence: float = 0.0  # Tesseract mean word confidence, 0..100
    match_score: float = 0.0  # difflib ratio of raw -> text, 0..1
    blank: bool = False
    ocr_ran: bool = True     # False when served from the change-gating cache
    #: Which variant of the preprocessing ladder this reading came from, as an
    #: index into it. Zero for a cell read straight off the gentle path, which
    #: is nearly all of them. It is here so the pipeline can say *which* rung
    #: won without anything having to re-derive it: "reached a polarity retry"
    #: and "was decided by one" are different facts, and only the second is
    #: worth printing -- an out-of-vocabulary label reaches every rung there is
    #: while reading perfectly.
    variant: int = 0
    #: Whether this reading came from an opposite-polarity retry rather than
    #: from the cell the way the ring read it. Retries are the exception arm
    #: of the ladder and are ranked as one: see :meth:`SoftkeyReader._rank`.
    fallback: bool = False
    by_screen: str = ""      # value came from this known softkey page
    confirmed_by: str = ""   # page agreed with a reading OCR was unsure of
    #: Background colour class, 0=black 1=white 2=yellow 3=red (see color.py).
    #: Measured from the cell's own pixels every frame, including on frames
    #: where the change gate served the text from cache -- a softkey becoming
    #: selected changes the background while the label stays identical.
    background: int = 0

    def describe(self) -> str:
        if self.blank:
            return "(blank)"
        extra = "" if self.raw == self.text else f" <- {self.raw!r}"
        cached = "" if self.ocr_ran else " [cached]"
        return f"{self.text!r}{extra} conf={self.confidence:.0f} match={self.match_score:.2f}{cached}"


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


class OcrEngine(Protocol):
    name: str

    def recognize(self, image: np.ndarray) -> tuple[str, float]:
        """Return (text, mean confidence 0..100) for a binarised cell."""

    def close(self) -> None:
        ...


def resolve_tessdata(configured: str | None) -> str | None:
    """Find a directory holding ``eng.traineddata``."""
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    env = os.environ.get("TESSDATA_PREFIX")
    if env:
        candidates.extend([env, str(Path(env) / "tessdata")])
    candidates.extend(TESSDATA_CANDIDATES)
    for candidate in candidates:
        if candidate and (Path(candidate) / "eng.traineddata").is_file():
            return str(Path(candidate))
    return None


class TesserocrEngine:
    """Persistent :class:`tesserocr.PyTessBaseAPI` (production path)."""

    name = "tesserocr"

    def __init__(self, config: OcrConfig) -> None:
        try:
            import tesserocr  # noqa: F401
        except ImportError as exc:
            raise OcrUnavailable(
                "tesserocr is not installed. Run install.cmd, which installs the pinned "
                "prebuilt wheel with `uv sync --locked`; in a dev checkout, `uv sync "
                "--locked` at the repository root does the same."
            ) from exc
        from PIL import Image

        self._Image = Image
        tessdata = resolve_tessdata(config.tessdata_path)
        kwargs = {"lang": config.lang, "psm": int(config.psm)}
        if tessdata:
            kwargs["path"] = tessdata
        try:
            self._api = tesserocr.PyTessBaseAPI(**kwargs)
        except RuntimeError as exc:
            raise OcrUnavailable(
                f"could not initialise Tesseract (tessdata={tessdata or 'TESSDATA_PREFIX'}): {exc}. "
                "Install the Tesseract language data and point ocr.tessdata_path at the "
                "directory that holds eng.traineddata."
            ) from exc
        if config.whitelist:
            self._api.SetVariable("tessedit_char_whitelist", config.whitelist)
        LOG.info("tesserocr ready (tessdata=%s, psm=%d)", tessdata or "TESSDATA_PREFIX", config.psm)

    def recognize(self, image: np.ndarray) -> tuple[str, float]:
        self._api.SetImage(self._Image.fromarray(image))
        text = self._api.GetUTF8Text()
        return text, float(self._api.MeanTextConf())

    def close(self) -> None:
        try:
            self._api.End()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            LOG.debug("ignoring tesserocr shutdown error: %s", exc)


def create_engine(config: OcrConfig) -> OcrEngine:
    """Build the OCR engine. One engine, kept as the seam the tests inject at."""
    return TesserocrEngine(config)


# ---------------------------------------------------------------------------
# Vocabulary snapping
# ---------------------------------------------------------------------------


def _key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def parse_labels(text: str) -> list[str]:
    """The labels in a vocabulary file, in order: one per line, '#' comments out.

    Here rather than inside :meth:`LabelVocabulary.from_file` because the
    update path reads the same files to merge a user's vocabulary with the one
    this version ships (``configmigrate.reconcile_labels``). Two readers of one
    format is how a merge comes to disagree with the daemon about what counts
    as a label, so there is one.
    """
    labels = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            labels.append(line.upper())
    return labels


class LabelVocabulary:
    """The known softkey labels, plus fuzzy lookup."""

    def __init__(self, labels: Sequence[str], cutoff: float = 0.62) -> None:
        self.labels = [label for label in labels if label]
        self.cutoff = cutoff
        self._by_key: dict[str, str] = {}
        for label in self.labels:
            self._by_key.setdefault(_key(label), label)
        self._keys = list(self._by_key)

    @classmethod
    def from_file(cls, path: str | Path, cutoff: float = 0.62) -> "LabelVocabulary":
        file = Path(path)
        if not file.is_file():
            raise FileNotFoundError(f"label vocabulary not found: {file}")
        labels = parse_labels(file.read_text(encoding="utf-8"))
        LOG.info("loaded %d labels from %s", len(labels), file)
        return cls(labels, cutoff)

    def snap(self, raw: str) -> tuple[str, float]:
        """Return (canonical label, 0..1 score); falls back to ``raw``."""
        key = _key(raw)
        if not key:
            return "", 0.0
        exact = self._by_key.get(key)
        if exact is not None:
            return exact, 1.0
        matches = difflib.get_close_matches(key, self._keys, n=1, cutoff=self.cutoff)
        if not matches:
            LOG.debug("no vocabulary match for %r (raw kept)", raw)
            return raw, 0.0
        score = difflib.SequenceMatcher(None, key, matches[0]).ratio()
        return self._by_key[matches[0]], score


def normalise(text: str, whitelist: str | None = None) -> str:
    """Tidy raw Tesseract output: uppercase, single spaces, whitelist only."""
    text = text.upper().replace("\n", " ")
    if whitelist:
        allowed = set(whitelist)
        text = "".join(ch for ch in text if ch in allowed or ch.isspace())
    return re.sub(r"\s+", " ", text).strip()


class SoftkeyReader:
    """Engine + vocabulary: binarised cell image in, :class:`CellResult` out."""

    def __init__(self, config: OcrConfig, engine: OcrEngine | None = None) -> None:
        self.config = config
        self.engine = engine or create_engine(config)
        self.vocabulary = LabelVocabulary.from_file(config.labels_file, config.fuzzy_cutoff)

    def read(self, index: int, image: np.ndarray) -> CellResult:
        text, confidence = self.engine.recognize(image)
        raw = normalise(text, self.config.whitelist)
        snapped, score = self.vocabulary.snap(raw)
        return CellResult(
            index=index, text=snapped, raw=raw, confidence=confidence, match_score=score
        )

    def read_best(self, index: int, images: Iterable[np.ndarray]) -> CellResult:
        """OCR each preprocessing variant and keep the most trustworthy answer.

        Ranked by: landing exactly on a known softkey label, then Tesseract's
        own confidence. An exact vocabulary hit outranks a confident miss,
        because the label set is small and closed -- a variant reading "B" at
        96% is certainly wrong, and only the vocabulary knows that.

        But an exact hit is not proof either, because the vocabulary holds
        near-identical members: every digit 0-7 is a valid softkey, so a 0
        misread as 2 also "matches exactly". The ladder therefore only stops
        early on a hit that is *also* confident; a shaky one is left to compete
        with the remaining rungs on confidence. In the common case the first
        rung is both, and this costs a single OCR call.

        ``images`` is pulled one at a time and never re-read, so a caller can
        hand over a generator and pay for a variant only when the search
        actually reaches it -- which is what the pipeline does, the ladder's
        preprocessing being about as expensive per rung as the early exit was
        saving on OCR.
        """
        retries_from = (
            len(self.config.sharpen_ladder) if self.config.retry_opposite_polarity else None
        )

        def numbered():
            for position, image in enumerate(images):
                result = self.read(index, image)
                result.variant = position
                result.fallback = retries_from is not None and position >= retries_from
                yield result

        return self.pick_best(numbered(), self.config.accept_confidence)

    @staticmethod
    def pick_best(results: Iterable["CellResult"], accept_confidence: float) -> "CellResult":
        """Rank already-OCR'd rungs and keep the most trustworthy, in order.

        This is the half of :meth:`read_best` that must never quietly drift,
        because it is the part that decides which reading a real strip
        publishes. It is factored out so the sharpening tuner can replay this
        exact ranking, unchanged, over rungs it has cached from a previous OCR
        pass -- comparing candidate ladders is then a matter of re-ordering
        cached results rather than re-running Tesseract for each one, and the
        tuner's notion of "correct" can never diverge from the pipeline's.
        """
        best: CellResult | None = None
        for result in results:
            if best is None or SoftkeyReader._rank(result) > SoftkeyReader._rank(best):
                best = result
            if (
                accept_confidence > 0
                and result.text
                and result.match_score >= 1.0
                and result.confidence >= accept_confidence
            ):
                break
        assert best is not None  # results is never empty
        return best

    @staticmethod
    def _rank(result: CellResult) -> tuple[int, float]:
        """Exact vocabulary hit first, then Tesseract's own confidence.

        An opposite-polarity retry that did *not* land on a known label ranks
        below everything else rather than competing on confidence, and that
        asymmetry is doing real work.

        Tesseract reads *something* out of a cell that is the wrong way up,
        and reports a confidence for it that means nothing. Without this, a
        cell that simply does not read hands its answer to whichever variant
        produced the most confident garbage -- and with six variants instead
        of three, that is now often a retry. The published label is then
        wrong, and the debug line blames the polarity for a cell whose
        polarity was never the problem.

        Landing on a known label is the only evidence there is that flipping
        was the right thing to do. Without it there is no evidence, so the
        polarity the ring chose keeps the cell. That is the same rule as
        ``LIGHT_BACKGROUND_RING`` one stage earlier: the exception has to
        prove itself, and an ambiguous answer means the common case.
        """
        exact = 1 if (result.text and result.match_score >= 1.0) else 0
        if result.fallback and not exact:
            return (-1, 0.0)
        return (exact, result.confidence)

    def close(self) -> None:
        self.engine.close()
