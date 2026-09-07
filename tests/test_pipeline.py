"""End-to-end offline pipeline tests: synthetic frame in, labels out."""

import pytest

from glasslinkxp import synth
from glasslinkxp.color import BLACK, RED, WHITE, YELLOW
from glasslinkxp.config import AppConfig, ColorConfig, DisplayConfig, StripGeometry
from glasslinkxp.ocr import CellResult
from glasslinkxp.pipeline import DisplayPipeline
from glasslinkxp.strip import auto_detect_strip, split_cells

MENUS = sorted(synth.MENUS)


def make_pipeline(reader, config, geometry=None, gating=True, color=None):
    display = DisplayConfig(key="pfd", geometry=geometry or StripGeometry())
    app = AppConfig(
        displays=(display,), ocr=config.ocr, change_gating=gating,
        color=color if color is not None else ColorConfig(),
    )
    return DisplayPipeline(display, reader, app)


@pytest.mark.parametrize("menu", MENUS)
def test_every_menu_is_read_exactly(menu, reader, config):
    pipeline = make_pipeline(reader, config)
    result = pipeline.process(synth.render_menu(menu))
    assert result.labels == synth.MENUS[menu]
    assert result.ocr_calls == sum(1 for label in synth.MENUS[menu] if label)


@pytest.mark.parametrize("menu", MENUS)
def test_only_the_rungs_that_are_read_are_preprocessed(menu, reader, config, monkeypatch):
    """Same labels, less work: a rung nobody OCRs is never built.

    Measured across the corpus before this was made lazy: 174 preprocessing
    passes -- three rungs for all 58 cells that reach OCR -- where the reader
    only ever looked at 63 of them.
    """
    from glasslinkxp import pipeline as pipeline_module

    real = pipeline_module.preprocess_cell
    calls = []

    def counted(cell, **kwargs):
        calls.append(kwargs["sharpen_amount"])
        return real(cell, **kwargs)

    monkeypatch.setattr(pipeline_module, "preprocess_cell", counted)

    result = make_pipeline(reader, config).process(synth.render_menu(menu))
    assert result.labels == synth.MENUS[menu], "laziness must not change the answer"

    rungs = len(config.ocr.sharpen_ladder)
    assert rungs > 1, "otherwise this test proves nothing"
    assert len(calls) >= result.ocr_calls, "every cell read gets at least rung one"
    assert len(calls) < result.ocr_calls * rungs, "and most stop there"
    assert calls.count(config.ocr.sharpen_ladder[0][0]) == result.ocr_calls


def test_the_ladder_charges_its_time_to_preprocessing_not_to_ocr(reader, config):
    """preprocess_ms now accrues inside read_best, and must still be its own
    figure: -v prints the two stages separately and they are read against each
    other when the loop is running late."""
    result = make_pipeline(reader, config).process(synth.render_menu("xpdr"))
    assert result.timings["preprocess_ms"] > 0
    assert result.timings["ocr_ms"] > 0


def test_the_ladder_stops_building_when_the_reader_stops_reading(reader, config):
    """The ladder object itself, without a pipeline around it."""
    from glasslinkxp.pipeline import SharpenLadder

    cell = split_cells(synth.render_menu("xpdr"), StripGeometry())[0]
    ladder = SharpenLadder(cell, config.ocr)
    assert ladder.rungs == 0 and ladder.elapsed_ms == 0.0, "nothing built until asked"
    first = next(iter(ladder))
    assert first is not None
    assert ladder.rungs == 1
    assert ladder.elapsed_ms > 0
    assert len(list(SharpenLadder(cell, config.ocr))) == len(config.ocr.sharpen_ladder)


def test_blank_cells_are_reported_empty_and_skip_ocr(reader, config):
    pipeline = make_pipeline(reader, config)
    result = pipeline.process(synth.render_menu("xpdr"))
    blanks = [cell for cell in result.cells if cell.blank]
    assert len(blanks) == 4
    assert all(cell.text == "" for cell in blanks)


