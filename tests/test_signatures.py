"""Shape-signature fallback for cells Tesseract is unsure about."""

from pathlib import Path

import cv2
import numpy as np
import pytest

from g1000_softkey.signatures import GRID, SignatureStore, distance, signature

FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


def _glyph(text, size=40, fg=0, bg=255, width=80, height=60):
    """Black-on-white, i.e. already preprocessed."""
    from PIL import Image, ImageDraw, ImageFont

    for candidate in FONTS:
        if Path(candidate).is_file():
            font = ImageFont.truetype(candidate, size)
            break
    else:  # pragma: no cover - depends on the host's fonts
        pytest.skip("no TrueType font available")

    image = Image.new("L", (width, height), bg)
    draw = ImageDraw.Draw(image)
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((width - box[2]) // 2, (height - box[3]) // 2), text, fill=fg, font=font)
    return np.array(image)


def test_signature_has_a_fixed_shape():
    assert signature(_glyph("0")).shape == (GRID, GRID)


def test_signature_survives_a_size_change():
    """A window resize must not invalidate a learned signature."""
    small, large = signature(_glyph("0", size=24)), signature(_glyph("0", size=56))
    assert distance(small, large) < 0.14


@pytest.mark.parametrize("fg,bg", [(40, 210), (60, 190), (80, 170)])
def test_signature_is_effectively_immune_to_brightness(fg, bg):
    """The original objection to hashing: brightness moved the answer.

    Signatures come from the binarised glyph, so contrast barely registers --
    measured at 1 of 256 cells across this range, versus the 0.14 a match is
    allowed. Not bit-identical, because anti-aliased edges cross the threshold
    at slightly different places, which is exactly why matching is by distance
    rather than equality.
    """
    reference = signature(_glyph("6", fg=0, bg=255))
    assert distance(reference, signature(_glyph("6", fg=fg, bg=bg))) <= 0.01


def test_different_digits_are_further_apart_than_the_same_digit():
    zero_a, zero_b = signature(_glyph("0", size=36)), signature(_glyph("0", size=44))
    eight = signature(_glyph("8", size=40))
    assert distance(zero_a, zero_b) < distance(zero_a, eight)


def test_store_round_trips_through_json(tmp_path):
    store = SignatureStore()
    store.add("0", signature(_glyph("0")))
    store.add("IDENT", signature(_glyph("IDENT", size=20)))
    path = tmp_path / "signatures.json"
    store.save(path)

    reloaded = SignatureStore.load(path)
    assert set(reloaded.entries) == {"0", "IDENT"}
    assert np.array_equal(reloaded.entries["0"][0], store.entries["0"][0])


def test_add_drops_near_duplicates():
    store = SignatureStore()
    assert store.add("0", signature(_glyph("0", size=40)))
    assert not store.add("0", signature(_glyph("0", size=40)))
    assert len(store.entries["0"]) == 1


def test_match_finds_the_right_label():
    store = SignatureStore()
    for digit in "0123456789":
        store.add(digit, signature(_glyph(digit)))
    label, dist = store.match(signature(_glyph("6", size=38)))
    assert label == "6"
    assert dist < 0.14


def test_match_declines_an_unknown_shape():
    store = SignatureStore()
    store.add("0", signature(_glyph("0")))
    store.add("1", signature(_glyph("1")))
    label, _ = store.match(signature(_glyph("W")))
    assert label is None, "an unlearned shape must not be forced onto a known label"


def test_match_declines_when_two_labels_are_too_close():
    """A confident wrong answer is worse than no answer."""
    store = SignatureStore()
    shape = signature(_glyph("0"))
    store.add("0", shape)
    store.add("ALSO_ZERO", shape.copy())
    label, _ = store.match(shape)
    assert label is None


def test_empty_store_matches_nothing():
    assert SignatureStore().match(signature(_glyph("0")))[0] is None


def test_missing_file_loads_as_empty(tmp_path):
    assert len(SignatureStore.load(tmp_path / "nope.json")) == 0


def test_corrupt_file_loads_as_empty_rather_than_raising(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert len(SignatureStore.load(path)) == 0
