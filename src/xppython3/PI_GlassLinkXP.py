"""XPPython3 plugin: writable datarefs for the G1000 softkey labels and the
colour of the cell each label sits on.

    glasslinkxp/softkey/pfd/1 .. 12        pilot PFD   (sim/GPS/g1000n1_softkeyN)
    glasslinkxp/softkey/mfd/1 .. 12        MFD         (sim/GPS/g1000n3_softkeyN)
    glasslinkxp/softkey/<display>/N/bg     int: 0 black, 1 white, 2 yellow, 3 red

Each label is a fixed 64-byte, NUL-padded UTF-8 field, so PilotsDeck reads it
as ``glasslinkxp/softkey/pfd/1:s64``. The ``/bg`` datarefs are Int, not byte arrays:
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
It only creates the datarefs, because the X-Plane Web API can *write* a
dataref but cannot *create* one. The daemon then writes them over the
WebSocket or REST API, both of which have been confirmed against a running
X-Plane. Having no work of its own to do, the plugin registers no flight-loop
callback at all: it costs exactly nothing per frame.

Install: copy this file to  <X-Plane>/Resources/plugins/PythonPlugins/

Note: XPPython3 also ships a higher-level ``datarefs.create_dataref(name,
'string')`` helper. The low-level accessor is used here because it pins the
field to exactly FIELD_WIDTH bytes, which is what the ':s64' address depends
on.

Standard library and the XPPython3 API only: this runs inside XPPython3's own
bundled Python, not the daemon's venv, so it can import nothing else.
"""

from XPPython3 import xp

DISPLAYS = ("pfd", "mfd")
CELLS = 12
FIELD_WIDTH = 64


class PythonInterface:
    def __init__(self):
        self.name = "GlassLinkXP"
        self.sig = "com.github.glasslinkxp"
        self.desc = ("Publishes G1000 softkey labels as writable byte-array datarefs "
                     "and their cell background colour as writable int datarefs.")
        self.accessors = []
        self.buffers = {}       # label dataref name -> bytearray(FIELD_WIDTH)
        self.ints = {}          # "<label>/bg" -> int 0..3

    # -- plugin lifecycle -------------------------------------------------
    def XPluginStart(self):
        for display in DISPLAYS:
            for index in range(1, CELLS + 1):
                name = "glasslinkxp/softkey/{}/{}".format(display, index)
                self._create_dataref(name)
                self._create_int_dataref(name + "/bg")
        xp.log("created {} softkey datarefs ({} labels of {} bytes + {} colours)".format(
            len(self.accessors), len(self.buffers), FIELD_WIDTH, len(self.ints)))
        return self.name, self.sig, self.desc

    def XPluginEnable(self):
        # Nothing to schedule: the datarefs exist from XPluginStart and the
        # daemon writes them from outside the sim, so there is no per-frame
        # work to register and no frame-rate cost to account for.
        return 1

    def XPluginDisable(self):
        pass

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
