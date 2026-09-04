import base64
import json

import pytest

from g1000_softkey.config import PublishConfig
from g1000_softkey.publish import (
    ConsolePublisher,
    FilePublisher,
    WebApiPublisher,
    create_publisher,
    default_json_path,
    encode_field,
    encode_field_b64,
)

NAMES = [f"g1000/softkey/pfd/{i}" for i in range(1, 13)]


def test_encode_field_pads_and_truncates():
    assert encode_field("PFD") == b"PFD" + b"\x00" * 13
    assert len(encode_field("PFD")) == 16
    long = encode_field("FLIGHT PLAN LEGS", 16)
    assert len(long) == 16 and long.endswith(b"\x00")
    assert encode_field("") == b"\x00" * 16
    assert base64.b64decode(encode_field_b64("OBS")) == encode_field("OBS")


def test_default_json_path_honours_the_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("G1000_SOFTKEY_JSON", str(tmp_path / "labels.json"))
    assert default_json_path() == tmp_path / "labels.json"
    monkeypatch.delenv("G1000_SOFTKEY_JSON")
    assert default_json_path().name == "g1000_softkey_labels.json"


def test_file_publisher_writes_atomically(tmp_path):
    target = tmp_path / "labels.json"
    publisher = FilePublisher(PublishConfig(target="file", json_path=str(target)))
    publisher.publish({NAMES[0]: "INSET", NAMES[1]: ""})
    payload = json.loads(target.read_text())
    assert payload["labels"][NAMES[0]] == "INSET"
    assert payload["version"] == 1
    assert not list(tmp_path.glob("*.tmp"))

    before = target.stat().st_mtime_ns
    publisher.publish({NAMES[0]: "INSET", NAMES[1]: ""})  # unchanged -> no rewrite
    assert target.stat().st_mtime_ns == before

    publisher.publish({NAMES[0]: "PFD", NAMES[1]: ""})
    assert json.loads(target.read_text())["labels"][NAMES[0]] == "PFD"
    publisher.close()


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeSession:
    """Stand-in for requests.Session recording what the client would send."""

    def __init__(self, ids=None, patch_status=200, fail=False):
        self.ids = ids if ids is not None else {name: 100 + i for i, name in enumerate(NAMES)}
        self.patch_status = patch_status
        self.fail = fail
        self.patches = []
        self.list_calls = 0
        self.closed = False

    def get(self, url, timeout=None):
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
    assert url.endswith("/api/v1/datarefs/100/value")
    assert base64.b64decode(body["data"]) == encode_field("INSET", 16)

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
