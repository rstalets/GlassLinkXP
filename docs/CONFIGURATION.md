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


## Loop — `[app]`

How often the softkey strip is read. Softkeys only change when you press one, so this mostly sets the worst case delay between a press and the Stream Deck catching up.

### `loop_hz`

*Rate (Hz)* — a number. Default: `12.0`.

Captures per second. 12 Hz keeps the worst case under about 85 ms. With change gating on, an unchanged cycle costs a fraction of a millisecond, so a high rate is nearly free.

### `change_gating`

*Skip unchanged cells* — true or false. Default: `true`.

Compare each cell against the previous frame and only re-read the ones whose pixels moved. In steady state that means no OCR at all. Leave it on.

### `change_tolerance`

*Change tolerance* — a whole number. Default: `6`.

How much a pixel has to move (0-255) to count as changed. Too low and anti-aliasing noise re-reads every frame; too high and a real change is missed.


## Window — `[display.<name>]`

Which window this display is captured from.

### `enabled`

*Use this display* — true or false. Default: `true`.

Turn off to ignore this display entirely -- for instance if you only fly with the PFD popped out.

### `window_title`

*Window title contains* — text. Default: `''`.

A distinctive part of the pop-out window's title, matched case insensitively. Use the Find windows tab to see the real titles.

### `dataref_prefix`

*Dataref prefix* — text (a path or a name). Default: `'g1000/softkey/<name>'`. Leave it out to leave it unset.

Where the labels are published. Leave empty for g1000/softkey/<name>. Changing it means re-editing every Stream Deck button.

### `manage_window_size`

*Let the daemon resize this window* — true or false. Default: `false`.

Off by default, and deliberately: a pop-out may be feeding external avionics hardware where its size and position are part of a physical setup, and breaking that to make OCR marginally easier is not a trade to make silently.

### `window_size`

*Resize to* — a TOML value. Default: not set. Leave it out to leave it unset.

The client size to force the window to, as [width, height]. Only used when the box above is ticked. The G1000 renders to a 1024x768 texture, so a smaller display area throws detail away before capture sees it -- but the pop-out includes the bezel, so the window has to be bigger than that. Find the number with one calibration pass.


## Softkey strip position — `[display.<name>.geometry]`

Where the strip sits inside the window, as fractions of the window (0-1) so it survives a resize. Set these from the Calibrate tab rather than by hand: the overlay picture shows you immediately whether each box sits around exactly one label.

### `x`

*Left edge* — a number. Default: `0.05`.

Fraction across the window where the strip starts.

### `y`

*Top edge* — a number. Default: `0.915`.

Fraction down the window where the strip starts.

### `w`

*Width* — a number. Default: `0.9`.

Fraction of the window width the strip covers.

### `h`

*Height* — a number. Default: `0.055`.

Fraction of the window height the strip covers.

### `cells`

*Cells* — a whole number. Default: `12`.

How many softkeys the strip is split into. The G1000 has 12.

### `cell_pad_x`

*Side trim* — a number. Default: `0.1`.

Fraction of each cell trimmed off the left and right before reading, to keep the neighbouring cell's label out of the crop.

### `cell_pad_y`

*Top/bottom trim* — a number. Default: `0.12`.

Fraction of the strip height trimmed off the top and bottom.


## Reading the labels — `[ocr]`

How the cropped cells are turned into text. The glyphs are only about 10 pixels tall, so most of this is about giving Tesseract a fair chance at them -- and about not trusting it too far when it fails.

### `lang`

*Language* — text. Default: `'eng'`.

Tesseract language data to use.

### `tessdata_path`

*Tessdata folder* — text (a path or a name). Default: not set. Leave it out to leave it unset.

The folder holding eng.traineddata. Leave empty to let Tesseract find it, or set TESSDATA_PREFIX instead.

### `psm`

*Page segmentation mode* — a whole number. Default: `7`.

Tesseract's layout mode. 7 means 'a single line of text', which is what a softkey label is. Note this cannot read a label the sim wraps onto two lines.

### `whitelist`

*Allowed characters* — text. Default: `'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -/'`.

Characters Tesseract may return. Restricting it is most of why the readings are as good as they are.

### `upscale`

*Upscale* — a number. Default: `4.0`.

How much each cell is enlarged before reading. Tesseract wants roughly a 30 pixel cap height: raise this for a small pop-out, lower it for a 4K one.

### `sharpen_ladder`

*Sharpening ladder* — a TOML value. Default: `[[0.0, 0.0], [0.5, 1.0], [1.0, 1.4]]`.

Unsharp mask settings tried in order, as [amount, radius] pairs. Thresholding a 10 pixel glyph can close the counters of 0, 6, 8 and 9, and a filled counter is not a character; sharpening reopens them, but too much rings and turns a 0 into a B. So rather than fix one value, each rung is tried and the answer the vocabulary agrees with is kept. The first rung is no sharpening at all, so this can never do worse than not trying.

### `threshold`

*Threshold method* — one of `otsu`, `adaptive`. Default: `'otsu'`.

How each cell is turned black and white. Applied per cell, never globally.

### `accept_confidence`

*Accept confidence* — a number. Default: `80.0`.

