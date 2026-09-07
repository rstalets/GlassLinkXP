"""Sharpening-ladder tuning across every labelled cell on every page handed to it.

The single-cell version of this (search preprocessing settings against one
``*_raw.png`` until one of them reads the expected label) shipped first and
found a real bug the hard way: a sharpen setting that opened up a 0 also
turned a 6 into a 5 and made a 7 unreadable, because nothing about the search
ever looked at any cell but the one it was aimed at. A setting is not an
improvement because it fixes the cell it was tuned against; it is an
improvement because it fixes that cell *and leaves every other cell exactly as
it read before*, since :class:`~.ocr.SoftkeyReader` runs the same ladder
against all twelve cells of every frame for as long as the daemon is running.

So this searches against a set of :class:`TuningCase` -- one or more captured
pages (a ``dump-cells`` output folder, a display key, and what some of its
cells should read) -- under the ladder shape the daemon actually uses
(``[[0.0, 0.0], ...]``, always trying no sharpening first) and only accepts a
candidate that fixes something without regressing anything that already read
correctly. "Reads correctly" is decided by
:meth:`~.ocr.SoftkeyReader.pick_best`, the exact function ``read_best`` uses
in production, replayed over OCR results this module caches -- so the
tuner's idea of "correct" cannot drift from the pipeline's, and comparing a
few thousand candidate ladders costs re-ordering cached results rather than
re-running Tesseract for each one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

import numpy as np

from .config import OcrConfig
from .ocr import CellResult, OcrUnavailable, SoftkeyReader, TesserocrEngine
from .strip import measure_ink, preprocess_cell

LOG = logging.getLogger(__name__)


class TuningError(Exception):
    """A truth file could not be read, or named a picture that is not there."""


# ---------------------------------------------------------------------------
# truth files
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TuningCase:
    """One captured page: a ``dump-cells`` folder, its display, and the cells
    on it whose correct label is known.

    ``expect`` only needs the cells somebody actually checked -- a page can
    contribute one cell or all twelve. Cells left out are neither required to
    match nor allowed to break anything: nobody has said what they should be.
    """

    label: str
    dir: Path
    display: str
    expect: dict[int, str]


def load_truth(path: str | Path) -> list[TuningCase]:
    """Parse a ``tune --truth`` file: one or more ``[[case]]`` tables.

        [[case]]
        dir = "cells_page1"        # a dump-cells output folder
        display = "pfd"            # optional, defaults to "pfd"
        expect = { 1 = "0", 6 = "6", 7 = "7" }   # 1-indexed cell -> label

    A relative ``dir`` is resolved against the truth file's own folder, the
    same rule ``[ocr] screens_file`` and ``labels_file`` use, so a truth file
    can be checked in next to the captures it describes and moved as a unit.
    """
    import tomllib

    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise TuningError(f"could not read {p}: {exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TuningError(f"{p}: {exc}") from exc

    raw_cases = data.get("case", [])
    if not isinstance(raw_cases, list) or not raw_cases:
        raise TuningError(f"{p}: no [[case]] blocks found")

    cases: list[TuningCase] = []
    for position, raw in enumerate(raw_cases, start=1):
        if not isinstance(raw, Mapping) or "dir" not in raw:
            raise TuningError(f"{p}: case {position} has no 'dir'")
        directory = Path(str(raw["dir"]))
        if not directory.is_absolute():
            directory = (p.parent / directory).resolve()
        display = str(raw.get("display", "pfd"))
        expect_raw = raw.get("expect", {})
        if not isinstance(expect_raw, Mapping) or not expect_raw:
            raise TuningError(
                f"{p}: case {position} ({directory}) has no 'expect' entries -- "
                "say what at least one cell should read"
            )
        try:
            expect = {int(k): str(v).strip().upper() for k, v in expect_raw.items()}
        except ValueError as exc:
            raise TuningError(f"{p}: case {position}: expect keys must be cell numbers") from exc
        cases.append(TuningCase(
            label=f"{directory.name}/{display}", dir=directory, display=display, expect=expect,
        ))
    return cases


def cell_image(case: TuningCase, cell: int) -> np.ndarray:
    """The raw crop ``dump-cells`` wrote for one of ``case``'s labelled cells."""
    import cv2

    path = case.dir / f"{case.display}_{cell:02d}_raw.png"
    image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise TuningError(f"{case.label}: cannot read {path} (run dump-cells into that folder first)")
    return image


