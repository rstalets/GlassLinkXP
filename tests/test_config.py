import pytest

from g1000_softkey.config import (
    AppConfig,
    ConfigError,
    PublishConfig,
    StripGeometry,
    default_config,
    from_mapping,
    load_config,
)

EXAMPLE = "config.example.toml"


def test_example_config_loads():
    config = load_config(EXAMPLE)
    assert {d.key for d in config.displays} == {"pfd", "mfd"}
    assert config.display("pfd").dataref_prefix == "g1000/softkey/pfd"
    assert config.display("pfd").dataref_names()[0] == "g1000/softkey/pfd/1"
    assert len(config.display("mfd").dataref_names()) == 12
    assert config.loop_hz == 12.0
    assert config.publish.field_width == 64
    assert config.color.enabled is True


def test_defaults_without_a_file():
    config = default_config()
    assert len(config.active_displays) == 2
    assert config.ocr.whitelist.startswith("ABC")


def test_missing_file_is_a_clean_error():
    with pytest.raises(ConfigError):
        load_config("/nonexistent/config.toml")


def test_unknown_display_lookup():
    with pytest.raises(ConfigError):
        default_config().display("hud")


def test_geometry_validation():
    with pytest.raises(ConfigError):
        StripGeometry.from_dict({"x": 1.4})
    with pytest.raises(ConfigError):
        StripGeometry.from_dict({"x": 0.9, "w": 0.5})
    with pytest.raises(ConfigError):
        StripGeometry.from_dict({"cells": 0})


def test_from_mapping_overrides_and_disabled_displays():
    config = from_mapping({
        "app": {"loop_hz": 10.0, "change_gating": False},
        "display": {
            "pfd": {"window_title": "PFD", "geometry": {"y": 0.8, "h": 0.1}},
            "mfd": {"enabled": False},
        },
        "publish": {"target": "file"},
    })
    assert isinstance(config, AppConfig)
    assert config.loop_hz == 10.0 and config.change_gating is False
    assert config.display("pfd").geometry.y == 0.8
    assert [d.key for d in config.active_displays] == ["pfd"]
    assert config.publish.target == "file"


def test_bad_loop_rate():
    with pytest.raises(ConfigError):
        from_mapping({"app": {"loop_hz": 0}})


# ---------------------------------------------------------------------------
# window_size: forcing the pop-out larger so the capture has more pixels
# ---------------------------------------------------------------------------


def test_window_size_is_read_as_a_tuple(tmp_path):
    """TOML gives a list; the frozen config needs a tuple."""
    path = tmp_path / "config.toml"
    path.write_text(
        '[display.pfd]\nwindow_title = "G1000 PFD"\nwindow_size = [1400, 1000]\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.display("pfd").window_size == (1400, 1000)


def test_window_size_defaults_to_none_so_nothing_is_resized(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[display.pfd]\nwindow_title = "G1000 PFD"\n', encoding="utf-8")
    assert load_config(path).display("pfd").window_size is None


@pytest.mark.parametrize("value", ["1400", "[1400]", "[1400, 1000, 900]"])
def test_a_malformed_window_size_is_rejected_with_the_display_named(tmp_path, value):
    path = tmp_path / "config.toml"
    path.write_text(f"[display.pfd]\nwindow_size = {value}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="display.pfd.window_size"):
        load_config(path)


def test_window_size_alone_does_not_authorise_resizing(tmp_path):
    """Off by default: a pop-out may be driving external avionics hardware."""
    path = tmp_path / "config.toml"
    path.write_text(
        '[display.pfd]\nwindow_title = "G1000 PFD"\nwindow_size = [1400, 1000]\n',
        encoding="utf-8",
    )
    display = load_config(path).display("pfd")
    assert display.window_size == (1400, 1000)
    assert display.manage_window_size is False


def test_resizing_happens_only_when_explicitly_enabled(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[display.pfd]\nwindow_title = "G1000 PFD"\n'
        "window_size = [1400, 1000]\nmanage_window_size = true\n",
        encoding="utf-8",
    )
    assert load_config(path).display("pfd").manage_window_size is True


def test_managing_the_size_without_a_size_is_rejected(tmp_path):
    """Otherwise it silently does nothing and looks like a resize that failed."""
    path = tmp_path / "config.toml"
    path.write_text(
        '[display.pfd]\nwindow_title = "G1000 PFD"\nmanage_window_size = true\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="no window_size is set"):
        load_config(path)


def test_displays_decide_independently(tmp_path):
    """One display can drive hardware while the other is ours to resize."""
    path = tmp_path / "config.toml"
    path.write_text(
        '[display.pfd]\nwindow_title = "PFD"\n'
        "window_size = [1400, 1000]\nmanage_window_size = true\n"
        '[display.mfd]\nwindow_title = "MFD"\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.display("pfd").manage_window_size is True
    assert config.display("mfd").manage_window_size is False


def test_the_field_width_agrees_with_the_plugin():
    """The width is fixed in three places; two of them are in this repo.

    The third is the ':sNN' on the user's PilotsDeck buttons, which nothing
    here can check -- which is exactly why a silent disagreement between these
    two would be so unpleasant to debug.
    """
    import importlib.util
    import sys
    import types
    from pathlib import Path

    plugin_path = Path(__file__).resolve().parents[1] / "xppython3" / "PI_G1000SoftkeyLabels.py"
    module = types.ModuleType("XPPython3")
    module.xp = types.SimpleNamespace(Type_Data=8, Type_Int=1, NO_PLUGIN_ID=-1)
    saved = sys.modules.get("XPPython3")
    sys.modules["XPPython3"] = module
    try:
        spec = importlib.util.spec_from_file_location("PI_G1000SoftkeyLabels_widthcheck", plugin_path)
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
    finally:
        if saved is None:
            del sys.modules["XPPython3"]
        else:
            sys.modules["XPPython3"] = saved

    assert plugin.FIELD_WIDTH == PublishConfig().field_width
    assert plugin.FIELD_WIDTH == load_config(EXAMPLE).publish.field_width
