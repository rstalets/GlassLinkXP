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
    -> classify each cell's background colour from a border ring          color.py
    -> X-Plane Web API PATCH into plugin-created datarefs                 publish.py
    -> PilotsDeck reads g1000/softkey/pfd/1:s64 and .../1/bg
```

See `PLAN.md` for the design rationale. **This is a POC**: it is verified
offline against synthetic frames (see *Not verified here* at the bottom) and
has not been run against a live X-Plane.

## Layout

```
g1000_softkey/
  main.py       CLI: run | list-windows | calibrate | dump-cells | dump-colors
                     | bench | synth
  capture.py    WGC backend (Windows) + PNG backend (offline dev/test)
  strip.py      strip crop, 12-cell split, per-cell preprocessing, auto-detect
  ocr.py        persistent Tesseract API, char whitelist, vocabulary snapping
  color.py      cell background -> black / white / yellow / red, + text colour
  pipeline.py   frame -> cells -> change gating -> labels + colours
  publish.py    X-Plane Web API client, JSON-file fallback, console output
  synth.py      synthetic G1000 softkey frames for offline work
  config.example.toml
  labels.txt    the softkey vocabulary (edit this)
xppython3/PI_G1000SoftkeyLabels.py   creates the 48 datarefs (24 labels + 24 colours)
scripts/
  install-windows.ps1        daemon: vcpkg + MSVC + uv venv + tesserocr wheel
  install-xplane-plugin.ps1  sim: XPPython3 + the dataref plugin, and -VerifyOnly
g1000.cmd       run any command without activating the venv
wheels/         the compiled tesserocr wheel (git-ignored, but keep it)
tests/          offline tests over the whole pipeline
```

## Windows setup

### The short version

```
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```

That script does everything in this section. Read on only if you want to know
what it is doing, or you would rather do it by hand.

**It compiles tesserocr exactly once.** The expensive part -- vcpkg building
Tesseract and Leptonica, then MSVC compiling the Cython extension -- produces a
single `.whl`, which is saved in `wheels/` and repaired with `delvewheel` so it
carries its own DLLs. Re-running the script finds that wheel and skips MSVC and
vcpkg altogether: installing from it takes well under a second instead of the
better part of an hour. Once it exists you can delete the vcpkg tree (several
GB) and still rebuild the environment freely. `-RebuildWheel` forces a
recompile; the cache is per Python minor version, so moving from 3.12 to 3.13
does mean one more build.

### Why `pip install tesserocr` fails on Windows

**tesserocr has never published a Windows wheel** -- every release on PyPI is
macOS/manylinux/musllinux only. So pip always falls back to the sdist and
compiles, and the compile needs Tesseract's *development* files. The UB
Mannheim installer ships only the runtime (`tesseract.exe` plus DLLs): no
headers, no `.lib` import libraries.

`PATH` is irrelevant to this. `tesserocr`'s `setup.py` reads two other
environment variables:

```python
if sys.platform == "win32":
    libpaths = os.getenv("LIBPATH", None)     # where the .lib files are
    ...
    includepaths = os.getenv("INCLUDE", None) # where the headers are
