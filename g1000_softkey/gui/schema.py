"""Every configuration setting, described once, for the Settings form.

The Settings tab is generated from this table rather than laid out by hand.
That is not only less code: it is the only way the form can be kept honest.
A setting added to ``config.py`` and forgotten here would simply not appear
in the GUI, and the user would have no way of knowing there was anything to
miss -- so ``tests/test_gui_schema.py`` walks the config dataclasses and fails
if a field is neither described here nor in :data:`NOT_IN_THE_FORM` with a
reason.

The help strings are the ones from ``config.example.toml``, shortened. They
are the point of the tab: someone who is not going to read a TOML file still
has to be told what ``blank_contrast`` does before they can sensibly move it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import (
    AppConfig,
    ColorConfig,
    DisplayConfig,
    OcrConfig,
    PublishConfig,
    StripGeometry,
)

#: Fields the form deliberately does not show, and why. Read by the coverage
#: test, so a field cannot be dropped from the GUI silently -- only on purpose.
NOT_IN_THE_FORM: dict[str, str] = {
    "DisplayConfig.key": "the display's name is the TOML table name, edited with the tab itself",
    "DisplayConfig.geometry": "a subsection, shown as its own group of fields",
    "AppConfig.displays": "the list of displays, shown as one tab per display",
    "AppConfig.ocr": "a subsection",
    "AppConfig.color": "a subsection",
    "AppConfig.publish": "a subsection",
}


@dataclass(frozen=True)
class Setting:
    """One editable setting."""

    key: str
    kind: str  # bool | int | float | text | choice | path | toml
    label: str
    help: str = ""
    choices: tuple[str, ...] = ()
    #: An empty box means "not set", rather than an empty string. Used for the
    #: settings whose absence is meaningful -- an unset tessdata_path means
    #: "ask Tesseract", which is different from setting it to "".
    optional: bool = False
    #: The built-in default is a file *inside the installed package*, so it is
    #: an absolute path into whichever checkout happens to be running. Writing
    #: that path into config.toml pins the configuration to one install: move
    #: it, or copy the file to another machine, and the daemon points at a
    #: file that is not there any more. The GUI therefore never writes these
    #: out at their default; an absent key means "the copy that ships with the
    #: package", which is how config.example.toml leaves them, and the form
    #: shows the current one as a hint beside an empty box rather than as a
    #: value in it. Implies :attr:`optional`.
    package_default: bool = False

    def __post_init__(self) -> None:
        if self.kind not in ("bool", "int", "float", "text", "choice", "path", "toml"):
            raise ValueError(f"{self.key}: unknown kind {self.kind!r}")
        if self.kind == "choice" and not self.choices:
            raise ValueError(f"{self.key}: a choice needs choices")
        if self.package_default and not self.optional:
            raise ValueError(
                f"{self.key}: a package default has to be optional -- an empty box is "
                "the only way back to letting the package decide"
            )


@dataclass(frozen=True)
class Group:
    """A titled block of settings in the form."""

    section: str  # the TOML table: app | ocr | color | publish | display | geometry
    title: str
    blurb: str
    settings: tuple[Setting, ...]


APP = Group(
    "app", "Loop",
    "How often the softkey strip is read. Softkeys only change when you press one, "
    "so this mostly sets the worst case delay between a press and the Stream Deck "
    "catching up.",
    (
        Setting("loop_hz", "float", "Rate (Hz)",
                "Captures per second. 12 Hz keeps the worst case under about 85 ms. "
                "With change gating on, an unchanged cycle costs a fraction of a "
                "millisecond, so a high rate is nearly free."),
        Setting("change_gating", "bool", "Skip unchanged cells",
                "Compare each cell against the previous frame and only re-read the ones "
                "whose pixels moved. In steady state that means no OCR at all. Leave it on."),
        Setting("change_tolerance", "int", "Change tolerance",
                "How much a pixel has to move (0-255) to count as changed. Too low and "
                "anti-aliasing noise re-reads every frame; too high and a real change is missed."),
    ),
)

DISPLAY = Group(
    "display", "Window",
    "Which window this display is captured from.",
    (
        Setting("enabled", "bool", "Use this display",
                "Turn off to ignore this display entirely -- for instance if you only "
                "fly with the PFD popped out."),
        Setting("window_title", "text", "Window title contains",
                "A distinctive part of the pop-out window's title, matched case "
                "insensitively. Use the Find windows tab to see the real titles."),
        Setting("dataref_prefix", "path", "Dataref prefix",
                "Where the labels are published. Leave empty for g1000/softkey/<name>. "
                "Changing it means re-editing every Stream Deck button.", optional=True),
        Setting("manage_window_size", "bool", "Let the daemon resize this window",
                "Off by default, and deliberately: a pop-out may be feeding external "
                "avionics hardware where its size and position are part of a physical "
                "setup, and breaking that to make OCR marginally easier is not a trade "
                "to make silently."),
        Setting("window_size", "toml", "Resize to",
                "The client size to force the window to, as [width, height]. Only used "
                "when the box above is ticked. The G1000 renders to a 1024x768 texture, "
                "so a smaller display area throws detail away before capture sees it -- "
                "but the pop-out includes the bezel, so the window has to be bigger than "
                "that. Find the number with one calibration pass.", optional=True),
    ),
)

GEOMETRY = Group(
    "geometry", "Softkey strip position",
    "Where the strip sits inside the window, as fractions of the window (0-1) so it "
    "survives a resize. Set these from the Calibrate tab rather than by hand: the "
    "overlay picture shows you immediately whether each box sits around exactly one label.",
    (
        Setting("x", "float", "Left edge", "Fraction across the window where the strip starts."),
        Setting("y", "float", "Top edge", "Fraction down the window where the strip starts."),
        Setting("w", "float", "Width", "Fraction of the window width the strip covers."),
        Setting("h", "float", "Height", "Fraction of the window height the strip covers."),
        Setting("cells", "int", "Cells", "How many softkeys the strip is split into. The G1000 has 12."),
        Setting("cell_pad_x", "float", "Side trim",
                "Fraction of each cell trimmed off the left and right before reading, to "
                "keep the neighbouring cell's label out of the crop."),
        Setting("cell_pad_y", "float", "Top/bottom trim",
                "Fraction of the strip height trimmed off the top and bottom."),
    ),
)

OCR = Group(
    "ocr", "Reading the labels",
    "How the cropped cells are turned into text. The glyphs are only about 10 pixels "
    "tall, so most of this is about giving Tesseract a fair chance at them -- and about "
    "not trusting it too far when it fails.",
    (
        Setting("engine", "choice", "Engine",
                "auto prefers tesserocr, which keeps one Tesseract instance alive. "
                "pytesseract shells out per cell and is roughly ten times slower.",
                choices=("auto", "tesserocr", "pytesseract")),
        Setting("lang", "text", "Language", "Tesseract language data to use."),
        Setting("tessdata_path", "path", "Tessdata folder",
                "The folder holding eng.traineddata. Leave empty to let Tesseract find "
                "it, or set TESSDATA_PREFIX instead.", optional=True),
        Setting("psm", "int", "Page segmentation mode",
                "Tesseract's layout mode. 7 means 'a single line of text', which is what "
                "a softkey label is. Note this cannot read a label the sim wraps onto two lines."),
        Setting("whitelist", "text", "Allowed characters",
                "Characters Tesseract may return. Restricting it is most of why the "
                "readings are as good as they are."),
        Setting("upscale", "float", "Upscale",
                "How much each cell is enlarged before reading. Tesseract wants roughly a "
                "30 pixel cap height: raise this for a small pop-out, lower it for a 4K one."),
        Setting("sharpen_ladder", "toml", "Sharpening ladder",
                "Unsharp mask settings tried in order, as [amount, radius] pairs. "
                "Thresholding a 10 pixel glyph can close the counters of 0, 6, 8 and 9, "
                "and a filled counter is not a character; sharpening reopens them, but too "
                "much rings and turns a 0 into a B. So rather than fix one value, each rung "
                "is tried and the answer the vocabulary agrees with is kept. The first rung "
                "is no sharpening at all, so this can never do worse than not trying."),
        Setting("threshold", "choice", "Threshold method",
                "How each cell is turned black and white. Applied per cell, never globally.",
                choices=("otsu", "adaptive")),
        Setting("accept_confidence", "float", "Accept confidence",
                "A vocabulary hit at least this confident stops the sharpening ladder "
                "early. Landing on a known label is not proof of being right when every "
                "digit 0-7 is a valid softkey, so below this the remaining rungs are still "
                "tried. 0 always tries every rung."),
        Setting("screen_confidence", "float", "Page lookup below",
                "Below this confidence, a cell may be filled in from a known softkey page. "
                "Recognising a 10 pixel digit is hard; recognising which page is showing, "
                "from the labels that did read cleanly, is easy -- and the page says what "
                "the hard cell must be. 0 turns page lookup off."),
        Setting("screen_match_confidence", "float", "Identify a page above",
                "A page is only recognised when its identifying cells all read at least "
                "this confidently. Higher than the setting above on purpose: an "
                "identification made from a guess would spread that guess into every cell "
                "it fills."),
        Setting("screens_file", "path", "Pages file",
                "The known softkey pages. Add one with the Screen template tab. Leave it "
                "empty to use the file that ships with the package, which is what the hint "
                "beside the box names -- set it only to point at a file of your own.",
                optional=True, package_default=True),
        Setting("labels_file", "path", "Vocabulary file",
                "The list of labels a reading is snapped to. Edit it in the Vocabulary tab; "
                "it is aircraft and version dependent. Leave it empty to use the file that "
                "ships with the package.",
                optional=True, package_default=True),
        Setting("fuzzy_cutoff", "float", "Snap cutoff",
                "How close a raw reading has to be to a known label before it is corrected "
                "to it. Lower corrects more, and snaps to the wrong label more."),
        Setting("blank_ink_ratio", "float", "Blank below",
                "What fraction of a cell has to be ink before the cell counts as having a "
                "label on it at all."),
        Setting("blank_contrast", "int", "Ink contrast",
                "How far a pixel has to sit from the cell's dominant tone to count as ink. "
                "The G1000 dims unavailable softkeys rather than hiding them, so too high a "
                "value reads a dim but present label as an empty cell -- and short labels "
                "break first. Lower it to about 20 if dim labels are being dropped."),
    ),
)

COLOR = Group(
    "color", "Background colour",
    "Each cell's background is classified black / white / yellow / red and published "
    "next to the label, so a button can show that a softkey is selected or that the "
    "sim is warning about something. THESE DEFAULTS HAVE NEVER BEEN CHECKED AGAINST A "
    "REAL G1000 FRAME -- they came from plausible swatches. Run the Colours tab against "
    "your own display and move them to fit what it prints.",
    (
        Setting("enabled", "bool", "Classify backgrounds", "Turn off to publish labels only."),
        Setting("ring_fraction", "float", "Sample ring",
                "The outermost fraction of each cell, per side, used to sample the "
                "background. Labels are centred, so this ring is essentially never glyph; "
                "too large and it starts eating into the text."),
        Setting("value_max", "int", "Black above brightness",
                "Brightness (V) at or below this is black, whatever the hue. Tested first, "
                "so dimming the display can only ever push a cell towards black and can "
                "never turn a yellow into a red. Raise it if a black cell reads as coloured; "
                "lower it if a dim caution reads as black."),
        Setting("saturation_max", "int", "White below saturation",
                "Saturation at or below this is colourless, so the cell is white -- "
                "brightness has already ruled out black."),
        Setting("red_hue_max", "int", "Red hue, low end",
                "Hue is OpenCV's 0-179, not 0-359, and red wraps around 0: a hue at or "
                "below this counts as red."),
        Setting("red_hue_wrap_min", "int", "Red hue, wrapped end",
                "A hue at or above this also counts as red, which is the other side of "
                "the wrap."),
        Setting("yellow_hue_min", "int", "Yellow hue, from", "Start of the yellow window."),
        Setting("yellow_hue_max", "int", "Yellow hue, to", "End of the yellow window."),
    ),
)

PUBLISH = Group(
    "publish", "Publishing",
    "Where the labels are sent. Watch them with 'console' first; switch to websocket "
    "once they look right.",
    (
        Setting("target", "choice", "Publish to",
                "websocket is the normal path: one message per cycle. webapi sends an "
                "HTTP request per changed cell; both write the same datarefs. console "
                "just prints.",
                choices=("websocket", "webapi", "console")),
        Setting("base_url", "text", "X-Plane web address",
                "Where X-Plane serves its web API. Enable it in Settings -> Network if it "
                "does not answer."),
        Setting("api_version", "choice", "API version",
                "v1 works on X-Plane 12.1.1 and later; v2 from 12.1.4.",
                choices=("v1", "v2")),
        Setting("field_width", "int", "Label field width",
                "Bytes per label dataref. THIS IS FIXED IN THREE PLACES THAT MUST AGREE: "
                "here, FIELD_WIDTH in the X-Plane plugin (which needs a sim restart), and "
                "the ':s64' on every Stream Deck button. Changing it means re-editing every "
                "button, so it is set generously once rather than tuned."),
        Setting("timeout", "float", "Timeout (s)", "How long to wait for X-Plane to answer."),
        Setting("retry_interval", "float", "Retry every (s)",
                "How long to wait between reconnection attempts when X-Plane is not answering."),
    ),
)

#: Ordered as the form shows them.
GROUPS: tuple[Group, ...] = (APP, DISPLAY, GEOMETRY, OCR, COLOR, PUBLISH)

#: Which config dataclass each group describes, and the TOML table it lives
#: in. Kept here rather than in the test that checks the coverage, so the
#: form, the reference documentation and that test all read it from one place.
SECTION_CLASSES: dict[str, tuple[type, str]] = {
    "app": (AppConfig, "[app]"),
    "display": (DisplayConfig, "[display.<name>]"),
    "geometry": (StripGeometry, "[display.<name>.geometry]"),
    "ocr": (OcrConfig, "[ocr]"),
    "color": (ColorConfig, "[color]"),
    "publish": (PublishConfig, "[publish]"),
}

BY_SECTION: dict[str, Group] = {group.section: group for group in GROUPS}


def setting(section: str, key: str) -> Setting:
    for item in BY_SECTION[section].settings:
        if item.key == key:
            return item
    raise KeyError(f"no setting {section}.{key}")


def kind_of(section: str, key: str) -> str:
    """The declared kind, or "" when the setting is not one we describe."""
    group = BY_SECTION.get(section)
    if group is None:
        return ""
    for item in group.settings:
        if item.key == key:
            return item.kind
    return ""


# ---------------------------------------------------------------------------
# the reference documentation
# ---------------------------------------------------------------------------

#: How each kind is described to somebody reading the reference rather than
#: filling in the form.
_KINDS = {
    "bool": "true or false",
    "int": "a whole number",
    "float": "a number",
    "text": "text",
    "path": "text (a path or a name)",
    "choice": "one of",
    "toml": "a TOML value",
}

DOC_HEADER = """\
# Configuration reference