# ---------------------------------------------------------------------------
# search space
# ---------------------------------------------------------------------------

#: The same grid the single-cell tuner always searched. Not moved wider along
#: with the cell count in the same change, so a result that looks different
#: from before can only be explained by one thing at a time.
PSMS: tuple[int, ...] = (7, 8, 10, 13)
METHODS: tuple[str, ...] = ("otsu", "adaptive")
UPSCALES: tuple[float, ...] = (3.0, 4.0, 6.0, 8.0)
#: Extra sharpening rungs tried on top of the mandatory (0.0, 0.0) rung.
RUNGS: tuple[tuple[float, float], ...] = tuple(
    (amount, radius)
    for amount in (0.3, 0.5, 0.8, 1.2, 1.6)
    for radius in (0.8, 1.0, 1.4)
)
#: How many extra rungs a candidate ladder may add. Two, because that is the
#: shape of the shipped default (see OcrConfig.sharpen_ladder) -- a longer
#: ladder costs an extra OCR call, on every cell, on every frame, for as long
#: as the daemon runs with it.
MAX_EXTRA_RUNGS = 2


@dataclass(frozen=True)
class CellId:
    case: int
    cell: int
    expected: str


def _cell_ids(cases: Sequence[TuningCase]) -> list[CellId]:
    ids = []
    for index, case in enumerate(cases):
        for cell, expected in sorted(case.expect.items()):
            ids.append(CellId(index, cell, expected))
    return ids


@dataclass(frozen=True)
class Candidate:
    psm: int
    method: str
    upscale: float
    #: Always starts with (0.0, 0.0) -- this can never do worse than no
    #: sharpening at all, the same guarantee the shipped ladder relies on.
    ladder: tuple[tuple[float, float], ...]


def _candidate_ladders(
    rungs: Sequence[tuple[float, float]], max_extra: int = MAX_EXTRA_RUNGS
) -> Iterable[tuple[tuple[float, float], ...]]:
    base = ((0.0, 0.0),)
    yield base
    if max_extra < 1:
        return
    for first in rungs:
        yield base + (first,)
    if max_extra < 2:
        return
    for first in rungs:
        for second in rungs:
            if first == second:
                continue
            yield base + (first, second)


#: (raw cell image, psm, threshold method, upscale, sharpen amount, sharpen
#: radius, polarity) -> a CellResult for that one variant. The default
#: evaluates it for real, through the same preprocess_cell + SoftkeyReader.read
#: the pipeline uses; tests substitute a scripted one so the search logic can
#: be checked without Tesseract or a real capture.
Evaluator = Callable[[np.ndarray, int, str, float, float, float, str], CellResult]


def default_evaluator(base: OcrConfig) -> tuple[Evaluator, Callable[[], None]]:
    """Build the default (real OCR) evaluator, plus a function to close it.

    One :class:`~.ocr.SoftkeyReader` per ``psm`` actually asked for, built
    lazily and kept for the life of the search -- the Tesseract API object is
    expensive to construct and this is called once per (cell, settings) pair.
    """
    readers: dict[int, SoftkeyReader | None] = {}

    def reader_for(psm: int) -> SoftkeyReader | None:
        if psm not in readers:
            try:
                readers[psm] = SoftkeyReader(replace(base, psm=psm))
            except OcrUnavailable as exc:
                LOG.warning("psm %d unavailable: %s", psm, exc)
                readers[psm] = None
        return readers[psm]

    def evaluate(
        cell: np.ndarray, psm: int, method: str, upscale: float, amount: float, radius: float,
        polarity: str = "auto",
    ) -> CellResult:
        reader = reader_for(psm)
        if reader is None:
            return CellResult(index=0, text="", confidence=0.0)
        try:
            image = preprocess_cell(
                cell, upscale=upscale, method=method, sharpen_amount=amount,
                sharpen_radius=radius, polarity=polarity,
            )
            return reader.read(0, image)
        except Exception:  # noqa: BLE001 - a bad combination is just a miss
            return CellResult(index=0, text="", confidence=0.0)

    def close() -> None:
        for reader in readers.values():
            if reader is not None:
                reader.close()

    return evaluate, close


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CellOutcome:
    cell_id: CellId
    case_label: str
    text: str
    raw: str
    confidence: float

    @property
    def ok(self) -> bool:
        return self.text == self.cell_id.expected


