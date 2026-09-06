"""Look at the pixels a geometry would actually read, and report what is wrong.

(Named ``checks`` rather than ``inspect`` so that importing it into a module
that also wants the standard library's ``inspect`` is not a trap.)

The calibration editor can show where the boxes are, but not what is inside
them. This runs the daemon's own crop over the captured frame and asks the
same question ``run -v`` asks of every cell: is the ink touching an edge?

It uses ``strip.split_cells`` and ``strip.clipped_edges`` rather than its own
crop and its own threshold, for the reason everything else in this tab does:
a warning derived from a second implementation would be a warning about
something other than what the reader is going to do.

What it reports is a hint, not a verdict. A label that genuinely fills its
cell reports an edge it is only close to, and a crop that has slid onto a
separator bar reports nothing at all. It is worth saying anyway -- "look at
cell 4" is a great deal more use than silence -- so long as it is offered as
a suspicion rather than as a finding.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import OcrConfig, StripGeometry


@dataclass(frozen=True)
class CellClip:
    """One cell whose ink reaches the edge of its crop."""

    #: 1-based, as the cells are numbered on the panel and in the datarefs.
    cell: int
    edges: tuple[str, ...]

    def __str__(self) -> str:
        return f"cell {self.cell} ({', '.join(self.edges)})"


def load_frame(path: str | Path):
    """The captured PNG as the daemon would read it, or None.

    Read with OpenCV rather than converted from the editor's own PIL image, so
    the array handed to ``split_cells`` is byte for byte the one the capture
    path produces -- same channel order, same depth.

    Which means the same flag ``capture.ImageCapture.grab`` uses, and that is
    the point of the sentence above: ``IMREAD_UNCHANGED`` keeps whatever the
    file happens to have, so a PNG with an alpha channel arrived here with
    four channels and reached the daemon's own crop with three. The clipping
    warning would then be measuring something the reader never sees.
    """
    import cv2  # noqa: PLC0415 - heavy; not wanted at import time

    p = Path(path)
    if not p.is_file():
        return None
    return cv2.imread(str(p), cv2.IMREAD_COLOR)


def check_cells(frame, geometry: StripGeometry, ocr: OcrConfig | None = None) -> list[CellClip]:
    """Cells whose ink touches an edge of the crop this geometry would take."""
    from ..strip import clipped_edges, ink_ratio, split_cells  # noqa: PLC0415

    if frame is None:
        return []
    ocr = ocr or OcrConfig()
    found: list[CellClip] = []
    for index, cell in enumerate(split_cells(frame, geometry), start=1):
        if cell.size == 0:
            continue
        # A blank softkey has no ink to touch anything, and reporting one as
        # clipped would put a warning on most of the strip most of the time --
        # the G1000 leaves plenty of keys empty. Skipped on the same test the
        # pipeline uses to decide a cell never reaches OCR.
        if ink_ratio(cell, ocr.blank_contrast) < ocr.blank_ink_ratio:
            continue
        edges = clipped_edges(cell, ocr.blank_contrast)
        if edges:
            found.append(CellClip(index, edges))
    return found


#: How many cells to name before summarising. A badly over-trimmed strip
#: reports all twelve, and a paragraph of them says nothing the count did not.
NAMED = 6


def describe(clips: list[CellClip], display: str = "") -> str:
    """One line naming the cells, for a status bar or a dialog."""
    if not clips:
        return ""
    where = f" on {display.upper()}" if display else ""
    if len(clips) == 1:
        return f"1 cell{where} may be clipped: {clips[0]}."
    named = ", ".join(str(c) for c in clips[:NAMED])
    rest = f", and {len(clips) - NAMED} more" if len(clips) > NAMED else ""
    return f"{len(clips)} cells{where} may be clipped: {named}{rest}."


ADVICE = (
    "Ink touching the edge of a box usually means the label is being cut off, and a "
    "half glyph reads as nothing at all rather than as the wrong character.\n\n"
    "If the edge named is the left or the right, it is the side trim; the top or the "
    "bottom, the top/bottom trim. If several cells report the same edge it is more "
    "likely the strip itself is a little too small.\n\n"
    "It is only a hint: a long label really can fill its cell. Look at the close-up "
    "before changing anything, and check the result on the Cells tab."
)
