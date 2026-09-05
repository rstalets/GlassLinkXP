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
    NO_PLUGIN_ID = -1

    def __init__(self):
        self.accessors = {}
        self.messages = []
        self.logs = []
        self.flight_loops = []

    def log(self, message):
        self.logs.append(message)

    def registerDataAccessor(self, name, dataType, writable, **kwargs):
        assert dataType == self.Type_Data and writable == 1
        self.accessors[name] = kwargs
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


def test_creates_24_writable_datarefs(plugin):
    instance, fake_xp, _ = plugin
    assert len(fake_xp.accessors) == 24
    assert "g1000/softkey/pfd/1" in fake_xp.accessors
    assert "g1000/softkey/mfd/12" in fake_xp.accessors
    assert all(len(buffer) == 16 for buffer in instance.buffers.values())
    assert fake_xp.messages, "custom datarefs should be announced to DataRefEditor"


def test_read_and_write_round_trip(plugin):
    instance, _, _ = plugin
    name = "g1000/softkey/pfd/1"
    instance.write_data(name, b"INSET" + b"\x00" * 11, 0, 16)
    assert instance.read_data(name, None, 0, 16) == 16
    out = bytearray(16)
    assert instance.read_data(name, out, 0, 16) == 16
    assert bytes(out).rstrip(b"\x00") == b"INSET"


def test_write_is_bounded(plugin):
    instance, _, _ = plugin
    name = "g1000/softkey/mfd/3"
    instance.write_data(name, b"X" * 64, 0, 64)
    assert len(instance.buffers[name]) == 16
    instance.write_data(name, b"Y", 99, 1)  # past the end: ignored
    assert len(instance.buffers[name]) == 16
    instance.write_data("g1000/softkey/none", b"Z", 0, 1)  # unknown: ignored


def test_json_fallback_is_applied(plugin):
    instance, _, json_file = plugin
    assert instance.poll(0, 0, 0, None) == 0.2  # no file yet, no crash

    json_file.write_text(json.dumps({
        "version": 1,
        "labels": {"g1000/softkey/pfd/1": "TMR/REF", "g1000/softkey/xxx/9": "ignored"},
    }))
    instance.poll(0, 0, 0, None)
    assert bytes(instance.buffers["g1000/softkey/pfd/1"]).rstrip(b"\x00") == b"TMR/REF"

    before = instance.last_mtime
    instance.poll(0, 0, 0, None)  # unchanged mtime -> no re-read
    assert instance.last_mtime == before


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
