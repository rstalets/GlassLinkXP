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


class ConfigError(Exception):
    """Raised for a malformed configuration file."""


class ConfigNotFound(ConfigError):
    """The named configuration file does not exist.

    Split out from :class:`ConfigError` because it is the one failure that is
    *not* a broken file: there is nothing to lose by carrying on without it,
    which is what the GUI does when asked to open a config that has yet to be
    written. Every other ConfigError -- a TOML syntax error, a value out of
    range -- means a file that exists and would be destroyed by treating it as
    absent, so the two must not be caught together.
    """


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

    def __post_init__(self) -> None:
        if not self.dataref_prefix:
            object.__setattr__(self, "dataref_prefix", f"glasslinkxp/softkey/{self.key}")

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
    lang: str = "eng"
    tessdata_path: str | None = None
    psm: int = 7  # single text line
    whitelist: str = DEFAULT_WHITELIST
    upscale: float = 4.0
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
    sharpen_ladder: tuple[tuple[float, float], ...] = (
        (0.0, 0.0),
        (0.5, 1.0),
        (1.0, 1.4),
    )
    threshold: str = "otsu"  # otsu | adaptive
    #: After the sharpening ladder, try every rung again with its light/dark
    #: polarity the other way round.
    #:
    #: A cell's polarity is read off its border ring, where there is never a
    #: glyph (see ``strip._background_is_white``). That is a wide margin
    #: rather than a guess, but it is still one measurement of one frame, and
    #: a cell it gets wrong reaches Tesseract white-on-black and reads as
    #: nothing or as garbage. Nothing else in the ladder can recover that:
    #: sharpening, upscaling and the threshold method all leave polarity
    #: alone, which is why a tuning run against such a cell reports that no
    #: candidate helped.
    #:
    #: On by default, and it costs nothing on a cell that reads: the retries
    #: sit *after* every sharpening rung, and ``read_best`` stops at the first
    #: variant that lands on a known label confidently. Turn it off only to
    #: reproduce the old behaviour.
    retry_opposite_polarity: bool = True
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


#: What a G1000 pop-out is sized to when the configured size cannot be used.
#: 4:3, and large enough that the 1024x768 display texture is not downsampled
#: before capture sees it.
DEFAULT_WINDOW_SIZE = (1280, 960)


@dataclass(frozen=True)
class WindowManagementConfig:
    """Whether this daemon opens, sizes and places the G1000 pop-outs itself.

    On by default, because the alternative is a checklist the user has to work
    through by hand before every flight -- pop out two windows, size them the
    same way as last time, drag them somewhere the taskbar will not sit over
    them -- and getting any step of it wrong shows up as OCR that reads
    nothing rather than as an error.

    It is one switch rather than three because the three parts are not
    independently useful: a window this daemon opened lands wherever X-Plane
    felt like putting it, at whatever size it felt like using, so opening one
    without also sizing and placing it just moves the manual step. Turn the
    whole thing off to manage the windows yourself.

    This is the only thing in the daemon that sizes or moves a window. Do not
    add a per-display size beside it: two settings fixing one window's size
    needs a rule about which of them wins, and the loser is then a setting that
    is quietly ignored rather than one that does what it says.
    """

    enabled: bool = True
    #: Client size for a managed pop-out, as [width, height].
    #:
    #: Must be 4:3. The G1000 draws a 4:3 panel, so a window of any other shape
    #: either letterboxes it or stretches it -- and the strip geometry is
    #: stored as *fractions of the client area*, so either one silently moves
    #: the softkey strip out from under everybody's calibration. A bad value
    #: therefore falls back to the default rather than being taken literally.
    size: tuple[int, int] = DEFAULT_WINDOW_SIZE

    def __post_init__(self) -> None:
        try:
            width, height = self.size
            width, height = int(width), int(height)
        except (TypeError, ValueError):
            LOG.warning(
                "window_management.size must be [width, height], got %r -- using %dx%d",
                self.size, *DEFAULT_WINDOW_SIZE,
            )
            object.__setattr__(self, "size", DEFAULT_WINDOW_SIZE)
            return
        if width <= 0 or height <= 0 or width * 3 != height * 4:
            LOG.warning(
                "window_management.size %dx%d is not 4:3, which is the shape the G1000 "
                "panel is drawn in -- using %dx%d instead. Multiply the height by 4/3 "
                "for a size that will be used as written.",
                width, height, *DEFAULT_WINDOW_SIZE,
            )
            object.__setattr__(self, "size", DEFAULT_WINDOW_SIZE)
        else:
            object.__setattr__(self, "size", (width, height))


