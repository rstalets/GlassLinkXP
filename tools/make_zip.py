"""Build the distribution zip: ``src/`` and nothing else.

    python tools/make_zip.py            -> dist/glasslinkxp-<version>.zip

What a user downloads is exactly the contents of ``src/``, so this zips that
directory with its own files at the top level -- extract it and install.cmd is
right there. Development-only anything (tests, docs, this script) lives
outside ``src/`` and so cannot end up in it by accident.

Excluded from the zip are the things a *developer's* src/ picks up but a fresh
download must not have: the venv, build leftovers, and any config, vocabulary
or captured frames from actually running the app here. A zip carrying a
config.toml would hand the next user this machine's calibration, and the
window would not offer them setup at all.
"""

from __future__ import annotations

import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "src"

#: Directories never zipped, matched on any path segment.
EXCLUDED_DIRS = frozenset({
    ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist",
    "tessdata", "calibration", "cells", "frames", "tuning",
})

#: Files never zipped: what running the app here leaves behind.
EXCLUDED_FILES = frozenset({
    "config.toml", "config.toml.bak", "gui.json",
    "labels.txt.bak", "labels.shipped.txt",
})

EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".bak")


def should_include(relative: Path) -> bool:
    """Whether a path inside src/ belongs in a fresh download.

    ``glasslinkxp/labels.txt`` is the shipped vocabulary and does belong; a
    ``labels.txt`` at the top level is the *user's* copy, made by running the
    app here, and does not.
    """
    if any(part in EXCLUDED_DIRS for part in relative.parts):
        return False
    if relative.suffix in EXCLUDED_SUFFIXES:
        return False
    if relative.name.endswith(".egg-info") or any(p.endswith(".egg-info") for p in relative.parts):
        return False
    if len(relative.parts) == 1 and relative.name in EXCLUDED_FILES:
        return False
    return relative.name not in {"gui.json", "config.toml"}


def version() -> str:
    data = tomllib.loads((SHIPPED / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def build(out_dir: Path | None = None) -> Path:
    out_dir = out_dir or (ROOT / "dist")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"glasslinkxp-{version()}.zip"
    written = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(SHIPPED.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(SHIPPED)
            if not should_include(relative):
                continue
            zf.write(path, relative.as_posix())
            written += 1
    print(f"{target}  ({written} files)")
    return target


if __name__ == "__main__":  # pragma: no cover - a one-line build
    sys.exit(0 if build() else 1)