```

An unset `LIBPATH` is what produces `Tesseract library not found in LIBPATH: []`.

Three further traps, all handled by the script:

* `setup.py` keeps only `.lib` files whose **path contains the major+minor
  digits** from `tesseract -v`, and rejects anything ending in `d.lib`.
  Tesseract's CMake emits `tesseract{MAJOR}{MINOR}.lib`, so 5.5.x needs
  `tesseract55.lib`; a generic `tesseract.lib` is silently skipped.
* `pyproject.toml` has **no `[build-system]` table**, so Cython is declared
  only through the legacy `setup_requires`. Under PEP 517 build isolation it is
  absent and the build fails -- install the build deps yourself and pass
  `--no-build-isolation`.
* Since Python 3.8 `PATH` is not searched for extension-module dependencies, so
  `tesseract55.dll` and `leptonica-*.dll` must sit beside the installed `.pyd`
  (or be registered with `os.add_dll_directory`).

### By hand

1. **Visual Studio 2022 Build Tools** with the "Desktop development with C++"
   workload.
2. **Tesseract development files** via vcpkg (pulls in Leptonica):
   ```
   git clone https://github.com/microsoft/vcpkg C:\vcpkg
   C:\vcpkg\bootstrap-vcpkg.bat
   C:\vcpkg\vcpkg install tesseract:x64-windows
   ```
3. From an **"x64 Native Tools Command Prompt for VS 2022"**, *append* to the
   toolchain's variables -- replacing `INCLUDE` loses `stdio.h`:
   ```
   set INCLUDE=%INCLUDE%;C:\vcpkg\installed\x64-windows\include
   set LIB=%LIB%;C:\vcpkg\installed\x64-windows\lib
   set LIBPATH=%LIBPATH%;C:\vcpkg\installed\x64-windows\lib
   ```
4. **Python packages** (uv or pip; the flags matter more than the tool):
   ```
   uv venv --python 3.12
   uv pip install setuptools wheel "Cython>=3.0.0,<3.2.0" cysignals
   uv pip install --no-build-isolation tesserocr
   uv pip install -r requirements.txt
   ```
5. Copy `C:\vcpkg\installed\x64-windows\bin\*.dll` next to the installed
   `tesserocr` package, and set `TESSDATA_PREFIX` to a directory holding
   `eng.traineddata` (vcpkg does not install language data; the UB Mannheim
   `tessdata` folder works).

### If you would rather not build anything

`conda install -c conda-forge tesserocr` ships the binding plus Tesseract and
Leptonica prebuilt. The catch: conda-forge's **win-64 builds stop at tesserocr
2.5.2, Python 3.8-3.11**. That is fine for this code -- it only uses
`PyTessBaseAPI`, `SetVariable`, `SetImage`, `GetUTF8Text`, `MeanTextConf` and
`End`, all present since 2.x -- but loosen the `tesserocr>=2.6` pin in
`requirements.txt` first.

Last resort: `engine = "pytesseract"` in `[ocr]`, which works with a plain UB
Mannheim install but restarts Tesseract for every cell (measured ~24x slower
per frame; same accuracy).

### The X-Plane side

XPPython3 plus the dataref plugin:

```
powershell -ExecutionPolicy Bypass -File scripts\install-xplane-plugin.ps1
```

`scripts\install-windows.ps1` runs this for you unless you pass `-SkipXPlane`.
What it does, and why each part is needed:

* **XPPython3** goes in `<X-Plane 12>/Resources/plugins/XPPython3`. Version 4
  bundles its own **Python 3.12**, so no system Python is required -- and note
  the plugin therefore runs in *that* interpreter, not this project's venv.
  That is why `PI_G1000SoftkeyLabels.py` imports nothing beyond the standard
  library and the XPPython3 API.
* **`Resources/plugins/PythonPlugins/`** is created by XPPython3 on the *first
  X-Plane run*, so on a fresh install it does not exist yet. The script creates
  it early, which is harmless and saves a launch cycle.
* **`PI_G1000SoftkeyLabels.py`** is copied into that folder (XPPython3 loads
  plugins by the `PI_` prefix). It creates 24 writable 16-byte datarefs and does
  nothing else: `g1000/softkey/pfd/1..12` and `g1000/softkey/mfd/1..12`.

The Web API can *write* datarefs but cannot *create* them, which is the only
reason a plugin exists at all. Everything expensive stays in the standalone
daemon so it never touches X-Plane's flight-loop thread.

Restart X-Plane, then confirm the datarefs actually registered:

```
powershell -ExecutionPolicy Bypass -File scripts\install-xplane-plugin.ps1 -VerifyOnly
```

That queries a running X-Plane over the web API and reports how many of the 24
exist and their `value_type` (expect `data`). If the plugin failed to load, look
in `<X-Plane>/Log.txt` and `<X-Plane>/XPPython3.log`.

### The X-Plane web server

X-Plane 12.1.1+ serves the REST API on `http://localhost:8086`. Check
`http://localhost:8086/api/v1/datarefs` in a browser; if it does not answer,
enable the web server in Settings -> Network.

## Calibration workflow

