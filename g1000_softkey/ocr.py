"""OCR: a persistent Tesseract instance plus vocabulary snapping.

Two things matter for speed and accuracy here:

* the Tesseract API object is created **once** and reused (pytesseract shells
  out and reloads the model per call, which is far too slow for video rates);
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
from typing import Protocol, Sequence

import numpy as np

from .config import OcrConfig
from .signatures import SignatureStore, signature

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
    by_signature: bool = False  # resolved by shape match, not by Tesseract
    by_screen: str = ""      # value came from this known softkey page
    confirmed_by: str = ""   # page agreed with a reading OCR was unsure of

    def describe(self) -> str:
        if self.blank:
            return "(blank)"
        extra = "" if self.raw == self.text else f" <- {self.raw!r}"
        cached = "" if self.ocr_ran else " [cached]"
        return f"{self.text!r}{extra} conf={self.confidence:.0f} match={self.match_score:.2f}{cached}"


# ---------------------------------------------------------------------------
# Engines
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
                "tesserocr is not installed (pip install tesserocr, or set ocr.engine "
                "= 'pytesseract' in the config to use the slower subprocess binding)"
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


class PytesseractEngine:
    """Fallback binding; correct but noticeably slower (process per call)."""

    name = "pytesseract"

    def __init__(self, config: OcrConfig) -> None:
        try:
            import pytesseract
        except ImportError as exc:
            raise OcrUnavailable("pytesseract is not installed (pip install pytesseract)") from exc
        from PIL import Image

        self._pytesseract = pytesseract
        self._Image = Image
        tessdata = resolve_tessdata(config.tessdata_path)
        flags = [f"--psm {config.psm}"]
        if config.whitelist:
            flags.append(f'-c tessedit_char_whitelist="{config.whitelist}"')
        if tessdata:
            flags.append(f'--tessdata-dir "{tessdata}"')
        self._config = " ".join(flags)
        self._lang = config.lang
        try:
            pytesseract.get_tesseract_version()
        except Exception as exc:  # noqa: BLE001 - EnvironmentError subclass varies
            raise OcrUnavailable(
                "the tesseract executable was not found on PATH. Install Tesseract "
                "(Windows: the UB-Mannheim installer) and make sure tesseract.exe is on PATH."
            ) from exc

    def recognize(self, image: np.ndarray) -> tuple[str, float]:
        data = self._pytesseract.image_to_data(
            self._Image.fromarray(image),
            lang=self._lang,
            config=self._config,
            output_type=self._pytesseract.Output.DICT,
        )
        words = [w for w in data["text"] if w.strip()]
        confs = [float(c) for c, w in zip(data["conf"], data["text"]) if w.strip() and float(c) >= 0]
        return " ".join(words), (sum(confs) / len(confs) if confs else 0.0)

    def close(self) -> None:
        return None


def create_engine(config: OcrConfig) -> OcrEngine:
    """Build the OCR engine named by ``config.engine`` ('auto' prefers tesserocr)."""
    if config.engine == "tesserocr":
        return TesserocrEngine(config)
    if config.engine == "pytesseract":
        return PytesseractEngine(config)
    if config.engine != "auto":
        raise OcrUnavailable(
            f"unknown ocr.engine {config.engine!r} (use 'auto', 'tesserocr' or 'pytesseract')"
        )
    try:
        return TesserocrEngine(config)
    except OcrUnavailable as exc:
        LOG.warning("tesserocr unavailable (%s); falling back to pytesseract", exc)
        return PytesseractEngine(config)


# ---------------------------------------------------------------------------
# Vocabulary snapping
# ---------------------------------------------------------------------------


def _key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


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
        labels = []
        for line in file.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                labels.append(line.upper())
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
        self.signatures = (
            SignatureStore.load(config.signatures_file)
            if config.signature_confidence > 0
            else SignatureStore()
        )

    def read(self, index: int, image: np.ndarray) -> CellResult:
        text, confidence = self.engine.recognize(image)
        raw = normalise(text, self.config.whitelist)
        snapped, score = self.vocabulary.snap(raw)
        return CellResult(
            index=index, text=snapped, raw=raw, confidence=confidence, match_score=score
        )

    def read_best(self, index: int, images: Sequence[np.ndarray]) -> CellResult:
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
        """
        best: CellResult | None = None
        for image in images:
            result = self.read(index, image)
            if best is None or self._rank(result) > self._rank(best):
                best = result
            if (
                self.config.accept_confidence > 0
                and result.text
                and result.match_score >= 1.0
                and result.confidence >= self.config.accept_confidence
            ):
                break
        assert best is not None  # images is never empty

        if (
            self.config.signature_confidence > 0
            and len(self.signatures)
            and best.confidence < self.config.signature_confidence
        ):
            # OCR is unsure. Ask what the shape looks like instead.
            shape = signature(images[0])
            label, dist = self.signatures.match(shape)
            if label is not None and label != best.text:
                LOG.debug(
                    "cell %d: signature says %r (d=%.3f), overriding OCR %r at %.0f%%",
                    index, label, dist, best.text, best.confidence,
                )
                return CellResult(
                    index=index, text=label, raw=best.raw,
                    confidence=best.confidence, match_score=1.0, by_signature=True,
                )
        return best

    @staticmethod
    def _rank(result: CellResult) -> tuple[int, float]:
        exact = 1 if (result.text and result.match_score >= 1.0) else 0
        return (exact, result.confidence)

    def close(self) -> None:
        self.engine.close()
