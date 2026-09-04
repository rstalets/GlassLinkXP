import pytest

from g1000_softkey.config import (
    AppConfig,
    ConfigError,
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
    assert config.loop_hz == 4.0
    assert config.publish.field_width == 16


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
