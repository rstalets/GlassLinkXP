"""Configuration model + TOML loading for the G1000 softkey OCR daemon.

Every setting has a working default, so a missing config file is not fatal;
``config.example.toml`` documents the same values.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

LOG = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent

DEFAULT_WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -/"
DEFAULT_JSON_FALLBACK = "g1000_softkey_labels.json"


class ConfigError(Exception):
    """Raised for a malformed configuration file."""


@dataclass(frozen=True)
class StripGeometry:
    """Softkey strip position as *fractions* of the captured client area.

    Fractions rather than pixels so the calibration survives a window resize.
    """

    x: float = 0.050
    y: float = 0.915
    w: float = 0.900
    h: float = 0.055
    cells: int = 12
    #: fraction of a single cell trimmed off each side before OCR
    cell_pad_x: float = 0.10
    cell_pad_y: float = 0.12

    def validate(self) -> None:
        for name in ("x", "y", "w", "h", "cell_pad_x", "cell_pad_y"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ConfigError(f"geometry.{name} must be within 0..1, got {value!r}")
        if self.x + self.w > 1.0 or self.y + self.h > 1.0:
            raise ConfigError("geometry crop extends past the frame edge")
        if self.cells < 1:
            raise ConfigError("geometry.cells must be >= 1")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StripGeometry":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            LOG.warning("ignoring unknown geometry keys: %s", ", ".join(sorted(unknown)))
        geom = cls(**{k: v for k, v in data.items() if k in known})
        geom.validate()
        return geom

    def as_dict(self) -> dict[str, Any]:
        return {f: getattr(self, f) for f in self.__dataclass_fields__}


@dataclass(frozen=True)
class DisplayConfig:
    """One captured display (a G1000 PFD or MFD pop-out window)."""

    key: str
    window_title: str = ""
    geometry: StripGeometry = field(default_factory=StripGeometry)
    dataref_prefix: str = ""
    enabled: bool = True
    #: Whether this daemon may resize the pop-out window at all.
    #:
    #: Off by default, and deliberately so: a pop-out may be feeding external
    #: avionics hardware -- a RealSimGear G1000 unit, say -- where its size and
    #: position are part of somebody's physical setup. Resizing that window
    #: would break their panel to make our OCR marginally easier, which is not
    #: a trade this daemon gets to make on its own.
    manage_window_size: bool = False
    #: Client size to force the pop-out window to, as [width, height].
    #: Only applied when manage_window_size is true.
    #:
    #: The G1000 renders to a 1024x768 texture, so a pop-out whose *display
    #: area* is smaller than that throws away real detail before capture ever
    #: sees it -- and the glyphs are already marginal for OCR at ~10 px. Note
    #: the pop-out includes the bezel, so the window has to be bigger than
    #: 1024x768 for the display area itself to reach it; find the number with
    #: one calibrate pass. Growing beyond that point only interpolates.
    #:
    #: Geometry is fractional, so a resize does not invalidate calibration.
    window_size: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if not self.dataref_prefix:
            object.__setattr__(self, "dataref_prefix", f"g1000/softkey/{self.key}")
        if self.manage_window_size and self.window_size is None:
            raise ConfigError(
                f"display.{self.key}: manage_window_size is on but no window_size is set, "
                "so there is nothing to resize to. Give a [width, height], or turn "
                "manage_window_size off."
            )

    def dataref_names(self) -> list[str]:
        """The label (string) datarefs, one per cell."""
        return [f"{self.dataref_prefix}/{i + 1}" for i in range(self.geometry.cells)]

    def background_dataref_names(self) -> list[str]:
        """The background-colour (int) datarefs, one per cell.

        Suffixed rather than given their own prefix so a cell's two datarefs
        sort together and read obviously as a pair in DataRefEditor.
        """
        return [f"{name}/bg" for name in self.dataref_names()]

    def all_dataref_names(self) -> list[str]:
        return self.dataref_names() + self.background_dataref_names()


@dataclass(frozen=True)
class OcrConfig:
    engine: str = "auto"  # auto | tesserocr | pytesseract
    lang: str = "eng"
    tessdata_path: str | None = None
    psm: int = 7  # single text line
    whitelist: str = DEFAULT_WHITELIST
    upscale: float = 4.0
    #: Unsharp mask settings tried in order, as (amount, radius) pairs.
    #:
    #: The glyphs are ~10 px tall and slightly soft, and thresholding at that
    #: size can close the counters of 0/6/8/9 -- a filled counter is not a
    #: character, so Tesseract returns nothing. Sharpening reopens them, but
    #: too much of it rings and grows strokes instead, turning a 0 into a B.
    #: The right amount depends on the font and the capture scale, which is not
    #: knowable in advance, so instead of guessing one value we try several and
    #: keep whichever result the vocabulary and Tesseract agree on. The first
    #: rung is no sharpening at all, so this can never do worse than not trying.
    #: Confidence above which an exact vocabulary hit ends the ladder early.
    #:
    #: Landing on a known label is not proof of being right when the label set
    #: contains near-identical members: every digit 0-7 is a valid softkey, so
    #: a 0 misread as 2 is still an "exact match" and would stop the search.
    #: Below this, the remaining rungs are tried and the most confident answer
    #: wins. Set to 0 to always try every rung.
    accept_confidence: float = 80.0
    #: Below this confidence a cell is a candidate for being filled in from a
    #: known softkey page (see screens.toml). Recognising a 10-pixel digit is
    #: hard; recognising which page is showing, from the labels that read
    #: cleanly, is easy -- and the page says what the hard cells must be.
    #: 0 disables page lookup.
    screen_confidence: float = 80.0
    #: A page is only identified when its match cells all read at least this
    #: confidently. Higher than screen_confidence on purpose: an identification
    #: made from a guess would propagate that guess into every cell it fills.
    screen_match_confidence: float = 85.0
    screens_file: str = str(PACKAGE_DIR / "screens.toml")
    #: Shape-signature fallback (see signatures.py). Off by default: page
    #: lookup covers the same cells with a stronger signal.
    signature_confidence: float = 0.0
    signatures_file: str = str(PACKAGE_DIR / "signatures.json")
    sharpen_ladder: tuple[tuple[float, float], ...] = (
        (0.0, 0.0),
        (0.5, 1.0),
        (1.0, 1.4),
    )
    threshold: str = "otsu"  # otsu | adaptive
    labels_file: str = str(PACKAGE_DIR / "labels.txt")
    fuzzy_cutoff: float = 0.62
    #: fraction of a cell's pixels that must deviate from the cell's dominant
    #: brightness for the cell to count as "has a label on it"
    blank_ink_ratio: float = 0.004
    #: how far a pixel must sit from the cell's dominant tone to count as ink.
    #: The G1000 draws unavailable softkeys dimmed rather than hiding them, so
    #: too high a value reads a dim-but-present label as an empty cell. Short
    #: labels break first: a lone digit carries far less evidence than a word.
    blank_contrast: int = 40


@dataclass(frozen=True)
class ColorConfig:
    """Background-colour classification for a softkey cell.

    Thresholds live here rather than in the code because the numbers that
    matter are the ones on *your* capture. The defaults below come from
    plausible G1000 swatches, not from a live X-Plane frame -- run
    ``dump-colors`` against a real capture and move them if they disagree.
    """

    enabled: bool = True
    #: Outermost fraction of the cell (each side) used to sample the
    #: background. Labels are centred, so this ring is essentially never
    #: glyph; too large a value starts eating into the text.
    ring_fraction: float = 0.15
    #: V at or below this is BLACK, whatever the hue. Tested first, so
    #: brightness can only ever push a cell into black. A red at 35%
    #: brightness sits near V=79, so this must stay well under that; raise it
    #: if a black cell reads as coloured, lower it if a dim caution reads as
    #: black.
    value_max: int = 60
    #: S at or below this is achromatic -> WHITE (V has already ruled out
    #: black). The swatches put white at S=0 and the coloured states above
    #: S=200, so anything in the middle is a comfortable cut.
    saturation_max: int = 60
    #: Hue windows, OpenCV convention (H is 0-179, and red wraps).
    red_hue_max: int = 8
    red_hue_wrap_min: int = 172
    yellow_hue_min: int = 18
    yellow_hue_max: int = 40

    def validate(self) -> None:
        if not 0.0 < self.ring_fraction <= 0.5:
            raise ConfigError(
                f"color.ring_fraction must be within 0..0.5, got {self.ring_fraction!r}"
            )
        for name in ("value_max", "saturation_max"):
            value = getattr(self, name)
            if not 0 <= value <= 255:
                raise ConfigError(f"color.{name} must be within 0..255, got {value!r}")
        for name in ("red_hue_max", "red_hue_wrap_min", "yellow_hue_min", "yellow_hue_max"):
            value = getattr(self, name)
            if not 0 <= value <= 179:
                raise ConfigError(
                    f"color.{name} must be within 0..179 (OpenCV hue), got {value!r}"
                )
        if self.yellow_hue_min > self.yellow_hue_max:
            raise ConfigError("color.yellow_hue_min must not exceed color.yellow_hue_max")


@dataclass(frozen=True)
class PublishConfig:
    target: str = "webapi"  # webapi | file | console
    base_url: str = "http://localhost:8086"
    api_version: str = "v1"
    #: Bytes per label dataref -> the PilotsDeck address suffix (':s64').
    #:
    #: 64 rather than a snug fit. The width is fixed in three places that have
    #: to agree -- FIELD_WIDTH in the plugin (an X-Plane restart, since the
    #: buffer is allocated when the accessor is registered), this setting, and
    #: every PilotsDeck button address -- so the cost of changing it later is
    #: paid by the user re-editing every button. The longest label in
    #: labels.txt is "FLIGHT PLAN" at 11 characters, which left the previous
    #: 16-byte field four characters of headroom for a vocabulary that grows
    #: whenever someone finds a softkey nobody had listed. 64 bytes is ~1.5 KB
    #: of plugin memory across all 24 fields and noise on the wire, so it is
    #: set generously once instead of tuned.
    field_width: int = 64
    timeout: float = 1.0
    json_path: str = ""
    #: seconds between reconnect attempts when X-Plane is not answering
    retry_interval: float = 5.0


@dataclass(frozen=True)
class AppConfig:
    loop_hz: float = 12.0
    change_gating: bool = True
    change_tolerance: int = 6
    displays: tuple[DisplayConfig, ...] = ()
    ocr: OcrConfig = field(default_factory=OcrConfig)
    color: ColorConfig = field(default_factory=ColorConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)

    def display(self, key: str) -> DisplayConfig:
        for disp in self.displays:
            if disp.key == key:
                return disp
        raise ConfigError(f"no display named {key!r} in config")

    @property
    def active_displays(self) -> tuple[DisplayConfig, ...]:
        return tuple(d for d in self.displays if d.enabled)


def default_displays() -> tuple[DisplayConfig, ...]:
    return (
        DisplayConfig(key="pfd", window_title="G1000 PFD"),
        DisplayConfig(key="mfd", window_title="G1000 MFD"),
    )


def default_config() -> AppConfig:
    return AppConfig(displays=default_displays())


def _subsection(data: Mapping[str, Any], name: str) -> dict[str, Any]:
    section = data.get(name, {})
    if not isinstance(section, Mapping):
        raise ConfigError(f"[{name}] must be a table")
    return dict(section)


def _coerce_ladder(value):
    """TOML gives lists; the config holds tuples so it stays hashable/frozen."""
    return tuple((float(a), float(b)) for a, b in value)


def _build(cls, data: Mapping[str, Any]):
    known = set(cls.__dataclass_fields__)
    unknown = set(data) - known
    if unknown:
        LOG.warning("ignoring unknown keys in config: %s", ", ".join(sorted(unknown)))
    return cls(**{k: v for k, v in data.items() if k in known})


def load_config(path: str | Path | None) -> AppConfig:
    """Load ``path``; fall back to built-in defaults when it is None."""
    if path is None:
        return default_config()
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - depends on user file
        raise ConfigError(f"{p}: {exc}") from exc
    return from_mapping(raw, base_dir=p.parent)


def from_mapping(raw: Mapping[str, Any], base_dir: Path | None = None) -> AppConfig:
    app_data = _subsection(raw, "app")
    ocr_data = _subsection(raw, "ocr")
    if "sharpen_ladder" in ocr_data:
        try:
            ocr_data["sharpen_ladder"] = _coerce_ladder(ocr_data["sharpen_ladder"])
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                "ocr.sharpen_ladder must be a list of [amount, radius] pairs, "
                f"e.g. [[0.0, 0.0], [0.5, 1.0]] -- got {ocr_data['sharpen_ladder']!r} ({exc})"
            ) from exc
    color_data = _subsection(raw, "color")
    publish_data = _subsection(raw, "publish")

    if base_dir is not None and ocr_data.get("screens_file"):
        screens = Path(ocr_data["screens_file"])
        if not screens.is_absolute():
            ocr_data["screens_file"] = str((base_dir / screens).resolve())
    if base_dir is not None and ocr_data.get("signatures_file"):
        signatures = Path(ocr_data["signatures_file"])
        if not signatures.is_absolute():
            ocr_data["signatures_file"] = str((base_dir / signatures).resolve())
    if base_dir is not None and ocr_data.get("labels_file"):
        labels = Path(ocr_data["labels_file"])
        if not labels.is_absolute():
            ocr_data["labels_file"] = str((base_dir / labels).resolve())

    displays: list[DisplayConfig] = []
    for key, value in _subsection(raw, "display").items():
        if not isinstance(value, Mapping):
            raise ConfigError(f"[display.{key}] must be a table")
        entry = dict(value)
        geometry = StripGeometry.from_dict(_subsection(entry, "geometry"))
        entry.pop("geometry", None)
        entry["key"] = key
        entry["geometry"] = geometry
        if entry.get("window_size") is not None:
            try:
                width, height = entry["window_size"]
                entry["window_size"] = (int(width), int(height))
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    f"display.{key}.window_size must be [width, height], "
                    f"got {entry['window_size']!r} ({exc})"
                ) from exc
        displays.append(_build(DisplayConfig, entry))

    config = AppConfig(
        displays=tuple(displays) if displays else default_displays(),
        ocr=_build(OcrConfig, ocr_data),
        color=_build(ColorConfig, color_data),
        publish=_build(PublishConfig, publish_data),
        **{k: v for k, v in app_data.items() if k in {"loop_hz", "change_gating", "change_tolerance"}},
    )
    if config.loop_hz <= 0:
        raise ConfigError("app.loop_hz must be > 0")
    config.color.validate()
    return config
