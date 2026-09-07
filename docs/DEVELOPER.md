# Developer notes

This is the material a contributor needs that a user installing GlassLinkXP
does not: how the repository is laid out, how to work on it without Windows
or X-Plane, what has actually been run and what has not, and where the
performance numbers came from. The root `README.md` is the user-facing doc --
install, first run, wiring a button, troubleshooting -- and it is what a
visitor to the repository reads; `CLAUDE.md` has the fuller set of conventions
this codebase follows.

## Layout

```
src/            EVERYTHING THAT SHIPS. Zip this directory and it is what a
                user downloads; the installer copies it to %appdata%\GlassLinkXP
  install.cmd   what they double-click
  install.ps1   copies this folder into %appdata%, runs uv sync, fetches the
                OCR language data, adds the desktop shortcut, and reconciles
                a config and vocabulary kept from an older install
  pyproject.toml, uv.lock, .python-version
                the manifest -- read by `uv sync` on the user's machine at
                install time, which is why it is inside the zip
  README.md, LICENSE   a short note in the zip: run install.cmd, and where
                the real documentation is. The user-facing doc is the
                repository's root README, which is what they land on
  glasslinkxp/
    main.py       CLI: run | gui | list-windows | manage-windows | calibrate
                       | dump-cells | dump-colors | bench | screen-template
                       | tune | synth | migrate-config
    gui/          the window: one tab per command, over the same CLI, plus the
                  setup wizard bar (wizard.py) -- see docs/GUI.md
    capture.py    WGC backend (Windows) + PNG backend (offline dev/test)
    windowmgr.py  opens, sizes and places the PFD/MFD pop-outs
    command.py    fires X-Plane commands over the web API (the pop-out commands)
    strip.py      strip crop, 12-cell split, per-cell preprocessing, auto-detect
    ocr.py        persistent Tesseract API, char whitelist, vocabulary snapping
    color.py      cell background -> black / white / yellow / red, + text colour
    pipeline.py   frame -> cells -> change gating -> labels + colours
    configmigrate.py  brings a config.toml kept across an update in line
                  with the settings this version has (the migrate-config
                  subcommand, which install.ps1 runs)
    publish.py    X-Plane WebSocket and REST clients, console output
    synth.py      synthetic G1000 softkey frames for offline work
    labels.txt    the softkey vocabulary (edit this)
    screens.toml  known softkey pages (see PagesTab, hidden from the GUI for now)
  xppython3/PI_GlassLinkXP.py   creates the 48 datarefs (24 labels + 24 colours)
  scripts/install-xplane-plugin.ps1   installs XPPython3 + the dataref plugin
  glasslinkxp.cmd, glasslinkxp-gui.cmd   run any command / open the window
                                          without activating a venv
  config.example.toml   what a new config.toml is seeded from
tests/          offline tests over the whole pipeline (not shipped)
tools/make_zip.py   builds dist/glasslinkxp-<version>.zip from src/, stamping
                the version in (see Releasing)
.github/
  workflows/release.yml   publishes a release -> the zip appears on it
  ISSUE_TEMPLATE/         bug report and enhancement forms, plus
                          config.yml -- blank issues are off and it
                          points questions at Discussions instead
docs/
  PIPELINE.md       flowcharts of the daemon loop and the per-frame path
  GUI.md            how the window is put together, and what it is coupled to
  CONFIGURATION.md  every setting and why its default is what it is
                    (generated from src/glasslinkxp/gui/schema.py)
  DEVELOPER.md      this file
```

`src/` is the unit of distribution: zip it and that is what a user
downloads, with `install.cmd` at the top level. The manifest lives in it for
that reason -- `pyproject.toml`, `uv.lock` and `.python-version` are read by
`uv sync` *at install time*, on the user's machine, so they cannot sit outside
the zip. `tools/make_zip.py` builds it, and `tests/test_packaging.py` keeps
the invariant honest in both directions: everything install time needs is
inside `src/`, and nothing development-only is.

That also means the installed layout and a dev checkout are the same shape:
`src/` here is `%appdata%\GlassLinkXP` there, venv and all.

## Working on it

```
uv sync --project src --locked   # once; the venv lands in src/.venv
src/.venv/bin/python -m pytest -q          # all offline, keep them green
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
xvfb-run -a src/.venv/bin/python -m pytest -q
```

