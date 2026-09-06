"""Publishing labels to X-Plane.

Three targets:

``websocket`` One ``dataref_set_values`` message per cycle over X-Plane's
            WebSocket API. The normal path: a changed cell costs part of a
            frame that was going to be sent anyway.
``webapi``  PATCH the plugin-created datarefs through X-Plane's built-in REST
            API (12.1.1+). Data (byte array) datarefs are base64 in both
            directions; the Int datarefs carrying the background colour are
            written as bare numbers. Dataref ids are session-scoped, so names
            are resolved to ids at startup and re-resolved whenever a write
            404s.
``console`` Log the labels; for development only.

Both X-Plane paths write the datarefs the XPPython3 plugin creates, and both
have been confirmed doing so against a running X-Plane.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Mapping, Protocol, Sequence

from .config import PublishConfig

LOG = logging.getLogger(__name__)


def encode_field(text: str, width: int = 16) -> bytes:
    """UTF-8, truncated to ``width - 1`` bytes, NUL padded to ``width``."""
    raw = text.encode("utf-8", errors="ignore")[: max(0, width - 1)]
    return raw + b"\x00" * (width - len(raw))


def encode_field_b64(text: str, width: int = 16) -> str:
    return base64.b64encode(encode_field(text, width)).decode("ascii")


#: What a publisher accepts per dataref. A ``str`` is a label bound for a byte
#: array; an ``int`` is a numeric dataref (today, the ``/bg`` colour class).
Value = str | int | float


def encode_value(value: Value, width: int = 16) -> str | int | float:
    """Encode one dataref value for the wire.

    X-Plane's Web API distinguishes the two by dataref *type*, not by any flag
    we send: a Data (byte array) dataref takes a base64 string in ``data`` /
    ``value``, and an Int or Float dataref takes a bare JSON number in the
    same field. So the publisher does not need a type registry -- the Python
    type of the value it was handed already says which dataref it is going to,
    and getting that wrong is a rejected write, not a silently wrong one.
    """
    if isinstance(value, str):
        return encode_field_b64(value, width)
    return value


class Publisher(Protocol):
    name: str

    def publish(self, values: Mapping[str, Value]) -> None:
        """Push ``{dataref name: label or number}``. Must never raise."""

    def close(self) -> None:
        ...


class ConsolePublisher:
    name = "console"

    def __init__(self, config: PublishConfig | None = None) -> None:
        self._last: dict[str, Value] = {}

    def publish(self, values: Mapping[str, Value]) -> None:
        changed = {k: v for k, v in values.items() if self._last.get(k) != v}
        if not changed:
            return
        self._last.update(values)
        for name in sorted(changed):
            LOG.info("%-24s = %r", name, changed[name])

    def close(self) -> None:
        return None


class WebApiPublisher:
    """X-Plane Web API client (REST, base64 for Data datarefs)."""

    name = "webapi"

    def __init__(
        self,
        config: PublishConfig,
        dataref_names: Sequence[str],
        session=None,
    ) -> None:
        if session is None:
            import requests  # imported here so tests can inject a stub

            session = requests.Session()
        self._session = session
        self.config = config
        self.names = list(dataref_names)
        self._ids: dict[str, int] = {}
        self._last: dict[str, Value] = {}
        self._next_resolve = 0.0
        self._warned = False
        self.resolve()

    # -- plumbing ---------------------------------------------------------
    @property
    def _root(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/api/{self.config.api_version}"

    def _log_offline(self, detail: str) -> None:
        if not self._warned:
            LOG.warning(
                "X-Plane Web API unreachable at %s (%s). Is X-Plane 12.1.1+ running with the "
                "web server enabled? Retrying every %.0fs.",
                self.config.base_url, detail, self.config.retry_interval,
            )
            self._warned = True

    # -- id resolution ----------------------------------------------------
    def resolve(self) -> bool:
        """Map dataref names to this session's numeric ids."""
        self._next_resolve = time.monotonic() + self.config.retry_interval
        try:
            response = self._session.get(f"{self._root}/datarefs", timeout=self.config.timeout)
        except Exception as exc:  # noqa: BLE001 - requests raises many types
            self._log_offline(str(exc))
            return False
        if getattr(response, "status_code", 0) != 200:
            self._log_offline(f"HTTP {getattr(response, 'status_code', '?')} listing datarefs")
            return False
        try:
            entries = response.json().get("data", [])
        except Exception as exc:  # noqa: BLE001
            self._log_offline(f"malformed dataref listing: {exc}")
            return False

        wanted = set(self.names)
        found = {
            entry["name"]: int(entry["id"])
            for entry in entries
            if entry.get("name") in wanted and entry.get("id") is not None
        }
        missing = wanted - set(found)
        if missing:
            LOG.warning(
                "%d of %d datarefs are not registered in X-Plane (e.g. %s). Is "
                "PI_G1000SoftkeyLabels.py installed in Resources/plugins/PythonPlugins/?",
                len(missing), len(wanted), sorted(missing)[0],
            )
        if found:
            self._warned = False
            LOG.info("resolved %d/%d dataref ids", len(found), len(wanted))
        self._ids = found
        self._last.clear()
        return bool(found)

    # -- writing ----------------------------------------------------------
    def publish(self, values: Mapping[str, Value]) -> None:
        if not self._ids and time.monotonic() >= self._next_resolve:
            self.resolve()
        stale = False
        for name, value in values.items():
            if self._last.get(name) == value:
                continue
            dataref_id = self._ids.get(name)
            if dataref_id is None:
                continue
            ok, retry = self._write(dataref_id, name, value)
            if ok:
                self._last[name] = value
            elif retry:
                stale = True
                break
        if stale:
            LOG.info("dataref ids look stale, re-resolving")
            self.resolve()

    def _write(self, dataref_id: int, name: str, value: Value) -> tuple[bool, bool]:
        """Returns (written, should_re_resolve)."""
        payload = {"data": encode_value(value, self.config.field_width)}
        try:
            response = self._session.patch(
                f"{self._root}/datarefs/{dataref_id}/value",
                json=payload,
                timeout=self.config.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            self._log_offline(str(exc))
            self._ids.clear()
            self._next_resolve = time.monotonic() + self.config.retry_interval
            return False, False
        status = getattr(response, "status_code", 0)
        if status in (200, 201, 204):
            return True, False
        if status in (400, 404, 409, 410):
            LOG.debug("write to %s (id=%d) failed with HTTP %s", name, dataref_id, status)
            return False, True
        LOG.warning("unexpected HTTP %s writing %s", status, name)
        return False, False

    def close(self) -> None:
        try:
            self._session.close()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("ignoring session close error: %s", exc)


class WebSocketPublisher(WebApiPublisher):
    """One WebSocket message per cycle instead of one HTTP round-trip per cell.

    The REST publisher issues a separate blocking PATCH for every changed
    dataref. A softkey press typically changes most of a 12-cell strip, so a
    single menu change costs a dozen sequential round-trips into X-Plane's
    embedded web server -- the dominant source of the lag, since OCR of the
    same strip is only tens of milliseconds.

    ``dataref_set_values`` accepts many datarefs in one message and needs no
    prior subscription, so the whole strip goes out as a single frame and we do
    not block waiting for a reply.

    Name-to-id resolution still uses REST (inherited); only writes move.
    """

    name = "websocket"

    def __init__(self, config: PublishConfig, dataref_names: Sequence[str], session=None) -> None:
        self._ws = None
        self._req_id = 0
        self._ws_warned = False
        self._ws_version = None
        self._connect_failures = 0
        self._next_ws_attempt = 0.0
        self._fell_back = False
        self._host_rewritten = False
        super().__init__(config, dataref_names, session=session)

    def _negotiate_version(self) -> str:
        """Ask /api/capabilities which API versions this X-Plane speaks.

        The endpoint is unversioned and reports e.g. {"api": {"versions":
        ["v1","v2","v3"]}}. Picking the highest avoids guessing wrong about
        which version carries the WebSocket interface.
        """
        if self._ws_version:
            return self._ws_version
        self._ws_version = self.config.api_version
        base = self.config.base_url.rstrip("/")
        try:
            response = self._session.get(f"{base}/api/capabilities", timeout=self.config.timeout)
            versions = (response.json() or {}).get("api", {}).get("versions", [])
            numbered = sorted(
                (v for v in versions if isinstance(v, str) and v.startswith("v")),
                key=lambda v: int(v[1:]) if v[1:].isdigit() else -1,
            )
            if numbered:
                self._ws_version = numbered[-1]
                LOG.info("X-Plane advertises API versions %s; using %s for the websocket",
                         ", ".join(versions), self._ws_version)
        except Exception as exc:  # noqa: BLE001 - endpoint is optional
            LOG.debug("could not read /api/capabilities (%s); using %s", exc, self._ws_version)
        return self._ws_version

    @property
    def _ws_url(self) -> str:
        root = self.config.base_url.rstrip("/")
        root = root.replace("https://", "wss://").replace("http://", "ws://")
        # X-Plane's web server binds only to 127.0.0.1. On Windows "localhost"
        # usually resolves to ::1 first, where nothing is listening -- requests
        # walks every resolved address so REST still finds the IPv4 socket, but
        # a websocket connect can sit on ::1 until it times out. Use the literal.
        if "//localhost" in root:
            root = root.replace("//localhost", "//127.0.0.1")
            if not self._host_rewritten:
                self._host_rewritten = True
                LOG.info("using 127.0.0.1 for the websocket (X-Plane binds IPv4 loopback only)")
        return f"{root}/api/{self._negotiate_version()}"

    def _fall_back(self, reason: str) -> None:
        """Note, once, that this cycle's writes are going out over REST.

        Two properties matter more than using the websocket:

        * never publish *nothing*. REST still works, so a cycle that cannot get
          a socket writes over REST rather than dropping the labels.
        * never be slower than the REST publisher. A blocking connect attempt
          every cycle costs the whole socket timeout, so failures back off and
          reconnection is retried on the same interval as id resolution.
        """
        if self._fell_back:
            return
        self._fell_back = True
        LOG.warning(
            "websocket unavailable (%s); publishing over REST instead and retrying the "
            "socket every %.0fs. Labels still update, just with one HTTP write per "
            "changed cell. Set publish.target = 'webapi' to stop trying.",
            reason, self.config.retry_interval,
        )

    def _connect(self) -> bool:
        if self._ws is not None:
            return True
        # NB: _fell_back must not short-circuit here. It records that REST is
        # covering the writes, not that the websocket is abandoned -- the
        # backoff below is what limits retries, and recovery depends on this
        # path still running.
        now = time.monotonic()
        if now < self._next_ws_attempt:
            return False
        try:
            from websocket import create_connection
        except ImportError:
            # Nothing will change at runtime, so stop attempting entirely.
            self._next_ws_attempt = float("inf")
            self._fall_back("websocket-client is not installed; pip install websocket-client")
            return False
        # Cap the connect timeout: this runs on the capture loop, and the loop
        # period is the budget we actually care about.
        connect_timeout = min(self.config.timeout, 0.5)
        try:
            self._ws = create_connection(self._ws_url, timeout=connect_timeout)
            # Writes are fire-and-forget; never block the capture loop on a reply.
            self._ws.settimeout(0.0)
        except Exception as exc:  # noqa: BLE001 - many socket error types
            self._ws = None
            self._connect_failures += 1
            self._next_ws_attempt = now + self.config.retry_interval
            self._fall_back(str(exc))
            LOG.debug("websocket connect failed (%s), retry in %.0fs",
                      exc, self.config.retry_interval)
            return False
        if self._fell_back:
            LOG.info("websocket now available; resuming batched writes")
        else:
            LOG.info("websocket connected to %s", self._ws_url)
        self._fell_back = False
        self._connect_failures = 0
        self._ws_warned = False
        return True

    def _drop(self, detail: str) -> None:
        LOG.debug("websocket dropped (%s); will reconnect", detail)
        self._next_ws_attempt = 0.0  # an established socket may reconnect at once
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
        self._ws = None

    def publish(self, values: Mapping[str, Value]) -> None:
        if not self._ids and time.monotonic() >= self._next_resolve:
            self.resolve()
        changed = [
            (self._ids[name], value)
            for name, value in values.items()
            if self._last.get(name) != value and name in self._ids
        ]
        if not changed:
            return
        if not self._connect():
            # No socket this cycle: write over REST so the labels still land.
            super().publish(values)
            return
        self._req_id += 1
        message = {
            "req_id": self._req_id,
            "type": "dataref_set_values",
            "params": {
                "datarefs": [
                    {"id": ref_id, "value": encode_value(value, self.config.field_width)}
                    for ref_id, value in changed
                ]
            },
        }
        try:
            self._ws.send(json.dumps(message))
        except Exception as exc:  # noqa: BLE001
            self._drop(str(exc))
            super().publish(values)
            return
        for name, value in values.items():
            if name in self._ids:
                self._last[name] = value

    def close(self) -> None:
        self._drop("closing")
        super().close()


def create_publisher(config: PublishConfig, dataref_names: Sequence[str]) -> Publisher:
    if config.target == "console":
        return ConsolePublisher(config)
    if config.target == "webapi":
        return WebApiPublisher(config, dataref_names)
    if config.target == "websocket":
        return WebSocketPublisher(config, dataref_names)
    raise ValueError(
        f"unknown publish target {config.target!r} (websocket | webapi | console)"
    )