@dataclass(frozen=True)
class CandidateScore:
    candidate: Candidate
    outcomes: tuple[CellOutcome, ...]
    fixed: int
    regressed: int

    @property
    def correct(self) -> int:
        return sum(1 for o in self.outcomes if o.ok)


@dataclass
class TuningResult:
    cases: list[TuningCase]
    base: OcrConfig
    baseline: CandidateScore
    best: CandidateScore
    candidates_tried: int
    #: The best candidate that changed *nothing but the ladder*, when that is
    #: not the winner. Reported so the answer to "why is it telling me to
    #: change psm instead of giving me a ladder?" is in the output rather than
    #: in somebody's head: either there was no ladder that did the job, or
    #: there was one and it fixed fewer cells, and those are different
    #: answers.
    best_ladder_only: CandidateScore | None = None
    #: (case, cell) -> which edges of that raw crop the ink touches.
    #:
    #: A property of the picture, not of any candidate, and the one thing this
    #: search cannot fix: no psm, threshold, upscale or rung removes a mark
    #: the crop should never have contained. Measured once and reported
    #: against the cells that stayed wrong, because the advice those lines
    #: used to carry -- "the glyph may be too small, use a larger pop-out" --
    #: is the wrong advice for a cell whose real problem is that its crop is
    #: catching the divider next to it. A lone digit is where that shows
    #: first: a word absorbs a stray mark at its edge into its own bounding
    #: box, while a digit sits in the middle of a wide cell and leaves the
    #: mark as a separate blob, which is how a "0" comes back as "/ 8".
    clipped: dict[tuple[int, int], tuple[str, ...]] = field(default_factory=dict)


def _variants(
    ladder: Sequence[tuple[float, float]], retry_opposite: bool
) -> tuple[tuple[str, float, float], ...]:
    """The (polarity, amount, radius) sequence, in the pipeline's order.

    Kept identical to ``pipeline.variant_ladder`` by a test that runs both over
    the same config, because the tuner's whole claim is that what it measures
    is what the daemon will do -- and the order is load-bearing, not
    cosmetic: ``pick_best`` stops early, so a different order can pick a
    different answer.
    """
    polarities = ("auto", "opposite") if retry_opposite else ("auto",)
    return tuple((p, a, r) for p in polarities for a, r in ladder)


def _evaluate_candidate(
    cache: Mapping[tuple, CellResult],
    cases: Sequence[TuningCase],
    cell_ids: Sequence[CellId],
    candidate: Candidate,
    accept_confidence: float,
    retry_opposite: bool,
) -> tuple[CellOutcome, ...]:
    outcomes = []
    variants = _variants(candidate.ladder, retry_opposite)
    for cid in cell_ids:
        def _results(cid=cid):
            for polarity, amount, radius in variants:
                yield cache[(cid.case, cid.cell, candidate.psm, candidate.method,
                             candidate.upscale, amount, radius, polarity)]

        result = SoftkeyReader.pick_best(_results(), accept_confidence)
        outcomes.append(CellOutcome(
            cell_id=cid, case_label=cases[cid.case].label,
            text=result.text, raw=result.raw, confidence=result.confidence,
        ))
    return tuple(outcomes)


def _score(
    candidate: Candidate, outcomes: tuple[CellOutcome, ...], baseline_ok: Mapping[CellId, bool],
) -> CandidateScore:
    fixed = sum(1 for o in outcomes if o.ok and not baseline_ok[o.cell_id])
    regressed = sum(1 for o in outcomes if not o.ok and baseline_ok[o.cell_id])
    return CandidateScore(candidate=candidate, outcomes=outcomes, fixed=fixed, regressed=regressed)