The window itself can be driven headlessly the same way: `xvfb-run -a python
-m glasslinkxp.gui`, with `PIL.ImageGrab` for screenshots. Point the frame
source at `frames/` and every tab works with no X-Plane and no Windows.

Every command takes `--image <png|dir>` in place of live capture, so the whole
pipeline runs without Windows or X-Plane. `-v` prints a line per cell showing
the raw OCR string, what it snapped to, confidence, and any substitution.

## Releasing

Publish a GitHub release tagged `vX.Y.Z` and `.github/workflows/release.yml`
attaches `glasslinkxp-X.Y.Z.zip` to it a minute later. There is nothing to
build first and nothing to bump by hand.

**The tree is unreleased.** `src/pyproject.toml` says `0.0.0` and so does the
`glasslinkxp` entry in `src/uv.lock`; the version is stamped in at build time
from the tag. A checkout is not a release of anything, and a zip a developer
builds locally says so on its face.

**`glasslinkxp/VERSION` is the one the running app reads, and it is not in
the tree.** The build *creates* it, for a release only. Every command logs it
on its first line (`GlassLinkXP 1.2.3: run`) and the window shows it in its
bottom-right corner, so a log or a screenshot says which download produced it.
A checkout finds no file and says `NO_VERSION`, which is deliberately not a
number -- a dev build cannot then be mistaken in an issue for a version
something was released at.

That asymmetry is load-bearing and was got wrong first: the file was checked
in holding `0.0.0`, so every clone reported `0.0.0`, which reads like a build
rather than like the absence of one. It is also why the version is a file and
not something derived from `pyproject.toml` at runtime -- the manifest is in
every checkout, so a dev build would get the same answer a release does.
`.gitignore` carries it, so a copy that acquires one locally (from an install
copied back, say) cannot be committed, and the build never zips one it finds.

**The manifest and the lock are stamped together, and that is not tidiness.** `uv.lock`
records the version it locked the project at, and `uv sync --locked` -- which
is what `install.ps1` runs on the *user's* machine -- refuses to run when the
lock and the manifest disagree. That was measured rather than assumed: bumping
`pyproject.toml` alone makes `uv lock --check` report the lockfile out of
date, and stamping both makes it pass again. So stamping one would produce a
zip that downloads, extracts, and then fails at `uv sync`, on a machine none
of us can see. `tools/make_zip.py` does all three in one operation and refuses to
build if either substitution finds nothing; `tests/test_release.py` pins that,
including that all three agree in the tree as checked in. Stamping writes into
the zip, never into the tree, so a build leaves the checkout as it found it.

The tag is the only input. `make_zip.py` normalises and validates it
(`v1.2.3` -> `1.2.3`, and a tag that is not a version stops the build), which
is why the workflow is four lines of shell: the version never reaches the
shell interpolated, only as an environment variable it quotes, because a tag
is text a human typed.

```
python tools/make_zip.py                    # dist/glasslinkxp-0.0.0.zip, no VERSION in it
python tools/make_zip.py --version v1.2.3   # dist/glasslinkxp-1.2.3.zip, stamped
```

A zip built without a tag carries no version file at all, so a copy installed
from one reports `NO_VERSION` exactly as a checkout does. Only a release has a
version.

To rehearse the packaging without cutting a version, run the workflow by hand
(**Actions -> Release -> Run workflow**) with a version: it builds exactly the
same zip and leaves it as a workflow artifact, attached to no release.

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

### Pop-out size has a peak, and it is not at either end

The G1000 is drawn from a fixed texture and scaled into the window. A larger
window interpolates it -- more pixels, no more detail, softer edges, and blur
is what closes the counters of 0, 6, 8 and 9. A smaller window throws away
pixels Tesseract needed. Both ends read worse than somewhere in the middle.

Measured on one live display, all else equal:

| `window_management.size` | result |
| --- | --- |
| 1024x768 | confidence **down** on many cells |
| 1280x960 (default) | best of the three |
| ~10% above the default | confidence **down** |

