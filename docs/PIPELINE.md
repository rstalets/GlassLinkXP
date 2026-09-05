# How the daemon works

Two views: what happens once at startup and on every cycle, and what happens to
a single captured frame.

> **On naming.** What the config and these diagrams call a *page* or *screen* --
> `screens.toml`, `screen_confidence` -- is the softkey-page signature idea:
> the labels that read cleanly identify which page is showing, and the page
> supplies the ones that did not. There is a separate, off-by-default
> *shape signature* fallback in `signatures.py` that compares glyph pixels;
> it is a different mechanism and is not in these diagrams.

## The daemon

```mermaid
flowchart TD
    START([g1000 run]) --> CFG[Load config.toml]
    CFG --> SRC["Open a capture source per display<br/>Windows Graphics Capture, or a PNG"]
    CFG --> RDR["Build the OCR reader<br/>persistent Tesseract API, labels.txt, screens.toml"]
    CFG --> PUB[Build the publisher]

    PUB --> RESOLVE{"Resolve dataref names to ids<br/>GET /api/v1/datarefs"}
    RESOLVE -->|24 of 24| READY[Ready]
    RESOLVE -->|some missing| WARN["Warn: is PI_G1000SoftkeyLabels.py<br/>installed in PythonPlugins?"]
    WARN --> READY
    RESOLVE -->|X-Plane not reachable| RETRY["Retry every retry_interval"]
    RETRY --> RESOLVE

    SRC --> READY
    RDR --> READY

    READY --> LOOP{{"Every 1 / loop_hz seconds"}}
    LOOP --> GRAB[Grab a frame per display]
    GRAB --> PROC["Process the frame<br/>see below"]
    PROC --> DIFF{"Any label changed<br/>since last publish?"}
    DIFF -->|no| SLEEP
    DIFF -->|yes| SEND["Publish the changed cells"]
    SEND --> SLEEP[Sleep the rest of the cycle]
    SLEEP --> LOOP

    LOOP -->|SIGINT| STOP([Close socket, Tesseract, capture])
```

Publishing prefers one WebSocket message for the whole strip and falls back to
one REST write per changed cell:

```mermaid
flowchart LR
    CHANGED["Cells whose label changed"] --> WS{"WebSocket up?"}
    WS -->|yes| BATCH["One dataref_set_values message<br/>fire and forget"]
    WS -->|"no, backing off"| REST["One PATCH per cell<br/>slower, but the labels still land"]
    BATCH -->|send fails| DROP["Drop the socket,<br/>reconnect next cycle"]
    DROP --> REST
```

## One frame

```mermaid
flowchart TD
    FRAME([Captured frame]) --> CROP["Crop the softkey strip<br/>fractional geometry from config"]
    CROP --> SPLIT["Split into 12 cells<br/>minus cell padding"]
    SPLIT --> GATE{"Cell pixels changed<br/>since last frame?"}

    GATE -->|"no (the common case)"| CACHE["Reuse the previous result<br/>no OCR at all"]
    GATE -->|yes| INK{"ink ratio >= blank_ink_ratio?"}

    INK -->|no| BLANK["Empty cell<br/>never reaches OCR"]
    INK -->|yes| LADDER["Preprocess once per sharpen_ladder rung:<br/>unsharp mask, upscale, threshold,<br/>normalise polarity, crop to content"]

    LADDER --> OCR["Tesseract reads each variant"]
    OCR --> SNAP["Normalise, then snap to the<br/>nearest label in labels.txt"]
    SNAP --> RANK{"Exact label hit<br/>and confidence >= accept_confidence?"}
    RANK -->|yes| TAKE["Take it, skip the remaining rungs"]
    RANK -->|"no, rungs left"| LADDER
    RANK -->|"no, rungs exhausted"| BEST["Keep the best:<br/>exact hit first, then confidence"]

    TAKE --> CELLS[12 cell results]
    BEST --> CELLS
    CACHE --> CELLS
    BLANK --> CELLS

    CELLS --> NEED{"Any cell below<br/>screen_confidence?"}
    NEED -->|"no -- nothing to help"| OUT
    NEED -->|yes| IDENT{"Do some page's match cells<br/>all read above<br/>screen_match_confidence?"}

    IDENT -->|"no page matches"| OUT
    IDENT -->|"two pages tie"| OUT
    IDENT -->|yes| APPLY["Take only the shaky cells<br/>that page defines"]

    APPLY --> AGREE{"Does the cell already<br/>agree with the page?"}
    AGREE -->|yes| CONF["Mark confirmed<br/>value unchanged"]
    AGREE -->|no| REPL["Replace with the page's value"]

    CONF --> OUT([12 labels])
    REPL --> OUT
```

