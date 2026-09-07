"""What a release is made of: the version, and who stamps it in.

The tree is unreleased -- everything that carries a version says ``0.0.0`` --
and the release workflow passes the tag it is building to
``tools/make_zip.py``, which stamps it into the three files that carry it. Two
of them have to move together: ``pyproject.toml`` names the version and
``uv.lock`` records the version it locked, and ``uv sync --locked`` -- what
install.ps1 runs on the user's machine -- refuses to run when they disagree.
Stamping one alone would build a zip that cannot install, and nothing
downstream of the build would notice.

The third, ``glasslinkxp/VERSION``, is the one the running app reads and logs,
and it is *not in the tree*: the build creates it, for a release only. A
checkout has never been released, so it must find no file and say
``NO_VERSION`` rather than report a number.

Nothing here needs Tk, a display or a network.
"""

import re
import shlex
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import make_zip  # noqa: E402

from glasslinkxp.version import (  # noqa: E402
    NO_VERSION,
    __version__,
    read_version,
)

WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
LOCK = ROOT / "src" / "uv.lock"
PYPROJECT = ROOT / "src" / "pyproject.toml"
VERSION_FILE = ROOT / "src" / "glasslinkxp" / "VERSION"


def read_version_of(contents: str) -> str:
    """What the app would make of a VERSION file holding ``contents``."""
    path = Path(tempfile.mkdtemp()) / "VERSION"
    path.write_text(contents, encoding="utf-8")
    return read_version(path)


def lock_version(text: str, name: str = "glasslinkxp") -> str:
    """The version uv recorded for one package, read back out of the lock."""
    match = re.search(
        r'(?m)^\[\[package\]\]\nname = "' + re.escape(name) + r'"\nversion = "([^"]*)"',
        text,
    )
    assert match, f"{name} is not in uv.lock"
    return match.group(1)


# ---------------------------------------------------------------------------
# The tree itself
# ---------------------------------------------------------------------------

def test_the_checked_in_version_is_the_unreleased_one():
    """A checkout is not a release of anything, and says so."""
    assert make_zip.version() == make_zip.DEFAULT_VERSION


def test_a_checkout_has_no_version_file_and_says_so():
    """The bug this replaced: `VERSION` was checked in holding `0.0.0`, so a
    clone reported `0.0.0` -- which reads like a build somebody released,
    rather than like the absence of one. The file a release stamps has to be
    made by the release; a dev build must find nothing."""
    assert not VERSION_FILE.exists(), "a checkout must not carry a version"
    assert read_version(VERSION_FILE) == NO_VERSION
    assert __version__ == NO_VERSION, "the running tree reports itself as unreleased"


def test_the_manifest_and_the_lock_agree_about_the_version():
    """`uv sync --locked` fails when they do not -- measured, not assumed.

    Bumping pyproject.toml alone makes `uv lock --check` report the lockfile
    out of date; stamping both makes it pass again. That is why the stamping
    is one operation over two files rather than a version bumped by hand.
    """
    assert lock_version(LOCK.read_text(encoding="utf-8")) == make_zip.version()


# ---------------------------------------------------------------------------
# Reading a tag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("1.2.3", "1.2.3"),
    ("v1.2.3", "1.2.3"),
    ("V1.2.3", "1.2.3"),
    ("  v1.2.3  ", "1.2.3"),
    ("1.2", "1.2"),
    ("1.2.3rc1", "1.2.3rc1"),
    ("v2.0.0b2", "2.0.0b2"),
    ("1.2.3.post1", "1.2.3.post1"),
])
def test_a_tag_becomes_the_version_it_names(raw, expected):
    assert make_zip.normalise_version(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "v", "latest", "release-1.2.3", "1.2.3-beta", "1.2.3 ; rm -rf /",
    "v1.2.3\n2.0.0", "../../etc/passwd",
])
def test_a_tag_that_is_not_a_version_stops_the_build(raw):
    """The workflow hands this whatever the release was tagged, and a tag is
    text a human typed. Refusing is the only safe reading of one that is not a
    version -- guessing would put it in the manifest and in a filename."""
    with pytest.raises(make_zip.VersionError):
        make_zip.normalise_version(raw)


def test_the_command_line_reports_a_bad_version_rather_than_building(tmp_path, capsys):
    assert make_zip.main(["--version", "banana", "--out-dir", str(tmp_path)]) == 2
    assert not list(tmp_path.glob("*.zip"))
    assert "banana" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Stamping
