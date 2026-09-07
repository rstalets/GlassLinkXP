from pathlib import Path

import pytest

from glasslinkxp.config import (
    AppConfig,
    ConfigError,
    ConfigNotFound,
    PublishConfig,
    StripGeometry,
    default_config,
    from_mapping,
    load_config,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "src" / "config.example.toml"


def test_example_config_loads():
    config = load_config(EXAMPLE)
    assert {d.key for d in config.displays} == {"pfd", "mfd"}
    assert config.display("pfd").dataref_prefix == "glasslinkxp/softkey/pfd"
    assert config.display("pfd").dataref_names()[0] == "glasslinkxp/softkey/pfd/1"
    assert len(config.display("mfd").dataref_names()) == 12
    assert config.loop_hz == 28.0
    assert config.publish.field_width == 16
    assert config.color.enabled is True


def test_defaults_without_a_file():
    config = default_config()
    assert len(config.active_displays) == 2
    assert config.ocr.whitelist.startswith("ABC")


def test_missing_file_is_a_clean_error():
    with pytest.raises(ConfigError):
        load_config("/nonexistent/config.toml")


def test_a_missing_file_is_distinguishable_from_a_broken_one(tmp_path):
    """Absent and invalid are different failures, and one caller acts on that.

    ``gui`` carries on without a config file, because making one is part of
    what it does. It must not carry on over a file that is there and wrong.
    """
    with pytest.raises(ConfigNotFound):
        load_config(tmp_path / "not-there.toml")

    broken = tmp_path / "broken.toml"
    broken.write_text("[color]\nvalue_max = 300\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(broken)
    assert not isinstance(exc.value, ConfigNotFound)

    unparseable = tmp_path / "unparseable.toml"
    unparseable.write_text("[app\nloop_hz =", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(unparseable)
    assert not isinstance(exc.value, ConfigNotFound)


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
        "publish": {"target": "console"},
    })
    assert isinstance(config, AppConfig)
    assert config.loop_hz == 10.0 and config.change_gating is False
    assert config.display("pfd").geometry.y == 0.8
    assert [d.key for d in config.active_displays] == ["pfd"]
    assert config.publish.target == "console"


def test_bad_loop_rate():
    with pytest.raises(ConfigError):
        from_mapping({"app": {"loop_hz": 0}})


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

    plugin_path = Path(__file__).resolve().parents[1] / "src" / "xppython3" / "PI_GlassLinkXP.py"
    module = types.ModuleType("XPPython3")
    module.xp = types.SimpleNamespace(Type_Data=8, Type_Int=1, NO_PLUGIN_ID=-1)
    saved = sys.modules.get("XPPython3")
    sys.modules["XPPython3"] = module
    try:
        spec = importlib.util.spec_from_file_location("PI_GlassLinkXP_widthcheck", plugin_path)
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
    finally:
        if saved is None:
            del sys.modules["XPPython3"]
        else:
            sys.modules["XPPython3"] = saved

    assert plugin.FIELD_WIDTH == PublishConfig().field_width
    assert plugin.FIELD_WIDTH == load_config(EXAMPLE).publish.field_width


# -- window management ------------------------------------------------------


def test_window_management_is_on_by_default_at_a_four_by_three_size():
    config = default_config()
    assert config.window_management.enabled is True
    assert config.window_management.size == (1280, 960)


def test_window_management_is_read_from_its_own_table():
    config = from_mapping({
        "window_management": {"enabled": False, "size": [1600, 1200]},
    })
    assert config.window_management.enabled is False
    assert config.window_management.size == (1600, 1200)


def test_a_size_from_toml_arrives_as_a_tuple():
    """TOML gives a list; the config is frozen and has to stay hashable."""
    config = from_mapping({"window_management": {"size": [1024, 768]}})
    assert config.window_management.size == (1024, 768)


def test_a_size_that_is_not_four_by_three_is_refused_rather_than_used(caplog):
    """And says so: a silently ignored setting is worse than a rejected one.

    The strip geometry is fractions of the window, so a pop-out of the wrong
    shape moves the softkey strip out from under the calibration -- which shows
    up as labels that will not read, a long way from the setting that caused it.
    """
    with caplog.at_level("WARNING"):
        config = from_mapping({"window_management": {"size": [1920, 1080]}})

    assert config.window_management.size == (1280, 960)
    assert "4:3" in caplog.text
