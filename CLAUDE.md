# Working on GlassLinkXP

## What this is and why it exists

X-Plane 12 computes the Garmin G1000's softkey labels inside the engine and
rasterises them straight to the GPU. No dataref exposes them. So a Stream Deck
can *send* `sim/GPS/g1000n1_softkey1..12` but can only show a static
"BTN 1..12" face, which makes the G1000 slow to operate.

This project reads the labels back out of the pixels and republishes them as
X-Plane datarefs, so a Stream Deck button can show what the key actually does.

Everything follows from that one constraint: **the screen is the only source of
truth, and it is a low-resolution, anti-aliased, GPU-composited image.**

## Read these first

| file | what it tells you |
| --- | --- |
| `docs/PIPELINE.md` | Flowcharts of the daemon loop and the per-frame path, why the stages are ordered as they are, and a key for reading `-v` output. **Start here.** |
| `docs/GUI.md` | How the window is put together, why it spawns the CLI rather than calling it, and the couplings that let it -- each pinned by a test. |
| `docs/CONFIGURATION.md` | Every setting, generated from `gui/schema.py`. `config.toml` carries no comments because the GUI rewrites it. |
| `docs/DEVELOPER.md` | Layout, offline dev workflow, latency numbers, known limitations, and -- importantly -- *Verified offline* and *Not verified here*. |
| `README.md` | The end-user install/use/troubleshoot doc that ships in the distribution zip. Keep it short; deeper material belongs in `docs/DEVELOPER.md`. |
| `PLAN.md` | The original design rationale, including approaches that were considered and rejected. |
| `src/config.example.toml` | A starting point to copy, commented because people read it. The generated reference above is the authority. |

Everything under `src/` ships in the distribution zip (see `install.ps1`);
everything else here (`tests/`, `docs/`, `CLAUDE.md`, `PLAN.md`) is dev-only.

## Shape of the system

Two processes, deliberately:

- **A standalone Python daemon** (`src/glasslinkxp/`) captures, recognises and
  publishes. All the expensive work lives here.
- **A small XPPython3 plugin** (`src/xppython3/PI_GlassLinkXP.py`) does
  nothing but *create* the datarefs, because X-Plane's Web API can write a
  dataref but cannot create one.

They are split this way because XPPython3 callbacks run inline with X-Plane's
flight loop, so anything slow there costs frame rate.

Plus a third, optional one: **the GUI** (`src/glasslinkxp/gui/`), which is a Tk
window over the same CLI. It implements no part of the pipeline -- every button
spawns `python -m glasslinkxp.main <subcommand>` and shows what it said, so
it cannot drift into doing something the documentation does not describe, and
so a crash in capture or OCR takes down a child rather than the window. It is
also the only way to stop the daemon with a signal it handles: `cmd_run` calls
`signal.signal`, which only works on the main thread, and Tk owns that.

```
src/glasslinkxp/
  main.py       CLI: run, gui, list-windows, manage-windows, calibrate,
                     dump-cells, dump-colors, bench, screen-template, tune,
                     synth, migrate-config
  capture.py    Windows Graphics Capture, plus a PNG backend for offline work
  windowmgr.py  opens/sizes/places the PFD and MFD pop-outs; the Windows calls
                are injectable, so the policy is tested without Windows
  command.py    fires X-Plane commands over the web API (the pop-out commands)
  strip.py      strip crop, 12-cell split, per-cell preprocessing
  ocr.py        Tesseract, label vocabulary, CellResult
  color.py      border-ring sampling, HSV background classification
  pipeline.py   DisplayPipeline.process(): one frame -> 12 CellResults
  publish.py    WebSocket / REST / console publishers
  screens.py    softkey page definitions (screens.toml)
  config.py     frozen dataclasses + TOML loader (reading only; the GUI
                writes with tomli-w)
  configmigrate.py  reconciles a kept config.toml with the settings this
                version has, on update (see migrate-config)
  gui/          the window (see docs/GUI.md), including the calibration editor
                where the strip is drawn on the frame with the mouse, and the
                setup wizard (wizard.py: the steps, no Tk in it). Only app.py,
                tabs.py and widgets.py import Tk; the rest, geometry.py and
                wizard.py included, is tested without a display
```

