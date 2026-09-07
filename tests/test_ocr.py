import numpy as np
import pytest

from glasslinkxp import synth
from glasslinkxp.config import OcrConfig, StripGeometry
from glasslinkxp.ocr import (
    CellResult,
    LabelVocabulary,
    SoftkeyReader,
    normalise,
    resolve_tessdata,
)
from glasslinkxp.strip import preprocess_cell, split_cells

VOCAB = LabelVocabulary.from_file(OcrConfig().labels_file, cutoff=0.62)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("INSET", "INSET"),
        ("lNSET", "INSET"),      # I/l confusion
        ("DCLTP", "DCLTR"),      # R read as P
        ("0BS", "OBS"),          # zero for O
        ("TMRIREF", "TMR/REF"),  # slash read as I
        ("STDBARO", "STD BARO"), # lost space
        ("TRAFFlC", "TRAFFIC"),
    ],
)
def test_snapping_fixes_typical_ocr_errors(raw, expected):
    snapped, score = VOCAB.snap(raw)
    assert snapped == expected
    assert score > 0.0


def test_snapping_keeps_unknown_text_raw():
    snapped, score = VOCAB.snap("ZZQQXY")
    assert snapped == "ZZQQXY"
    assert score == 0.0


def test_snapping_empty_string():
    assert VOCAB.snap("   ") == ("", 0.0)


def test_normalise_applies_whitelist():
    assert normalise("  std\nbaro!  ", OcrConfig().whitelist) == "STD BARO"


def test_vocabulary_missing_file():
    with pytest.raises(FileNotFoundError):
        LabelVocabulary.from_file("/nonexistent/labels.txt")


def test_resolve_tessdata_prefers_the_configured_path(tmp_path):
    (tmp_path / "eng.traineddata").write_bytes(b"")
    assert resolve_tessdata(str(tmp_path)) == str(tmp_path)
    assert resolve_tessdata(str(tmp_path / "missing")) != str(tmp_path / "missing")


def test_reader_reads_a_real_cell(reader):
    frame = synth.render_menu("pfd_top")
    cell = split_cells(frame, StripGeometry())[0]
    result = reader.read(0, preprocess_cell(cell, upscale=3.0))
    assert result.text == "INSET"
    assert result.confidence > 0
    assert "INSET" in result.describe()


def test_cell_result_describe_shows_the_raw_string():
    result = CellResult(index=0, text="DCLTR", raw="DCLTP", confidence=80, match_score=0.8)
    assert "DCLTP" in result.describe()
    assert CellResult(index=1, blank=True).describe() == "(blank)"


def test_engine_handles_an_empty_image(reader):
    white = np.full((40, 120), 255, np.uint8)
    text, _ = reader.engine.recognize(white)
    assert normalise(text) == ""


# ---------------------------------------------------------------------------
# read_best: trying several preprocessings and keeping the most trustworthy
# ---------------------------------------------------------------------------


class _ScriptedEngine:
    """Returns a prepared (text, confidence) for each variant, in order."""

    name = "scripted"

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def recognize(self, image):
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        return answer

    def close(self):
        return None


def _reader(answers):
    return SoftkeyReader(OcrConfig(), engine=_ScriptedEngine(answers))


def test_read_best_stops_at_the_first_exact_vocabulary_hit():
    """The no-worse guarantee.

    Rung one is no sharpening, i.e. the previous behaviour. If it already lands
    on a known label we stop there, so a later, more aggressive rung can never
    overwrite an answer the gentle path got right.
    """
    reader = _reader([("INSET", 90.0), ("GARBAGE", 99.0), ("WORSE", 99.0)])
    result = reader.read_best(0, [object(), object(), object()])
    assert result.text == "INSET"
    assert reader.engine.calls == 1, "must not run the later variants"


def test_read_best_only_pulls_the_variants_it_reads():
    """The early exit has to save the preprocessing too, not just the OCR.

    The pipeline hands over a generator, so anything read_best does not reach
    is never built. If it ever materialises its argument -- list(images), a
    len(), a second pass -- the rung it stopped before gets preprocessed
    anyway and the saving quietly disappears.
    """
    built = []

    def variants(n):
        for i in range(n):
            built.append(i)
            yield object()

    reader = _reader([("INSET", 90.0), ("GARBAGE", 99.0), ("WORSE", 99.0)])
    assert reader.read_best(0, variants(3)).text == "INSET"
    assert built == [0], "the rungs after an exact, confident hit are never built"

    built.clear()
    reader = _reader([("", 0.0), ("", 0.0), ("", 0.0)])
    reader.read_best(0, variants(3))
    assert built == [0, 1, 2], "and every rung is still built when none of them answers"


def test_read_best_recovers_a_cell_the_gentle_rung_could_not_read():
    """A filled counter returns nothing; a sharpened variant recovers it."""
    reader = _reader([("", 0.0), ("0", 88.0)])
    result = reader.read_best(0, [object(), object()])
    assert result.text == "0"


def test_read_best_prefers_a_known_label_over_a_confident_unknown():
    """0 -> B is what over-sharpening looks like: confident, and not a label."""
    reader = _reader([("B", 96.0), ("0", 70.0)])
    result = reader.read_best(0, [object(), object()])
    assert result.text == "0"


def test_read_best_falls_back_to_confidence_when_nothing_matches():
    reader = _reader([("", 0.0), ("ZZQ", 42.0)])
    result = reader.read_best(0, [object(), object()])
    assert result.raw == "ZZQ"


def test_a_shaky_exact_match_does_not_end_the_ladder():
    """Reported live: a 0 read as '2' at 54% stopped the search.

    Every digit is a valid softkey, so a misread digit still matches the
    vocabulary exactly. Only a *confident* hit is allowed to stop early.
    """
    reader = _reader([("2", 54.0), ("0", 88.0)])
    result = reader.read_best(0, [object(), object()])
    assert result.text == "0"
    assert reader.engine.calls == 2


def test_a_confident_exact_match_still_ends_the_ladder():
    reader = _reader([("2", 96.0), ("0", 99.0)])
    result = reader.read_best(0, [object(), object()])
    assert result.text == "2"
    assert reader.engine.calls == 1, "no extra OCR when the first rung is solid"


def test_accept_confidence_of_zero_always_tries_every_rung():
    engine = _ScriptedEngine([("2", 96.0), ("0", 99.0)])
    reader = SoftkeyReader(OcrConfig(accept_confidence=0.0), engine=engine)
    result = reader.read_best(0, [object(), object()])
    assert result.text == "0"
    assert engine.calls == 2