Two earlier versions of this section were wrong in opposite directions --
first that a larger pop-out helps, then that the native texture size is the
reference. Neither is right, and the reason is worth keeping: **the number in
the config is not the number of pixels capture receives.** `windowmgr` asks
Windows for a client size and does nothing about display scaling, so on a
display at 125% the captured frame is larger than the figure in `config.toml`
by that factor. "Set it to the native texture size" therefore does not do what
it says on such a machine, which is exactly the display the table above came
from. `list-windows` reports the client size capture is actually given; that
is the number to reason about, not the configured one.

So this is a per-machine tunable and the only honest advice is to measure it.
`bench` reports mean confidence and how many cells landed on a known label
alongside the timings, so:

```
glasslinkxp -c config.toml bench -n 20
```

at one size, then another, is the comparison. Change one thing at a time.

Not verified here: none of this can be run without Windows and X-Plane, and
the DPI behaviour above is inferred from `windowmgr` having no DPI handling in
it plus a reported capture larger than the configured size. Nobody has
confirmed which Windows DPI-awareness mode X-Plane or the daemon runs in.

### What `tune` prefers, and why it may not hand you a ladder

Candidates are ranked on, in order: how many previously-wrong cells they fix;
how many of `psm`, `threshold` and `upscale` they leave **unchanged**; then how
short the ladder is; then confidence. A candidate that breaks a cell which
already read correctly is refused outright, whatever it fixes.

The middle two used to be the other way round, on the reasoning that a longer
ladder costs an extra OCR call on every cell of every frame. It does not:
`read_best` stops at the first variant that lands on a known label
confidently, so a rung is only ever paid for by a cell that already failed --
across the offline corpus, the two extra rungs cost no OCR calls at all.
Changing `psm` is not like that. It changes what Tesseract is asked for every
cell of every frame, including every cell that is not in the truth file and
whose reading therefore moved without being measured.

So if `tune` reports `psm = 10` and a bare `sharpen_ladder = [[0.0, 0.0]]`, it
now also says whether a ladder alone would have done the job, and by how much.
"No ladder was suggested" and "no ladder helped" are different answers and the
report distinguishes them.

### If a label reads as garbage after a good-looking calibration

Check `run -v` for `POLARITY read the wrong way up`, and check that the
label is in `labels.txt`. Those are the two failures that look like bad OCR
and are not.

The first means the border ring answered for the wrong rectangle -- see
*Which way up a cell is* in `docs/PIPELINE.md`. In practice that is a crop
that has slipped off its cell onto a separator bar or a neighbour, since the
ring is background by construction on a crop that is on its cell. The
polarity rung reads it anyway, so the label comes out right; the line is
there because the calibration is worth another look.

The second is quieter. A label the vocabulary does not know is reported raw,
which is usually right, but it can never score an exact match -- so it never
stops the ladder early and walks every variant there is on every change. On
the offline corpus, adding the two missing labels took `alerts` from 19
preprocessing passes to 9.

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

* The polarity failure and the measurement that replaced the guess.
  `eng.traineddata` here is byte-identical to the file `install.ps1`
  downloads (sha256 `7d4322bd...70b2`, tessdata_fast 4.1.0), so this is the
  model a user's install runs.

  Polarity was decided on the middle of the cell, on the assumption that the
  glyphs are the minority there. Over the 58 non-blank cells of the offline
  corpus, that band puts the two cases 0.41 apart (0.13-0.31 light-on-dark,
  0.72-0.87 light background) with the threshold at 0.50. The border ring
  puts them 0.83 apart (0.00-0.08 against 0.91-1.00). Both get today's frames
  right; the difference is how close each comes to getting them wrong, and a
  live MFD closed the first gap while leaving the second untouched.

  Rendered words at one cell size and one font, only the word changing, ring
  fraction against the middle band:

  | word | middle band | border ring |
  | --- | --- | --- |
  | TERRAIN | 0.44 | 0.05 |
  | NEXRAD | 0.42 | 0.06 |
  | ENGINE | 0.43 | 0.00 |
  | DCLTR-1 | 0.36 | 0.02 |
  | MAP | 0.27 | 0.00 |

  which is the shape of the reported failure: `DCLTR-1` is the longest string
  in the list and among the lowest on both, because what fills the middle of a
  cell is which letters a word is made of, not how many. The rendering is
  DejaVu, not X-Plane's font, so these locate the *mechanism* -- the live
  capture is the measurement that matters, and on that one TERRAIN and NEXRAD
  were over the line and DCLTR-1 was not.
