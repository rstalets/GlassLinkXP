"""Starting, reading and stopping a child process."""

import sys
import time

import pytest

from glasslinkxp.gui import prefs
from glasslinkxp.gui.runner import (
    CommandRunner,
    Failed,
    Finished,
    Line,
    Started,
    child_environment,
)


def _drain(runner, seconds=10.0):
    """Everything the child said, once it has exited."""
    events = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        events += runner.drain()
        if any(isinstance(e, (Finished, Failed)) for e in events):
            return events
        time.sleep(0.02)
    raise AssertionError(f"child did not finish; saw {events}")


def _python(code):
    return [sys.executable, "-c", code]


def test_output_arrives_in_order():
    runner = CommandRunner()
    runner.start(_python("for i in range(5): print(i)"))
    events = _drain(runner)
    assert isinstance(events[0], Started)
    assert [e.text for e in events if isinstance(e, Line)] == ["0", "1", "2", "3", "4"]
    assert events[-1] == Finished(0)


def test_stderr_is_merged_into_the_same_stream():
    """The daemon prints to stdout and logs to stderr; the order matters."""
    runner = CommandRunner()
    runner.start(_python(
        "import sys\n"
        "print('out', flush=True)\n"
        "print('err', file=sys.stderr, flush=True)\n"
    ))
    lines = [e.text for e in _drain(runner) if isinstance(e, Line)]
    assert lines == ["out", "err"]


def test_the_exit_code_is_reported():
    runner = CommandRunner()
    runner.start(_python("raise SystemExit(3)"))
    assert _drain(runner)[-1] == Finished(3)


def test_a_command_that_does_not_exist_fails_without_raising():
    runner = CommandRunner()
    runner.start(["definitely-not-a-real-program-8b21"])
    events = runner.drain()
    assert len(events) == 1 and isinstance(events[0], Failed)
    assert not runner.is_running


def test_a_long_running_child_is_stopped():
    runner = CommandRunner()
    runner.start(_python("import time\nwhile True: time.sleep(0.05)"))
    time.sleep(0.5)
    assert runner.is_running
    runner.stop(grace=3.0)
    assert not runner.is_running


def test_stopping_lets_the_child_clean_up_first():
    """The daemon closes its publisher and Tesseract in a `finally`, and only
    gets to do that if the stop arrives as a signal it handles."""
    runner = CommandRunner()
    runner.start(_python(
        "import signal, sys, time\n"
        "def bye(*_):\n"
        "    print('cleaned up', flush=True)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGINT, bye)\n"
        "if hasattr(signal, 'SIGBREAK'): signal.signal(signal.SIGBREAK, bye)\n"
        "print('ready', flush=True)\n"
        "while True: time.sleep(0.05)\n"
    ))
    deadline = time.monotonic() + 10
    seen = []
    while time.monotonic() < deadline and "ready" not in seen:
        seen += [e.text for e in runner.drain() if isinstance(e, Line)]
        time.sleep(0.02)
    assert "ready" in seen
    runner.stop(grace=5.0)
    seen += [e.text for e in runner.drain() if isinstance(e, Line)]
    assert "cleaned up" in seen


def test_a_child_that_ignores_the_signal_is_killed_anyway():
    runner = CommandRunner()
    runner.start(_python(
        "import signal, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "while True: time.sleep(0.05)\n"
    ))
    time.sleep(0.5)
    runner.stop(grace=1.0)
    assert not runner.is_running


def test_stopping_something_that_is_not_running_is_harmless():
    CommandRunner().stop()


def test_starting_twice_is_a_programming_error():
    runner = CommandRunner()
    runner.start(_python("import time\ntime.sleep(5)"))
    with pytest.raises(RuntimeError):
        runner.start(_python("print(1)"))
    runner.stop(grace=2.0)


def test_a_runner_can_be_used_again_after_its_child_exits():
    runner = CommandRunner()
    runner.start(_python("print('first')"))
    _drain(runner)
    runner.start(_python("print('second')"))
    assert [e.text for e in _drain(runner) if isinstance(e, Line)] == ["second"]


def test_draining_is_bounded_so_the_ui_stays_responsive():
    runner = CommandRunner()
    runner.start(_python("for i in range(50): print(i)"))
    _drain(runner)
    runner.events.queue.clear()
    for index in range(30):
        runner.events.put(Line(str(index)))
    assert len(runner.drain(limit=10)) == 10
    assert len(runner.drain()) == 20


def test_children_are_told_not_to_buffer():
    """A block-buffered pipe would hold the daemon's output back for ever."""
    env = child_environment({"PATH": "/usr/bin"})
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PATH"] == "/usr/bin"


def test_output_is_decoded_even_when_it_is_not_valid_utf8():
    runner = CommandRunner()
    runner.start(_python(
        "import sys\nsys.stdout.buffer.write(b'label \\xff\\n')\nsys.stdout.flush()"
    ))
    lines = [e.text for e in _drain(runner) if isinstance(e, Line)]
    assert lines and lines[0].startswith("label ")


# -- preferences -----------------------------------------------------------


def test_preferences_round_trip(tmp_path):
    path = tmp_path / "gui.json"
    prefs.save({"config_path": "/a/b.toml", "verbose": True, "unknown": 1}, path)
    stored = prefs.load(path)
    assert stored["config_path"] == "/a/b.toml"
    assert stored["verbose"] is True
    assert "unknown" not in stored


def test_a_corrupt_preferences_file_is_ignored(tmp_path):
    path = tmp_path / "gui.json"
    path.write_text("{not json", encoding="utf-8")
    assert prefs.load(path) == prefs.DEFAULTS


def test_missing_preferences_are_the_defaults(tmp_path):
    assert prefs.load(tmp_path / "nothing.json") == prefs.DEFAULTS


def test_an_unwritable_preferences_path_does_not_raise(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    prefs.save({"verbose": True}, blocker / "gui.json")


def test_the_config_path_asked_for_wins(tmp_path):
    assert prefs.resolve_config_path(tmp_path / "x.toml", "/other.toml") == tmp_path / "x.toml"


def test_the_last_used_config_is_reopened(tmp_path):
    stored = tmp_path / "stored.toml"
    stored.write_text("", encoding="utf-8")
    assert prefs.resolve_config_path(None, str(stored), tmp_path) == stored


def test_a_last_used_config_that_has_been_deleted_is_skipped(tmp_path):
    beside = tmp_path / "config.toml"
    beside.write_text("", encoding="utf-8")
    assert prefs.resolve_config_path(None, str(tmp_path / "gone.toml"), tmp_path) == beside


def test_no_config_anywhere_is_not_an_error(tmp_path):
    assert prefs.resolve_config_path(None, None, tmp_path) is None


def test_the_example_config_ships_with_the_project():
    assert prefs.example_config() is not None
