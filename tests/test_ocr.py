import numpy as np
import pytest

from g1000_softkey import synth
from g1000_softkey.config import OcrConfig, StripGeometry
from g1000_softkey.ocr import (
    CellResult,
    LabelVocabulary,
    OcrUnavailable,
    create_engine,
    normalise,
    resolve_tessdata,
)
from g1000_softkey.strip import preprocess_cell, split_cells

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


def test_unknown_engine_is_a_clean_error():
    with pytest.raises(OcrUnavailable):
        create_engine(OcrConfig(engine="nonsense"))


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
