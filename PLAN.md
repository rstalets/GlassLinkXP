# G1000 Softkey Label OCR → X-Plane Dataref → Stream Deck (PilotsDeck)

## Problem

X-Plane 12's G1000 softkey labels are computed inside the engine and rasterized straight to
the GPU. They are not exposed as datarefs, so a Stream Deck can only issue
`sim/GPS/g1000nX_softkeyN` commands with static "BTN 1..12" faces. The goal is to put the
*live* label on each key.

## Approach

Read the pixels, since that is the only place the truth exists.

```
X-Plane pop-out PFD/MFD windows (backgrounded)
        │  Windows Graphics Capture (works while occluded)
        ▼
   frame (BGRA numpy)
        │  crop softkey strip → split into 12 cells
        ▼
   per-cell image
        │  upscale → grayscale → invert → threshold
        ▼
   tesserocr (persistent API, PSM 7, uppercase whitelist)
        │  snap to known G1000 label vocabulary (fuzzy)
        ▼
   24 label strings
        │  X-Plane Web API (REST, base64) PATCH
        ▼
   byte-array datarefs created by a tiny XPPython3 plugin
        │  PilotsDeck reads `glasslinkxp/softkey/pfd/1:s16`
        ▼
   Stream Deck button face
```

## Why this shape

**Two processes, not one.** A ~40-line XPPython3 plugin does nothing but *create* 24
writable byte-array datarefs (the Web API can write datarefs but cannot create them). All
the capture/OCR logic lives in a standalone Python daemon that can be restarted, tuned, and
crash-looped without touching the sim. Heavy OCR work also stays off X-Plane's flight loop
thread, which matters — XPPython3 callbacks run inline with the sim.

**Windows Graphics Capture, not BitBlt.** X-Plane renders through Vulkan/OpenGL; `BitBlt`
and `PrintWindow` return black or stale frames for GPU-composited, occluded windows. WGC
(`windows-capture`) is designed for exactly this and keeps working when the window is
behind others. Note: on Windows 10 and early Win11 builds WGC draws a yellow capture border;
it can be disabled on Win11 build 20348+ via `IsBorderRequired`.

**Dictionary snapping, not hashing.** The user ruled out hashing (brightness-sensitive,
complex). But post-OCR *fuzzy matching against the known set of G1000 softkey labels* is a
different mechanism and a large accuracy win: it turns `DCLTP` into `DCLTR` and `lNSET`
into `INSET` for free, and it costs a `difflib` call. The vocabulary lives in an editable
`labels.txt`.

**Per-cell change gating.** Compare each cell's pixels to the previous frame; only run OCR
on cells that actually changed. Softkeys change rarely, so steady-state CPU is near zero.
This is a plain array comparison used as a cache check — not a hash lookup table.

## Deliverables

```
glasslinkxp/
  main.py            CLI: run | list-windows | calibrate | dump-cells | bench
  capture.py         WGC backend + static-image backend (for offline dev/test)
  strip.py           strip crop, 12-cell split, preprocessing
  ocr.py             persistent tesserocr API, whitelist, vocab snapping
  publish.py         X-Plane Web API client (base64 PATCH) + JSON-file fallback
  config.example.toml
  labels.txt
xppython3/PI_GlassLinkXP.py   creates the 48 datarefs (24 labels + 24 colours)
tests/                               offline pipeline tests on synthetic strips
```

Dataref names: `glasslinkxp/softkey/pfd/1..12`, `glasslinkxp/softkey/mfd/1..12` (16-byte each).
PilotsDeck address: `glasslinkxp/softkey/pfd/1:s16`.

## Calibration is the real risk

Strip geometry depends on pop-out window size, bezel, and title bar. So the POC ships a
`calibrate` mode that saves the raw frame, the proposed crop, and a cell-boundary overlay
as PNGs, plus `dump-cells` to write each of the 24 cell images. Geometry is expressed as
*fractions* of the client area in TOML so it survives resizes, with a coarse auto-detect
(find the dark horizontal band at the bottom) to seed the values.

## Success criteria for the POC

1. Correct label on all 12 PFD keys across ≥5 different softkey menu levels (top level, MAP,
   PFD, XPDR, CDI submenus).
2. End-to-end latency under ~300 ms from softkey press to updated Stream Deck face.
3. Steady-state CPU under ~5% of one core at 4 Hz with change gating on.
4. No interference with sim frame rate.

## Open questions to resolve on real hardware

- Does the X-Plane Web API accept a PATCH write to a *plugin-created* Data dataref?
  (Fallback: plugin polls a JSON file the daemon writes — implemented behind a flag.)
- Exact pop-out window title/class for HWND matching (`list-windows` mode answers this).
- Whether the highlighted/selected softkey background hurts OCR (mitigation: per-cell
  adaptive threshold rather than a global one; the highlight state is also worth exposing
  later as a separate numeric dataref).

## Explicitly out of scope for the POC

Auto-start/supervision, installer, multi-aircraft profiles, copilot PFD (`g1000n2`),
selected-state datarefs, and any Stream Deck profile authoring.
