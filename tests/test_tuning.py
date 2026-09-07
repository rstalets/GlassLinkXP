"""The multi-cell sharpening tuner: truth files, and the no-regression search.

The search itself is tested against a scripted evaluator rather than real
Tesseract, so the exact regression this tool exists to catch -- a rung that
fixes one cell and quietly breaks another -- can be reproduced deterministically
instead of hoping a real capture happens to exhibit it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from glasslinkxp.config import OcrConfig
from glasslinkxp.gui import configio
from glasslinkxp.ocr import CellResult
from glasslinkxp.tuning import (
    Candidate,
    TuningCase,
    TuningError,
    format_report,
    load_truth,
    result_block,
    run_tuning,
)

# ---------------------------------------------------------------------------
# truth files
# ---------------------------------------------------------------------------


def test_load_truth_resolves_relative_dirs_against_the_truth_file(tmp_path):
    (tmp_path / "captures").mkdir()
    truth = tmp_path / "truth.toml"
    truth.write_text(
        '[[case]]\ndir = "captures"\ndisplay = "mfd"\nexpect = { 1 = "0", 6 = "6" }\n',
        encoding="utf-8",
    )
    [case] = load_truth(truth)
    assert case.dir == (tmp_path / "captures").resolve()
    assert case.display == "mfd"
    assert case.expect == {1: "0", 6: "6"}


def test_load_truth_defaults_display_to_pfd(tmp_path):
    truth = tmp_path / "truth.toml"
    truth.write_text('[[case]]\ndir = "cells"\nexpect = { 1 = "0" }\n', encoding="utf-8")
    [case] = load_truth(truth)
    assert case.display == "pfd"


def test_load_truth_rejects_a_case_with_no_expect(tmp_path):
    truth = tmp_path / "truth.toml"
    truth.write_text('[[case]]\ndir = "cells"\n', encoding="utf-8")
    with pytest.raises(TuningError, match="no 'expect' entries"):
        load_truth(truth)


def test_load_truth_rejects_a_missing_file(tmp_path):
    with pytest.raises(TuningError):
        load_truth(tmp_path / "nope.toml")


def test_load_truth_rejects_no_cases(tmp_path):
    truth = tmp_path / "truth.toml"
    truth.write_text("app = 1\n", encoding="utf-8")
    with pytest.raises(TuningError, match=r"\[\[case\]\]"):
        load_truth(truth)


def test_gui_written_truth_file_loads_back(tmp_path):
    """The GUI writes this file without importing tuning.py at all (it must
    not pull cv2/Tesseract into the window process) -- so what it produces is
    checked against the real loader here instead."""
    cases = [
        {"dir": str(tmp_path / "page1"), "display": "pfd", "expect": {1: "0", 6: "6"}},
        {"dir": str(tmp_path / "page2"), "display": "mfd", "expect": {3: "PROC"}},
    ]
    truth = tmp_path / "truth.toml"
    truth.write_text(configio.dumps_truth(cases), encoding="utf-8")

    loaded = load_truth(truth)
    assert [c.dir for c in loaded] == [tmp_path / "page1", tmp_path / "page2"]
    assert loaded[0].display == "pfd"
    assert loaded[0].expect == {1: "0", 6: "6"}
    assert loaded[1].expect == {3: "PROC"}


# ---------------------------------------------------------------------------
# the search: no regressions allowed
# ---------------------------------------------------------------------------

#: Two cells, told apart by the constant each is filled with -- the fake
#: evaluator below reads that back out of the image instead of running OCR.
CELL_A = np.full((10, 10), 1, np.uint8)   # expects "0"
CELL_B = np.full((10, 10), 2, np.uint8)   # expects "6"


def _cases() -> list[TuningCase]:
    return [TuningCase(label="page/pfd", dir="unused", display="pfd",
                       expect={1: "0", 2: "6"})]


def _scripted_evaluator_factory(script):
    """script: {(cell_id, amount, radius): (text, confidence)}

    Keyed without the polarity, and the "opposite" polarity always reads
    nothing: these tests are about which *ladder* the search picks, and a
    scripted cell that read equally well either way up would let a candidate
    score on a variant no real cell produces. The polarity fallback has its
    own test below.
    """

    def factory(_base):
        def evaluate(cell, psm, method, upscale, amount, radius, polarity="auto"):
            if polarity != "auto":
                return CellResult(index=0, text="", raw="", confidence=0.0)
            cell_id = int(cell.flat[0])
            text, confidence = script.get((cell_id, amount, radius), ("", 0.0))
            return CellResult(index=0, text=text, raw=text, confidence=confidence,
                              match_score=1.0 if text else 0.0)
        return evaluate, (lambda: None)

    return factory


@pytest.fixture(autouse=True)
def _fake_cell_images(monkeypatch):
    """Cells are identified by a fill value, not read from disk."""
    images = {1: CELL_A, 2: CELL_B}
    monkeypatch.setattr(
        "glasslinkxp.tuning.cell_image", lambda case, cell: images[cell]
    )


def test_search_avoids_a_rung_that_fixes_one_cell_by_breaking_another():
    """The exact bug report this tool exists for: tuning sharpening to read a
    0 correctly turned a 6 into a 5. A safe rung exists too, and the search
    must prefer it over the one that regresses."""
    script = {
        # baseline (0.0, 0.0): 0 misreads as blank, 6 already reads correctly
        # but only at 70% -- below accept_confidence, so later rungs are
        # still allowed to compete for it.
        (1, 0.0, 0.0): ("", 0.0),
        (2, 0.0, 0.0): ("6", 70.0),
        # the regression-causing rung: fixes 0, flips 6 into a confident 5.
        (1, 0.5, 1.0): ("0", 90.0),
        (2, 0.5, 1.0): ("5", 95.0),
        # a safe rung: fixes 0, and 6 is read correctly (and not overridden).
        (1, 0.3, 1.4): ("0", 85.0),
        (2, 0.3, 1.4): ("6", 82.0),
    }
    result = run_tuning(
        _cases(), OcrConfig(),
        evaluator_factory=_scripted_evaluator_factory(script),
        psms=(7,), methods=("otsu",), upscales=(4.0,),
        rungs=((0.5, 1.0), (0.3, 1.4)),
        max_extra_rungs=1,
    )

    assert result.baseline.outcomes[0].ok is False   # cell 1 (0) wrong at baseline
    assert result.baseline.outcomes[1].ok is True    # cell 2 (6) already right

    assert result.best.regressed == 0
    assert result.best.fixed == 1
    assert (0.3, 1.4) in result.best.candidate.ladder
    assert (0.5, 1.0) not in result.best.candidate.ladder
    outcomes = {o.cell_id.cell: o for o in result.best.outcomes}
    assert outcomes[1].text == "0"
    assert outcomes[2].text == "6"


def test_search_keeps_the_baseline_when_every_fix_regresses_something():
    """If every rung that fixes cell 1 also breaks cell 2, nothing must be
    reported as an improvement -- the regression-free baseline wins."""
    script = {
        (1, 0.0, 0.0): ("", 0.0),
        (2, 0.0, 0.0): ("6", 70.0),
        (1, 0.5, 1.0): ("0", 90.0),
        (2, 0.5, 1.0): ("5", 95.0),
    }
    result = run_tuning(
        _cases(), OcrConfig(),
        evaluator_factory=_scripted_evaluator_factory(script),
        psms=(7,), methods=("otsu",), upscales=(4.0,),
        rungs=((0.5, 1.0),),
        max_extra_rungs=1,
    )
    assert result.best.fixed == 0
    assert result.best.regressed == 0
    assert result.best.candidate.ladder == ((0.0, 0.0),)
    assert result.best.outcomes[1].text == "6"  # cell 2 still reads right


def test_search_reports_success_when_everything_already_reads_correctly():
    script = {
        (1, 0.0, 0.0): ("0", 90.0),
        (2, 0.0, 0.0): ("6", 90.0),
    }
    result = run_tuning(
        _cases(), OcrConfig(),
        evaluator_factory=_scripted_evaluator_factory(script),
        psms=(7,), methods=("otsu",), upscales=(4.0,),
        rungs=(), max_extra_rungs=0,
    )
    assert all(o.ok for o in result.baseline.outcomes)
    assert result.best.fixed == 0
    assert result.best.regressed == 0


def test_result_block_is_valid_toml_the_gui_can_read_back():
    import tomllib

    from glasslinkxp.gui.logparse import parse_tuning_result

    candidate = Candidate(psm=8, method="adaptive", upscale=6.0, ladder=((0.0, 0.0), (0.5, 1.0)))
    block = result_block(candidate)
    parsed = tomllib.loads(block)["tuning_result"]
    assert parsed == {
        "psm": 8, "threshold": "adaptive", "upscale": 6.0,
        "sharpen_ladder": [[0.0, 0.0], [0.5, 1.0]],
    }
    # and the GUI's own parser, fed the block the way `tune`'s stdout carries it
    lines = block.splitlines() + ["", "trailing prose the GUI must not swallow"]
    assert parse_tuning_result(lines) == parsed


def test_the_search_sees_both_polarities_and_the_pipeline_reads_them_in_that_order():
    """The tuner's variant order is the pipeline's, or its answers are not the
    daemon's.

    ``pick_best`` stops at the first confident exact hit, so the order the
    variants are offered in decides which reading wins. Two modules build that
    order -- ``pipeline.variant_ladder`` for the daemon and ``tuning._variants``
    for the search -- and this is what keeps them the same list.
    """
    from dataclasses import replace

    from glasslinkxp.pipeline import variant_ladder
    from glasslinkxp.tuning import _variants

    ocr = OcrConfig()
    assert _variants(ocr.sharpen_ladder, ocr.retry_opposite_polarity) == variant_ladder(ocr)

    off = replace(ocr, retry_opposite_polarity=False)
    assert _variants(off.sharpen_ladder, off.retry_opposite_polarity) == variant_ladder(off)


def test_a_cell_only_the_opposite_polarity_can_read_is_not_reported_as_hopeless():
    """The report a wrongly-polarised cell used to get, and the one it gets now.

    Nothing in the search space -- psm, threshold method, upscale, sharpening
    -- changes a cell's polarity, so a cell whose polarity was read the wrong
    way round reads as nothing under every candidate, and ``tune`` truthfully
    reported that no candidate improved on the baseline and handed back the
    baseline's own settings. That is what "tuning stopped working" looked
    like. With the polarity in the ladder the baseline reads it.
    """
    def factory(_base):
        def evaluate(cell, psm, method, upscale, amount, radius, polarity="auto"):
            if polarity == "opposite":
                return CellResult(index=0, text="0", raw="0", confidence=95.0, match_score=1.0)
            return CellResult(index=0, text="", raw="", confidence=0.0)
        return evaluate, (lambda: None)

    result = run_tuning(
        [TuningCase(label="page", dir=Path("."), display="pfd", expect={1: "0"})],
        OcrConfig(),
        evaluator_factory=factory,
        psms=(7,), methods=("otsu",), upscales=(4.0,), rungs=(), max_extra_rungs=0,
    )
    assert result.baseline.outcomes[0].ok, "the fallback is part of the baseline, not a candidate"
    report = format_report(result)
    assert "nothing to fix" in report


def test_adding_a_rung_beats_changing_psm_when_both_fix_the_same_cell():
    """The least invasive fix has to be able to win, not merely be reachable.

    ``run_tuning`` calls "just add a rung, change nothing else" the least
    invasive fix and searches the base's own settings so it is reachable. It
    could not win: the ranking put ladder length above keeping the base
    settings, so a one-rung candidate at a different psm outranked the base
    psm with a rung added, every time they fixed the same number of cells.

    Which is backwards. A rung is paid for only by a cell that already failed
    -- ``read_best`` stops at the first confident exact hit -- while a changed
    psm is paid for by every cell of every frame, including all the ones
    nobody put in the truth file.
    """
    script = {}

    def factory(_base):
        def evaluate(cell, psm, method, upscale, amount, radius, polarity="auto"):
            if polarity != "auto":
                return CellResult(index=0, text="", raw="", confidence=0.0)
            # The cell reads at the base psm with a rung, and also at psm 10
            # with no rung at all. Both fix exactly one cell, neither regresses.
            hit = (psm == 7 and (amount, radius) == (0.5, 1.0)) or (psm == 10 and amount == 0.0)
            if hit:
                return CellResult(index=0, text="0", raw="0", confidence=95.0, match_score=1.0)
            return CellResult(index=0, text="", raw="", confidence=0.0)
        return evaluate, (lambda: None)

    result = run_tuning(
        [TuningCase(label="page", dir=Path("."), display="pfd", expect={1: "0"})],
        OcrConfig(psm=7),
        evaluator_factory=factory,
        psms=(7, 10), methods=("otsu",), upscales=(4.0,),
        rungs=((0.5, 1.0),), max_extra_rungs=1,
    )

    assert result.best.fixed == 1
    assert result.best.candidate.psm == 7, "the base's own psm should have been kept"
    assert (0.5, 1.0) in result.best.candidate.ladder
    assert result.best_ladder_only is None, "the winner already changes nothing else"


def test_the_report_says_why_it_changed_a_global_setting_instead_of_adding_a_rung():
    """A user who expected a ladder and got `psm = 10` should not have to guess
    whether a ladder existed and lost, or never existed at all."""
    def factory(_base):
        def evaluate(cell, psm, method, upscale, amount, radius, polarity="auto"):
            # Only psm 10 reads it. No ladder at the base psm can help.
            if polarity == "auto" and psm == 10:
                return CellResult(index=0, text="0", raw="0", confidence=95.0, match_score=1.0)
            return CellResult(index=0, text="", raw="", confidence=0.0)
        return evaluate, (lambda: None)

    result = run_tuning(
        [TuningCase(label="page", dir=Path("."), display="pfd", expect={1: "0"})],
        OcrConfig(psm=7),
        evaluator_factory=factory,
        psms=(7, 10), methods=("otsu",), upscales=(4.0,),
        rungs=((0.5, 1.0),), max_extra_rungs=1,
    )

    assert result.best.candidate.psm == 10
    report = format_report(result)
    assert "applies to every cell of every frame" in report
    assert "there was no rung" in report


def test_a_cell_whose_crop_is_cutting_the_glyph_is_named_as_such():
    """"The crop is wrong" and "the glyph is too small" had one verdict between
    them, and it was the second one, unconditionally.

    Ink in the outermost pixels of a crop is the one thing this search cannot
    do anything about -- no psm, threshold, upscale or rung puts back a stroke
    the crop cut off. Measured from the raw image, which the search has
    already loaded, and reported against the cells that stayed wrong so the
    next thing somebody tries is the calibration rather than another rung.
    """
    import numpy as np

    from glasslinkxp import tuning

    # A cell with ink hard against both side edges, and one with ink nowhere
    # near them. Neither reads, under anything.
    cut = np.zeros((20, 40), dtype=np.uint8)
    cut[8:12, :] = 255
    clean = np.zeros((20, 40), dtype=np.uint8)
    clean[8:12, 18:22] = 255
    images = {1: cut, 2: clean}

    def factory(_base):
        def evaluate(*_args, **_kwargs):
            return CellResult(index=0, text="", raw="", confidence=0.0)
        return evaluate, (lambda: None)

    original = tuning.cell_image
    tuning.cell_image = lambda case, cell: images[cell]
    try:
        result = run_tuning(
            [TuningCase(label="page", dir=Path("."), display="pfd",
                        expect={1: "CHKLIST", 2: "0"})],
            OcrConfig(),
            evaluator_factory=factory,
            psms=(7,), methods=("otsu",), upscales=(4.0,), rungs=(), max_extra_rungs=0,
        )
    finally:
        tuning.cell_image = original

    assert result.clipped[(0, 1)] == ("left", "right")
    assert result.clipped[(0, 2)] == ()

    report = format_report(result)
    assert "INK TOUCHES left,right" in report
    assert "before tuning anything else" in report
    assert "*_prep.png" in report, "the cell that is not clipped gets the other advice"
