"""Glue: frame -> strip -> cells -> (gated) OCR -> 12 labels."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace

import numpy as np

from .color import BLACK, background_name, classify_cell
from .config import AppConfig, DisplayConfig
from .ocr import CellResult, SoftkeyReader
from .screens import ScreenLibrary
from .strip import (
    changed_cells,
    clipped_edges,
    ink_bounds,
    ink_ratio,
    is_blank,
    preprocess_cell,
    snapshot_cells,
    split_cells,
)

LOG = logging.getLogger(__name__)


@dataclass
class DisplayResult:
    display: str
    cells: list[CellResult]
    timings: dict[str, float] = field(default_factory=dict)
    ocr_calls: int = 0

    @property
    def labels(self) -> list[str]:
        return [cell.text for cell in self.cells]

    @property
    def backgrounds(self) -> list[int]:
        return [cell.background for cell in self.cells]


class DisplayPipeline:
    """Per-display state: previous cell pixels and previous results.

    Only cells whose pixels changed since the last frame are re-OCR'd; the
    rest are served from the previous result. Softkeys change rarely, so
    steady-state cost is one crop + one array compare per frame.
    """

    def __init__(
        self,
        display: DisplayConfig,
        reader: SoftkeyReader,
        app: AppConfig,
        screens: ScreenLibrary | None = None,
    ) -> None:
        self.display = display
        self.reader = reader
        self.app = app
        self.screens = (
            screens if screens is not None else ScreenLibrary.load(reader.config.screens_file)
        )
        self._previous_cells: list[np.ndarray] | None = None
        self._previous_results: list[CellResult] | None = None

    def _apply_screen(self, results: list[CellResult]) -> str:
        """Fill low-confidence cells from a page the confident ones identify.

        Returns a one-line account of what happened, for the debug log: which
        page matched or why none did, and how many cells it replaced.
        """
        if self.reader.config.screen_confidence <= 0:
            return "disabled (ocr.screen_confidence = 0)"
        if not len(self.screens):
            return f"no page definitions loaded from {self.reader.config.screens_file}"

        # Nothing to help, so do not go looking for a page. Identification only
        # ever serves cells that read below the threshold; when every cell is
        # confident there is no question to answer, and the common case costs a
        # single comparison instead of a scan over every known page.
        shaky = [
            r.index + 1 for r in results
            if not r.blank and r.confidence < self.reader.config.screen_confidence
        ]
        if not shaky:
            return (
                f"not needed: every cell read at or above "
                f"{self.reader.config.screen_confidence:.0f}%"
            )

        labels = {r.index + 1: r.text for r in results}
        confidences = {r.index + 1: r.confidence for r in results}
        screen = self.screens.identify(
            self.display.key, labels, confidences,
            self.reader.config.screen_match_confidence,
        )
        if screen is None:
            return (
                f"no page matched ({len(self.screens)} known); {len(shaky)} cell(s) below "
                f"{self.reader.config.screen_confidence:.0f}% went unhelped: {shaky}"
            )
        replaced: list[int] = []
        confirmed: list[int] = []
        for result in results:
            cell = result.index + 1
            expected = screen.labels.get(cell)
            if expected is None or result.blank:
                continue
            if result.confidence >= self.reader.config.screen_confidence:
                continue
            if result.text == expected:
                # The page was consulted and agreed. Worth recording: a shaky
                # reading the page backs up is on much firmer ground than one
                # nothing corroborated, and without this the two are
                # indistinguishable in the log.
                results[result.index] = replace(result, confirmed_by=screen.name)
                confirmed.append(cell)
                continue
            # Not logged here: the per-cell debug lines are emitted after this
            # stage so they can show the replacement, and duplicating it would
            # print every substitution twice.
            results[result.index] = replace(
                result, text=expected, match_score=1.0, by_screen=screen.name
            )
            replaced.append(cell)
        parts = []
        if replaced:
            parts.append(f"replaced {replaced}")
        if confirmed:
            parts.append(f"confirmed {confirmed}")
        return (
            f"matched {screen.name!r} on cells {sorted(screen.match)}; "
            + ("; ".join(parts) if parts else "no low-confidence cells to act on")
        )

    def reset(self) -> None:
        self._previous_cells = None
        self._previous_results = None

    def process(self, frame: np.ndarray) -> DisplayResult:
        timings: dict[str, float] = {}
        geom = self.display.geometry

        t0 = time.perf_counter()
        cells = split_cells(frame, geom)
        gray_cells = snapshot_cells(cells)
        timings["split_ms"] = (time.perf_counter() - t0) * 1000.0

        t0 = time.perf_counter()
        if self.app.change_gating:
            changed = changed_cells(self._previous_cells, gray_cells, self.app.change_tolerance)
        else:
            changed = [True] * len(cells)
        timings["gate_ms"] = (time.perf_counter() - t0) * 1000.0

        # Colour is measured for every cell on every frame, deliberately
        # outside the change gate. The gate exists to skip OCR, which is the
        # expensive stage; a median over a border ring is not. Doing it here
        # means a cell whose background changes while its label does not -- a
        # softkey becoming selected, which is the single most common colour
        # event on the strip -- picks up the new colour even on the path that
        # serves the text from cache, and does not depend on the grayscale
        # gate happening to notice the colour change.
        t0 = time.perf_counter()
        if self.app.color.enabled:
            backgrounds = [classify_cell(cell, self.app.color) for cell in cells]
        else:
            backgrounds = [BLACK] * len(cells)
        timings["color_ms"] = (time.perf_counter() - t0) * 1000.0

        results: list[CellResult] = []
        diagnostics: dict[int, str] = {}

        def bg_tag(index: int) -> str:
            """``bg=<name>`` for a debug line, or nothing when colour is off."""
            if not self.app.color.enabled:
                return ""
            return f"bg={background_name(backgrounds[index]):<6} "

        preprocess_ms = 0.0
        ocr_ms = 0.0
        ocr_calls = 0

        for index, cell in enumerate(cells):
            previous = (
                self._previous_results[index]
                if self._previous_results is not None and index < len(self._previous_results)
                else None
            )
            if not changed[index] and previous is not None:
                cached = CellResult(
                    **{**previous.__dict__, "ocr_ran": False, "background": backgrounds[index]}
                )
                results.append(cached)
                if previous.background != cached.background:
                    # The gate said these pixels did not move, yet the colour
                    # did. Worth a line of its own: it is the one case where
                    # the published datarefs change while OCR never ran, and
                    # without this the frame looks completely idle in the log.
                    diagnostics[index] = (
                        f"CACHED  {bg_tag(index)}was bg="
                        f"{background_name(previous.background)} "
                        f"-- colour changed, label unchanged, no OCR"
                    )
                continue

            t0 = time.perf_counter()
            ink = ink_ratio(cell, self.reader.config.blank_contrast)
            blank = ink < self.reader.config.blank_ink_ratio
            if blank:
                preprocess_ms += (time.perf_counter() - t0) * 1000.0
                # Distinguishing "gated out as empty" from "OCR read nothing" is
                # the whole question when a short label goes missing, so say which.
                diagnostics[index] = (
                    f"BLANK   {bg_tag(index)}ink={ink:.4f} < "
                    f"{self.reader.config.blank_ink_ratio:.4f} "
                    f"(contrast={self.reader.config.blank_contrast}) -- never reached OCR"
                )
                results.append(
                    CellResult(index=index, blank=True, background=backgrounds[index])
                )
                continue
            variants = [
                preprocess_cell(
                    cell,
                    upscale=self.reader.config.upscale,
                    method=self.reader.config.threshold,
                    sharpen_amount=amount,
                    sharpen_radius=radius,
                )
                for amount, radius in self.reader.config.sharpen_ladder
            ]
            preprocess_ms += (time.perf_counter() - t0) * 1000.0

            t0 = time.perf_counter()
            cell_result = replace(
                self.reader.read_best(index, variants), background=backgrounds[index]
            )
            results.append(cell_result)
            ocr_ms += (time.perf_counter() - t0) * 1000.0
            ocr_calls += 1
            x0, x1 = ink_bounds(cell, self.reader.config.blank_contrast)
            # Named edges rather than a bare marker: which side is being cut
            # is the whole of what to do about it, and the calibration editor
            # warns from this same function so the two always agree.
            touching = clipped_edges(cell, self.reader.config.blank_contrast)
            clipped = f" CLIPPED? {','.join(touching)}" if touching else ""
            diagnostics[index] = (
                f"{bg_tag(index)}ink={ink:.4f} x={x0:.2f}-{x1:.2f} "
                f"raw={cell_result.raw!r:<12} ocr={cell_result.text!r:<12} "
                f"conf={cell_result.confidence:5.1f} "
                f"match={cell_result.match_score:.2f}{clipped}"
            )

        timings["preprocess_ms"] = preprocess_ms
        timings["ocr_ms"] = ocr_ms

        t0 = time.perf_counter()
        if ocr_calls == 0 and self._previous_results is not None:
            # Every cell came from the change-gating cache, and the cache holds
            # results from *after* page lookup ran, so re-running it would
            # rediscover the same page and reach the same conclusions. Skipping
            # keeps a static strip genuinely idle instead of re-identifying the
            # page at the loop rate.
            outcome = "skipped: no cell changed"
        else:
            outcome = self._apply_screen(results)
        timings["screen_ms"] = (time.perf_counter() - t0) * 1000.0

        # Logged here rather than inside the loop above: page lookup can change
        # a cell after OCR has spoken, and a debug line that stops at the OCR
        # answer disagrees with the dataref that actually gets published.
        # Gated on there being a line to print rather than on ocr_calls: a
        # frame where only a background moved does no OCR at all, and that is
        # exactly the frame worth seeing.
        if LOG.isEnabledFor(logging.DEBUG) and diagnostics:
            if ocr_calls:
                LOG.debug("%s screen lookup: %s", self.display.key, outcome)
            for result in results:
                detail = diagnostics.get(result.index)
                if detail is None:
                    continue
                if result.by_screen:
                    detail += f" -> {result.text!r} FROM PAGE {result.by_screen!r}"
                elif result.confirmed_by:
                    detail += f" -> CONFIRMED BY PAGE {result.confirmed_by!r}"
                elif result.by_signature:
                    detail += f" -> {result.text!r} FROM SIGNATURE"
                LOG.debug("%s cell %-2d %s", self.display.key, result.index + 1, detail)

        self._previous_cells = gray_cells
        self._previous_results = results
        return DisplayResult(
            display=self.display.key, cells=results, timings=timings, ocr_calls=ocr_calls
        )
