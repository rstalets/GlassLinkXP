"""Build the distribution zip: ``src/`` and nothing else.

    python tools/make_zip.py                    -> dist/glasslinkxp-0.0.0.zip
    python tools/make_zip.py --version v1.2.3   -> dist/glasslinkxp-1.2.3.zip

What a user downloads is exactly the contents of ``src/``, so this zips that
directory with its own files at the top level -- extract it and install.cmd is
right there. Development-only anything (tests, docs, this script) lives
outside ``src/`` and so cannot end up in it by accident.

Excluded from the zip are the things a *developer's* src/ picks up but a fresh
download must not have: the venv, build leftovers, and any config, vocabulary
or captured frames from actually running the app here. A zip carrying a
config.toml would hand the next user this machine's calibration, and the
window would not offer them setup at all.

The version is stamped in at build time, not held in the tree: the checked-in
files say ``0.0.0`` and the release workflow passes the tag it is building.
Three files carry it, and they are stamped in one operation because the first
two have to move *together* -- ``pyproject.toml`` names the version and
``uv.lock`` records the version it locked, and ``uv sync --locked`` (which is
what install.ps1 runs on the user's machine) *fails* when they disagree. That
was measured, not assumed: bumping pyproject.toml alone makes `uv lock
--check` report the lockfile out of date, and stamping both makes it pass
again. So stamping either without the other would ship a zip that cannot
install.

The third is ``glasslinkxp/VERSION``, which is the only one the *running* app
reads: every command logs it on its first line, so a log says which download
produced it. It is stamped here rather than derived from the manifest at
runtime because the manifest sits beside the install root, not inside the
package, and because a number a user reads in a bug report should not depend
on an import succeeding.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "src"

#: What an unreleased tree says it is. A build with no --version is a
#: developer's build and is not a release of anything.
DEFAULT_VERSION = "0.0.0"

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

#: A release number, PEP 440's common shapes only: 1.2.3, 1.2.3rc1,
#: 1.2.3.post1, 1.2.3.dev1. Deliberately narrow -- a tag that is not one of
#: these is a mistake to stop at, not a string to write into the manifest.
VERSION_RE = re.compile(
    r"^[0-9]+(?:\.[0-9]+)*"
    r"(?:(?:a|b|rc)[0-9]+)?"
    r"(?:\.post[0-9]+)?"
    r"(?:\.dev[0-9]+)?$"
)


class VersionError(ValueError):
    """A tag or --version that is not something to build from."""


def normalise_version(raw: str) -> str:
    """``v1.2.3`` (how tags are written) -> ``1.2.3`` (what the manifest takes).

    Raises rather than guessing: the caller is a release workflow, and a
    version it cannot parse means the release is named something nobody
    intended to ship.
    """
    version = raw.strip()
    if version[:1] in "vV":
        version = version[1:]
    if not VERSION_RE.fullmatch(version):
        raise VersionError(
            f"{raw!r} is not a version to build from -- expected something "
            "like 1.2.3, 1.2.3rc1 or v1.2.3"
        )
    return version


def _substitute(text: str, pattern: re.Pattern[str], version: str, what: str) -> str:
    """Replace exactly one version, or refuse to build.

    Not `count=1 and hope`: if the file's shape changed, the substitution
    silently doing nothing would ship a zip stamped 0.0.0 under a release
    number, which is the sort of thing nobody notices until an install fails.
    """
    stamped, n = pattern.subn(lambda m: m.group(1) + version + m.group(2), text, count=1)
    if n != 1:
        raise VersionError(f"could not find the version to stamp in {what}")
    return stamped


def stamp_pyproject(text: str, version: str) -> str:
    """Set ``version`` in the ``[project]`` table."""
    pattern = re.compile(r'(?m)^(\[project\]\n(?:(?!\[).*\n)*?version = ")[^"]*(")')
    return _substitute(text, pattern, version, "pyproject.toml")


def stamp_lock(text: str, version: str, name: str) -> str:
    """Set the version uv recorded for the project itself.

    Only the project's own ``[[package]]`` block: every other one is a
    dependency, pinned and hashed, and none of them move for a release.
    """
    pattern = re.compile(
        r'(?m)^(\[\[package\]\]\nname = "' + re.escape(name) + r'"\nversion = ")[^"]*(")'
    )
    return _substitute(text, pattern, version, "uv.lock")


def stamp_version_file(text: str, version: str) -> str:
    """The whole file is the version, so there is nothing to substitute.

    ``text`` is taken only to keep the three stampers one shape; a VERSION
    file with anything else in it is not something to preserve.
    """
    return f"{version}\n"


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


#: The files a release stamps, and how. Every one is written into the zip
#: from memory rather than edited in the tree: a build leaves the checkout as
#: it found it, so there is no half-stamped state to commit by accident.
STAMPED = {
    "pyproject.toml": lambda text, version, name: stamp_pyproject(text, version),
    "uv.lock": lambda text, version, name: stamp_lock(text, version, name),
    "glasslinkxp/VERSION": lambda text, version, name: stamp_version_file(text, version),
}


def project() -> dict:
    return tomllib.loads((SHIPPED / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def version() -> str:
    """What the tree says it is -- ``0.0.0`` unless someone edited it."""
    return str(project()["version"])


def build(out_dir: Path | None = None, release_version: str | None = None) -> Path:
    """Zip ``src/``, stamping ``release_version`` in if one was given."""
    out_dir = out_dir or (ROOT / "dist")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamped = release_version or version()
    name = str(project()["name"])
    target = out_dir / f"{name}-{stamped}.zip"
    written = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(SHIPPED.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(SHIPPED)
            if not should_include(relative):
                continue
            arcname = relative.as_posix()
            stamper = STAMPED.get(arcname) if release_version else None
            if stamper is not None:
                zf.writestr(arcname, stamper(path.read_text(encoding="utf-8"), stamped, name))
            else:
                zf.write(path, arcname)
            written += 1
    print(f"{target}  ({written} files)")
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the distribution zip from src/.")
    parser.add_argument(
        "--version",
        help="version to stamp into pyproject.toml and uv.lock, with or "
             "without a leading v. Omit for a developer build, which keeps "
             f"the tree's {DEFAULT_VERSION}.",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=ROOT / "dist",
        help="where to write the zip (default: dist/)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        release_version = normalise_version(args.version) if args.version else None
    except VersionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    build(args.out_dir, release_version)
    return 0


if __name__ == "__main__":  # pragma: no cover - a one-line build
    sys.exit(main())
