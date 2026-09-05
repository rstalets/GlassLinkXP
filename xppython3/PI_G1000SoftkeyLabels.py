"""XPPython3 plugin: writable datarefs for the G1000 softkey labels and the
colour of the cell each label sits on.

    g1000/softkey/pfd/1 .. 12        pilot PFD   (sim/GPS/g1000n1_softkeyN)
    g1000/softkey/mfd/1 .. 12        MFD         (sim/GPS/g1000n3_softkeyN)
    g1000/softkey/<display>/N/bg     int: 0 black, 1 white, 2 yellow, 3 red

Each label is a fixed 64-byte, NUL-padded UTF-8 field, so PilotsDeck reads it
as ``g1000/softkey/pfd/1:s64``. The ``/bg`` datarefs are Int, not byte arrays:
X-Plane types a dataref at registration, and a Stream Deck plugin choosing an
image by value wants a number, not the string "2".

FIELD_WIDTH is one of three places the label width is fixed -- the others are
``publish.field_width`` in the daemon's config and the ``:sNN`` suffix on
every PilotsDeck button. All three have to agree, and changing this one needs
an X-Plane restart, because the buffer is allocated when the accessor is
registered. Hence 64 rather than a snug fit: the longest label in labels.txt
is "FLIGHT PLAN" at 11 characters, which left the original 16-byte field four
characters of headroom for a vocabulary that grows whenever somebody finds a
softkey nobody had listed. 64 bytes across all 24 fields is about 1.5 KB.

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
field to exactly FIELD_WIDTH bytes, which is what the ':s64' address depends
on.

Standard library and the XPPython3 API only: this runs inside XPPython3's own
bundled Python, not the daemon's venv, so it can import nothing else.
"""

import json
import os
import tempfile

from XPPython3 import xp

DISPLAYS = ("pfd", "mfd")
CELLS = 12
FIELD_WIDTH = 64
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
        self.desc = ("Publishes G1000 softkey labels as writable byte-array datarefs "
                     "and their cell background colour as writable int datarefs.")
        self.accessors = []
        self.buffers = {}       # label dataref name -> bytearray(FIELD_WIDTH)
        self.ints = {}          # "<label>/bg" -> int 0..3
        self.json_file = json_path()
        self.last_mtime = 0.0
        self.flight_loop = None

    # -- plugin lifecycle -------------------------------------------------
    def XPluginStart(self):
        for display in DISPLAYS:
            for index in range(1, CELLS + 1):
                name = "g1000/softkey/{}/{}".format(display, index)
                self._create_dataref(name)
                self._create_int_dataref(name + "/bg")
        xp.log("created {} softkey datarefs ({} labels of {} bytes + {} colours); "
               "JSON fallback: {}".format(
                   len(self.accessors), len(self.buffers), FIELD_WIDTH,
                   len(self.ints), self.json_file))
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

    def _create_int_dataref(self, name):
        """One Int dataref for a cell's background colour class."""
        self.ints[name] = 0
        accessor = xp.registerDataAccessor(
            name,
            xp.Type_Int,
            1,  # writable, so the Web API can PATCH it
            readInt=self.read_int,
            writeInt=self.write_int,
            readRefCon=name,
            writeRefCon=name,
        )
        self.accessors.append(accessor)
        self._announce(name)

    def read_int(self, refCon):
        return self.ints.get(refCon, 0)

    def write_int(self, refCon, value):
        if refCon not in self.ints:
            return
        try:
            self.ints[refCon] = int(value)
        except (TypeError, ValueError):
            # A malformed write is a dropped colour, never a plugin that stops
            # servicing the flight loop.
            pass

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
            # "numbers" arrived with v2 of the file. A v1 file simply has none,
            # which is why this is a separate table rather than a type check on
            # a mixed one: an old daemon keeps working against a new plugin.
            for name, value in payload.get("numbers", {}).items():
                self.write_int(name, value)
        except (OSError, ValueError, AttributeError) as exc:
            xp.log("could not apply {}: {}".format(self.json_file, exc))
        return POLL_INTERVAL