def _unchanged(candidate: Candidate, base: OcrConfig) -> int:
    """How many of the base's psm / threshold / upscale this candidate keeps."""
    return (
        int(candidate.psm == base.psm)
        + int(candidate.method == base.threshold)
        + int(candidate.upscale == base.upscale)
    )


def _rank_key(score: CandidateScore, base: OcrConfig) -> tuple:
    """Fixes first, then the least invasive way of getting them.

    ``unchanged`` outranks ladder length, and the order of those two is the
    whole of what this function decides. It used to be the other way round,
    on the reasoning that "a longer ladder costs an extra OCR call, on every
    cell, on every frame". That reasoning is wrong, and measurably so:
    ``read_best`` stops at the first variant that lands on a known label
    confidently, so a rung is only ever paid for by a cell that already
    failed. Across the offline corpus, adding two rungs to the mandatory one
    costs *no* extra OCR calls at all -- every cell that reads stops at the
    first.

    Changing ``psm``, ``threshold`` or ``upscale`` is not like that. It
    changes what Tesseract is asked, for every cell of every frame, including
    every cell nobody put in a truth file and whose reading therefore moved
    without being measured. Twenty-nine labelled cells is a good truth set and
    it is still not the whole G1000.

    So preferring a shorter ladder over the base's own settings had the tuner
    recommending a global change where "add a rung, change nothing else" would
    have done -- which ``run_tuning`` already calls "the least invasive fix",
    and which could never win the ranking that was supposed to deliver it.
    """
    confidence = sum(o.confidence for o in score.outcomes if o.ok)
    return (score.fixed, _unchanged(score.candidate, base), -len(score.candidate.ladder),
            confidence)