A vocabulary hit at least this confident stops the sharpening ladder early. Landing on a known label is not proof of being right when every digit 0-7 is a valid softkey, so below this the remaining rungs are still tried. 0 always tries every rung.

### `screen_confidence`

*Page lookup below* — a number. Default: `80.0`.

Below this confidence, a cell may be filled in from a known softkey page. Recognising a 10 pixel digit is hard; recognising which page is showing, from the labels that did read cleanly, is easy -- and the page says what the hard cell must be. 0 turns page lookup off.

### `screen_match_confidence`

*Identify a page above* — a number. Default: `85.0`.

A page is only recognised when its identifying cells all read at least this confidently. Higher than the setting above on purpose: an identification made from a guess would spread that guess into every cell it fills.

### `screens_file`

*Pages file* — text (a path or a name). Default: `'/home/user/g1000-softkey/g1000_softkey/screens.toml'`. Leave it out to use the copy that comes with the package -- the default above is a path into this install, so writing it into your config file would tie the file to it.

The known softkey pages. Add one with the Screen template tab. Set this only to point at a file of your own.

### `labels_file`

*Vocabulary file* — text (a path or a name). Default: `'/home/user/g1000-softkey/g1000_softkey/labels.txt'`. Leave it out to use the copy that comes with the package -- the default above is a path into this install, so writing it into your config file would tie the file to it.

The list of labels a reading is snapped to. Edit it in the Vocabulary tab; it is aircraft and version dependent.

### `fuzzy_cutoff`

*Snap cutoff* — a number. Default: `0.62`.

How close a raw reading has to be to a known label before it is corrected to it. Lower corrects more, and snaps to the wrong label more.

### `blank_ink_ratio`

*Blank below* — a number. Default: `0.004`.

What fraction of a cell has to be ink before the cell counts as having a label on it at all.

### `blank_contrast`

*Ink contrast* — a whole number. Default: `40`.

How far a pixel has to sit from the cell's dominant tone to count as ink. The G1000 dims unavailable softkeys rather than hiding them, so too high a value reads a dim but present label as an empty cell -- and short labels break first. Lower it to about 20 if dim labels are being dropped.


## Background colour — `[color]`

Each cell's background is classified black / white / yellow / red and published next to the label, so a button can show that a softkey is selected or that the sim is warning about something. THESE DEFAULTS HAVE NEVER BEEN CHECKED AGAINST A REAL G1000 FRAME -- they came from plausible swatches. Run the Colours tab against your own display and move them to fit what it prints.

### `enabled`

*Classify backgrounds* — true or false. Default: `true`.

Turn off to publish labels only.

### `ring_fraction`

*Sample ring* — a number. Default: `0.15`.

The outermost fraction of each cell, per side, used to sample the background. Labels are centred, so this ring is essentially never glyph; too large and it starts eating into the text.

### `value_max`

*Black above brightness* — a whole number. Default: `60`.

Brightness (V) at or below this is black, whatever the hue. Tested first, so dimming the display can only ever push a cell towards black and can never turn a yellow into a red. Raise it if a black cell reads as coloured; lower it if a dim caution reads as black.

### `saturation_max`

*White below saturation* — a whole number. Default: `60`.

Saturation at or below this is colourless, so the cell is white -- brightness has already ruled out black.

### `red_hue_max`

*Red hue, low end* — a whole number. Default: `8`.

Hue is OpenCV's 0-179, not 0-359, and red wraps around 0: a hue at or below this counts as red.

### `red_hue_wrap_min`

*Red hue, wrapped end* — a whole number. Default: `172`.

A hue at or above this also counts as red, which is the other side of the wrap.

### `yellow_hue_min`

*Yellow hue, from* — a whole number. Default: `18`.

Start of the yellow window.

### `yellow_hue_max`

*Yellow hue, to* — a whole number. Default: `40`.

End of the yellow window.


## Publishing — `[publish]`

Where the labels are sent. Watch them with 'console' first; switch to websocket once they look right.

### `target`

*Publish to* — one of `websocket`, `webapi`, `console`. Default: `'webapi'`.

websocket is the normal path: one message per cycle. webapi sends an HTTP request per changed cell; both write the same datarefs. console just prints.

### `base_url`

*X-Plane web address* — text. Default: `'http://localhost:8086'`.

Where X-Plane serves its web API. Enable it in Settings -> Network if it does not answer.

### `api_version`

*API version* — one of `v1`, `v2`, `v3`. Default: `'v1'`.

Only a fallback. The daemon asks X-Plane which API versions it serves and uses the newest one; this is what it falls back to when that question goes unanswered, which means a sim too old to answer it. Leave it at v1.

### `field_width`

*Label field width* — a whole number. Default: `64`.

Bytes per label dataref. THIS IS FIXED IN THREE PLACES THAT MUST AGREE: here, FIELD_WIDTH in the X-Plane plugin (which needs a sim restart), and the ':s64' on every Stream Deck button. Changing it means re-editing every button, so it is set generously once rather than tuned.

### `timeout`

*Timeout (s)* — a number. Default: `1.0`.

How long to wait for X-Plane to answer.

### `retry_interval`

*Retry every (s)* — a number. Default: `5.0`.

How long to wait between reconnection attempts when X-Plane is not answering.