Every setting in `config.toml`, what it does, and why its default is what it
is. The GUI's Settings tab shows the same text beside each field.

`config.toml` itself carries no comments: the GUI rewrites the whole file when
you save from the form, so anything written in there would be lost the first
time somebody pressed a button. This file is where the reasoning lives
instead. `config.example.toml` is a starting point to copy; a missing config
file is not an error, and every setting below has a working default.

> Generated from `g1000_softkey/gui/schema.py`, which is also what the
> Settings form is built from -- so the form and this document cannot say
> different things. Regenerate with:
>
> ```
> python -m g1000_softkey.gui.schema > docs/CONFIGURATION.md
> ```
"""


def default_value(section: str, setting: "Setting") -> Any:
    """What the daemon uses for this setting when the config file is silent.

    Read off the config dataclass rather than repeated here, so the form's
    hints, this document and the daemon cannot disagree about a default.
    """
    cls, _table = SECTION_CLASSES[section]
    instance = cls(key="<name>") if cls is DisplayConfig else cls()
    return getattr(instance, setting.key)


def _default_for(section: str, setting: "Setting") -> str:
    value = default_value(section, setting)
    if value is None:
        return "not set"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, tuple):
        return "`" + str([list(v) if isinstance(v, tuple) else v for v in value]) + "`"
    return f"`{value!r}`"


def as_markdown() -> str:
    """The reference documentation for every setting."""
    out = [DOC_HEADER]
    for group in GROUPS:
        _cls, table = SECTION_CLASSES[group.section]
        out.append(f"\n## {group.title} — `{table}`\n")
        out.append(group.blurb + "\n")
        for setting in group.settings:
            out.append(f"### `{setting.key}`\n")
            kind = _KINDS.get(setting.kind, setting.kind)
            if setting.choices:
                kind += " " + ", ".join(f"`{c}`" for c in setting.choices)
            note = f"{kind}. Default: {_default_for(group.section, setting)}."
            if setting.package_default:
                note += (" Leave it out to use the copy that comes with the package -- "
                         "the default above is a path into this install, so writing it "
                         "into your config file would tie the file to it.")
            elif setting.optional:
                note += " Leave it out to leave it unset."
            out.append(f"*{setting.label}* — {note}\n")
            out.append(setting.help + "\n")
    return "\n".join(out).rstrip() + "\n"


if __name__ == "__main__":  # pragma: no cover - a one-line regeneration
    print(as_markdown(), end="")