def run_tuning(
    cases: Sequence[TuningCase],
    base: OcrConfig,
    *,
    evaluator_factory: Callable[[OcrConfig], tuple[Evaluator, Callable[[], None]]] = default_evaluator,
    psms: Sequence[int] = PSMS,
    methods: Sequence[str] = METHODS,
    upscales: Sequence[float] = UPSCALES,
    rungs: Sequence[tuple[float, float]] = RUNGS,
    max_extra_rungs: int = MAX_EXTRA_RUNGS,
    progress: Callable[[str], None] | None = None,
) -> TuningResult:
    """Search sharpening-ladder settings across every labelled cell in ``cases``.

    Never returns a candidate that reads a cell wrongly which the baseline
    (``base``'s own psm/threshold/upscale, ladder truncated to no sharpening
    at all) already read correctly -- that guarantee is the entire point, so
    it is enforced here rather than left to whoever reads the report.
    """
    cell_ids = _cell_ids(cases)
    if not cell_ids:
        raise TuningError("no cells with expected labels across the given cases")

    # The baseline's own settings are always searched too, even if they are
    # not among the defaults above -- "just add a rung, change nothing else"
    # is the least invasive fix and has to be reachable when it works.
    search_psms = tuple(dict.fromkeys((*psms, base.psm)))
    search_methods = tuple(dict.fromkeys((*methods, base.threshold)))
    search_upscales = tuple(dict.fromkeys((*upscales, base.upscale)))
    rung_settings = ((0.0, 0.0), *rungs)
    # Both polarities are always cached, even when the config has the retry
    # switched off: the cost is one bitwise_not and a crop per entry, and a
    # search that could not see the other polarity would report "nothing
    # helps" for exactly the cells this is here to explain.
    search_polarities = ("auto", "opposite")

    images = {(cid.case, cid.cell): cell_image(cases[cid.case], cid.cell) for cid in cell_ids}
    clipped = {
        key: measure_ink(image, base.blank_contrast).clipped_edges()
        for key, image in images.items()
    }

    cache: dict[tuple, CellResult] = {}
    total_settings = len(search_psms) * len(search_methods) * len(search_upscales)
    done_settings = 0
    for psm in search_psms:
        evaluate, close = evaluator_factory(replace(base, psm=psm))
        try:
            for method in search_methods:
                for upscale in search_upscales:
                    for amount, radius in rung_settings:
                        for polarity in search_polarities:
                            for cid in cell_ids:
                                result = evaluate(images[(cid.case, cid.cell)], psm, method,
                                                  upscale, amount, radius, polarity)
                                # Set here rather than copied per candidate: the
                                # cache key already includes the polarity, so
                                # each entry belongs to exactly one arm of the
                                # ladder and pick_best can rank it as the
                                # pipeline would.
                                result.fallback = polarity != "auto"
                                cache[(cid.case, cid.cell, psm, method, upscale,
                                       amount, radius, polarity)] = result
                    done_settings += 1
                    if progress:
                        progress(f"evaluated {done_settings}/{total_settings} base settings "
                                 f"(psm={psm} threshold={method} upscale={upscale})")
        finally:
            close()

    baseline_candidate = Candidate(base.psm, base.threshold, base.upscale, ((0.0, 0.0),))
    baseline_outcomes = _evaluate_candidate(
        cache, cases, cell_ids, baseline_candidate, base.accept_confidence,
        base.retry_opposite_polarity,
    )
    baseline_ok = {o.cell_id: o.ok for o in baseline_outcomes}
    baseline_score = CandidateScore(candidate=baseline_candidate, outcomes=baseline_outcomes,
                                    fixed=0, regressed=0)

    best = baseline_score
    # Tracked alongside, restricted to candidates that change nothing but the
    # ladder. When the winner *is* one of these it is the same object and the
    # report says nothing; when it is not, the difference is the answer to
    # "why am I being told to change psm instead of given a ladder?".
    best_ladder_only = baseline_score
    tried = 0
    for psm in search_psms:
        for method in search_methods:
            for upscale in search_upscales:
                for ladder in _candidate_ladders(rungs, max_extra_rungs):
                    candidate = Candidate(psm, method, upscale, ladder)
                    outcomes = _evaluate_candidate(cache, cases, cell_ids, candidate,
                                                   base.accept_confidence,
                                                   base.retry_opposite_polarity)
                    tried += 1
                    score = _score(candidate, outcomes, baseline_ok)
                    if score.regressed:
                        continue
                    if _rank_key(score, base) > _rank_key(best, base):
                        best = score
                    if (_unchanged(candidate, base) == 3
                            and _rank_key(score, base) > _rank_key(best_ladder_only, base)):
                        best_ladder_only = score
    if progress:
        progress(f"compared {tried} candidate ladders")

    return TuningResult(
        cases=list(cases), base=base, baseline=baseline_score, best=best,
        candidates_tried=tried,
        best_ladder_only=None if best_ladder_only is best else best_ladder_only,
        clipped=clipped,
    )


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def _ladder_toml(ladder: Sequence[tuple[float, float]]) -> str:
    return "[" + ", ".join(f"[{amount}, {radius}]" for amount, radius in ladder) + "]"


def result_block(candidate: Candidate) -> str:
    """The ``[tuning_result]`` block ``tune`` prints, and the GUI parses back.

    Deliberately just valid TOML -- ``tomllib`` parses it directly rather
    than a hand-rolled pattern, since these are exactly the values the
    Settings form already edits, and a second notation for them would be one
    more way to disagree with the file they end up in.
    """
    return "\n".join([
        "[tuning_result]",
        f"psm = {candidate.psm}",
        f'threshold = "{candidate.method}"',
        f"upscale = {candidate.upscale}",
        f"sharpen_ladder = {_ladder_toml(candidate.ladder)}",
    ])


