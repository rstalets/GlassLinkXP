import base64
import json
import sys
import types
from types import SimpleNamespace

import pytest

from g1000_softkey import publish
from g1000_softkey.config import PublishConfig
from g1000_softkey.publish import (
    ConsolePublisher,
    WebApiPublisher,
    create_publisher,
    encode_field,
    encode_field_b64,
    encode_value,
)

NAMES = [f"g1000/softkey/pfd/{i}" for i in range(1, 13)]


def test_encode_field_pads_and_truncates():
    assert encode_field("PFD") == b"PFD" + b"\x00" * 13
    assert len(encode_field("PFD")) == 16
    long = encode_field("FLIGHT PLAN LEGS", 16)
    assert len(long) == 16 and long.endswith(b"\x00")
    assert encode_field("") == b"\x00" * 16
    assert base64.b64decode(encode_field_b64("OBS")) == encode_field("OBS")


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeSession:
    """Stand-in for requests.Session recording what the client would send.

    It answers /api/capabilities the way a current X-Plane does, because the
    publisher asks that question before every id resolution and the answer
    decides which /api/vN the rest of the traffic goes to. The two GETs are
    counted separately so a test about re-resolution is not also counting
    version negotiation.
    """

    def __init__(self, ids=None, patch_status=200, fail=False, versions=("v1", "v2", "v3")):
        self.ids = ids if ids is not None else {name: 100 + i for i, name in enumerate(NAMES)}
        self.patch_status = patch_status
        self.fail = fail
        self.versions = versions
        self.patches = []
        self.list_calls = 0
        self.capability_calls = 0
        self.closed = False

    def get(self, url, timeout=None):
        if url.endswith("/api/capabilities"):
            self.capability_calls += 1
            if self.fail:
                raise ConnectionError("connection refused")
            return FakeResponse(200, {"api": {"versions": list(self.versions)}})
        self.list_calls += 1
        if self.fail:
            raise ConnectionError("connection refused")
        data = [{"id": i, "name": n} for n, i in self.ids.items()]
        data.append({"id": 1, "name": "sim/flightmodel/position/latitude"})
        return FakeResponse(200, {"data": data})

    def patch(self, url, json=None, timeout=None):
        if self.fail:
            raise ConnectionError("connection refused")
        self.patches.append((url, json))
        return FakeResponse(self.patch_status)

    def close(self):
        self.closed = True


def test_webapi_resolves_ids_and_patches_base64():
    session = FakeSession()
    publisher = WebApiPublisher(PublishConfig(), NAMES, session=session)
    assert session.list_calls == 1
    publisher.publish({NAMES[0]: "INSET"})
    url, body = session.patches[0]
    # v3, not the configured v1: the version comes from /api/capabilities.
    assert url.endswith("/api/v3/datarefs/100/value")
    assert base64.b64decode(body["data"]) == encode_field("INSET", PublishConfig().field_width)

    publisher.publish({NAMES[0]: "INSET"})  # unchanged -> no traffic
    assert len(session.patches) == 1
    publisher.publish({NAMES[0]: "PFD"})
    assert len(session.patches) == 2


def test_webapi_re_resolves_after_a_404():
    session = FakeSession(patch_status=404)
    publisher = WebApiPublisher(PublishConfig(), NAMES, session=session)
    publisher.publish({NAMES[0]: "INSET"})
    assert session.list_calls == 2  # startup + re-resolve


def test_webapi_survives_xplane_not_running(caplog):
    session = FakeSession(fail=True)
    publisher = WebApiPublisher(PublishConfig(), NAMES, session=session)
    publisher.publish({NAMES[0]: "INSET"})  # must not raise
    assert "unreachable" in caplog.text.lower()
    publisher.close()
    assert session.closed


def test_webapi_warns_about_missing_datarefs(caplog):
    session = FakeSession(ids={NAMES[0]: 42})
    WebApiPublisher(PublishConfig(), NAMES, session=session)
    assert "not registered" in caplog.text


