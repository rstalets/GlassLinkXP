"""Reading and writing config.toml on the GUI's behalf.

The writer is only trustworthy if what it writes loads back as what it was
given, so that is what most of this checks -- through the daemon's own
``load_config``, not through a second reader written to agree with it.
"""

import pytest

from g1000_softkey.config import (
    AppConfig,
    ColorConfig,
    ConfigError,
    DisplayConfig,
    OcrConfig,
    PublishConfig,
    StripGeometry,
    default_config,
    load_config,
)
from g1000_softkey.gui import configio, schema


def test_the_defaults_round_trip_through_the_writer(tmp_path):
    path = tmp_path / "config.toml"
    configio.save(path, configio.default_document(), backup=False)
    assert load_config(path) == default_config()


def test_a_thoroughly_non_default_config_round_trips(tmp_path):
    """Every field moved off its default, so nothing is passing by accident."""
    config = AppConfig(
        loop_hz=7.5,
        change_gating=False,
        change_tolerance=11,
        displays=(
            DisplayConfig(
                key="pfd", window_title='the "left" one', enabled=False,
                dataref_prefix="my/prefix", manage_window_size=True,
                window_size=(1400, 1000),
                geometry=StripGeometry(x=0.1, y=0.8, w=0.7, h=0.09, cells=6,
                                       cell_pad_x=0.2, cell_pad_y=0.3),
            ),
        ),
        ocr=OcrConfig(
            engine="pytesseract", lang="deu", tessdata_path="/opt/tess data",
            psm=10, whitelist="ABC/-\\", upscale=6.5,
            sharpen_ladder=((0.0, 0.0), (0.9, 1.7)), threshold="adaptive",
            accept_confidence=55.0, screen_confidence=44.0,
            screen_match_confidence=91.0, signature_confidence=33.0,
            fuzzy_cutoff=0.5, blank_ink_ratio=0.02, blank_contrast=21,
        ),
        color=ColorConfig(enabled=False, ring_fraction=0.22, value_max=70,
                          saturation_max=80, red_hue_max=9, red_hue_wrap_min=170,
                          yellow_hue_min=20, yellow_hue_max=38),
        publish=PublishConfig(target="file", base_url="http://example:1/",
                              api_version="v2", field_width=32, timeout=2.5,
                              json_path="/tmp/labels.json", retry_interval=9.0),
    )
    path = tmp_path / "config.toml"
    configio.save(path, configio.document_from_config(config), backup=False)
    assert load_config(path) == config


def test_an_unset_optional_setting_is_written_as_a_comment(tmp_path):
    path = tmp_path / "config.toml"
    configio.save(path, configio.default_document(), backup=False)
    text = path.read_text(encoding="utf-8")
    assert "# tessdata_path is not set" in text
    assert load_config(path).ocr.tessdata_path is None


def test_saving_keeps_the_previous_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("# my own notes\n[app]\nloop_hz = 3.0\n", encoding="utf-8")
    backup = configio.save(path, configio.default_document())
    assert backup is not None and backup.read_text().startswith("# my own notes")