`pyproject.toml` maps `[tool.setuptools] package-dir` at the repository root
to `src/`, so `uv sync --locked` at the root behaves identically for a dev
checkout and for a copy installed under `%appdata%` by `install.ps1` --
the installer just copies the manifest and `src/` there and runs the same
command.

## The lesson this codebase was built on

Most of the time spent on this project went into **guessing at pixels that
could not be seen**, and most of those guesses were wrong.

A fixed unsharp-mask setting tuned against synthetic DejaVu glyphs made the
live display *worse* -- it turned a `0` into a `B`. Four separate theories for
one unreadable digit (page-segmentation mode, blank-contrast threshold, a
slashed zero, edge clipping) were each plausible, each tested, and each wrong;
the actual cause was closed counters filling in during thresholding, and it was
only found once the user sent a picture of the preprocessed cell.

So:

- **Measure before you tune.** If you are choosing a threshold, a colour, or a
  filter strength, add a diagnostic that prints the real values from a real
  capture first. `tune`, `dump-cells`, `dump-colors`, `calibrate` and
  `screen-template` all exist because of this.
  A test suite proves a classifier's *logic*; only a live capture proves its
  *threshold values*. `tests/test_color.py` and the live run between them have
  closed the `[color]` case -- keep both halves in mind when adding any other
  classifier, because passing tests are not evidence that a threshold is right.
- **This applies to any number a human will look at a pixel through**, not just
  numbers the pipeline reads. *A preview of a diagnostic image is part of the
  diagnostic.* The Cells tab exists because the closed-counter bug was only
  found once someone saw a picture of the preprocessed cell -- so a preview that
  resamples that picture defeats the tab rather than serving it. Framing
  constants that only decide layout need no measurement, but they do need to be
  one named constant: two numbers describing one affordance (a handle drawn at
  one size and grabbed at another) is the `FIELD_WIDTH` problem in miniature.
- **Synthetic frames are for regression, not calibration.** `synth.py` renders
  softkey strips for the test suite. They do not use X-Plane's font and must
  never be the basis for a tuning decision.
- **Prefer a mechanism that cannot be wrong over a value that must be right.**
  The sharpening *ladder* (try several, keep what the vocabulary agrees with)
  replaced a single tuned value for this reason. Page lookup replaced trying
  ever harder to recognise a ten-pixel digit.
- **Say what was verified and what was not.** This project is developed on
  Linux with no Windows and no X-Plane, so large parts cannot be executed here.
  Claiming otherwise wastes the user's time discovering it.

## Things that will catch you out

**Publishing dispatches on the Python type of the value, not a registry.**
`publish(values: Mapping[str, Value])` sends a `str` as base64 into a Data
dataref and a number bare into an Int one. X-Plane types the dataref, so the
daemon and the plugin have to agree without either checking: send a number to a
name the plugin registered as `Type_Data` and the write fails at the sim, not
here. Adding a dataref means touching both sides.

**The plugin may only import the standard library.** It runs inside XPPython3's
own bundled Python 3.12, not this project's venv. No numpy, no requests.

**Field width is set in three places that must agree**: `FIELD_WIDTH` in the
plugin (changing it needs an X-Plane restart -- the buffer is allocated at
accessor registration), `publish.field_width` in config, and every PilotsDeck
button address the user has written (`:s64`). Out of step means truncated or
garbage labels. It went 16 -> 64 once already, which cost the user a re-edit of
every button; it is deliberately generous now so it does not move again.

**Change gating caches post-processing results.** A cell whose pixels have not
moved is served from cache, and that cache holds the result from *after* page
lookup. Re-running a later stage over cached results makes it re-derive its own
earlier output -- which shipped once as a bug where corrections were re-reported
as confirmations. Note what the gate is *for*: skipping OCR, which is
expensive. Background colour is classified outside it, on every cell of every
frame, because a softkey becoming selected changes the background while leaving
the label identical -- so anything cheap that must not miss that case belongs
outside the gate too.

