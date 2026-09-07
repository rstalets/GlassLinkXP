# GlassLinkXP

Puts the **live** G1000 softkey labels on a Stream Deck, by reading them off
the X-Plane 12 screen and republishing them as datarefs.

**[Download and install instructions](src/README.md)** — that file is the one
that ships to users, inside the zip.

> **This is experimental.** See
> [`docs/DEVELOPER.md`](docs/DEVELOPER.md#verified-offline) for what has and
> has not been checked against real hardware.

## This repository

| | |
| --- | --- |
| `src/` | **everything that ships.** Zipped as-is, it is what a user downloads and extracts; `install.cmd` sits at its top level. Nothing outside it is available at install time. |
| `tests/` | the offline test suite (`xvfb-run -a src/.venv/bin/python -m pytest -q`) |
| `docs/` | [PIPELINE](docs/PIPELINE.md), [GUI](docs/GUI.md), [CONFIGURATION](docs/CONFIGURATION.md), [DEVELOPER](docs/DEVELOPER.md) |
| `tools/make_zip.py` | builds `dist/glasslinkxp-<version>.zip` from `src/` |
| `CLAUDE.md` | the conventions this codebase is written to |

```
uv sync --project src        # the venv lands in src/.venv
xvfb-run -a src/.venv/bin/python -m pytest -q
src/.venv/bin/python tools/make_zip.py
```
