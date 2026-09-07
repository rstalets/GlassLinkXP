# Developer notes

This is the material a contributor needs that a user installing GlassLinkXP
does not: how the repository is laid out, how to work on it without Windows
or X-Plane, what has actually been run and what has not, and where the
performance numbers came from. See `README.md` for install/use/troubleshoot,
and `CLAUDE.md` for the fuller set of conventions this codebase follows.

## Layout

```
install.cmd / install.ps1   the end-user installer (see README.md)
pyproject.toml, uv.lock, .python-version   dependency manifest, shared by
                                            dev checkouts and every install
src/                         everything that ships: this is copied wholesale
                              into %appdata%\GlassLinkXP by the installer
  glasslinkxp/
    main.py       CLI: run | gui | list-windows | manage-windows | calibrate
                       | dump-cells | dump-colors | bench | screen-template
                       | tune | synth
    gui/          the window: one tab per command, over the same CLI (see docs/GUI.md)
    capture.py    WGC backend (Windows) + PNG backend (offline dev/test)
    windowmgr.py  opens, sizes and places the PFD/MFD pop-outs
    command.py    fires X-Plane commands over the web API (the pop-out commands)
    strip.py      strip crop, 12-cell split, per-cell preprocessing, auto-detect
    ocr.py        persistent Tesseract API, char whitelist, vocabulary snapping
    color.py      cell background -> black / white / yellow / red, + text colour
    pipeline.py   frame -> cells -> change gating -> labels + colours
    publish.py    X-Plane WebSocket and REST clients, console output
    synth.py      synthetic G1000 softkey frames for offline work
    labels.txt    the softkey vocabulary (edit this)
    screens.toml  known softkey pages (see PagesTab, hidden from the GUI for now)
  xppython3/PI_GlassLinkXP.py   creates the 48 datarefs (24 labels + 24 colours)
  scripts/install-xplane-plugin.ps1   installs XPPython3 + the dataref plugin
  glasslinkxp.cmd, glasslinkxp-gui.cmd   run any command / open the window
                                          without activating a venv
  config.example.toml
tests/          offline tests over the whole pipeline (not shipped)
docs/
  PIPELINE.md       flowcharts of the daemon loop and the per-frame path
  GUI.md            how the window is put together, and what it is coupled to
  CONFIGURATION.md  every setting and why its default is what it is
                    (generated from src/glasslinkxp/gui/schema.py)
  DEVELOPER.md      this file
```

`pyproject.toml` points `[tool.setuptools] package-dir` at `src/`, so
`uv sync --locked` at the repository root works exactly the same way for a
dev checkout as it does inside an installed copy under `%appdata%` -- the
installer just copies the same manifest and `src/` into that folder and runs
the same command there.

## Working on it

```
uv sync --locked                 # once, or after pulling a lockfile change
python -m pytest -q              # all offline, keep them green
python -m glasslinkxp.main synth --out frames
python -m glasslinkxp.main run --once --image frames/xpdr.png --publisher console -v
python -m glasslinkxp.main gui                       # the window
```

The GUI's own tests need tkinter to import and a display to run, and those are
two different failures. With tkinter present and no display the widget tests
skip themselves and the rest still run; **without tkinter the suite does not
collect at all**, because `gui/tabs.py` imports it at module scope. If
`python -m pytest` dies during collection, check `python -c "import tkinter"`
before anything else. To run all of them here:

```
xvfb-run -a python -m pytest -q
```

The window itself can be driven headlessly the same way: `xvfb-run -a python
-m glasslinkxp.gui`, with `PIL.ImageGrab` for screenshots. Point the frame
source at `frames/` and every tab works with no X-Plane and no Windows.

Every command takes `--image <png|dir>` in place of live capture, so the whole
pipeline runs without Windows or X-Plane. `-v` prints a line per cell showing
the raw OCR string, what it snapped to, confidence, and any substitution.

## Offline development (no X-Plane, no Windows)

Everything downstream of capture is platform independent, and there is a
synthetic frame generator, so the whole pipeline runs anywhere:

