"""The Settings form has to describe every setting the daemon has.

A setting added to config.py and forgotten in the GUI would simply not appear
in the window, and there would be nothing to notice: no error, no blank field,
just an option nobody using the GUI can reach. So the coverage is asserted
rather than eyeballed.
"""

from dataclasses import fields

import pytest

from g1000_softkey import config as daemon_config
from g1000_softkey.gui import schema

#: Which config dataclass each form group describes.
SECTIONS = (
    ("app", daemon_config.AppConfig),
    ("display", daemon_config.DisplayConfig),
    ("geometry", daemon_config.StripGeometry),
    ("ocr", daemon_config.OcrConfig),
    ("color", daemon_config.ColorConfig),
    ("publish", daemon_config.PublishConfig),
)


@pytest.mark.parametrize("section,cls", SECTIONS, ids=[s for s, _ in SECTIONS])
def test_every_config_field_is_in_the_form_or_excluded_on_purpose(section, cls):
    described = {setting.key for setting in schema.BY_SECTION[section].settings}
    excluded = {
        key.split(".", 1)[1] for key in schema.NOT_IN_THE_FORM
        if key.startswith(cls.__name__ + ".")
    }
    missing = {f.name for f in fields(cls)} - described - excluded
    assert not missing, (
        f"{cls.__name__} has settings the GUI does not show: {sorted(missing)}. "
        f"Add them to schema.{section.upper()}, or to NOT_IN_THE_FORM with a reason."
    )


@pytest.mark.parametrize("section,cls", SECTIONS, ids=[s for s, _ in SECTIONS])
def test_the_form_does_not_describe_settings_that_do_not_exist(section, cls):
    described = {setting.key for setting in schema.BY_SECTION[section].settings}
    assert described <= {f.name for f in fields(cls)}


def test_every_exclusion_names_a_real_field():
    for key, reason in schema.NOT_IN_THE_FORM.items():
        class_name, field_name = key.split(".", 1)
        cls = getattr(daemon_config, class_name)
        assert field_name in {f.name for f in fields(cls)}, key
        assert reason, f"{key} is excluded without saying why"


def test_every_setting_is_explained():
    """The help text is the point of the form -- a bare label is not enough."""
    for group in schema.GROUPS:
        assert group.blurb
        for setting in group.settings:
            assert setting.help, f"{group.section}.{setting.key} has no explanation"


def test_choices_match_what_the_daemon_accepts():
    assert set(schema.setting("ocr", "engine").choices) == {"auto", "tesserocr", "pytesseract"}
    assert set(schema.setting("ocr", "threshold").choices) == {"otsu", "adaptive"}
    assert set(schema.setting("publish", "target").choices) == \
        {"websocket", "webapi", "file", "console"}


def test_the_publish_targets_are_the_ones_create_publisher_knows():
    """Both lists are short and both are edited by hand; keep them together."""
    import inspect

    from g1000_softkey.publish import create_publisher

    source = inspect.getsource(create_publisher)
    for target in schema.setting("publish", "target").choices:
        assert f'"{target}"' in source, f"publish.py does not handle {target!r}"


def test_a_setting_with_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        schema.Setting("x", "colour-wheel", "X", "help")


def test_a_choice_without_choices_is_refused():
    with pytest.raises(ValueError):
        schema.Setting("x", "choice", "X", "help")


def test_kind_of_is_quiet_about_things_it_does_not_describe():
    assert schema.kind_of("app", "loop_hz") == "float"
    assert schema.kind_of("app", "not_a_setting") == ""
    assert schema.kind_of("not_a_section", "anything") == ""