**Windows-only paths cannot be tested here**: capture, window enumeration,
resizing and placement, the installer scripts, anything touching a live
X-Plane, and three things in the GUI -- `pythonw.exe`, `CTRL_BREAK_EVENT` as
the way Stop reaches the daemon, and `os.startfile`.

**Only `[window_management]` sizes or moves a window, and nothing else does at
all.** Opening a capture finds its window and changes nothing about it. There
was briefly a per-display `manage_window_size` / `window_size` beside it, and
two settings fixing one window's size needs a rule about which wins -- making
the loser a setting that is quietly ignored, which is how the Settings form
came to draw a tickbox that did nothing. If you are about to add a second way
to size a window, don't.

**A frame from a closed window is not a frame.** `_LatestFrame` goes empty for
good once the capture reports `on_closed`, and `run` treats that as "reopen the
pop-out and build a new source" -- a WGC session does not outlive its window,
so there is nothing to reconnect. It shipped without that rule and the bug hid
itself completely: the slot kept serving the last frame of a closed pop-out, so
the labels froze where they were, the starved-display warning never fired
because frames were still arriving, and the recovery never ran. If you are
tempted to serve a cached frame when a source has nothing, this is why not.

**A window's size and its position are measured from different rectangles.**
`WindowInfo.width/height` is the *client* area, because that is what capture
sees; `WindowInfo.x/y` is the *window* origin, because that is the corner
Windows positions by. The difference is the frame, which `_set_window`
measures rather than assumes. Do not "fix" them into agreement.

**Any code that parses another module's output is a coupling, and every one
needs a test that builds its input by calling the real producer.** Never a
pasted sample: a sample records what the formatter did for one easy case, and
the cases that break are the ones nobody thinks to paste. That is not
hypothetical -- `WindowInfo.__str__` formats with `repr`, which switches to
double quotes when a title contains an apostrophe, and a hand-typed fixture
hid it until a window called `Cirrus SR22's PFD` vanished from the list with
"No windows matched" on screen. Parsers therefore live in `gui/logparse.py`,
never beside their caller, so this is enforceable by looking in one place.
Adding a parser means adding a row here and a format-then-parse test.

None of these couplings is visible to the type checker:

| if you change | the test that fails |
| --- | --- |
| a subcommand or a flag in `main.py` | `test_gui_commands.py` -- every argv the GUI can build is parsed by `build_parser()`, and it asserts the GUI covers every subcommand |
| what `_format_row` prints | `test_gui_logparse.py` -- it formats a `DisplayResult` and parses it back |
| `WindowInfo.__str__` | `test_gui_logparse.py` -- `parse_window` is fed a formatted `WindowInfo`, over quoting, backslashes, non-ASCII, and the negative screen coordinates a monitor left of or above the primary is addressed by |
| which displays `manage_windows` reports as managed | `test_capture.py` -- `sources_for` must not resize a window window management already sized |
| what `calibrate`, `dump-colors`, `screen-template` or `tune` print | `test_gui_logparse.py` -- `parse_calibration` / `parse_colors` / `parse_screen_block` / `parse_tuning_result` are fed the real commands' output over synthetic frames or a scripted search |
| the shape of a `tune --truth` file | `test_tuning.py` -- the GUI's `configio.dumps_truth` (which cannot import `tuning.py`: that pulls in cv2 and Tesseract, and the GUI must never run the pipeline in its own process) is fed through the real `tuning.load_truth` |
| a field on any config dataclass | `test_gui_schema.py` -- a field must be in `gui/schema.py` or in `NOT_IN_THE_FORM` with a reason |
| `strip_rect` or `cell_rects` | `test_gui_geometry.py` and `test_gui_canvas.py` -- the calibration editor draws what `cell_rects` returns, and both check it against the real thing |
| `clipped_edges` or `CLIP_MARGIN` | `test_gui_checks.py` -- the margin is 0 because the ink extents were measured across the corpus, and a test keeps that measurement true; the calibration editor and `run -v` share the function |
| a tab class name, or the setup steps | `test_gui_wizard.py` -- every step in `gui/wizard.py` names its tab by class name, and the test resolves each through `tabs.tab_index` |

