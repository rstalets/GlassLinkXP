"""What ships is exactly ``src/``, and it is self-sufficient.

``src/`` is zipped as-is and handed to a user, so anything the installer needs
at install time has to be inside it -- pyproject.toml and uv.lock included.
This was wrong once: the manifest sat at the repository root, outside the zip,
so the installer would have had nothing to `uv sync` from.

Nothing here needs Tk or a display; it is a check on the tree.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "src"

#: Everything the user needs at install time or afterwards.
MUST_SHIP = (
    "install.cmd",                        # what they double-click
    "install.ps1",
    "pyproject.toml",                     # what uv sync reads
    "uv.lock",
    ".python-version",
    "glasslinkxp/main.py",                # the app
    "glasslinkxp/labels.txt",             # the vocabulary it ships with
    "glasslinkxp/screens.toml",
    "glasslinkxp.cmd",                    # the launchers
    "glasslinkxp-gui.cmd",
    "config.example.toml",                # what a new config is seeded from
    "xppython3/PI_GlassLinkXP.py",        # the X-Plane side
    "scripts/install-xplane-plugin.ps1",
    "README.md",
    "LICENSE",
)

#: Development-only, and must not be dragged into the zip.
MUST_NOT_SHIP = ("tests", "docs", "CLAUDE.md", "PLAN.md")


@pytest.mark.parametrize("relative", MUST_SHIP)
def test_the_zip_contains_everything_install_time_needs(relative):
    assert (SHIPPED / relative).exists(), f"src/{relative} is missing from what ships"


@pytest.mark.parametrize("relative", MUST_NOT_SHIP)
def test_development_only_things_stay_out_of_the_zip(relative):
    assert not (SHIPPED / relative).exists(), f"src/{relative} would ship to users"
    assert (ROOT / relative).exists(), f"{relative} should still be in the repository"


def test_the_installer_only_requires_files_that_ship():
    """The installer's own sanity check, read out of it rather than restated.

    It refuses to run if one of these is missing next to it, so every one has
    to be a file the zip actually contains.
    """
    text = (SHIPPED / "install.ps1").read_text(encoding="utf-8")
    match = re.search(r"foreach \(\$item in ([^)]+)\) \{", text)
    assert match, "could not find the installer's sanity check"
    required = re.findall(r"'([^']+)'", match.group(1))
    assert required, "the sanity check names nothing"
    for name in required:
        assert (SHIPPED / name).exists(), f"install.ps1 requires {name}, which does not ship"


def test_the_installer_never_reaches_outside_its_own_folder():
    """Its folder is the whole of what was downloaded; there is no above."""
    text = (SHIPPED / "install.ps1").read_text(encoding="utf-8")
    assert "$SourceRoot\\.." not in text
    assert "Join-Path $SourceRoot '..'" not in text


def test_the_launchers_find_the_venv_the_installer_builds():
    """uv sync runs at the install root, so .venv is beside the launchers."""
    for name in ("glasslinkxp.cmd", "glasslinkxp-gui.cmd"):
        text = (SHIPPED / name).read_text(encoding="utf-8")
        assert "%~dp0.venv\\Scripts\\" in text, f"{name} looks for the venv somewhere else"
        assert "%~dp0..\\" not in text, f"{name} reaches above the install root"