> **Use `g1000.cmd`.** It calls the venv interpreter directly, so there is
> nothing to activate and PowerShell's execution policy never enters into it
> (a `.cmd` file is not a PowerShell script):
>
> ```powershell
> .\g1000 list-windows
> .\g1000 calibrate --display pfd
> .\g1000 run
> ```
>
> Activating still works if you prefer it (`.venv\Scripts\Activate.ps1` in
> PowerShell, `activate.bat` in cmd), and so does calling
> `.venv\Scripts\python.exe -m g1000_softkey.main` directly. What does *not*
> work is a bare `python`/`py` with no venv active: that picks up a system
> interpreter and fails with `ModuleNotFoundError: No module named 'numpy'`.
> (`py` does honour an *active* venv -- it just falls back silently when there
> is none.)


The strip geometry depends on the pop-out window size and bezel, so it is
expressed as *fractions* of the client area and has to be set once per setup.

1. Pop the PFD and MFD out into their own windows in X-Plane.
2. Find the window titles:
   ```
   .\g1000 list-windows
   ```
   Copy a distinctive substring of each title into `window_title` under
   `[display.pfd]` / `[display.mfd]` in your `config.toml` (copy
   `config.example.toml` to start).
3. Dump the calibration images and a suggested geometry:
   ```
   .\g1000 -c config.toml calibrate --out calibration
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
   .\g1000 -c config.toml dump-cells --out cells
   ```
   `<display>_NN_prep.png` should be black text on a white background, with
   the glyphs roughly 30 px tall, including for the highlighted (selected)
   softkey. If a cell is inverted or the text is clipped, fix the geometry
   before blaming the OCR.
6. Watch the labels live before wiring anything to X-Plane:
   ```
   .\g1000 -c config.toml run --publisher console
   ```