Fix the GUI in the same commit; do not weaken the test. A GUI that has drifted
from the daemon still looks like it is working, which is what makes it worth a
test rather than a comment.

## Working on it

```
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

The window itself can be driven headlessly the same way, which is how it was
checked: `xvfb-run -a python -m glasslinkxp.gui`, with `PIL.ImageGrab` for
screenshots. Point the frame source at `frames/` and every tab works with no
X-Plane and no Windows.

Every command takes `--image <png|dir>` in place of live capture, so the whole
pipeline runs without Windows or X-Plane. `-v` prints a line per cell showing
the raw OCR string, what it snapped to, confidence, and any substitution.

`-c config.toml` selects a config; `-v` and `-c` work before or after the
subcommand.

## Updating the documentation

Docs here are load-bearing -- they are how the next session avoids repeating a
week of dead ends. When you change behaviour, update the docs in the same
commit, not afterwards.

| if you change | also update |
| --- | --- |
| a pipeline stage, or the order of stages | the flowcharts in `docs/PIPELINE.md` |
| how the pop-out windows are opened, sized or placed | the *Managing the pop-out windows* flowchart and rationale in `docs/PIPELINE.md`, and the `[window_management]` block in `src/config.example.toml` |
| anything printed by `-v` | the "Reading the debug output" table in `docs/PIPELINE.md` |
| a config setting, or its default | `gui/schema.py` (the help text is the documentation), then regenerate `docs/CONFIGURATION.md` -- a test fails until you do -- and `src/config.example.toml` |
| a CLI subcommand or flag | the command list in `CLAUDE.md` and the relevant `README.md` section, and `gui/commands.py` -- `test_gui_commands.py` fails until the GUI covers it |
| a tab, or how the GUI runs a command | `docs/GUI.md`, including its flowchart and the tab table |
| the setup steps, or what a step checks | the *setup wizard is a bar* section and its flowchart in `docs/GUI.md`, and the first-run list in `README.md` |
| anything a CLI subcommand prints that the GUI reads | `gui/logparse.py` -- and its format-then-parse test; see *Things that will catch you out* |
| a config dataclass field | `gui/schema.py` -- see *Things that will catch you out* |
| dataref names, types or field width | `README.md` PilotsDeck wiring, the plugin docstring, `src/config.example.toml` |
| what has been tested on real hardware | the *Verified offline* / *Not verified here* sections of `docs/DEVELOPER.md` |

**The Mermaid diagrams in `docs/` must be re-rendered to confirm they still
parse.**
A malformed diagram renders as an error graphic on GitHub, not a build failure,
so nothing else will catch it:

```
npx -y @mermaid-js/mermaid-cli@11 -i docs/PIPELINE.md -o /tmp/render.md \
  -p /tmp/pc.json      # pc.json: {"args":["--no-sandbox"],"executablePath":"<chromium>"}
npx -y @mermaid-js/mermaid-cli@11 -i docs/GUI.md -o /tmp/render-gui.md -p /tmp/pc.json
```

Check the output SVGs contain real nodes and no "syntax error" text.

**Write commit messages that explain why**, and distinguish what was measured
from what was assumed. The git history is the main record of which approaches
were tried and rejected, and that record is what stops the next session
re-trying them.

## Conventions

- Branch from the current `main`; open a PR against `main`. There is no PR
  template -- write the body plainly, with an explicit note of what was
  verified and what could not be.
- End every commit message with:

  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  ```

- Do not put a model name in commits, PR bodies, code comments, or any other
  file in the repository.
- The `labels.txt` vocabulary and `screens.toml` page definitions are meant to
  be edited by users. Keep them commented and readable rather than terse.
