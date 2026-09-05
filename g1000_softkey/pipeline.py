"""Glue: frame -> strip -> cells -> (gated) OCR -> 12 labels."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import numpy as np

from .config import AppConfig, DisplayConfig
from .ocr import CellResult, SoftkeyReader
from .strip import (
    changed_cells,
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


class DisplayPipeline:
    """Per-display state: previous cell pixels and previous results.

    Only cells whose pixels changed since the last frame are re-OCR'd; the
    rest are served from the previous result. Softkeys change rarely, so
    steady-state cost is one crop + one array compare per frame.
    """

    def __init__(self, display: DisplayConfig, reader: SoftkeyReader, app: AppConfig) -> None:
        self.display = display
        self.reader = reader
        self.app = app
        self._previous_cells: list[np.ndarray] | None = None
        self._previous_results: list[CellResult] | None = None

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

        results: list[CellResult] = []
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
                cached = CellResult(**{**previous.__dict__, "ocr_ran": False})
                results.append(cached)
                continue

            t0 = time.perf_counter()
            ink = ink_ratio(cell, self.reader.config.blank_contrast)
            blank = ink < self.reader.config.blank_ink_ratio
            if blank:
                preprocess_ms += (time.perf_counter() - t0) * 1000.0
                # Distinguishing "gated out as empty" from "OCR read nothing" is
                # the whole question when a short label goes missing, so say which.
                LOG.debug(
                    "%s cell %-2d BLANK   ink=%.4f < %.4f (contrast=%d) -- never reached OCR",
                    self.display.key, index + 1, ink,
                    self.reader.config.blank_ink_ratio, self.reader.config.blank_contrast,
                )
                results.append(CellResult(index=index, blank=True))
                continue
            image = preprocess_cell(
                cell, upscale=self.reader.config.upscale, method=self.reader.config.threshold
            )
            preprocess_ms += (time.perf_counter() - t0) * 1000.0

            t0 = time.perf_counter()
            cell_result = self.reader.read(index, image)
            results.append(cell_result)
            ocr_ms += (time.perf_counter() - t0) * 1000.0
            ocr_calls += 1
            x0, x1 = ink_bounds(cell, self.reader.config.blank_contrast)
            clipped = " CLIPPED?" if (x0 <= 0.02 or x1 >= 0.98) else ""
            LOG.debug(
                "%s cell %-2d ink=%.4f x=%.2f-%.2f raw=%-12r -> %-12r conf=%5.1f match=%.2f%s",
                self.display.key, index + 1, ink, x0, x1, cell_result.raw, cell_result.text,
                cell_result.confidence, cell_result.match_score, clipped,
            )

        timings["preprocess_ms"] = preprocess_ms
        timings["ocr_ms"] = ocr_ms

        self._previous_cells = gray_cells
        self._previous_results = results
        return DisplayResult(
            display=self.display.key, cells=results, timings=timings, ocr_calls=ocr_calls
        )