def test_webapi_negotiates_the_highest_advertised_api_version():
    """REST resolves the version the same way the websocket does.

    It used to interpolate publish.api_version verbatim, so the websocket
    auto-upgraded while REST stayed on whatever the config file said -- v1 by
    default, two versions behind what X-Plane 12 now serves.
    """
    session = FakeSession(versions=("v1", "v2", "v3"))
    publisher = WebApiPublisher(PublishConfig(api_version="v1"), NAMES, session=session)
    publisher.publish({NAMES[0]: "INSET"})
    assert publisher._root.endswith("/api/v3")
    assert session.patches[0][0].endswith("/api/v3/datarefs/100/value")


def test_api_version_falls_back_to_the_floor_when_capabilities_is_unreachable():
    """The configured version is a floor, used only when the sim does not say.

    An X-Plane that does not answer /api/capabilities is an old one, so the
    floor has to stay at the oldest version rather than track the newest.
    """

    class NoCapabilities(FakeSession):
        def get(self, url, timeout=None):
            if url.endswith("/api/capabilities"):
                self.capability_calls += 1
                raise ConnectionError("404 not found")
            return super().get(url, timeout=timeout)

    session = NoCapabilities()
    publisher = WebApiPublisher(PublishConfig(api_version="v1"), NAMES, session=session)
    publisher.publish({NAMES[0]: "INSET"})
    assert publisher._root.endswith("/api/v1")
    assert session.patches[0][0].endswith("/api/v1/datarefs/100/value"), "writes still land"


def test_capabilities_is_asked_once_and_then_cached():
    """Not per publish, and not per write: X-Plane cannot change its answer
    without a restart, which invalidates the dataref ids anyway."""
    session = FakeSession()
    publisher = WebApiPublisher(PublishConfig(), NAMES, session=session)
    assert session.capability_calls == 1
    for label in ("INSET", "PFD", "OBS", "CDI"):
        publisher.publish({NAMES[0]: label, NAMES[1]: label})
    assert len(session.patches) == 8
    assert session.capability_calls == 1


def test_a_failed_negotiation_is_retried_on_the_next_resolve():
    """The floor is not cached: a daemon started before X-Plane must pick up
    the real answer once the sim is up, not stay on v1 for the session."""

    class LateStart(FakeSession):
        answering = False

        def get(self, url, timeout=None):
            if url.endswith("/api/capabilities") and not self.answering:
                self.capability_calls += 1
                raise ConnectionError("connection refused")
            return super().get(url, timeout=timeout)

    session = LateStart(patch_status=404)
    publisher = WebApiPublisher(PublishConfig(retry_interval=0.0), NAMES, session=session)
    assert publisher._root.endswith("/api/v1")
    session.answering = True
    publisher.publish({NAMES[0]: "INSET"})  # the 404 forces a re-resolve
    assert publisher._root.endswith("/api/v3")


def test_console_publisher_only_logs_changes(caplog):
    import logging

    caplog.set_level(logging.INFO)
    publisher = ConsolePublisher()
    publisher.publish({NAMES[0]: "INSET"})
    publisher.publish({NAMES[0]: "INSET"})
    assert caplog.text.count("INSET") == 1


def test_create_publisher_rejects_unknown_targets():
    with pytest.raises(ValueError):
        create_publisher(PublishConfig(target="carrier-pigeon"), NAMES)
    assert create_publisher(PublishConfig(target="console"), NAMES).name == "console"


# ---------------------------------------------------------------------------
# WebSocketPublisher: one message per cycle instead of one PATCH per dataref
# ---------------------------------------------------------------------------


class _FakeWs:
    def __init__(self):
        self.sent = []
        self.closed = False

    def settimeout(self, _value):
        return None

    def send(self, message):
        self.sent.append(message)

    def close(self):
        self.closed = True


@pytest.fixture
def ws_env(monkeypatch):
    """A stubbed websocket module plus a REST session that resolves ids."""
    names = [f"g1000/softkey/pfd/{i}" for i in range(1, 13)]
    return _ws_env(monkeypatch, names)


