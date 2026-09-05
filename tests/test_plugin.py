"""The XPPython3 plugin's logic, exercised against a stubbed SDK.

XPPython3 only exists inside X-Plane, so the ``XPPython3`` module is faked
here. This does not prove the plugin runs in the sim -- it proves the dataref
buffer handling and the JSON-fallback poll are correct.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "xppython3" / "PI_G1000SoftkeyLabels.py"


class FakeXP:
    Type_Data = 8
    Type_Int = 1
    NO_PLUGIN_ID = -1

    def __init__(self):
        self.accessors = {}
        self.messages = []
        self.logs = []
        self.flight_loops = []

    def log(self, message):
        self.logs.append(message)

    def registerDataAccessor(self, name, dataType, writable, **kwargs):
        assert dataType in (self.Type_Data, self.Type_Int) and writable == 1
        # An Int dataref must be registered with the int callbacks and a Data
        # dataref with the data ones: X-Plane types the dataref here, and a
        # mismatch is a dataref that reads as 0 forever.
        if dataType == self.Type_Int:
            assert set(kwargs) >= {"readInt", "writeInt"}, kwargs
            assert "readData" not in kwargs and "writeData" not in kwargs
        else:
            assert set(kwargs) >= {"readData", "writeData"}, kwargs
            assert "readInt" not in kwargs and "writeInt" not in kwargs
        self.accessors[name] = dict(kwargs, dataType=dataType)
        return f"accessor:{name}"

    def unregisterDataAccessor(self, accessor):
        self.accessors.pop(accessor.split(":", 1)[1], None)

    def registerFlightLoopCallback(self, callback, interval, refCon):
        self.flight_loops.append((callback, interval))
        return callback

    def unregisterFlightLoopCallback(self, callback, refCon):
        self.flight_loops = [f for f in self.flight_loops if f[0] is not callback]

    def findPluginBySignature(self, signature):
        return 7 if "DataRefEditor" in signature else self.NO_PLUGIN_ID

    def sendMessageToPlugin(self, plugin, message, param):
        self.messages.append((plugin, message, param))


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    fake_xp = FakeXP()
    module = types.ModuleType("XPPython3")
    module.xp = fake_xp
    monkeypatch.setitem(sys.modules, "XPPython3", module)
    monkeypatch.setenv("G1000_SOFTKEY_JSON", str(tmp_path / "labels.json"))

    spec = importlib.util.spec_from_file_location("PI_G1000SoftkeyLabels", PLUGIN_PATH)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    instance = loaded.PythonInterface()
    instance.XPluginStart()
    return instance, fake_xp, tmp_path / "labels.json"


def test_creates_a_label_and_a_colour_dataref_per_cell(plugin):
    instance, fake_xp, _ = plugin
    assert len(fake_xp.accessors) == 48  # 24 labels + 24 backgrounds
    assert "g1000/softkey/pfd/1" in fake_xp.accessors
    assert "g1000/softkey/mfd/12" in fake_xp.accessors
    assert "g1000/softkey/pfd/1/bg" in fake_xp.accessors
    assert "g1000/softkey/mfd/12/bg" in fake_xp.accessors
    assert len(instance.buffers) == 24 and len(instance.ints) == 24
    assert fake_xp.accessors["g1000/softkey/pfd/1"]["dataType"] == fake_xp.Type_Data
    assert fake_xp.accessors["g1000/softkey/pfd/1/bg"]["dataType"] == fake_xp.Type_Int
    assert fake_xp.messages, "custom datarefs should be announced to DataRefEditor"


def test_the_field_is_wide_enough_for_a_colour_prefixed_label(plugin):
    """20 bytes for '[[#000000FLIGHT PLAN' plus its NUL; the old 16 truncated it."""
    instance, _, _ = plugin
    from g1000_softkey.publish import encode_field

    assert all(len(buffer) == 64 for buffer in instance.buffers.values())
    name = "g1000/softkey/pfd/1"
    longest = "[[#000000FLIGHT PLAN"
    instance.write_data(name, encode_field(longest, 64), 0, 64)
    assert bytes(instance.buffers[name]).rstrip(b"\x00").decode() == longest


def test_int_datarefs_round_trip_and_ignore_junk(plugin):
    instance, _, _ = plugin
    name = "g1000/softkey/pfd/4/bg"
    assert instance.read_int(name) == 0
    instance.write_int(name, 2)
    assert instance.read_int(name) == 2
    instance.write_int(name, 3.0)  # the Web API may deliver a float
    assert instance.read_int(name) == 3
    instance.write_int(name, "not a number")  # must not raise or clobber
    assert instance.read_int(name) == 3
    instance.write_int("g1000/softkey/nope/1/bg", 1)  # unknown: ignored
    assert instance.read_int("g1000/softkey/nope/1/bg") == 0


def test_read_and_write_round_trip(plugin):
    instance, _, _ = plugin
    name = "g1000/softkey/pfd/1"
    instance.write_data(name, b"INSET" + b"\x00" * 59, 0, 64)
    assert instance.read_data(name, None, 0, 64) == 64
    out = bytearray(64)
    assert instance.read_data(name, out, 0, 64) == 64
    assert bytes(out).rstrip(b"\x00") == b"INSET"


def test_write_is_bounded(plugin):
    instance, _, _ = plugin
    name = "g1000/softkey/mfd/3"
    instance.write_data(name, b"X" * 256, 0, 256)
    assert len(instance.buffers[name]) == 64
    instance.write_data(name, b"Y", 999, 1)  # past the end: ignored
    assert len(instance.buffers[name]) == 64
    instance.write_data("g1000/softkey/none", b"Z", 0, 1)  # unknown: ignored


def test_json_fallback_is_applied(plugin):
    instance, _, json_file = plugin
    assert instance.poll(0, 0, 0, None) == 0.2  # no file yet, no crash

    json_file.write_text(json.dumps({
        "version": 2,
        "labels": {"g1000/softkey/pfd/1": "TMR/REF", "g1000/softkey/xxx/9": "ignored"},
        "numbers": {"g1000/softkey/pfd/1/bg": 1, "g1000/softkey/xxx/9/bg": 2},
    }))
    instance.poll(0, 0, 0, None)
    assert bytes(instance.buffers["g1000/softkey/pfd/1"]).rstrip(b"\x00") == b"TMR/REF"
    assert instance.ints["g1000/softkey/pfd/1/bg"] == 1

    before = instance.last_mtime
    instance.poll(0, 0, 0, None)  # unchanged mtime -> no re-read
    assert instance.last_mtime == before


def test_a_version_1_file_without_numbers_still_applies(plugin):
    """An older daemon writes labels only; the plugin must not care."""
    instance, _, json_file = plugin
    json_file.write_text(json.dumps({
        "version": 1,
        "labels": {"g1000/softkey/mfd/2": "MAP"},
    }))
    instance.poll(0, 0, 0, None)
    assert bytes(instance.buffers["g1000/softkey/mfd/2"]).rstrip(b"\x00") == b"MAP"
    assert instance.ints["g1000/softkey/mfd/2/bg"] == 0


def test_malformed_json_does_not_raise(plugin):
    instance, fake_xp, json_file = plugin
    json_file.write_text("{not json")
    instance.poll(0, 0, 0, None)
    assert any("could not apply" in message for message in fake_xp.logs)


def test_enable_disable_registers_the_flight_loop(plugin):
    instance, fake_xp, _ = plugin
    assert instance.XPluginEnable() == 1
    assert fake_xp.flight_loops and fake_xp.flight_loops[0][1] == 0.2
    instance.XPluginDisable()
    assert not fake_xp.flight_loops
    instance.XPluginStop()
    assert not fake_xp.accessors
