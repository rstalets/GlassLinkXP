"""The multi-cell sharpening tuner: truth files, and the no-regression search.

The search itself is tested against a scripted evaluator rather than real
Tesseract, so the exact regression this tool exists to catch -- a rung that
fixes one cell and quietly breaks another -- can be reproduced deterministically
instead of hoping a real capture happens to exhibit it.
"""

from __future__ import annotations

import numpy as np
import pytest

from glasslinkxp.config import OcrConfig
from glasslinkxp.gui import configio
from glasslinkxp.ocr import CellResult
from glasslinkxp.tuning import (
    Candidate,
    TuningCase,
    TuningError,
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
    """script: {(cell_id, amount, radius): (text, confidence)}"""

    def factory(_base):
        def evaluate(cell, psm, method, upscale, amount, radius):
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