@pytest.fixture
def ws_env_with_colors(monkeypatch):
    """The same, with the /bg int datarefs interleaved as the daemon sends them."""
    names = []
    for i in range(1, 13):
        names.append(f"g1000/softkey/pfd/{i}")
        names.append(f"g1000/softkey/pfd/{i}/bg")
    return _ws_env(monkeypatch, names)


def _ws_env(monkeypatch, names):
    ws = _FakeWs()
    module = types.ModuleType("websocket")
    module.create_connection = lambda url, timeout=None: ws
    monkeypatch.setitem(sys.modules, "websocket", module)

    session = SimpleNamespace(
        get=lambda url, timeout=None: SimpleNamespace(
            status_code=200,
            json=lambda: {"data": [{"id": 1000 + i, "name": n} for i, n in enumerate(names)]},
        ),
        # the publisher writes over REST on any cycle without a socket
        patch=lambda url, json=None, timeout=None: SimpleNamespace(status_code=200),
        close=lambda: None,
    )
    return names, ws, session


def test_websocket_batches_a_whole_menu_into_one_message(ws_env):
    names, ws, session = ws_env
    pub = publish.WebSocketPublisher(PublishConfig(target="websocket"), names, session=session)
    labels = ["INSET", "", "PFD", "OBS", "CDI", "DME",
              "XPDR", "IDENT", "TMR/REF", "NRST", "", "ALERTS"]

    pub.publish(dict(zip(names, labels)))

    assert len(ws.sent) == 1, "a 12-cell change must cost exactly one message"
    message = json.loads(ws.sent[0])
    assert message["type"] == "dataref_set_values"
    assert len(message["params"]["datarefs"]) == 12
    first = message["params"]["datarefs"][0]
    assert first["id"] == 1000
    assert base64.b64decode(first["value"]).rstrip(b"\x00").decode() == "INSET"


def test_websocket_sends_nothing_when_labels_are_unchanged(ws_env):
    names, ws, session = ws_env
    pub = publish.WebSocketPublisher(PublishConfig(target="websocket"), names, session=session)
    labels = {n: "PFD" for n in names}

    pub.publish(labels)
    ws.sent.clear()
    pub.publish(labels)

    assert ws.sent == []


def test_websocket_sends_only_the_cells_that_changed(ws_env):
    names, ws, session = ws_env
    pub = publish.WebSocketPublisher(PublishConfig(target="websocket"), names, session=session)
    labels = {n: "PFD" for n in names}
    pub.publish(labels)
    ws.sent.clear()

    labels[names[4]] = "GPS"
    pub.publish(labels)

    assert len(ws.sent) == 1
    payload = json.loads(ws.sent[0])["params"]["datarefs"]
    assert len(payload) == 1
    assert payload[0]["id"] == 1004


def test_websocket_reconnects_after_a_send_failure(ws_env):
    names, ws, session = ws_env
    pub = publish.WebSocketPublisher(PublishConfig(target="websocket"), names, session=session)

    def _boom(_message):
        raise OSError("connection reset")

    ws.send = _boom
    pub.publish({names[0]: "INSET"})
    assert pub._ws is None, "a failed send must drop the socket so the next cycle reconnects"

    # The failed send fell through to a REST write, so INSET is already the
    # published value; only a genuinely new label produces another message.
    ws.send = ws.sent.append
    pub.publish({names[0]: "OBS"})
    assert pub._ws is not None
    assert len(ws.sent) == 1


def test_create_publisher_accepts_websocket_target(ws_env):
    names, _ws, session = ws_env
    pub = publish.WebSocketPublisher(PublishConfig(target="websocket"), names, session=session)
    assert pub.name == "websocket"

