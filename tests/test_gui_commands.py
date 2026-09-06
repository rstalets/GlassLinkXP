"""The GUI can only build argv the real CLI accepts.

This is the test that matters most in the GUI's direction: every button in the
window turns into a subprocess argv, and a flag renamed in main.py would turn
the whole tab into an error dialog with nothing to say about why. So every
command the GUI can produce is parsed here by main.py's own parser.
"""

import pytest

from g1000_softkey.gui import commands
from g1000_softkey.main import build_parser


def _sample(option: commands.Option):
    """A plausible value for an option, so a full argv can be built."""
    if option.kind == "flag":
        return True
    if option.choices:
        return option.choices[0]
    if option.default is not None:
        return option.default
    return {"labels": "A,B,C", "hz": "8"}.get(option.key, "value")


@pytest.mark.parametrize("spec", commands.COMMANDS, ids=lambda s: s.name)
def test_every_command_parses_with_no_options(spec):
    """The bare form -- what a button does before anything is filled in."""
    values = {o.key: _sample(o) for o in spec.options if o.required}
    argv = commands.build_argv(spec, values)
    args = build_parser().parse_args(argv)
    assert args.command == spec.name


@pytest.mark.parametrize("spec", commands.COMMANDS, ids=lambda s: s.name)
def test_every_command_parses_with_every_option_set(spec):
    values = {option.key: _sample(option) for option in spec.options}
    argv = commands.build_argv(spec, values, config="config.toml", verbose=True)
    args = build_parser().parse_args(argv)
    assert args.command == spec.name
    assert args.config == "config.toml"
    assert args.verbose is True
    # Every value we supplied has to have reached the namespace, or the GUI is
    # collecting something the CLI silently drops.
    for option in spec.options:
        attribute = option.flag.lstrip("-").replace("-", "_")
        if option.key == "iterations":
            attribute = "iterations"
        assert hasattr(args, attribute), f"{spec.name} dropped {option.flag}"


def test_flags_are_omitted_when_false():
    argv = commands.build_argv(commands.RUN, {"once": False, "timing": False})
    assert argv == ["run"]


def test_empty_values_are_omitted():
    argv = commands.build_argv(commands.RUN, {"image": "  ", "hz": "", "publisher": None})
    assert argv == ["run"]


def test_config_and_verbose_come_first():
    argv = commands.build_argv(commands.RUN, {}, config="c.toml", verbose=True)
    assert argv[:4] == ["-c", "c.toml", "-v", "run"]


def test_a_required_option_left_empty_is_refused():
    with pytest.raises(commands.MissingOption):
        commands.build_argv(commands.TUNE, {})


def test_a_value_outside_the_choices_is_refused():
    with pytest.raises(ValueError):
        commands.build_argv(commands.RUN, {"publisher": "carrier-pigeon"})


def test_the_declared_output_option_is_a_real_option():
    """It was declared on three commands and read nowhere, so nothing would
    have noticed it naming an option that had been renamed away."""
    for spec in commands.COMMANDS:
        if spec.output_option:
            assert spec.option(spec.output_option).default, \
                f"{spec.name}: the output folder needs a default to fall back on"


def test_an_output_option_that_names_nothing_is_refused():
    with pytest.raises(KeyError):
        commands.CommandSpec("x", "X", "summary", output_option="out")


def test_the_output_folder_is_what_the_child_would_write_into():
    assert commands.output_folder(commands.CALIBRATE, {"out": " pictures "}) == "pictures"
    # empty box: the child falls back to the option default, so this must too
    assert commands.output_folder(commands.CALIBRATE, {"out": ""}) == \
        commands.CALIBRATE.option("out").default
    assert commands.output_folder(commands.CALIBRATE) == \
        commands.CALIBRATE.option("out").default


def test_a_command_that_writes_no_folder_has_none_to_show():
    assert commands.output_folder(commands.RUN, {"out": "somewhere"}) == ""


def test_run_is_the_only_long_running_command():
    assert [s.name for s in commands.COMMANDS if s.long_running] == ["run"]


def test_options_are_unique_within_a_command():
    for spec in commands.COMMANDS:
        keys = [option.key for option in spec.options]
        assert len(keys) == len(set(keys)), spec.name


def test_the_gui_covers_every_subcommand_the_cli_has():
    """A subcommand added to the CLI has to be given a home in the GUI.

    'gui' itself is the exception: a button that opened another window would
    be a curiosity rather than a feature.
    """
    parser = build_parser()
    actions = [a for a in parser._subparsers._group_actions if a.choices]  # noqa: SLF001
    cli = set(actions[0].choices) - {"gui"}
    assert cli == set(commands.BY_NAME)


def test_child_interpreter_prefers_the_console_build(tmp_path):
    (tmp_path / "pythonw.exe").write_text("")
    (tmp_path / "python.exe").write_text("")
    assert commands.child_interpreter(str(tmp_path / "pythonw.exe")) == \
        str(tmp_path / "python.exe")


def test_child_interpreter_keeps_pythonw_when_there_is_no_console_build(tmp_path):
    pythonw = tmp_path / "pythonw.exe"
    pythonw.write_text("")
    assert commands.child_interpreter(str(pythonw)) == str(pythonw)


def test_child_interpreter_leaves_an_ordinary_interpreter_alone():
    assert commands.child_interpreter("/usr/bin/python3") == "/usr/bin/python3"


def test_quoted_command_survives_a_path_with_spaces():
    quoted = commands.quote_command(["/opt/my python/python", "-m", "x", "--out", "a b"])
    assert quoted == '"/opt/my python/python" -m x --out "a b"'
