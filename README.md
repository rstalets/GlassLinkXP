# g1000-softkey

Put the **live** G1000 softkey labels on your Stream Deck.

X-Plane 12 computes the G1000 softkey labels inside the engine and rasterizes
them straight to the GPU, so there is no dataref to read: a Stream Deck can
send `sim/GPS/g1000n1_softkey1..12` but can only show a static "BTN 1..12"
face. This proof of concept reads the labels back out of the pixels:

```
X-Plane pop-out PFD/MFD windows (may be occluded)
    -> Windows Graphics Capture              capture.py
    -> crop the softkey strip, split into 12 cells, threshold each cell   strip.py
    -> Tesseract (persistent API, PSM 7) + fuzzy snap to labels.txt       ocr.py
    -> X-Plane Web API PATCH (base64) into plugin-created datarefs        publish.py
    -> PilotsDeck reads g1000/softkey/pfd/1:s16
```

See `PLAN.md` for the design rationale. **This is a POC**: it is verified
offline against synthetic frames (see *Not verified here* at the bottom) and
has not been run against a live X-Plane.

## Layout

```
g1000_softkey/
  main.py       CLI: run | list-windows | calibrate | dump-cells | bench | synth
  capture.py    WGC backend (Windows) + PNG backend (offline dev/test)
  strip.py      strip crop, 12-cell split, per-cell preprocessing, auto-detect
  ocr.py        persistent Tesseract API, char whitelist, vocabulary snapping
  pipeline.py   frame -> cells -> change gating -> labels
  publish.py    X-Plane Web API client, JSON-file fallback, console output
  synth.py      synthetic G1000 softkey frames for offline work
  config.example.toml
  labels.txt    the softkey vocabulary (edit this)
xppython3/PI_G1000SoftkeyLabels.py   creates the 24 datarefs
tests/          offline tests over the whole pipeline
```

## Windows setup

