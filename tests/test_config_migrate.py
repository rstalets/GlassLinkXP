"""Bringing an older config.toml up to date with the settings a version has.

The rule under test: a setting this version has and the file does not is
added; one the file has and this version does not is removed; one in both
keeps the user's value. Changing a *default* deliberately does not propagate
-- there is no way to tell a value somebody chose from the same value copied
out of an older example -- and the last test here pins that, so nobody
"fixes" it by accident later.
"""

import tomllib
from pathlib import Path

from glasslinkxp import configmigrate
from glasslinkxp.config import load_config
from glasslinkxp.main import main

EXAMPLE = Path(__file__).resolve().parents[1] / "src" / "config.example.toml"


def _example() -> dict:
    return tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))


# -- the rule ---------------------------------------------------------------


def test_a_setting_this_version_has_is_added_at_the_example_value():
    example = {"app": {"loop_hz": 28.0, "change_tolerance": 6}}
    merged, changes = configmigrate.reconcile({"app": {"loop_hz": 12.0}}, example)
    assert merged["app"]["change_tolerance"] == 6
    assert changes.added == ["app.change_tolerance"]


def test_a_setting_this_version_no_longer_has_is_removed():
    example = {"app": {"loop_hz": 28.0}}
    user = {"app": {"loop_hz": 12.0, "retired": True}}
    merged, changes = configmigrate.reconcile(user, example)
    assert "retired" not in merged["app"]
    assert changes.removed == ["app.retired"]


def test_a_value_the_user_set_is_kept_even_when_the_default_moved():
    """The accepted cost of the rule, pinned so it cannot drift into a surprise.

    A config written when the default was 12 keeps 12 after upgrading to a
    version whose default is 28: nothing here can tell a chosen 12 from a
    copied one.
    """
    merged, changes = configmigrate.reconcile(
        {"app": {"loop_hz": 12.0}}, {"app": {"loop_hz": 28.0}}
    )
    assert merged["app"]["loop_hz"] == 12.0
    assert not changes.any


def test_nested_tables_are_reconciled_too():
    example = {"display": {"pfd": {"window_title": "", "geometry": {"x": 0.05, "y": 0.915}}}}
    user = {"display": {"pfd": {"window_title": "MY PFD", "geometry": {"x": 0.031, "gone": 1}}}}
    merged, changes = configmigrate.reconcile(user, example)
    assert merged["display"]["pfd"]["geometry"] == {"x": 0.031, "y": 0.915}
    assert changes.added == ["display.pfd.geometry.y"]
    assert changes.removed == ["display.pfd.geometry.gone"]


# -- the two shapes a blanket key diff would damage --------------------------


def test_a_display_the_user_added_is_kept_and_filled_in():
    """[display.<name>] is the user's own set, not a fixed list of settings."""
    user = {"display": {"copilot": {"window_title": "Copilot PFD"}}}
    merged, changes = configmigrate.reconcile(user, _example())
    assert "copilot" in merged["display"]
    assert merged["display"]["copilot"]["window_title"] == "Copilot PFD"
    # and it gains the settings every display has
    assert "geometry" in merged["display"]["copilot"]
    assert any(c.startswith("display.copilot.") for c in changes.added)


def test_a_display_the_user_deleted_is_not_resurrected():
    user = {"display": {"pfd": {"window_title": "MY PFD"}}}
    merged, _changes = configmigrate.reconcile(user, _example())
    assert list(merged["display"]) == ["pfd"], "mfd was deleted on purpose"


def test_a_first_install_with_no_displays_gets_the_example_ones():
    merged, changes = configmigrate.reconcile({}, _example())
    assert set(merged["display"]) == {"pfd", "mfd"}
    assert "display" in changes.added


def test_something_the_daemon_does_not_read_is_left_alone():
    """A hand-written [[screen]] block is not a setting this version dropped."""
    user = {"app": {"loop_hz": 12.0}, "screen": [{"name": "mine"}], "notes": "keep me"}
    merged, changes = configmigrate.reconcile(user, {"app": {"loop_hz": 28.0}})
    assert merged["screen"] == [{"name": "mine"}]
    assert merged["notes"] == "keep me"
    assert changes.removed == []
    assert set(changes.kept_untouched) == {"screen", "notes"}


def test_the_inputs_are_not_modified():
    user = {"app": {"loop_hz": 12.0, "retired": True}}
    example = {"app": {"loop_hz": 28.0, "change_tolerance": 6}}
    configmigrate.reconcile(user, example)
    assert user == {"app": {"loop_hz": 12.0, "retired": True}}
    assert example == {"app": {"loop_hz": 28.0, "change_tolerance": 6}}


# -- the whole thing, through the CLI, against the real example --------------


def test_an_older_config_migrates_and_still_loads(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text(
        "[app]\n"
        "loop_hz = 12.0\n"
        "retired_setting = true\n"
        "\n"
        "[display.pfd]\n"
        'window_title = "MY OWN PFD"\n'
        "[display.pfd.geometry]\n"
        "x = 0.031\n",
        encoding="utf-8",
    )

    assert main(["migrate-config", "-c", str(path)]) == 0

    config = load_config(path)
    assert config.loop_hz == 12.0, "the user's own value survives"
    assert config.display("pfd").window_title == "MY OWN PFD"
    assert config.display("pfd").geometry.x == 0.031
    assert config.publish.field_width == 64, "a section it never had is filled in"
    assert "retired_setting" not in tomllib.loads(path.read_text(encoding="utf-8"))["app"]
    assert (tmp_path / "config.toml.bak").is_file(), "the previous file is kept"

    out = capsys.readouterr().out
    assert "removed app.retired_setting" in out


def test_migrating_twice_changes_nothing_the_second_time(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text("[app]\nloop_hz = 12.0\n", encoding="utf-8")
    main(["migrate-config", "-c", str(path)])
    after_first = path.read_text(encoding="utf-8")

    assert main(["migrate-config", "-c", str(path)]) == 0

    assert path.read_text(encoding="utf-8") == after_first
    assert "already up to date" in capsys.readouterr().out


def test_a_first_install_has_nothing_to_migrate(tmp_path, capsys):
    """The installer runs this every time, including the first."""
    assert main(["migrate-config", "-c", str(tmp_path / "config.toml")]) == 0
    assert "nothing to migrate" in capsys.readouterr().out


def test_the_shipped_example_needs_no_migration(tmp_path, capsys):
    """A fresh config is seeded by copying the example, so it must be current."""
    path = tmp_path / "config.toml"
    path.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    assert main(["migrate-config", "-c", str(path)]) == 0
    assert "already up to date" in capsys.readouterr().out
