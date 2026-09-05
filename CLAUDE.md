# Working on g1000-softkey

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
| `README.md` | Setup, calibration workflow, PilotsDeck wiring, troubleshooting, and -- importantly -- *Verified offline* and *Not verified here*. |
| `PLAN.md` | The original design rationale, including approaches that were considered and rejected. |
| `config.example.toml` | Every setting, with the reasoning for its default written beside it. Often the fastest answer to "why is this value what it is". |

## Shape of the system

Two processes, deliberately:

- **A standalone Python daemon** (`g1000_softkey/`) captures, recognises and
  publishes. All the expensive work lives here.
- **A small XPPython3 plugin** (`xppython3/PI_G1000SoftkeyLabels.py`) does
  nothing but *create* the datarefs, because X-Plane's Web API can write a
  dataref but cannot create one.

They are split this way because XPPython3 callbacks run inline with X-Plane's
flight loop, so anything slow there costs frame rate.

```
g1000_softkey/
  main.py       CLI: run, list-windows, calibrate, dump-cells, dump-colors,
                     bench, screen-template, learn, tune, synth
  capture.py    Windows Graphics Capture, plus a PNG backend for offline work
  strip.py      strip crop, 12-cell split, per-cell preprocessing
  ocr.py        Tesseract, label vocabulary, CellResult
  color.py      border-ring sampling, HSV background classification
  pipeline.py   DisplayPipeline.process(): one frame -> 12 CellResults
  publish.py    WebSocket / REST / file / console publishers
  screens.py    softkey page definitions (screens.toml)
  signatures.py glyph shape fallback, off by default
  config.py     frozen dataclasses + TOML loader
```

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
  `screen-template` all exist because of this. One such measurement is still
  outstanding: the `[color]` HSV thresholds ship as plausible swatch values and
  have never been checked against a real G1000 frame -- `dump-colors` prints
  what a live capture actually contains, so replace them rather than trusting
  them.
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

**Windows-only paths cannot be tested here**: capture, window enumeration and
resizing, the installer scripts, and anything touching a live X-Plane.

## Working on it

```
python -m pytest -q              # 219 tests, all offline, keep them green
python -m g1000_softkey.main synth --out frames
python -m g1000_softkey.main run --once --image frames/xpdr.png --publisher console -v
```

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
| anything printed by `-v` | the "Reading the debug output" table in `docs/PIPELINE.md` |
| a config setting, or its default | `config.example.toml`, including *why* the default is what it is |
| a CLI subcommand or flag | the command list in `CLAUDE.md` and the relevant `README.md` section |
| dataref names, types or field width | `README.md` PilotsDeck wiring, the plugin docstring, `config.example.toml` |
| what has been tested on real hardware | the *Verified offline* / *Not verified here* sections of `README.md` |

**The Mermaid diagrams must be re-rendered to confirm they still parse.**
A malformed diagram renders as an error graphic on GitHub, not a build failure,
so nothing else will catch it:

```
npx -y @mermaid-js/mermaid-cli@11 -i docs/PIPELINE.md -o /tmp/render.md \
  -p /tmp/pc.json      # pc.json: {"args":["--no-sandbox"],"executablePath":"<chromium>"}
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