@dataclass(frozen=True)
class PublishConfig:
    target: str = "webapi"  # websocket | webapi | console
    base_url: str = "http://localhost:8086"
    #: Which version of X-Plane's Web API to talk -- a *floor*, used only when
    #: the sim does not say. Both publishers ask the unversioned
    #: /api/capabilities endpoint first and take the highest version it
    #: advertises, so this value is what is left when that endpoint cannot be
    #: reached at all.
    #:
    #: "v1" because an X-Plane that does not answer /api/capabilities is an
    #: old one, and v1 is the version every release with a Web API has served.
    #: Raising the floor could only affect a sim too old to have been asked,
    #: which is exactly the sim that would not understand a newer version.
    api_version: str = "v1"
    #: Bytes per label dataref -> the PilotsDeck address suffix (':s16').
    #:
    #: The width is fixed in three places that have to agree -- FIELD_WIDTH in
    #: the plugin (an X-Plane restart, since the buffer is allocated when the
    #: accessor is registered), this setting, and every PilotsDeck button
    #: address -- so the cost of changing it is paid by the user re-editing
    #: every button.
    #:
    #: 15 usable bytes plus the NUL. A test walks every label in labels.txt
    #: against this value, so a vocabulary that outgrows the field fails there
    #: rather than on a button.
    field_width: int = 16
    timeout: float = 1.0
    #: seconds between reconnect attempts when X-Plane is not answering
    retry_interval: float = 5.0


@dataclass(frozen=True)
class AppConfig:
    loop_hz: float = 28.0
    change_gating: bool = True
    change_tolerance: int = 6
    displays: tuple[DisplayConfig, ...] = ()
    ocr: OcrConfig = field(default_factory=OcrConfig)
    color: ColorConfig = field(default_factory=ColorConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    window_management: WindowManagementConfig = field(default_factory=WindowManagementConfig)

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
        raise ConfigNotFound(f"config file not found: {p}")
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
    window_data = _subsection(raw, "window_management")
    if window_data.get("size") is not None:
        # Coerced here rather than trusted: TOML hands back a list, and the
        # dataclass wants a tuple so it stays hashable. An outright malformed
        # value falls back in __post_init__ along with a wrong aspect ratio,
        # so this only has to turn a well-formed pair into a tuple.
        size = window_data["size"]
        if isinstance(size, (list, tuple)) and len(size) == 2:
            window_data["size"] = tuple(size)

    if base_dir is not None and ocr_data.get("screens_file"):
        screens = Path(ocr_data["screens_file"])
        if not screens.is_absolute():
            ocr_data["screens_file"] = str((base_dir / screens).resolve())
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
        displays.append(_build(DisplayConfig, entry))

    config = AppConfig(
        displays=tuple(displays) if displays else default_displays(),
        ocr=_build(OcrConfig, ocr_data),
        color=_build(ColorConfig, color_data),
        publish=_build(PublishConfig, publish_data),
        window_management=_build(WindowManagementConfig, window_data),
        **{k: v for k, v in app_data.items() if k in {"loop_hz", "change_gating", "change_tolerance"}},
    )
    if config.loop_hz <= 0:
        raise ConfigError("app.loop_hz must be > 0")
    config.color.validate()
    return config