### Why it is shaped like this

**Change gating first.** Softkeys change rarely, so almost every cycle finds
nothing new and costs one crop and one array compare. All the expensive work
below only runs on cells whose pixels actually moved.

**A ladder rather than one sharpening setting.** The glyphs are around ten
pixels tall. Thresholding at that size can close the counters of 0, 6, 8 and 9,
and a filled counter is not a character -- Tesseract returns an empty string
rather than a wrong digit. Sharpening reopens them, but too much rings and
grows strokes instead, turning a 0 into a B. The amount that works depends on
the font and the capture scale, so each rung is tried and the answer the
vocabulary agrees with wins. The first rung is no sharpening at all.

**Confidence gates the early exit.** Landing on a known label is not proof:
every digit 0-7 is a valid softkey, so a 0 misread as 2 still matches exactly.
Only a hit that is also confident ends the search.

**Page lookup runs only when something needs it.** The stage exists to serve
cells OCR was unsure about, so the first question is whether any exist. When
every cell read confidently there is nothing to answer and no page is looked
for at all -- which is the common case, and costs one comparison rather than a
scan of the page library.

**Page lookup last.** Recognising a ten-pixel 0 is hard. Noticing that IDENT,
BKSP and BACK occupy cells 9-11 is easy, and that only happens on the
transponder keypad -- whose layout is fixed. So the cells OCR finds hardest are
exactly the ones a page can answer without recognising anything. The page is
identified once per strip, from cells read at a *higher* bar than the one it
fills, so an identification is never built on a guess.

## Reading the debug output

`run -v` prints one line per cell, after page lookup, so the line always
matches the value that gets published:

```
pfd screen lookup: matched 'xpdr-code' on cells [9, 10, 11]; replaced [1]; confirmed [5, 6, 8]
pfd cell 1  ink=0.0498 x=0.53-0.63 raw=''   ocr=''   conf=  0.0 match=0.00 -> '0' FROM PAGE 'xpdr-code'
pfd cell 5  ink=0.0405 x=0.51-0.60 raw='4'  ocr='4'  conf= 43.0 match=1.00 -> CONFIRMED BY PAGE 'xpdr-code'
pfd cell 7  ink=0.0447 x=0.50-0.60 raw='6'  ocr='6'  conf= 96.0 match=1.00
pfd cell 12 BLANK   ink=0.0000 < 0.0040 (contrast=40) -- never reached OCR
```

| what you see | what it means |
| --- | --- |
| `raw` then `ocr` | what Tesseract returned, then the label it snapped to |
| `FROM PAGE` | OCR was unsure and the page supplied the value |
| `CONFIRMED BY PAGE` | OCR was unsure, but the page agreed -- value unchanged |
| neither | read confidently enough that the page was not consulted |
| `BLANK` | discarded before OCR; the ink figure says by how much |
| `x=` | horizontal extent of the ink; `CLIPPED?` if it touches an edge |

The distinction between the middle two matters when a label looks wrong: a cell
carrying `CONFIRMED` was checked against a known page, while a bare line means
nothing corroborated it.