def test_the_raw_editor_writes_text_through_unchanged(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[app]\nloop_hz = 1.0\n", encoding="utf-8")
    text = "# a comment the form would have eaten\n[app]\nloop_hz = 9.0\n"
    configio.save_text(path, text)
    assert path.read_text(encoding="utf-8") == text
    assert load_config(path).loop_hz == 9.0


def test_the_raw_editor_refuses_to_write_broken_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[app]\nloop_hz = 1.0\n", encoding="utf-8")
    with pytest.raises(configio.ConfigIoError):
        configio.save_text(path, "[app\nloop_hz =")
    assert load_config(path).loop_hz == 1.0  # the old file is still intact


def test_a_table_the_gui_does_not_know_about_is_kept(tmp_path):
    document = configio.default_document()
    document["something_new"] = {"a": 1}
    path = tmp_path / "config.toml"
    configio.save(path, document, backup=False)
    assert "[something_new]" in path.read_text(encoding="utf-8")


def test_validation_rejects_what_the_daemon_would_reject():
    document = configio.default_document()
    document["app"]["loop_hz"] = 0
    with pytest.raises(ConfigError):
        configio.validate(document)


def test_validation_rejects_a_crop_off_the_edge_of_the_frame():
    document = configio.default_document()
    document["display"]["pfd"]["geometry"]["y"] = 0.99
    with pytest.raises(ConfigError):
        configio.validate(document)


def test_reading_a_file_that_is_not_there_says_so(tmp_path):
    with pytest.raises(configio.ConfigIoError):
        configio.read_document(tmp_path / "nope.toml")


def test_reading_a_broken_file_says_where(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("[app\n", encoding="utf-8")
    with pytest.raises(configio.ConfigIoError) as exc:
        configio.read_document(path)
    assert "bad.toml" in str(exc.value)


# -- form fields -----------------------------------------------------------


@pytest.mark.parametrize("section,key,text,expected", [
    ("app", "loop_hz", "12.5", 12.5),
    ("app", "change_gating", "true", True),
    ("app", "change_gating", "false", False),
    ("app", "change_tolerance", "6", 6),
    ("ocr", "whitelist", " ABC ", "ABC"),
    ("ocr", "sharpen_ladder", "[[0.0, 0.0], [0.5, 1.0]]", [[0.0, 0.0], [0.5, 1.0]]),
    ("display", "window_size", "[1400, 1000]", [1400, 1000]),
    ("ocr", "tessdata_path", "", None),
])
def test_form_values_parse(section, key, text, expected):
    assert configio.parse_field(schema.setting(section, key), text) == expected


def test_a_required_field_left_empty_is_refused():
    with pytest.raises(configio.ConfigIoError):
        configio.parse_field(schema.setting("app", "loop_hz"), "")


def test_a_number_field_given_words_is_refused():
    with pytest.raises(configio.ConfigIoError) as exc:
        configio.parse_field(schema.setting("app", "change_tolerance"), "quite a lot")
    assert "whole number" in str(exc.value)


def test_a_malformed_composite_field_is_refused():
    with pytest.raises(configio.ConfigIoError):
        configio.parse_field(schema.setting("ocr", "sharpen_ladder"), "[[0.0, ")


def test_composite_fields_survive_a_form_round_trip():
    setting = schema.setting("ocr", "sharpen_ladder")
    ladder = ((0.0, 0.0), (0.5, 1.0))
    assert configio.parse_field(setting, configio.format_field(setting, ladder)) == \
        [[0.0, 0.0], [0.5, 1.0]]


def test_floats_keep_their_decimal_point():
    assert configio.format_field(schema.setting("app", "loop_hz"), 12) == "12.0"


# -- geometry shared between displays --------------------------------------


def test_a_displays_geometry_comes_back_with_the_defaults_filled_in():
    document = {"display": {"pfd": {"geometry": {"x": 0.25}}}}
    geometry = configio.geometry_of(document, "pfd")
    assert geometry.x == 0.25
    assert geometry.cells == StripGeometry().cells


def test_a_display_with_no_geometry_at_all_is_the_defaults():
    assert configio.geometry_of({}, "pfd") == StripGeometry()


def test_a_fresh_configuration_has_both_displays_in_the_same_place():
    """Which is why linking them can be the default without overwriting work."""
    assert configio.same_geometry(configio.default_document(), "pfd", "mfd")


def test_displays_calibrated_apart_are_seen_to_differ():
    document = configio.default_document()
    configio.set_geometry(document, "mfd", StripGeometry(x=0.2, w=0.5))
    assert not configio.same_geometry(document, "pfd", "mfd")


def test_writing_a_geometry_writes_every_field(tmp_path):
    from g1000_softkey.config import load_config

    document = configio.default_document()
    source = StripGeometry(x=0.0273, y=0.915, w=0.9461, h=0.0675,
                           cells=6, cell_pad_x=0.2, cell_pad_y=0.3)
    configio.set_geometry(document, "mfd", source)
    path = tmp_path / "config.toml"
    configio.save(path, document, backup=False)
    assert load_config(path).display("mfd").geometry == source


def test_copying_one_display_onto_another_makes_them_match():
    document = configio.default_document()
    configio.set_geometry(document, "pfd", StripGeometry(x=0.0273, w=0.9461))
    configio.set_geometry(document, "mfd", configio.geometry_of(document, "pfd"))
    assert configio.same_geometry(document, "pfd", "mfd")


def test_setting_a_nested_key_creates_the_tables():
    document: dict = {}
    configio.set_in(document, ("display", "pfd", "geometry", "x"), 0.5)
    assert document == {"display": {"pfd": {"geometry": {"x": 0.5}}}}


def test_setting_a_key_to_none_removes_it():
    document = {"ocr": {"tessdata_path": "/x"}}
    configio.set_in(document, ("ocr", "tessdata_path"), None)
    assert document == {"ocr": {}}


def test_getting_a_missing_key_gives_the_default():
    assert configio.get_in({}, ("a", "b"), "fallback") == "fallback"


def test_a_string_with_quotes_and_backslashes_survives(tmp_path):
    document = configio.default_document()
    document["display"]["pfd"]["window_title"] = 'C:\\X-Plane "12"\ta\nb'
    path = tmp_path / "config.toml"
    configio.save(path, document, backup=False)
    assert load_config(path).display("pfd").window_title == 'C:\\X-Plane "12"\ta\nb'