def test_change_gating_skips_unchanged_cells(reader, config):
    pipeline = make_pipeline(reader, config, gating=True)
    first = pipeline.process(synth.render_menu("pfd_top"))
    assert first.ocr_calls == 10

    second = pipeline.process(synth.render_menu("pfd_top"))
    assert second.ocr_calls == 0
    assert second.labels == first.labels
    assert all(not cell.ocr_ran for cell in second.cells)

    labels = list(synth.MENUS["pfd_top"])
    labels[4] = "BACK"
    changed = synth.render_frame(labels, selected=synth.SELECTED["pfd_top"])
    third = pipeline.process(changed)
    assert third.ocr_calls == 1
    assert third.labels[4] == "BACK"
    assert third.labels[3] == first.labels[3]


def test_gating_can_be_switched_off(reader, config):
    pipeline = make_pipeline(reader, config, gating=False)
    frame = synth.render_menu("inset")
    pipeline.process(frame)
    again = pipeline.process(frame)
    assert again.ocr_calls == sum(1 for label in synth.MENUS["inset"] if label)
    assert again.labels == synth.MENUS["inset"]


def test_timings_are_reported(reader, config):
    pipeline = make_pipeline(reader, config)
    result = pipeline.process(synth.render_menu("mfd_top"))
    assert set(result.timings) == {
        "split_ms", "gate_ms", "color_ms", "preprocess_ms", "ocr_ms", "screen_ms",
    }
    assert result.timings["ocr_ms"] > 0


def test_snapping_is_actually_used(reader, config):
    """At least one label per test corpus needs the vocabulary snap; assert
    the raw string is kept alongside the snapped one."""
    pipeline = make_pipeline(reader, config)
    result = pipeline.process(synth.render_menu("pfd_top"))
    tmr = result.cells[8]
    assert tmr.text == "TMR/REF"
    assert tmr.raw != "" and tmr.match_score > 0


def test_auto_detected_geometry_is_usable(reader, config):
    """The coarse auto-detect is only a seed for calibration, but it should
    still get the large majority of cells right."""
    correct = total = 0
    for menu in MENUS:
        frame = synth.render_menu(menu)
        geometry = auto_detect_strip(frame)
        assert geometry is not None
        pipeline = make_pipeline(reader, config, geometry=geometry)
        for got, want in zip(pipeline.process(frame).labels, synth.MENUS[menu]):
            total += 1
            correct += got == want
    assert correct / total >= 0.95, f"only {correct}/{total} cells correct"


def test_frames_from_disk_round_trip(tmp_path, reader, config):
    from glasslinkxp.capture import ImageCapture

    paths = synth.write_menus(tmp_path)
    source = ImageCapture(tmp_path)
    pipeline = make_pipeline(reader, config)
    assert len(paths) == len(synth.MENUS)
    frame = source.grab()
    assert frame is not None
    labels = pipeline.process(frame).labels
    assert any(labels == menu for menu in synth.MENUS.values())


def _pipeline(screens=None):
    """A DisplayPipeline with only the fields _apply_screen touches."""
    from glasslinkxp.config import AppConfig, DisplayConfig, OcrConfig
    from glasslinkxp.pipeline import DisplayPipeline
    from glasslinkxp.screens import ScreenLibrary

    class _Reader:
        def __init__(self):
            self.config = OcrConfig()

    return DisplayPipeline(
        DisplayConfig(key="pfd"), _Reader(), AppConfig(),
        screens=screens if screens is not None else ScreenLibrary(),
    )

# ---------------------------------------------------------------------------
# Debug output must agree with what actually gets published
# ---------------------------------------------------------------------------


def test_apply_screen_reports_a_match_and_what_it_replaced():
    from glasslinkxp.screens import Screen, ScreenLibrary

    screen = Screen(
        name="xpdr-code", display="pfd",
        match={9: "IDENT", 10: "BKSP"}, labels={1: "0"},
    )
    results = [
        CellResult(index=0, text="2", raw="2", confidence=54.0, match_score=1.0),
        CellResult(index=8, text="IDENT", raw="IDENT", confidence=95.0, match_score=1.0),
        CellResult(index=9, text="BKSP", raw="BKSP", confidence=95.0, match_score=1.0),
    ]
    pipeline = _pipeline(screens=ScreenLibrary([screen]))
    outcome = pipeline._apply_screen(results)

    assert "xpdr-code" in outcome and "replaced [1]" in outcome
    assert results[0].text == "0"
    assert results[0].by_screen == "xpdr-code"
    assert results[0].raw == "2", "the OCR answer stays visible for debugging"


