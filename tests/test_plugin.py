"""The XPPython3 plugin's logic, exercised against a stubbed SDK.

XPPython3 only exists inside X-Plane, so the ``XPPython3`` module is faked
here. This does not prove the plugin runs in the sim -- it proves the dataref
buffer handling is correct.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "src" / "xppython3" / "PI_GlassLinkXP.py"


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
        # Kept as a trap rather than removed: the whole point of the plugin is
        # that it does no per-frame work, so a callback appearing here is the
        # regression to catch, not an API to support.
        self.flight_loops.append((callback, interval))
        return callback

    def findPluginBySignature(self, signature):
        return 7 if "DataRefEditor" in signature else self.NO_PLUGIN_ID

    def sendMessageToPlugin(self, plugin, message, param):
        self.messages.append((plugin, message, param))


@pytest.fixture
def plugin(monkeypatch):
    fake_xp = FakeXP()
    module = types.ModuleType("XPPython3")
    module.xp = fake_xp
    monkeypatch.setitem(sys.modules, "XPPython3", module)

    spec = importlib.util.spec_from_file_location("PI_GlassLinkXP", PLUGIN_PATH)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    instance = loaded.PythonInterface()
    instance.XPluginStart()
    return instance, fake_xp


def test_creates_a_label_and_a_colour_dataref_per_cell(plugin):
    instance, fake_xp = plugin
    assert len(fake_xp.accessors) == 48  # 24 labels + 24 backgrounds
    assert "glasslinkxp/softkey/pfd/1" in fake_xp.accessors
    assert "glasslinkxp/softkey/mfd/12" in fake_xp.accessors
    assert "glasslinkxp/softkey/pfd/1/bg" in fake_xp.accessors
    assert "glasslinkxp/softkey/mfd/12/bg" in fake_xp.accessors
    assert len(instance.buffers) == 24 and len(instance.ints) == 24
    assert fake_xp.accessors["glasslinkxp/softkey/pfd/1"]["dataType"] == fake_xp.Type_Data
    assert fake_xp.accessors["glasslinkxp/softkey/pfd/1/bg"]["dataType"] == fake_xp.Type_Int
    assert fake_xp.messages, "custom datarefs should be announced to DataRefEditor"


def test_the_field_holds_the_longest_label_with_room_to_spare(plugin):
    instance, _ = plugin
    from glasslinkxp.publish import encode_field

    assert all(len(buffer) == 64 for buffer in instance.buffers.values())
    name = "glasslinkxp/softkey/pfd/1"
    longest = "FLIGHT PLAN"
    instance.write_data(name, encode_field(longest, 64), 0, 64)
    assert bytes(instance.buffers[name]).rstrip(b"\x00").decode() == longest


def test_int_datarefs_round_trip_and_ignore_junk(plugin):
    instance, _ = plugin
    name = "glasslinkxp/softkey/pfd/4/bg"
    assert instance.read_int(name) == 0
    instance.write_int(name, 2)
    assert instance.read_int(name) == 2
    instance.write_int(name, 3.0)  # the Web API may deliver a float
    assert instance.read_int(name) == 3
    instance.write_int(name, "not a number")  # must not raise or clobber
    assert instance.read_int(name) == 3
    instance.write_int("glasslinkxp/softkey/nope/1/bg", 1)  # unknown: ignored
    assert instance.read_int("glasslinkxp/softkey/nope/1/bg") == 0


def test_read_and_write_round_trip(plugin):
    instance, _ = plugin
    name = "glasslinkxp/softkey/pfd/1"
    instance.write_data(name, b"INSET" + b"\x00" * 59, 0, 64)
    assert instance.read_data(name, None, 0, 64) == 64
    out = bytearray(64)
    assert instance.read_data(name, out, 0, 64) == 64
    assert bytes(out).rstrip(b"\x00") == b"INSET"


def test_write_is_bounded(plugin):
    instance, _ = plugin
    name = "glasslinkxp/softkey/mfd/3"
    instance.write_data(name, b"X" * 256, 0, 256)
    assert len(instance.buffers[name]) == 64
    instance.write_data(name, b"Y", 999, 1)  # past the end: ignored
    assert len(instance.buffers[name]) == 64
    instance.write_data("glasslinkxp/softkey/none", b"Z", 0, 1)  # unknown: ignored


def test_the_plugin_does_no_per_frame_work(plugin):
    """Enable must register nothing with the flight loop.

    The plugin exists only to create the datarefs; the daemon writes them from
    outside the sim over the WebSocket or REST API, both confirmed working
    against a running X-Plane. It used to also poll a JSON file at 5 Hz, as a
    hedge against the Web API refusing to write a plugin-created dataref, and
    that hedge is no longer needed -- so the correct per-frame cost is zero,
    and this is the test that keeps it there.
    """
    instance, fake_xp = plugin
    assert instance.XPluginEnable() == 1
    assert not fake_xp.flight_loops
    instance.XPluginDisable()
    assert not fake_xp.flight_loops
    instance.XPluginStop()
    assert not fake_xp.accessors
