"""What version this is, read from the file the release build writes.

There is no version in the source, and no ``VERSION`` file in a checkout:
``tools/make_zip.py`` creates one, holding the release tag, when it builds
the zip. So a copy that came from a download says which download, and a copy
running from a clone finds nothing and says :data:`NO_VERSION`.

That asymmetry is the point, and it was got wrong first time round: the file
was checked in holding ``0.0.0``, so every clone reported ``0.0.0`` -- which
reads in a log or a bug report like a build somebody released, not like the
absence of one. A dev build has no version, and the honest thing is to say so.

A plain text file rather than the manifest, for the same reason: the manifest
is in every checkout, so anything derived from it would give a dev build the
same answer a release gets. It is also read on every start of every command,
and there is nothing here worth a TOML parse or an installed-metadata lookup
that an editable install can get wrong.
"""

from __future__ import annotations

from pathlib import Path

#: One line, no leading v: ``1.2.3``.
VERSION_FILE = Path(__file__).with_name("VERSION")

#: What a copy with no readable VERSION file reports. A dev build run straight
#: from a working tree that has not got the file, or a copy the file did not
#: ship in, is not a reason to refuse to start -- but it must not look like a
#: version either, so this is deliberately not a number: shouted, unparseable,
#: and obviously not something a release was ever tagged.
NO_VERSION = "NO_VERSION"


def read_version(path: Path = VERSION_FILE) -> str:
    """The version in ``path``, or :data:`NO_VERSION` if it cannot be read."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return NO_VERSION
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    return first or NO_VERSION


#: Read once, at import: the file does not change under a running process.
__version__ = read_version()


def display_version(version: str = __version__) -> str:
    """How the version is shown to a person: ``v1.2.3``.

    The placeholder is passed through as it is -- ``vNO_VERSION`` would read
    like a version somebody tagged, which is the one thing it must not do.
    """
    return version if version == NO_VERSION else f"v{version}"