# ---------------------------------------------------------------------------

def test_stamping_the_manifest_sets_the_version_and_nothing_else():
    text = PYPROJECT.read_text(encoding="utf-8")
    stamped = make_zip.stamp_pyproject(text, "1.2.3")
    data = tomllib.loads(stamped)
    assert data["project"]["version"] == "1.2.3"
    # Everything else is the file it was: same length bar the version itself.
    assert len(stamped) - len(text) == len("1.2.3") - len(make_zip.DEFAULT_VERSION)
    assert tomllib.loads(text)["project"]["name"] == data["project"]["name"]


def test_stamping_the_lock_touches_only_the_project_it_locked():
    """Every other block is a dependency: pinned, hashed, and not ours to move."""
    text = LOCK.read_text(encoding="utf-8")
    stamped = make_zip.stamp_lock(text, "1.2.3", "glasslinkxp")
    assert lock_version(stamped) == "1.2.3"
    for other in ("numpy", "pillow", "requests", "tesserocr"):
        assert lock_version(stamped, other) == lock_version(text, other)


def test_the_version_file_is_the_version_and_a_newline():
    """It is read by a `read_text().strip()`, so the file is just the number."""
    assert make_zip.version_file("1.2.3") == "1.2.3\n"
    assert read_version_of(make_zip.version_file("1.2.3")) == "1.2.3"


def test_stamping_refuses_a_file_it_does_not_recognise():
    """A silent no-op here would ship a zip stamped 0.0.0 under a release
    number, and the first sign of it would be a user's install failing."""
    with pytest.raises(make_zip.VersionError):
        make_zip.stamp_pyproject("name = 'something else'\n", "1.2.3")
    with pytest.raises(make_zip.VersionError):
        make_zip.stamp_lock('[[package]]\nname = "numpy"\nversion = "1"\n', "1.2.3", "glasslinkxp")


# ---------------------------------------------------------------------------
# The zip
# ---------------------------------------------------------------------------

def test_a_release_build_carries_the_stamped_manifest(tmp_path):
    target = make_zip.build(tmp_path, "1.2.3")
    assert target.name == "glasslinkxp-1.2.3.zip"
    with zipfile.ZipFile(target) as zf:
        manifest = tomllib.loads(zf.read("pyproject.toml").decode("utf-8"))
        lock = zf.read("uv.lock").decode("utf-8")
        shipped = zf.read("glasslinkxp/VERSION").decode("utf-8")
    assert manifest["project"]["version"] == "1.2.3"
    assert lock_version(lock) == "1.2.3", "the lock would refuse to install against that manifest"
    assert read_version_of(shipped) == "1.2.3", "the app would report a version this was not built at"


def test_a_release_build_leaves_the_checkout_alone(tmp_path):
    """Stamping writes into the zip, never into the tree: a build that edited
    files in place would leave a half-stamped checkout to commit by accident."""
    before = [f.read_text(encoding="utf-8") for f in (PYPROJECT, LOCK)]
    make_zip.build(tmp_path, "9.9.9")
    assert [f.read_text(encoding="utf-8") for f in (PYPROJECT, LOCK)] == before
    assert not VERSION_FILE.exists(), "the build wrote a version into the tree"


def test_a_developer_build_stamps_nothing_and_ships_no_version(tmp_path):
    """A zip built without a tag is not a release, so it carries no version at
    all -- installed from one, the app says NO_VERSION, same as a checkout."""
    target = make_zip.build(tmp_path)
    assert target.name == f"glasslinkxp-{make_zip.DEFAULT_VERSION}.zip"
    with zipfile.ZipFile(target) as zf:
        assert zf.read("pyproject.toml").decode("utf-8") == PYPROJECT.read_text(encoding="utf-8")
        assert zf.read("uv.lock").decode("utf-8") == LOCK.read_text(encoding="utf-8")
        assert make_zip.VERSION_MEMBER not in zf.namelist()


def test_a_stray_version_file_in_a_working_tree_never_ships(tmp_path, monkeypatch):
    """One copied back from an install, or left by an older build: shipping it
    would put a number on this zip that this build did not stamp."""
    stray = ROOT / "src" / "glasslinkxp" / "VERSION"
    stray.write_text("9.9.9\n", encoding="utf-8")
    try:
        with zipfile.ZipFile(make_zip.build(tmp_path)) as zf:
            assert make_zip.VERSION_MEMBER not in zf.namelist()
        with zipfile.ZipFile(make_zip.build(tmp_path, "1.2.3")) as zf:
            assert zf.read(make_zip.VERSION_MEMBER).decode("utf-8").strip() == "1.2.3"
    finally:
        stray.unlink()


