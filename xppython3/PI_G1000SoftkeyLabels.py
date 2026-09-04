"""XPPython3 plugin: 24 writable string (byte-array) datarefs for the G1000
softkey labels.

    g1000/softkey/pfd/1 .. 12     pilot PFD   (sim/GPS/g1000n1_softkeyN)
    g1000/softkey/mfd/1 .. 12     MFD         (sim/GPS/g1000n3_softkeyN)

Each is a fixed 16-byte, NUL-padded UTF-8 field, so PilotsDeck reads it as
``g1000/softkey/pfd/1:s16``.

The plugin deliberately does no OCR and no capture -- X-Plane calls its
flight loop inline with the sim, so anything expensive here costs frame rate.
It only:

1. creates the datarefs (the X-Plane Web API can *write* datarefs but cannot
   *create* them), so the OCR daemon can PATCH them over REST; and
2. as a fallback, polls a small JSON file written by the daemon at 5 Hz, for
   the case where the Web API refuses to write a plugin-created dataref.

Install: copy this file to  <X-Plane>/Resources/plugins/PythonPlugins/

Note: XPPython3 also ships a higher-level ``datarefs.create_dataref(name,
'string')`` helper. The low-level accessor is used here because it pins the
field to exactly 16 bytes, which is what the ':s16' address depends on.
"""

import json
import os
import tempfile

from XPPython3 import xp

DISPLAYS = ("pfd", "mfd")
CELLS = 12
FIELD_WIDTH = 16
POLL_INTERVAL = 0.2  # seconds; 5 Hz
JSON_ENV_VAR = "G1000_SOFTKEY_JSON"
JSON_BASENAME = "g1000_softkey_labels.json"


def json_path():
    """Same resolution order as the daemon's publish.default_json_path()."""
    override = os.environ.get(JSON_ENV_VAR)
    if override:
        return override
    return os.path.join(tempfile.gettempdir(), JSON_BASENAME)


class PythonInterface:
    def __init__(self):
        self.name = "G1000 Softkey Labels"
        self.sig = "com.github.g1000softkey.labels"
        self.desc = "Publishes G1000 softkey label strings as writable byte-array datarefs."
        self.accessors = []
        self.buffers = {}       # dataref name -> bytearray(FIELD_WIDTH)
        self.json_file = json_path()
        self.last_mtime = 0.0
        self.flight_loop = None

    # -- plugin lifecycle -------------------------------------------------
    def XPluginStart(self):
        for display in DISPLAYS:
            for index in range(1, CELLS + 1):
                self._create_dataref("g1000/softkey/{}/{}".format(display, index))
        xp.log("created {} softkey datarefs; JSON fallback: {}".format(
            len(self.accessors), self.json_file))
        return self.name, self.sig, self.desc

    def XPluginEnable(self):
        self.flight_loop = xp.registerFlightLoopCallback(self.poll, POLL_INTERVAL, None)
        return 1

    def XPluginDisable(self):
        if self.flight_loop is not None:
            xp.unregisterFlightLoopCallback(self.flight_loop, None)
            self.flight_loop = None

    def XPluginStop(self):
        for accessor in self.accessors:
            xp.unregisterDataAccessor(accessor)
        self.accessors = []

    def XPluginReceiveMessage(self, who, message, param):
        pass

    # -- datarefs ---------------------------------------------------------
    def _create_dataref(self, name):
        self.buffers[name] = bytearray(FIELD_WIDTH)
        accessor = xp.registerDataAccessor(
            name,
            xp.Type_Data,
            1,  # writable, so the Web API can PATCH it
            readData=self.read_data,
            writeData=self.write_data,
            readRefCon=name,
            writeRefCon=name,
        )
        self.accessors.append(accessor)
        self._announce(name)

    def read_data(self, refCon, outValue, offset, maxLength):
        buffer = self.buffers.get(refCon, b"")
        if outValue is None:
            return len(buffer)
        chunk = buffer[offset:offset + maxLength]
        outValue[:len(chunk)] = chunk
        return len(chunk)

    def write_data(self, refCon, inValue, offset, length):
        buffer = self.buffers.get(refCon)
        if buffer is None:
            return
        data = bytes(inValue[:length])
        end = min(FIELD_WIDTH, offset + len(data))
        if offset >= FIELD_WIDTH:
            return
        buffer[offset:end] = data[:end - offset]

    def _announce(self, name):
        """Tell DataRefEditor / DataRefTool about the custom dataref."""
        for signature in ("xplanesdk.examples.DataRefEditor", "com.leecbaker.datareftool"):
            try:
                plugin = xp.findPluginBySignature(signature)
                if plugin != xp.NO_PLUGIN_ID:
                    xp.sendMessageToPlugin(plugin, 0x01000000, name)
            except Exception as exc:  # noqa: BLE001 - never break plugin start
                xp.log("dataref announce failed: {}".format(exc))

    # -- JSON fallback ----------------------------------------------------
    def poll(self, sinceLast, sinceStart, counter, refCon):
        """Cheap flight-loop callback: stat, and read only when it changed."""
        try:
            mtime = os.path.getmtime(self.json_file)
        except OSError:
            return POLL_INTERVAL  # no file: the Web API path is in use
        if mtime == self.last_mtime:
            return POLL_INTERVAL
        self.last_mtime = mtime
        try:
            with open(self.json_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            for name, text in payload.get("labels", {}).items():
                buffer = self.buffers.get(name)
                if buffer is None:
                    continue
                raw = text.encode("utf-8", "ignore")[:FIELD_WIDTH - 1]
                buffer[:] = raw + b"\x00" * (FIELD_WIDTH - len(raw))
        except (OSError, ValueError, AttributeError) as exc:
            xp.log("could not apply {}: {}".format(self.json_file, exc))
        return POLL_INTERVAL