```
python -m glasslinkxp.main synth --out frames
python -m glasslinkxp.main run --image frames/pfd_top.png --publisher console --once
python -m glasslinkxp.main dump-colors --image frames/alerts.png
python -m glasslinkxp.main bench --image frames/pfd_menu.png -n 50
```

`--image` accepts a PNG or a directory of PNGs (a directory is cycled, one
frame per loop iteration; a file named `<display>.png` in it is used for that
display).

## Where the Windows tesserocr wheel comes from

**tesserocr has never published a Windows wheel** -- every release on PyPI is
macOS/manylinux/musllinux only. pip therefore falls back to the sdist and
compiles, which needs Tesseract's *development* files that the runtime
installers do not ship.
[simonflueckiger/tesserocr-windows_build](https://github.com/simonflueckiger/tesserocr-windows_build)
publishes prebuilt Windows wheels that bundle their own Tesseract and
Leptonica, which removes both the compiler and a separate Tesseract install.
`pyproject.toml` names the wheel by exact URL under `[tool.uv.sources]`,
gated on `sys_platform == 'win32'`. `uv lock` records its digest and `uv sync
--locked` verifies every download against it and fails closed on a mismatch;
nothing is vendored into this repository.

Recording a digest on first lock is trust-on-first-use: it defends against
later substitution, not against a bad artefact on day one. The day-one check
was done once by hand; repeat it before bumping the pin, do not bump it blind.
conda-forge is not a usable second source: its `win-64` tesserocr builds stop
at 2.5.2, with no Python 3.12 build.

If the wheel is ever withdrawn, the previous vcpkg + MSVC source-build
installer is preserved in git history: `git show e916727:scripts/install-windows.ps1`
(from before the rename -- run `git log --all --oneline -- '*install-windows*'`
if that hash has been pruned).

## Tuning

* `app.loop_hz` (default 28) -- how often to capture. Softkeys only change
  when you press one, so this mostly sets worst-case latency.
* `app.change_gating` (default true) -- compare each cell against the
  previous frame and skip OCR for unchanged cells.
* `ocr.upscale`, `ocr.fuzzy_cutoff`, `ocr.blank_ink_ratio`, `color.*` -- see
  `docs/CONFIGURATION.md` for the full reference, generated from the same
  text the GUI's Settings tab shows beside each field.

## Latency

Where the delay between a softkey press and the Stream Deck face actually
comes from, worst case:

| stage | cost | notes |
| --- | --- | --- |
| polling interval | up to `1/loop_hz` | ~36 ms at the default 28 Hz |
| capture | a few ms | WGC hands over the latest composed frame |
| OCR | ~45 ms per display | only for cells whose pixels changed; unchanged cycles are ~0.3 ms |
| publish | **1 message** | one `dataref_set_values` WebSocket message, not one HTTP PATCH per changed cell |

`target = "websocket"` sends the entire strip in a single message and does
not wait for a reply; `target = "webapi"` keeps the old per-dataref REST
behaviour. Run with `--timing` to see the breakdown on your own machine:

```
cycle 118 ms (work) + 0 ms (sleep budget)  publish=0.8  ocr_ms=86.9  preprocess_ms=7.8  split_ms=0.4
```

If `publish` is large, X-Plane is the bottleneck; if `ocr_ms` is large, look
at the crop (`dump-cells`) -- an over-wide strip means more non-blank cells
than there really are.

## Known limitations

* PSM 7 reads a **single line**. Softkey labels that X-Plane draws on two
  lines will not read correctly; they would need a per-cell line split first.
* The coarse auto-detect finds the vertical band reliably but only
  approximates the horizontal extent; in the offline corpus, auto-detected
  geometry read 59/60 cells correctly against 60/60 for calibrated geometry.
* The selected/highlighted state is detected only implicitly (the per-cell
  threshold handles it); it is not published as a separate dataref.
* One `tesserocr` API instance is shared by both displays and used serially.
  That is fine at typical loop rates; it is not thread safe.
* The screen/page-lookup mechanism (`screens.toml`, `PagesTab`) exists and is
  tested but is deliberately not exposed in the GUI yet.

## Verified offline

On Linux/CPython 3.11, Tesseract 5.3.4 (system) with `tesserocr` 2.11:

* The tests pass (`python -m pytest -q`), including the full frame -> labels
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
  frame costs ~0.4 ms. (`capture_ms` in the bench output is PNG decode for
  the offline source, not Windows Graphics Capture.)
* Window management's decisions, driven through injected Windows operations
  (`tests/test_windowmgr.py`, 30 cases): a switched-off feature does not so
  much as enumerate the desktop; a desktop with no X-Plane on it is left alone
  rather than having commands fired into it; windows already the right size in
  the right corner are not touched, and a second pass over the first pass's
  work changes nothing; each display gets its own command; a window opened for
  one display is seen by the next rather than popped out again; the target
  corner comes from X-Plane's monitor and not the pop-out's own; the main
  window is picked out from among same-class windows by elimination and size;
  and a command that fails, or that opens nothing, is reported rather than
  raised. The command client refuses to activate anything whose name it did
  not match exactly, which is what stops an X-Plane too old for `filter[name]`
  -- it answers with the *whole* command list -- from having an arbitrary
  command fired in somebody's cockpit.
* The frame slot the capture thread writes into (`tests/test_capture.py`): the
  newest frame wins, what comes out is a copy, and a slot whose window has
  closed has no frame to give even though one was captured -- permanently,
  because a WGC session does not outlive its window. Shutting down is kept
  distinct from losing the window: both stop frames, only one invalidates the
  last one. Plus the reopen decision itself (`tests/test_main.py`): a lost
  capture is replaced whether management reopened the window or found it
  already back, nothing is rebuilt when the window could not be brought back,
  and neither `--image` nor window management switched off reopens anything.
* Vocabulary snapping fixed 1 of 49 labels in the offline corpus
  (`TMRIREF` -> `TMR/REF`), and is unit-tested against the usual confusions
  (`lNSET`, `DCLTP`, `0BS`, `STDBARO`).
* The sharpening tuner's no-regression guarantee -- a rung strong enough to
  fix one cell must not read a different, already-correct cell wrongly -- is
  tested against a scripted OCR engine reproducing the exact failure this
  tool exists for (a rung that opens up a `0` and also flips a `6` into a
  `5`), and the search finds the rung that fixes the first without the
  second. `tune --truth <file>` was also run for real, over the synthetic
  corpus with real Tesseract: the search of ~7,200 candidate ladders across
  32 psm/threshold/upscale combinations took a few seconds.

### The GUI, offline

On the same container, with Tk 8.6 under Xvfb and the synthetic frames as the
frame source, the window was driven end to end and every step did what it
says:

* Every tab builds, and the whole window was clicked through.
* **Start here** wrote the synthetic frames and pointed the frame source at
  them; **Run** started the daemon with the console publisher and the softkey
  board filled in, with the selected cell drawn white; **Stop** ended it
  cleanly (exit code 0, through the daemon's own SIGINT handler, not a kill).
* **Calibrate** produced the picture; a simulated mouse drag from frame pixel
  (60, 725) to (1219, 781) produced exactly that rectangle in the config, the
  six numbers followed the drag, nudging the left edge moved it by one frame
  pixel while holding the right edge still, and the twelve green boxes drawn
  on the canvas matched `strip.cell_rects` -- the function `split_cells`
  actually slices with -- for every geometry tried. Saving wrote the numbers
  into `config.toml` and they loaded back. Over-trimming the cells turned the
  offending boxes amber and made Save ask before writing; at a correct
  geometry it asked nothing, on every frame in the offline corpus.
* **Cells** showed all 24 cell pictures, at their true pixel size rather than
  shrunk to fit; typing an expected label into two of them, queuing the page
  and pressing **Run tuning** shelled out to a real `tune --truth ...` (real
  Tesseract, the full psm/threshold/upscale/ladder search over the synthetic
  corpus, in a few seconds), reported "everything already reads correctly at
  baseline", and Save suggested settings wrote the settings it found into
  `config.toml` and read back the same values.
* **Find windows** failed as it must on Linux, and the tab showed the command
  that failed and the daemon's own explanation of why.
* The tests cover this without a display too: over 690 of them, most of which
  need Tk and skip themselves when there is no display. Several are there to
  stop the GUI drifting from the daemon -- every argv the GUI can build is
  parsed by `main.build_parser()`, every parser in `gui/logparse.py` is fed
  the output of the command it reads, the settings form is checked against
  the config dataclasses field by field, and the calibration editor's boxes
  are compared with the rectangles `strip.py` crops.

## Not verified here

This project is developed in a Linux container with no Windows and no
X-Plane. The following code paths are written from the documented APIs but
have **never been executed**:

* **Windows Graphics Capture** (`WgcCapture`). The `windows-capture` callback
  wiring, the BGRA frame layout, `draw_border=False` behaviour on Windows 10
  vs 11, and capture of an occluded X-Plane pop-out are all unexercised.
* **`list_windows()` / `find_window()`** (ctypes `user32` enumeration). The
  non-Windows error path is tested; the Windows path is not.
* **Window management, everywhere it touches Windows or the sim** -- the
  decisions are covered offline (`tests/test_windowmgr.py`), but:
  * **That the pop-out commands are named what this thinks.**
    `sim/GPS/g1000n1_popout` and `sim/GPS/g1000n3_popout` come from published
    command references, not from a sim anyone here has queried.
  * **That the commands are momentary rather than toggles.**
  * **That every X-Plane window carries the class `X-System`.**
  * **`SetWindowPos`, `MonitorFromWindow` and `GetMonitorInfoW`**, including
    per-monitor DPI scaling.
  * **The reopen path end to end** -- `on_closed` firing on a real run is
    confirmed; the pop-out command going out, the window coming back, and a
    new capture attaching to it are not.
* **The details of the X-Plane API clients.** Both the WebSocket and the REST
  publisher have been seen updating the plugin's datarefs against a running
  X-Plane. Id resolution, re-resolving on a 404, and the X-Plane-not-running
  paths are only tested against a stub session.
* **The XPPython3 plugin inside X-Plane.** Its buffer handling is tested
  against a stubbed `XPPython3` module; `registerDataAccessor` argument names
  and the `Type_Data` read/write callback contract have not been validated
  against a real XPPython3 runtime.
* **Real G1000 geometry, fonts and colours.** All accuracy numbers above come
  from synthetic frames rendered with Liberation Sans, not from X-Plane
  screenshots.
* **`install.ps1` / `install.cmd`, and `src/scripts/install-xplane-plugin.ps1`.**
  Everything they orchestrate is verified piece by piece -- the wheel's
  contents were inspected by hand, `uv lock` records its digest, and `uv sync
  --locked` provisions an interpreter and passes the whole suite on Linux --
  but **the PowerShell itself has never been run, or even syntax-checked**:
  there is no PowerShell in the development container.
* **The `Type_Int` datarefs.** The plugin registers them with `readInt` /
  `writeInt` per the XPPython3 documentation and the round trip is tested
  against the stubbed SDK, but no X-Plane has created one.
* **End-to-end latency to a Stream Deck face** and the effect on sim frame
  rate.
* **The GUI on Windows.** It is plain Tk and was exercised under Xvfb on
  Linux, but nothing here has opened it on Windows. `pythonw.exe` launching
  it without a console, `CREATE_NEW_PROCESS_GROUP` + `CTRL_BREAK_EVENT` as
  the way Stop reaches the daemon, and `os.startfile` behind the "Open
  folder" buttons have never run.
* **That Tk is present in the venv the installer builds.** uv's
  python-build-standalone Windows builds do ship the tcl/tk files; this has
  not been confirmed on Windows here, so both the installer and
  `glasslinkxp-gui.cmd` check for Tk and warn rather than assuming.