# ---------------------------------------------------------------------------
# The workflow, which is the only caller that matters
# ---------------------------------------------------------------------------

def build_lines() -> list[str]:
    """Every command line in the workflow that runs the builder.

    A `run:` step, whether written inline or in a block -- and never a comment
    that happens to name the script.
    """
    lines = []
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "make_zip.py" not in stripped:
            continue
        if stripped.startswith("run:"):
            stripped = stripped[len("run:"):].strip()
        if stripped.startswith("python "):
            lines.append(stripped)
    assert lines, "the release workflow never builds the zip"
    return lines


def test_the_workflow_invokes_the_builder_as_it_actually_is():
    """The argv the workflow writes, parsed by the parser that will see it.

    A renamed flag would otherwise be found by a release, at the point where
    the asset does not appear.
    """
    for line in build_lines():
        argv = shlex.split(line.replace('"$RAW_VERSION"', "1.2.3"))
        assert argv[:2] == ["python", "tools/make_zip.py"]
        args = make_zip.build_parser().parse_args(argv[2:])
        assert make_zip.normalise_version(args.version) == "1.2.3"
        # Whatever it builds into is where the upload steps look.
        assert f"{args.out_dir}/*.zip" in WORKFLOW.read_text(encoding="utf-8")


def test_the_tag_reaches_the_build_through_the_environment():
    """A tag is text somebody typed, and `run:` is a shell.

    GitHub substitutes ``${{ }}`` into the script *before* the shell sees it,
    so a tag containing shell syntax would run as shell. Passing it as an
    environment variable and quoting the expansion is what stops that, and it
    is invisible in review, so it is pinned here: no ``${{`` inside any run
    block.
    """
    run_indent = None
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if run_indent is not None and indent <= run_indent:
            run_indent = None
        if re.match(r"\s*run:", line):
            run_indent = indent
            assert "${{" not in line, f"interpolated into a shell command: {line.strip()}"
        elif run_indent is not None:
            assert "${{" not in line, f"interpolated into a shell command: {line.strip()}"


def test_the_issue_templates_are_the_shape_github_expects():
    yaml = pytest.importorskip("yaml", reason="PyYAML is not in this environment")
    directory = ROOT / ".github" / "ISSUE_TEMPLATE"
    forms = sorted(p for p in directory.glob("*.yml") if p.name != "config.yml")
    assert [p.name for p in forms] == ["bug_report.yml", "enhancement.yml"]
    for path in forms:
        form = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert form["name"] and form["description"], f"{path.name} needs a name and description"
        for field in form["body"]:
            assert field["type"] in {
                "markdown", "input", "textarea", "dropdown", "checkboxes",
            }, f"{path.name}: {field['type']} is not an issue-form field"
            if field["type"] != "markdown":
                assert field["id"], f"{path.name}: every field needs an id"
    config = yaml.safe_load((directory / "config.yml").read_text(encoding="utf-8"))
    assert isinstance(config["blank_issues_enabled"], bool)


# ---------------------------------------------------------------------------
# What the running app makes of that file
# ---------------------------------------------------------------------------

def test_the_version_is_read_from_the_file_the_build_writes(tmp_path):
    stamped = tmp_path / "VERSION"
    stamped.write_text(make_zip.version_file("1.2.3"), encoding="utf-8")
    assert read_version(stamped) == "1.2.3"


@pytest.mark.parametrize("contents", ["", "   \n", "\n\n"])
def test_an_empty_version_file_reads_as_no_version(tmp_path, contents):
    path = tmp_path / "VERSION"
    path.write_text(contents, encoding="utf-8")
    assert read_version(path) == NO_VERSION


def test_a_missing_version_file_does_not_stop_the_app(tmp_path):
    """A dev build run from a tree without the file still starts, and says so.

    The placeholder is not a number on purpose: `NO_VERSION` in a log cannot
    be mistaken for something a release was tagged, and cannot be compared,
    sorted or reported as one."""
    assert read_version(tmp_path / "nothing-here") == NO_VERSION
    assert not NO_VERSION[0].isdigit()