7. Then run for real (`target = "webapi"` in `[publish]`, or `--publisher webapi`):
   ```
   .\g1000 -c config.toml run
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
| Display value | `g1000/softkey/pfd/1:s64` |

`:s64` is PilotsDeck's string-dataref address syntax: read 64 bytes as a
NUL-terminated string. The daemon writes exactly 64 bytes, NUL padded.

**The width is fixed in three places and they must agree**: `FIELD_WIDTH` in
`PI_G1000SoftkeyLabels.py` (changing it needs an X-Plane restart -- the buffer
is allocated when the accessor is registered), `publish.field_width` in the
config, and the `:sNN` on every button. That is why it is 64 and not a snug
fit: the longest label is 11 characters, and the optional colour prefix below
adds 9, so raising it later would mean re-editing every button you had made.

### Softkey colours

Alongside each label the daemon publishes an int dataref naming the colour of
the cell the label sits on:

| dataref | value |
| --- | --- |
| `g1000/softkey/pfd/1/bg` | `0` black, `1` white (selected/inverted), `2` yellow, `3` red |

Use it to pick the button image or background -- a PilotsDeck display value of
`g1000/softkey/pfd/1/bg` switches on a number, no string parsing needed. The
implied text colour is white on `0` and black on `1`, `2` and `3`.

That text colour is a *legibility rule* for the Stream Deck face, not a
measurement of the G1000's own font colour, which the daemon deliberately does
not try to read. If you want the daemon to apply it for you, set:

```toml
[publish]
embed_text_color = true
```

and each label goes out prefixed with PilotsDeck's inline colour marker --
`[[#000000STD BARO`, say -- which overrides the button's configured text
colour for that update. It is **off by default**: a PilotsDeck build that
predates the feature does not interpret the prefix and renders a literal
`[[#000000` on the button face, which looks exactly like the daemon has
broken. Try it on one button before turning it on for a whole profile. The
`/bg` dataref is published either way.

Check the classification against your own display before relying on it:

```
g1000 -c config.toml dump-colors
```

It prints each cell's border-ring BGR and HSV and how those classified, plus
the thresholds that produced the answer. Move the thresholds in `[color]` to
fit what you see -- the shipped defaults came from plausible swatches, not
from a capture.

Softkey N maps to `sim/GPS/g1000n1_softkeyN` (pilot PFD) and
`sim/GPS/g1000n3_softkeyN` (MFD); `g1000n2` is the copilot PFD and is out of
scope for this POC.

## Offline development (no X-Plane, no Windows)

Everything downstream of capture is platform independent, and there is a
synthetic frame generator, so the whole pipeline runs anywhere:

```
python -m g1000_softkey.main synth --out frames
python -m g1000_softkey.main run --image frames/pfd_top.png --publisher console --once
python -m g1000_softkey.main dump-colors --image frames/alerts.png
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
* `color.value_max` / `color.saturation_max` / the hue windows -- where the
  four background colours are cut apart. Set them from `dump-colors` output
  rather than from the shipped defaults; the order they are applied in (V,
  then S, then hue) means a wrong `value_max` shows up as coloured cells
  reading black, and a wrong `saturation_max` as white cells reading
  coloured.
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
| `ModuleNotFoundError: No module named 'numpy'` | The venv is not active, so a system Python is running. `.venv\Scripts\Activate.ps1` (PowerShell), or call `.venv\Scripts\python.exe` directly. |
| `N of 48 datarefs are not registered in X-Plane` | The XPPython3 plugin is not installed or failed to load. Check `<X-Plane>/Log.txt` and `XPPython3.log`. |
| Labels are garbage or empty | Geometry. Run `dump-cells` and look at the `_prep.png` images. |
| One cell is always wrong | Missing entry in `labels.txt`, or a two-line label (see limitations). |
| Blank cells produce short nonsense strings | The crop includes something bright above or below the strip; tighten `y`/`h`, or raise `ocr.blank_ink_ratio`. |

Run any command with `-v` for debug logging (per-cell raw OCR strings,
confidences and match scores).

## How it works

`docs/PIPELINE.md` has flowcharts of the daemon loop and of what happens to a
single frame, plus a key for reading the `-v` output.

## Latency

Where the delay between a softkey press and the Stream Deck face actually comes
from, worst case:

| stage | cost | notes |
| --- | --- | --- |
| polling interval | up to `1/loop_hz` | 250 ms at the old 4 Hz default; ~83 ms at 12 Hz |
| capture | a few ms | WGC hands over the latest composed frame |
| OCR | ~45 ms per display | only for cells whose pixels changed; unchanged cycles are ~0.3 ms |
| publish | **1 message** | was one blocking HTTP PATCH *per changed cell* |

The two things that dominated were the polling interval and the publish path,
not the OCR. A softkey press typically changes most of a 12-cell strip, and the
REST publisher issued a separate blocking `PATCH` for each one -- a dozen
sequential round-trips into X-Plane's embedded web server per press, against
tens of milliseconds for recognising the whole strip.

`target = "websocket"` sends the entire strip in a single `dataref_set_values`
message and does not wait for a reply. `dataref_set_values` accepts many
datarefs at once and needs no prior subscription; name-to-id resolution still
uses REST. `target = "webapi"` keeps the old per-dataref REST behaviour if you
need it.

Run with `--timing` to see the breakdown on your own machine; it prints a line
whenever the labels change:

```
cycle 118 ms (work) + 0 ms (sleep budget)  publish=0.8  ocr_ms=86.9  preprocess_ms=7.8  split_ms=0.4
```

If `publish` is large, X-Plane is the bottleneck; if `ocr_ms` is large, look at
the crop (`dump-cells`) -- an over-wide strip means more non-blank cells than
there really are.

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
* **The colour thresholds in `[color]`.** The classifier is tested against
  synthetic swatches at several brightnesses, which shows it separates four
  backgrounds and that dimming moves V while leaving hue and saturation alone.
  It does not show that the shipped `value_max`, `saturation_max` and hue
  windows match X-Plane's actual softkey colours -- those numbers came from
  plausible swatches, and nothing here has seen a real frame. `dump-colors`
  exists so the real numbers can replace them without guessing.
* **PilotsDeck's `[[#RRGGBB` inline text colour.** `publish.embed_text_color`
  emits the prefix as documented to this project, but the marker is not
  described in PilotsDeck's public README (which does document the `:sNN`
  string suffix), and no PilotsDeck build has rendered one of these strings
  here. That is the other reason the setting is off by default.
* **The `Type_Int` datarefs.** The plugin registers them with `readInt` /
  `writeInt` per the XPPython3 documentation and the round trip is tested
  against the stubbed SDK, but no X-Plane has created one, and no Web API has
  written a bare number to one.
* **End-to-end latency to a Stream Deck face** and the effect on sim frame
  rate (success criteria 2 and 4 in PLAN.md).