def test_websocket_falls_back_to_rest_when_it_cannot_connect(monkeypatch):
    """A dead socket must never mean dropped labels, nor a stall per cycle."""
    names = [f"g1000/softkey/pfd/{i}" for i in range(1, 13)]
    patched = []
    session = SimpleNamespace(
        get=lambda url, timeout=None: SimpleNamespace(
            status_code=200,
            json=lambda: {"data": [{"id": 1000 + i, "name": n} for i, n in enumerate(names)]},
        ),
        patch=lambda url, json=None, timeout=None: (
            patched.append(url), SimpleNamespace(status_code=200)
        )[1],
        close=lambda: None,
    )
    attempts = []
    module = types.ModuleType("websocket")

    def _refuse(url, timeout=None):
        attempts.append(url)
        raise TimeoutError("timed out")

    module.create_connection = _refuse
    monkeypatch.setitem(sys.modules, "websocket", module)

    pub = publish.WebSocketPublisher(
        PublishConfig(target="websocket", retry_interval=60.0), names, session=session
    )
    for cycle in range(5):
        pub.publish({n: f"L{cycle}{i}" for i, n in enumerate(names)})

    assert len(attempts) == 1, "connect must back off, not retry every cycle"
    assert len(patched) == 60, "labels must still be written over REST"


def test_websocket_resumes_batching_once_the_socket_comes_back(monkeypatch):
    names = [f"g1000/softkey/pfd/{i}" for i in range(1, 13)]
    session = SimpleNamespace(
        get=lambda url, timeout=None: SimpleNamespace(
            status_code=200,
            json=lambda: {"data": [{"id": 1000 + i, "name": n} for i, n in enumerate(names)]},
        ),
        patch=lambda url, json=None, timeout=None: SimpleNamespace(status_code=200),
        close=lambda: None,
    )
    ws = _FakeWs()
    state = {"up": False}
    module = types.ModuleType("websocket")

    def _maybe(url, timeout=None):
        if not state["up"]:
            raise TimeoutError("timed out")
        return ws

    module.create_connection = _maybe
    monkeypatch.setitem(sys.modules, "websocket", module)

    pub = publish.WebSocketPublisher(
        PublishConfig(target="websocket", retry_interval=0.0), names, session=session
    )
    pub.publish({n: "A" for n in names})
    assert pub._fell_back is True
    assert ws.sent == []

    state["up"] = True
    pub.publish({n: "B" for n in names})
    assert pub._fell_back is False
    assert len(ws.sent) == 1, "one batched message once the socket is back"


def test_websocket_negotiates_the_highest_advertised_api_version(monkeypatch):
    names = ["g1000/softkey/pfd/1"]

    def _get(url, timeout=None):
        if url.endswith("/api/capabilities"):
            return SimpleNamespace(
                status_code=200, json=lambda: {"api": {"versions": ["v1", "v2", "v3"]}}
            )
        return SimpleNamespace(
            status_code=200, json=lambda: {"data": [{"id": 1, "name": names[0]}]}
        )

    session = SimpleNamespace(get=_get, close=lambda: None)
    ws = _FakeWs()
    seen = []
    module = types.ModuleType("websocket")
    module.create_connection = lambda url, timeout=None: (seen.append(url), ws)[1]
    monkeypatch.setitem(sys.modules, "websocket", module)

    pub = publish.WebSocketPublisher(
        PublishConfig(target="websocket", api_version="v1"), names, session=session
    )
    pub.publish({names[0]: "INSET"})

    assert seen == ["ws://127.0.0.1:8086/api/v3"]  # localhost is rewritten; see the IPv4 test


def test_websocket_uses_the_ipv4_literal_instead_of_localhost(monkeypatch):
    """X-Plane binds 127.0.0.1 only; ::1 (which Windows prefers) just hangs."""
    names = ["g1000/softkey/pfd/1"]

    def _get(url, timeout=None):
        if url.endswith("/api/capabilities"):
            return SimpleNamespace(status_code=200, json=lambda: {"api": {"versions": ["v3"]}})
        return SimpleNamespace(
            status_code=200, json=lambda: {"data": [{"id": 1, "name": names[0]}]}
        )

    session = SimpleNamespace(get=_get, close=lambda: None)
    ws = _FakeWs()
    seen = []
    module = types.ModuleType("websocket")
    module.create_connection = lambda url, timeout=None: (seen.append(url), ws)[1]
    monkeypatch.setitem(sys.modules, "websocket", module)

    pub = publish.WebSocketPublisher(
        PublishConfig(target="websocket", base_url="http://localhost:8086"),
        names,
        session=session,
    )
    pub.publish({names[0]: "INSET"})

    assert seen == ["ws://127.0.0.1:8086/api/v3"]
    # REST keeps whatever the user configured; only the websocket is rewritten.
    assert pub.config.base_url == "http://localhost:8086"