* The fallback is free on everything that already read: 59 preprocessing
  variants built across the six synthetic menus, none of them a retry, every
  label identical with `retry_opposite_polarity` on and off.

* **A release build installs.** `tools/make_zip.py --version v1.4.2` was
  built, extracted, and `uv lock --check` run against the extracted folder:
  it resolves, which is the check `uv sync --locked` makes on the user's
  machine before installing anything. Stamping `pyproject.toml` alone fails
  that same check -- that is the measurement the two-file stamping exists
  for, and both directions were run rather than reasoned about.
* **The PowerShell parses, and the X-Plane side actually ran.** PowerShell
  7.4.6 for Linux was fetched into a scratch directory (it is not in the
  container by default and is not a dependency of anything):

  ```
  pwsh -NoProfile -Command '$e=$null; $t=$null;
    [System.Management.Automation.Language.Parser]::ParseFile(
      (Resolve-Path src/install.ps1).Path, [ref]$t, [ref]$e); $e'
  ```

  Both scripts parse with no errors. `install-xplane-plugin.ps1` was then run
  against a fake X-Plane root (`X-Plane.exe` plus `Resources/plugins`; PowerShell
  normalises the scripts' backslash paths on Linux, so the tree is ordinary
  directories) over every branch of the XPPython3 check: none installed and
  answered *no* (warns, still copies the plugin, and the summary says the
  X-Plane side will not load yet); an `XPPython3` folder holding no `.xpl`
  (reported as not installed, rather than counted as one); a real `.xpl`
  present (left alone); `-SkipXPPython3`; and `-Yes`, which downloaded
  `xp3-win32.zip` from the location the XPPython3 documentation names and
  extracted `XPPython3/win_x64/XPPython3.xpl` -- the layout that documentation
  describes. What that does *not* cover is Windows: file locking, paths with
  drive letters, `Expand-Archive` under PowerShell 5.1, and a real X-Plane
  loading what was extracted.
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
* **`install.ps1` / `install.cmd`, and `src/scripts/install-xplane-plugin.ps1`,
  on Windows.** Both scripts now parse clean and the plugin script has been
  *run*, but under PowerShell 7 on Linux (see *Verified offline*), which is
  not the Windows PowerShell 5.1 a user's `install.cmd` starts. Untested
  either way: everything Windows-only in them -- the `WScript.Shell` desktop
  shortcut, `SendMessageTimeout` broadcasting the new `TESSDATA_PREFIX`,
  `winget`, `%APPDATA%` layout, the `uv` bootstrap, and `install.cmd` itself.
  `install.ps1` has never been run at all: it copies into `%APPDATA%` and
  runs `uv sync` on the machine it is on.
* **The `Type_Int` datarefs.** The plugin registers them with `readInt` /
  `writeInt` per the XPPython3 documentation and the round trip is tested
  against the stubbed SDK, but no X-Plane has created one.
* **End-to-end latency to a Stream Deck face** and the effect on sim frame
  rate. This includes **what the label field width costs on the deck**. The
  field went 16 -> 64 -> 16; both the reasoning for widening it (a wider field
  costs nothing but plugin memory) and the report that reversed it (at `:s64`
  the buttons updated visibly more slowly and an empty field rendered as `0`)
  are about PilotsDeck's own read path, which nothing here can run. What *is*
  checked offline is that 16 is wide enough: `tests/test_plugin.py` walks every
  label in `labels.txt` through `encode_field` at the plugin's `FIELD_WIDTH`.
* **The GUI on Windows.** It is plain Tk and was exercised under Xvfb on
  Linux, but nothing here has opened it on Windows. `pythonw.exe` launching
  it without a console, `CREATE_NEW_PROCESS_GROUP` + `CTRL_BREAK_EVENT` as
  the way Stop reaches the daemon, and `os.startfile` behind the "Open
  folder" buttons have never run.
* **That Tk is present in the venv the installer builds.** uv's
  python-build-standalone Windows builds do ship the tcl/tk files; this has
  not been confirmed on Windows here, so both the installer and
  `glasslinkxp-gui.cmd` check for Tk and warn rather than assuming.