def test_apply_screen_says_why_no_page_matched():
    from glasslinkxp.screens import Screen, ScreenLibrary

    screen = Screen(name="x", display="pfd", match={9: "IDENT"}, labels={1: "0"})
    results = [CellResult(index=0, text="2", raw="2", confidence=54.0, match_score=1.0)]
    outcome = _pipeline(screens=ScreenLibrary([screen]))._apply_screen(results)

    assert "no page matched" in outcome
    assert "below" in outcome, "should name the cells that wanted help"
    assert results[0].text == "2", "nothing changed"


def test_apply_screen_says_when_it_is_switched_off():
    from dataclasses import replace as _replace

    from glasslinkxp.screens import Screen, ScreenLibrary

    screen = Screen(name="x", display="pfd", match={9: "IDENT"}, labels={1: "0"})
    pipeline = _pipeline(screens=ScreenLibrary([screen]))
    pipeline.reader.config = _replace(pipeline.reader.config, screen_confidence=0.0)
    assert "disabled" in pipeline._apply_screen([])


def test_apply_screen_leaves_a_confident_cell_alone():
    """Page lookup fills gaps; it does not overrule a clear reading."""
    from glasslinkxp.screens import Screen, ScreenLibrary

    screen = Screen(name="x", display="pfd", match={9: "IDENT"}, labels={1: "0"})
    results = [
        CellResult(index=0, text="9", raw="9", confidence=99.0, match_score=1.0),
        CellResult(index=8, text="IDENT", raw="IDENT", confidence=95.0, match_score=1.0),
    ]
    _pipeline(screens=ScreenLibrary([screen]))._apply_screen(results)
    assert results[0].text == "9"
    assert not results[0].by_screen