# ---------------------------------------------------------------------------
# Numeric datarefs (the /bg background colour), across all four publishers
# ---------------------------------------------------------------------------

BG_NAMES = [f"{name}/bg" for name in NAMES]


def test_encode_value_splits_strings_from_numbers():
    """X-Plane types the dataref, so the Python type is the whole signal:
    a Data dataref takes base64, an Int dataref takes a bare number."""
    assert encode_value("PFD", 16) == encode_field_b64("PFD", 16)
    assert encode_value(2) == 2
    assert encode_value(0) == 0  # not falsy-dropped, and not "0"
    assert isinstance(encode_value(3), int)


def test_webapi_patches_a_number_not_base64():
    session = FakeSession(ids={NAMES[0]: 100, BG_NAMES[0]: 200})
    publisher = WebApiPublisher(PublishConfig(), [NAMES[0], BG_NAMES[0]], session=session)
    publisher.publish({NAMES[0]: "INSET", BG_NAMES[0]: 3})
    bodies = {url.rsplit("/", 2)[-2]: body for url, body in session.patches}
    assert base64.b64decode(bodies["100"]["data"]) == encode_field("INSET", 64)
    assert bodies["200"] == {"data": 3}, "an Int dataref is written as a bare number"


def test_webapi_republishes_a_colour_change_with_an_unchanged_label():
    session = FakeSession(ids={NAMES[0]: 100, BG_NAMES[0]: 200})
    publisher = WebApiPublisher(PublishConfig(), [NAMES[0], BG_NAMES[0]], session=session)
    publisher.publish({NAMES[0]: "STD BARO", BG_NAMES[0]: 0})
    assert len(session.patches) == 2
    publisher.publish({NAMES[0]: "STD BARO", BG_NAMES[0]: 1})
    assert len(session.patches) == 3
    assert session.patches[-1][1] == {"data": 1}


def test_console_publisher_logs_numbers(caplog):
    import logging

    caplog.set_level(logging.INFO)
    publisher = ConsolePublisher()
    publisher.publish({NAMES[0]: "INSET", BG_NAMES[0]: 2})
    publisher.publish({NAMES[0]: "INSET", BG_NAMES[0]: 2})
    assert caplog.text.count("INSET") == 1
    assert BG_NAMES[0] in caplog.text


def test_websocket_batches_labels_and_colours_into_one_message(ws_env_with_colors):
    """One frame is one message whether or not it carries numbers, and each
    dataref is encoded for its own type inside that message."""
    names, ws, session = ws_env_with_colors
    pub = publish.WebSocketPublisher(PublishConfig(target="websocket"), names, session=session)

    values = {}
    for index, name in enumerate(names):
        values[name] = 1 if name.endswith("/bg") else f"KEY{index}"
    pub.publish(values)

    assert len(ws.sent) == 1
    datarefs = json.loads(ws.sent[0])["params"]["datarefs"]
    assert len(datarefs) == 24
    labels = [d for d in datarefs if isinstance(d["value"], str)]
    numbers = [d for d in datarefs if not isinstance(d["value"], str)]
    assert len(labels) == 12 and len(numbers) == 12
    assert base64.b64decode(labels[0]["value"]).rstrip(b"\x00").decode() == "KEY0"
    assert numbers[0]["value"] == 1


def test_websocket_sends_a_colour_change_with_an_unchanged_label(ws_env_with_colors):
    names, ws, session = ws_env_with_colors
    pub = publish.WebSocketPublisher(PublishConfig(target="websocket"), names, session=session)
    values = {n: (0 if n.endswith("/bg") else "PFD") for n in names}
    pub.publish(values)
    ws.sent.clear()

    values["g1000/softkey/pfd/3/bg"] = 1  # softkey 3 became selected
    pub.publish(values)

    datarefs = json.loads(ws.sent[0])["params"]["datarefs"]
    assert len(datarefs) == 1
    assert datarefs[0]["value"] == 1
