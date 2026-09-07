# GlassLinkXP

Puts the **live** G1000 softkey labels on a Stream Deck.

X-Plane 12 draws the G1000's softkey labels straight to the screen and offers
no dataref for them, so a Stream Deck can send `sim/GPS/g1000n1_softkey1..12`
but can only show a static "BTN 1..12" face. GlassLinkXP reads the labels back
out of the picture and republishes them as X-Plane datarefs, so a Stream Deck
button (via PilotsDeck) can show what the key actually does.

> **This is experimental.** It has been run against a live X-Plane and
> publishes real datarefs, but large parts of the Windows/sim integration are
> lightly exercised so far. Expect rough edges, and see
> [`docs/DEVELOPER.md`](docs/DEVELOPER.md#verified-offline) for exactly
> what has and has not been checked.

## Install

Requires Windows and X-Plane 12.1.1+ (for its web API).

1. Download the zip and extract it anywhere.
2. Double-click **`install.cmd`** in the extracted folder.

   It copies GlassLinkXP into `%appdata%\GlassLinkXP`, downloads its Python
   environment and OCR language data, adds a **GlassLinkXP** shortcut to your
   desktop, and offers to install the small X-Plane plugin GlassLinkXP
   publishes into (say yes -- without it there is nowhere for the labels to
   go).

   Running the installer again replaces an existing install after asking
   first; there is no in-place update yet.

## First run

Start X-Plane with your aircraft **on the ground**, then open GlassLinkXP
from the desktop shortcut. With no configuration yet, it opens on a
walkthrough that covers, in order:

1. Popping the PFD and MFD out into their own windows.
2. Telling GlassLinkXP which windows those are.
3. Calibrating: drawing a box around the softkey strip on a captured picture.
4. Checking that the reader is looking at clean text.
5. Fixing a label that reads wrong, if one does, with the sharpening tuner.
6. Watching the labels locally before wiring anything to X-Plane.

Every step has a button straight to the tab that does it. If you have no
X-Plane to hand yet, the same tab can generate sample pictures so you can see
the whole thing work first.

## Wiring a PilotsDeck button

For PFD softkey 1:

| Field | Value |
| --- | --- |
| Command (press) | `sim/GPS/g1000n1_softkey1` |
| Display value | `glasslinkxp/softkey/pfd/1:s64` |

`:s64` is PilotsDeck's string-dataref address syntax: read 64 bytes as a
NUL-terminated string.

Alongside each label, GlassLinkXP also publishes the colour of the cell the
label sits on, as an int dataref:

| dataref | value |
| --- | --- |
| `glasslinkxp/softkey/pfd/1/bg` | `0` black, `1` white (selected/inverted), `2` yellow, `3` red |

Use it to pick the button image or background. Check the classification
against your own display with the Colours tab (or `glasslinkxp dump-colors`)
before relying on it -- the shipped thresholds came from plausible swatches,
not a real capture.

Softkey N maps to `sim/GPS/g1000n1_softkeyN` (pilot PFD) and
`sim/GPS/g1000n3_softkeyN` (MFD); `g1000n2` is the copilot PFD and is not
supported.

## Configuration

`docs/CONFIGURATION.md` documents every setting; the Settings tab in the
window shows the same text beside each field. `src/config.example.toml` is a
commented starting point if you would rather edit `config.toml` by hand.

`labels.txt` (Vocabulary tab) is the list of labels a reading is corrected
to. It is aircraft and G1000 version dependent -- add anything your setup
shows that is missing.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `no visible window title contains ...` | The display is not popped out, or the title differs. Use Find windows. |
| Find windows lists nothing | Only X-Plane's own windows are listed by default. Tick *Show every window*. |
| A pop-out ends up smaller than expected | X-Plane enforces a minimum size; the log says what it settled at. Recalibrate against that size. |
| Labels froze and stopped following the sim | The pop-out was closed. It is reopened automatically within a cycle unless window management is off. |
| `could not initialise Tesseract` | The OCR language data was not found. Re-run `install.cmd`, or point `ocr.tessdata_path` at the folder holding `eng.traineddata`. |
| `X-Plane Web API unreachable` | X-Plane is not running, is older than 12.1.1, or its web server is off (Settings -> Network). |
| `N of 48 datarefs are not registered in X-Plane` | The X-Plane plugin is not installed, or failed to load. Check `Log.txt` and `XPPython3.log` in the X-Plane folder, or re-run `src\scripts\install-xplane-plugin.ps1` from your install folder. |
| Labels are garbage or empty | Almost always geometry. Open the Calibrate tab and check the boxes, or the Cells tab to see exactly what is being read. |
| One cell is always wrong | A missing entry in `labels.txt`, or a label the sim draws on two lines (not supported -- see known limitations). |
| The window says "this Python has no Tk support" | Re-run the installer; the command line still works without it. |

Run any command with `-v` for debug logging (per-cell raw OCR strings,
confidences and match scores).

## More

`docs/PIPELINE.md` has flowcharts of the daemon loop and of what happens to a
single frame. `docs/GUI.md` covers the window. `docs/DEVELOPER.md` has the
project layout, how to work on it without Windows or X-Plane, latency
numbers, known limitations, and exactly what has and has not been verified.
