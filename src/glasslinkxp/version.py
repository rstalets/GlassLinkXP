"""What version this is, read from the file the release build stamps.

There is no version in the source: ``VERSION`` beside this module says
``0.0.0`` in a checkout, and ``tools/make_zip.py`` writes the release tag into
it (along with ``pyproject.toml`` and ``uv.lock``) when it builds the zip. So
a running copy can always say which download it came from, and a copy built
from a checkout says ``0.0.0``, which is true.

A plain text file rather than the manifest: this is read on every start of
every command, ``pyproject.toml`` is not beside the *package* but beside the
install root, and a version is one line -- there is nothing here worth a TOML
parse or an installed-metadata lookup that an editable install can get wrong.
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
