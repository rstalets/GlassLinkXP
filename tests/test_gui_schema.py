"""The Settings form has to describe every setting the daemon has.

A setting added to config.py and forgotten in the GUI would simply not appear
in the window, and there would be nothing to notice: no error, no blank field,
just an option nobody using the GUI can reach. So the coverage is asserted
rather than eyeballed.
"""

from dataclasses import fields
from pathlib import Path

import pytest

from glasslinkxp import config as daemon_config
from glasslinkxp.gui import schema

#: Which config dataclass each form group describes. Read from the schema
#: itself rather than restated here: the reference documentation is generated
#: from the same table, and a second copy of it would be a third thing to keep
#: in step.
SECTIONS = tuple((section, cls) for section, (cls, _table)
                 in schema.SECTION_CLASSES.items())


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
    assert set(schema.setting("ocr", "threshold").choices) == {"otsu", "adaptive"}
    assert set(schema.setting("publish", "target").choices) == \
        {"websocket", "webapi", "console"}


def test_the_publish_targets_are_the_ones_create_publisher_knows():
    """Both lists are short and both are edited by hand; keep them together."""
    import inspect

    from glasslinkxp.publish import create_publisher

    source = inspect.getsource(create_publisher)
    for target in schema.setting("publish", "target").choices:
        assert f'"{target}"' in source, f"publish.py does not handle {target!r}"


def test_a_setting_with_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        schema.Setting("x", "colour-wheel", "X", "help")


def test_a_choice_without_choices_is_refused():
    with pytest.raises(ValueError):
        schema.Setting("x", "choice", "X", "help")


# ---------------------------------------------------------------------------
# the generated reference
# ---------------------------------------------------------------------------

DOC = Path(__file__).resolve().parents[1] / "docs" / "CONFIGURATION.md"


def test_the_reference_documentation_is_up_to_date():
    """docs/CONFIGURATION.md is generated from this schema, not written.

    config.toml carries no comments -- the GUI rewrites the whole file when
    the form is saved -- so that document is where the reasoning for every
    setting lives, and it has to be regenerated when the schema changes:

        python -m glasslinkxp.gui.schema > docs/CONFIGURATION.md
    """
    assert DOC.is_file(), f"{DOC} is missing; regenerate it"
    assert DOC.read_text(encoding="utf-8") == schema.as_markdown(), (
        "docs/CONFIGURATION.md no longer matches gui/schema.py. Regenerate it:\n"
        "    python -m glasslinkxp.gui.schema > docs/CONFIGURATION.md"
    )


def test_every_setting_reaches_the_reference():
    text = schema.as_markdown()
    for group in schema.GROUPS:
        for setting in group.settings:
            assert f"### `{setting.key}`" in text, f"{group.section}.{setting.key}"
            assert setting.help in text


def test_the_reference_says_where_each_setting_goes():
    text = schema.as_markdown()
    for _cls, table in schema.SECTION_CLASSES.values():
        assert f"`{table}`" in text


def test_the_reference_gives_the_default_for_every_setting():
    for group in schema.GROUPS:
        for setting in group.settings:
            assert schema._default_for(group.section, setting)


def test_every_group_describes_a_real_config_table():
    assert set(schema.SECTION_CLASSES) == {g.section for g in schema.GROUPS}


# -- and the form has to actually render them ------------------------------
#
# The coverage above says every setting is *described*. It does not say the
# group holding it is ever put on a page, and those are different failures
# with the same symptom: a setting nobody using the GUI can reach. The form
# used to name each group in the body of `refresh`, so a group added to
# GROUPS and forgotten there was fully documented, passed every test above,
# and appeared nowhere in the window -- which is how [window_management]
# arrived, and it was caught by looking at a screenshot. The layout is now a
# table, and this is what reads it.


def test_every_group_of_settings_is_rendered_somewhere_in_the_form():
    tabs = pytest.importorskip("glasslinkxp.gui.tabs")

    placed = {group for _page, group, _path in tabs.SETTINGS_PAGES}
    placed |= set(tabs.PER_DISPLAY_GROUPS)
    missing = [group.title for group in schema.GROUPS if group not in placed]
    assert not missing, (
        f"these groups are in schema.GROUPS but on no page of the form: {missing}. "
        "Add them to tabs.SETTINGS_PAGES, or to tabs.PER_DISPLAY_GROUPS if they are "
        "rendered once per display."
    )


def test_the_form_does_not_render_a_group_twice():
    tabs = pytest.importorskip("glasslinkxp.gui.tabs")

    groups = [group for _page, group, _path in tabs.SETTINGS_PAGES]
    assert len(groups) == len(set(groups))
    assert not set(groups) & set(tabs.PER_DISPLAY_GROUPS)


def test_every_group_is_placed_on_a_page_that_exists():
    tabs = pytest.importorskip("glasslinkxp.gui.tabs")

    unknown = {page for page, _group, _path in tabs.SETTINGS_PAGES
               if page not in tabs.SETTINGS_PAGE_NAMES}
    assert not unknown, f"no such Settings page: {sorted(unknown)}"


def test_each_group_reads_from_the_toml_table_it_documents():
    """The path into the document has to be the section the schema names.

    Pointed at the wrong table, a group renders perfectly and edits something
    else -- or nothing, silently creating a section the daemon never reads.
    """
    tabs = pytest.importorskip("glasslinkxp.gui.tabs")

    for _page, group, path in tabs.SETTINGS_PAGES:
        assert path == (group.section,), (
            f"{group.title} is read from {path} but documents [{group.section}]"
        )