def test_the_page_is_identified_once_per_strip_not_once_per_cell():
    """Identification is a property of the strip, so it happens once.

    Doing it per cell would be wasted work, and worse, would let different
    cells on one strip be filled from different pages.
    """
    from glasslinkxp.screens import Screen, ScreenLibrary

    screen = Screen(
        name="xpdr-code", display="pfd",
        match={9: "IDENT", 10: "BKSP", 11: "BACK"},
        labels={1: "0", 2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7"},
    )

    class Counting(ScreenLibrary):
        def __init__(self, screens):
            super().__init__(screens)
            self.calls = 0

        def identify(self, *args, **kwargs):
            self.calls += 1
            return super().identify(*args, **kwargs)

    live = [
        (0, "2", 54.0), (1, "1", 30.0), (2, "2", 96.0), (3, "8", 41.0),
        (4, "4", 22.0), (5, "5", 96.0), (6, "5", 38.0), (7, "1", 29.0),
        (8, "IDENT", 95.0), (9, "BKSP", 92.0), (10, "BACK", 95.0), (11, "", 0.0),
    ]
    results = [
        CellResult(index=i, text=t, raw=t, confidence=c, match_score=1.0, blank=(t == ""))
        for i, t, c in live
    ]
    library = Counting([screen])
    _pipeline(screens=library)._apply_screen(results)

    assert library.calls == 1, "one identification for the whole strip"
    assert [r.text for r in results[:8]] == list("01234567")
    assert sum(1 for r in results if r.by_screen) == 4


def test_a_low_confidence_cell_the_page_agrees_with_is_recorded_as_confirmed():
    """Not a replacement, but not nothing either.

    A shaky reading the page corroborates stands on much firmer ground than one
    nothing checked. Without recording it, the log cannot tell "the page looked
    and agreed" apart from "the page never considered this cell".
    """
    from glasslinkxp.screens import Screen, ScreenLibrary

    screen = Screen(name="x", display="pfd", match={9: "IDENT"}, labels={1: "0"})
    results = [
        CellResult(index=0, text="0", raw="0", confidence=25.0, match_score=1.0),
        CellResult(index=8, text="IDENT", raw="IDENT", confidence=95.0, match_score=1.0),
    ]
    outcome = _pipeline(screens=ScreenLibrary([screen]))._apply_screen(results)

    assert results[0].text == "0", "the value is unchanged"
    assert not results[0].by_screen, "it was not replaced"
    assert results[0].confirmed_by == "x", "but the page did back it up"
    assert "confirmed [1]" in outcome


def test_a_confident_cell_is_neither_replaced_nor_confirmed():
    """Above the threshold the page is not consulted, so it corroborates nothing."""
    from glasslinkxp.screens import Screen, ScreenLibrary

    screen = Screen(name="x", display="pfd", match={9: "IDENT"}, labels={1: "0"})
    results = [
        CellResult(index=0, text="0", raw="0", confidence=99.0, match_score=1.0),
        CellResult(index=8, text="IDENT", raw="IDENT", confidence=95.0, match_score=1.0),
    ]
    outcome = _pipeline(screens=ScreenLibrary([screen]))._apply_screen(results)

    assert not results[0].by_screen and not results[0].confirmed_by
    assert "not needed" in outcome, "no page should even be looked for"


def test_no_page_is_looked_for_when_every_cell_read_confidently():
    """Identification only ever serves cells below the threshold.

    Scanning the page library when nothing needs help is wasted work, and
    implying otherwise made the documented flow read backwards.
    """
    from glasslinkxp.screens import Screen, ScreenLibrary

    class Counting(ScreenLibrary):
        def __init__(self, screens):
            super().__init__(screens)
            self.calls = 0

        def identify(self, *args, **kwargs):
            self.calls += 1
            return super().identify(*args, **kwargs)

    screen = Screen(name="x", display="pfd", match={9: "IDENT"}, labels={1: "0"})
    library = Counting([screen])
    results = [
        CellResult(index=0, text="0", raw="0", confidence=99.0, match_score=1.0),
        CellResult(index=8, text="IDENT", raw="IDENT", confidence=95.0, match_score=1.0),
    ]
    outcome = _pipeline(screens=library)._apply_screen(results)

    assert library.calls == 0, "no identification when nothing is below the threshold"
    assert "not needed" in outcome


def test_a_page_is_looked_for_as_soon_as_one_cell_is_shaky():
    from glasslinkxp.screens import Screen, ScreenLibrary

    screen = Screen(name="x", display="pfd", match={9: "IDENT"}, labels={1: "0"})
    results = [
        CellResult(index=0, text="2", raw="2", confidence=41.0, match_score=1.0),
        CellResult(index=8, text="IDENT", raw="IDENT", confidence=95.0, match_score=1.0),
    ]
    outcome = _pipeline(screens=ScreenLibrary([screen]))._apply_screen(results)

    assert results[0].text == "0"
    assert "replaced [1]" in outcome


def test_an_unchanged_frame_does_no_work_at_all():
    """A static strip should be genuinely idle.

    The change-gating cache holds results from *after* page lookup, so
    re-running it on an unchanged frame rediscovers the same page and reaches
    the same conclusions -- at the loop rate, and printing a line each time.
    """
    import cv2

    from glasslinkxp.config import OcrConfig
    from glasslinkxp.ocr import SoftkeyReader
    from glasslinkxp.pipeline import DisplayPipeline

    frame = synth.render_menu("xpdr")
    reader = SoftkeyReader(OcrConfig())
    try:
        pipeline = DisplayPipeline(
            DisplayConfig(key="pfd", geometry=StripGeometry()), reader, AppConfig()
        )
        first = pipeline.process(frame)
        assert first.ocr_calls > 0

        second = pipeline.process(frame.copy())
        assert second.ocr_calls == 0, "nothing changed, so nothing should be re-read"
        assert second.timings["screen_ms"] == 0.0 or second.timings["screen_ms"] < 0.05
        assert second.labels == first.labels, "the cached answer must be the same answer"
    finally:
        reader.close()


def test_a_changed_cell_brings_page_lookup_back():
    import numpy as np

    from glasslinkxp.config import OcrConfig
    from glasslinkxp.ocr import SoftkeyReader
    from glasslinkxp.pipeline import DisplayPipeline

    reader = SoftkeyReader(OcrConfig())
    try:
        pipeline = DisplayPipeline(
            DisplayConfig(key="pfd", geometry=StripGeometry()), reader, AppConfig()
        )
        pipeline.process(synth.render_menu("xpdr"))
        changed = pipeline.process(synth.render_menu("pfd_top"))
        assert changed.ocr_calls > 0
    finally:
        reader.close()


# ---------------------------------------------------------------------------
# background colour
# ---------------------------------------------------------------------------


def test_backgrounds_are_reported_per_cell(reader, config):
    pipeline = make_pipeline(reader, config)
    result = pipeline.process(synth.render_menu("alerts"))
    assert result.backgrounds[4] == YELLOW
    assert result.backgrounds[5] == RED
    assert result.backgrounds[8] == WHITE
    assert result.backgrounds[0] == BLACK
    assert [c.background for c in result.cells] == result.backgrounds


def test_a_background_change_alone_is_still_seen_through_the_gate(reader, config):
    """The case this feature is most likely to get wrong.

    A softkey becoming selected changes the cell's background and nothing
    else -- the label is identical, character for character. Colour is
    therefore measured outside the change gate, so this must hold whether or
    not the gate decides the cell moved.
    """
    labels = list(synth.MENUS["pfd_top"])
    pipeline = make_pipeline(reader, config, gating=True)

    plain = pipeline.process(synth.render_frame(labels))
    assert plain.backgrounds[2] == BLACK
    assert plain.cells[2].text == "PFD"

    selected = pipeline.process(synth.render_frame(labels, backgrounds={2: "white"}))
    assert selected.labels == plain.labels, "the label must not have changed"
    assert selected.backgrounds[2] == WHITE
    assert selected.backgrounds != plain.backgrounds


def test_a_cached_cell_still_carries_the_current_colour(reader, config):
    """A cell served from the OCR cache must not carry a stale background."""
    frame = synth.render_menu("alerts")
    pipeline = make_pipeline(reader, config, gating=True)
    first = pipeline.process(frame)
    second = pipeline.process(frame)
    assert second.ocr_calls == 0
    assert all(not cell.ocr_ran for cell in second.cells)
    assert second.backgrounds == first.backgrounds


def test_a_blank_cell_still_reports_its_background(reader, config):
    """An empty yellow cell is still a yellow button face."""
    labels = [""] * 12
    pipeline = make_pipeline(reader, config)
    result = pipeline.process(synth.render_frame(labels, backgrounds={7: "yellow"}))
    assert result.cells[7].blank
    assert result.backgrounds[7] == YELLOW


def test_colour_can_be_switched_off(reader, config):
    pipeline = make_pipeline(reader, config, color=ColorConfig(enabled=False))
    result = pipeline.process(synth.render_menu("alerts"))
    assert result.backgrounds == [BLACK] * 12
    assert result.timings["color_ms"] >= 0.0


def test_the_debug_line_carries_the_background(reader, config, caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="glasslinkxp.pipeline")
    pipeline = make_pipeline(reader, config)
    pipeline.process(synth.render_menu("alerts"))
    lines = {}
    for record in caplog.records:
        parts = record.getMessage().split()
        if len(parts) > 2 and parts[1] == "cell" and parts[2].isdigit():
            lines[int(parts[2])] = record.getMessage()
    assert "bg=yellow" in lines[5]
    assert "bg=red" in lines[6]
    assert "bg=white" in lines[9]
    assert "bg=black" in lines[1]
    assert "bg=black" in lines[4], "a blank cell still has a background"


def test_a_colour_only_change_is_logged_even_though_no_cell_was_ocrd(reader, config, caplog):
    """The frame that would otherwise look completely idle in the log.

    Forced here with a change tolerance nothing can trip, because colour is
    measured outside the gate and so must be reported outside it too.
    """
    import logging

    display = DisplayConfig(key="pfd", geometry=StripGeometry())
    app = AppConfig(displays=(display,), ocr=config.ocr, change_gating=True,
                    change_tolerance=255)
    pipeline = DisplayPipeline(display, reader, app)

    labels = list(synth.MENUS["pfd_top"])
    pipeline.process(synth.render_frame(labels))
    caplog.set_level(logging.DEBUG, logger="glasslinkxp.pipeline")
    result = pipeline.process(synth.render_frame(labels, backgrounds={2: "white"}))

    assert result.ocr_calls == 0, "the gate must have swallowed the pixel change"
    assert result.backgrounds[2] == WHITE
    cached = [r.getMessage() for r in caplog.records if "CACHED" in r.getMessage()]
    assert len(cached) == 1
    assert "cell 3" in cached[0]
    assert "bg=white" in cached[0] and "was bg=black" in cached[0]


def test_a_quiet_frame_still_logs_nothing(reader, config, caplog):
    import logging

    frame = synth.render_menu("pfd_top")
    pipeline = make_pipeline(reader, config, gating=True)
    pipeline.process(frame)
    caplog.set_level(logging.DEBUG, logger="glasslinkxp.pipeline")
    pipeline.process(frame)
    assert [r.getMessage() for r in caplog.records] == []