1. **Python 3.11 or 3.12**, 64-bit (`tomllib` needs >= 3.11).
2. **Tesseract**. Install the UB Mannheim build
   (<https://github.com/UB-Mannheim/tesseract/wiki>), let it add
   `C:\Program Files\Tesseract-OCR` to `PATH`, and set
   `TESSDATA_PREFIX=C:\Program Files\Tesseract-OCR\tessdata` (or point
   `ocr.tessdata_path` at that directory in the config).
3. **Python packages**:
   ```
   pip install -r requirements.txt
   ```
   `windows-capture` only installs on Windows; it is the Rust-backed Windows
   Graphics Capture binding. If `tesserocr` refuses to build, install a
   prebuilt wheel (tesserocr-windows_build releases / conda-forge) or fall
   back with `engine = "pytesseract"` in `[ocr]` -- it is roughly an order of
   magnitude slower per cell because it restarts Tesseract for every call.
4. **XPPython3 plugin**: copy `xppython3/PI_G1000SoftkeyLabels.py` into
   `<X-Plane 12>/Resources/plugins/PythonPlugins/` and restart X-Plane. It
   creates 24 writable 16-byte datarefs and does nothing else:
   `g1000/softkey/pfd/1..12` and `g1000/softkey/mfd/1..12`.
   (The Web API can *write* datarefs but cannot *create* them, hence the
   plugin. All the expensive work stays in the standalone daemon so it never
   touches X-Plane's flight-loop thread.)
5. **X-Plane web server**: 12.1.1+ serves the REST API on
   `http://localhost:8086`. Check `http://localhost:8086/api/v1/datarefs`
   in a browser; if it does not answer, enable the web server in
   Settings -> Network.

## Calibration workflow

The strip geometry depends on the pop-out window size and bezel, so it is
expressed as *fractions* of the client area and has to be set once per setup.

1. Pop the PFD and MFD out into their own windows in X-Plane.
2. Find the window titles:
   ```
   python -m g1000_softkey.main list-windows
   ```
   Copy a distinctive substring of each title into `window_title` under
   `[display.pfd]` / `[display.mfd]` in your `config.toml` (copy
   `config.example.toml` to start).
3. Dump the calibration images and a suggested geometry:
   ```
   python -m g1000_softkey.main -c config.toml calibrate --out calibration
   ```
   This writes `<display>_raw.png` (what was captured), `<display>_strip.png`
   (the current crop), `<display>_overlay.png` (crop + numbered cell
   boundaries) and, when the coarse auto-detect finds the dark band at the
   bottom of the frame, `<display>_overlay_auto.png` plus a TOML snippet on
   stdout.
4. Paste the suggested numbers into the config, re-run `calibrate`, and look
   at `<display>_overlay.png`: each green box must sit around exactly one
   label, with no bleed into the neighbouring cell and none of the bezel or
   the moving map inside the box. Nudge `x/y/w/h` and `cell_pad_x/y` until it
   does. **Do not skip this step** -- the auto-detect is only a seed; it gets
   the vertical band right but the horizontal extent only approximately.
5. Check what Tesseract actually sees:
   ```
   python -m g1000_softkey.main -c config.toml dump-cells --out cells
   ```
   `<display>_NN_prep.png` should be black text on a white background, with
   the glyphs roughly 30 px tall, including for the highlighted (selected)
   softkey. If a cell is inverted or the text is clipped, fix the geometry
   before blaming the OCR.
6. Watch the labels live before wiring anything to X-Plane:
   ```
   python -m g1000_softkey.main -c config.toml run --publisher console
   ```
7. Then run for real (`target = "webapi"` in `[publish]`, or `--publisher webapi`):
   ```
   python -m g1000_softkey.main -c config.toml run
   ```

If the Web API refuses to write the plugin's datarefs, use the fallback --
`--publisher file` -- which atomically writes
`%TEMP%\g1000_softkey_labels.json`; the plugin polls that file at 5 Hz and
copies the strings into the same datarefs. Both sides honour the
`G1000_SOFTKEY_JSON` environment variable if you want the file elsewhere.

## Wiring a PilotsDeck button

For PFD softkey 1:

| Field | Value |
| --- | --- |
| Command (press) | `sim/GPS/g1000n1_softkey1` |
| Display value | `g1000/softkey/pfd/1:s16` |

`:s16` is PilotsDeck's string-dataref address syntax: read 16 bytes as a
NUL-terminated string. The daemon writes exactly 16 bytes, NUL padded.

Softkey N maps to `sim/GPS/g1000n1_softkeyN` (pilot PFD) and
`sim/GPS/g1000n3_softkeyN` (MFD); `g1000n2` is the copilot PFD and is out of
scope for this POC.

## Offline development (no X-Plane, no Windows)

Everything downstream of capture is platform independent, and there is a
synthetic frame generator, so the whole pipeline runs anywhere:

```
python -m g1000_softkey.main synth --out frames
python -m g1000_softkey.main run --image frames/pfd_top.png --publisher console --once
python -m g1000_softkey.main bench --image frames/pfd_menu.png -n 50
python -m pytest -q
```

`--image` accepts a PNG or a directory of PNGs (a directory is cycled, one
frame per loop iteration; a file named `<display>.png` in it is used for that
display).

## Tuning

* `app.loop_hz` (default 4) -- how often to capture. Softkeys only change when
  you press one, so this mostly sets worst-case latency (1/4 s + OCR).
* `app.change_gating` (default true) -- compare each cell against the previous
  frame and skip OCR for unchanged cells. In steady state that means zero OCR
  calls per frame.
* `ocr.upscale` (default 3.0) -- Tesseract wants roughly a 30 px cap height.
  Raise it for a small pop-out window, lower it for a 4K one.
* `ocr.fuzzy_cutoff` (default 0.62) -- how close a raw OCR string has to be to
  a `labels.txt` entry before it is snapped. Lower = more aggressive
  correction and more risk of snapping to the wrong label.
* `ocr.blank_ink_ratio` (default 0.004) -- below this fraction of "ink" a cell
  is reported as an empty string instead of being OCR'd.
* `labels.txt` -- the vocabulary. It is version and aircraft dependent; add
  anything your setup shows that is missing. Unknown strings are passed
  through raw (and logged at debug level) rather than being forced onto a
  wrong label.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `no visible window title contains ...` | The display is not popped out, or the title differs. Run `list-windows`. |
| `list-windows` errors on Linux/macOS | Expected; the capture path is Windows-only. Use `--image`. |
| `could not initialise Tesseract` / `tesseract executable was not found` | `TESSDATA_PREFIX` is unset or wrong. Point `ocr.tessdata_path` at the directory holding `eng.traineddata`. |
| `X-Plane Web API unreachable` | X-Plane is not running, is older than 12.1.1, or the web server is off. The daemon keeps retrying; it never crashes the loop. |
| `N of 24 datarefs are not registered in X-Plane` | The XPPython3 plugin is not installed or failed to load. Check `<X-Plane>/Log.txt` and `XPPython3.log`. |
| Labels are garbage or empty | Geometry. Run `dump-cells` and look at the `_prep.png` images. |
| One cell is always wrong | Missing entry in `labels.txt`, or a two-line label (see limitations). |
| Blank cells produce short nonsense strings | The crop includes something bright above or below the strip; tighten `y`/`h`, or raise `ocr.blank_ink_ratio`. |

Run any command with `-v` for debug logging (per-cell raw OCR strings,
confidences and match scores).

## Known limitations

* PSM 7 reads a **single line**. Softkey labels that X-Plane draws on two
  lines will not read correctly; they would need a per-cell line split first.
* The coarse auto-detect finds the vertical band reliably but only
  approximates the horizontal extent; in the offline corpus, auto-detected
  geometry read 59/60 cells correctly against 60/60 for calibrated geometry.
* The selected/highlighted state is detected only implicitly (the per-cell
  threshold handles it); it is not published as a separate dataref.
* One `tesserocr` API instance is shared by both displays and used serially.
  That is fine at 4 Hz; it is not thread safe.

## Verified offline

On Linux/CPython 3.11, Tesseract 5.3.4 (system) with `tesserocr` 2.11:

* 76 tests pass (`python -m pytest -q`), including the full frame -> labels
  pipeline over 5 synthetic softkey menus (60 cells: 49 labels + 11 blanks),
  all read exactly, blanks included, with the highlighted cell read correctly.
* `bench --image frames/pfd_menu.png -n 50` on this container (1280x800
  frames, 10 non-blank cells):

  | stage | gating off | gating on (steady state) |
  | --- | --- | --- |
  | split | 0.20 ms | 0.21 ms |
  | change gate | 0.00 ms | 0.14 ms |
  | preprocess | 4.58 ms | 0.00 ms |
  | OCR | 46.2 ms (10 calls, ~4.6 ms/cell) | 0.00 ms |
  | **pipeline total** | **51.0 ms** | **0.39 ms** |

  So a full re-read of one display costs ~51 ms of CPU, and an unchanged
  frame costs ~0.4 ms. At 4 Hz that is well under the "5% of one core"
  target once the labels are stable. (`capture_ms` in the bench output is PNG
  decode for the offline source, not Windows Graphics Capture.)
* Vocabulary snapping fixed 1 of 49 labels in the offline corpus
  (`TMRIREF` -> `TMR/REF`), and is unit-tested against the usual confusions
  (`lNSET`, `DCLTP`, `0BS`, `STDBARO`).

## Not verified here

This POC was developed in a Linux container with no Windows and no X-Plane.
The following code paths are written from the documented APIs but have
**never been executed**:

* **Windows Graphics Capture** (`WgcCapture`). The `windows-capture` callback
  wiring, the BGRA frame layout, `draw_border=False` behaviour on Windows 10
  vs 11, and capture of an occluded X-Plane pop-out are all unexercised.
* **`list_windows()` / `find_window()`** (ctypes `user32` enumeration). The
  non-Windows error path is tested; the Windows path is not.
* **The X-Plane Web API writes.** The client is tested against a stub session
  (id resolution, base64 body, re-resolve on 404, X-Plane-not-running), not
  against X-Plane. In particular, PLAN.md's open question stands: it is
  **unknown whether the Web API will accept a PATCH to a plugin-created Data
  dataref**. That is exactly why `--publisher file` exists.
* **The XPPython3 plugin inside X-Plane.** Its buffer handling and JSON poll
  are tested against a stubbed `XPPython3` module, so the logic is exercised,
  but `registerDataAccessor` argument names, the `Type_Data` read/write
  callback contract, and flight-loop registration have not been validated
  against a real XPPython3 runtime.
* **Real G1000 geometry, fonts and colours.** All accuracy numbers above come
  from synthetic frames rendered with Liberation Sans, not from X-Plane
  screenshots. Real-world OCR accuracy, the true default strip fractions in
  `config.example.toml`, and whether X-Plane wraps any label onto two lines
  are all unknown.
* **End-to-end latency to a Stream Deck face** and the effect on sim frame
  rate (success criteria 2 and 4 in PLAN.md).