def format_report(result: TuningResult) -> str:
    total = len(result.baseline.outcomes)
    base_wrong = [o for o in result.baseline.outcomes if not o.ok]
    lines = [
        "",
        f"  {len(result.cases)} case(s), {total} labelled cell(s)",
        "",
        f"  baseline (psm={result.base.psm} threshold={result.base.threshold!r} "
        f"upscale={result.base.upscale}, sharpen_ladder=[[0.0, 0.0]]): "
        f"{total - len(base_wrong)}/{total} correct",
    ]
    for outcome in base_wrong:
        lines.append(
            f"    WRONG  {outcome.case_label} cell {outcome.cell_id.cell:2d}: "
            f"expected {outcome.cell_id.expected!r}, got {outcome.text!r} "
            f"(raw {outcome.raw!r}, conf {outcome.confidence:.0f})"
        )

    if not base_wrong:
        lines.append("\n  Everything already reads correctly at baseline -- nothing to fix.")
    else:
        best = result.best
        lines.append(
            f"\n  best candidate: psm={best.candidate.psm} threshold={best.candidate.method!r} "
            f"upscale={best.candidate.upscale} sharpen_ladder={_ladder_toml(best.candidate.ladder)}"
        )
        lines.append(
            f"    fixed {best.fixed} of {len(base_wrong)} previously-wrong cell(s), "
            f"{best.regressed} regression(s) among {total - len(base_wrong)} "
            "previously-correct cell(s)"
        )
        still_wrong = [o for o in best.outcomes if not o.ok]
        for outcome in still_wrong:
            edges = result.clipped.get((outcome.cell_id.case, outcome.cell_id.cell), ())
            touching = f"  INK TOUCHES {','.join(edges)}" if edges else ""
            lines.append(
                f"    STILL WRONG  {outcome.case_label} cell {outcome.cell_id.cell:2d}: "
                f"expected {outcome.cell_id.expected!r}, got {outcome.text!r} "
                f"(raw {outcome.raw!r}, conf {outcome.confidence:.0f}){touching}"
            )
        if still_wrong:
            clipped_wrong = [
                o for o in still_wrong
                if result.clipped.get((o.cell_id.case, o.cell_id.cell))
            ]
            if clipped_wrong:
                lines.append(
                    "\n  The cells marked INK TOUCHES have ink in the outermost pixels of "
                    "their crop, and nothing in this search can change that: no psm, "
                    "threshold, upscale or rung removes a mark the crop should not have "
                    "contained, or puts back a stroke it cut off. Look at their *_raw.png "
                    "before tuning anything else."
                )
            if len(clipped_wrong) < len(still_wrong):
                lines.append(
                    "\n  The rest did not come right under any setting tried. Look at their "
                    "*_prep.png -- that is what Tesseract was actually given, and it is the "
                    "only thing that says whether the glyph arrived intact. A counter that "
                    "closes up or a stroke that breaks during thresholding is a different "
                    "problem from a glyph too small to carry its shape, and they do not have "
                    "the same fix. Page lookup (screens.toml) exists for the second: the "
                    "transponder keypad is a fixed layout, so a digit that cannot be "
                    "recognised can be looked up instead."
                )
        if best.fixed == 0:
            lines.append(
                "\n  No candidate improved on the baseline without regressing something else "
                "that already read correctly -- the settings below are the baseline's own."
            )

        alternative = result.best_ladder_only
        if alternative is not None and best.fixed:
            changed = [
                name for name, mine, theirs in (
                    ("psm", best.candidate.psm, result.base.psm),
                    ("threshold", best.candidate.method, result.base.threshold),
                    ("upscale", best.candidate.upscale, result.base.upscale),
                )
                if mine != theirs
            ]
            lines.append(
                f"\n  Note that this changes {' and '.join(changed)}, which applies to every "
                "cell of every frame -- including the ones not in your truth file, whose "
                "readings have moved without being measured."
            )
            if alternative.fixed:
                lines.append(
                    f"    Changing nothing but the ladder, the best is "
                    f"{_ladder_toml(alternative.candidate.ladder)}, which fixes "
                    f"{alternative.fixed} of {len(base_wrong)}. Prefer it if that is enough: "
                    "a rung is only ever paid for by a cell that already failed."
                )
            else:
                lines.append(
                    "    No ladder on its own fixed anything here, so this is not a case of "
                    "the search preferring a global change to a rung -- there was no rung."
                )

    lines += ["", "  Settings found (also what the GUI's Save button reads):", ""]
    lines.append(result_block(result.best.candidate))
    lines.append("")
    return "\n".join(lines)
