"""Publishing labels to X-Plane.

Three targets:

``webapi``  PATCH the plugin-created byte-array datarefs through X-Plane's
            built-in REST API (12.1.1+). Data datarefs are base64 in both
            directions. Dataref ids are session-scoped, so names are resolved
            to ids at startup and re-resolved whenever a write 404s.
``file``    Atomically write a small JSON file that the XPPython3 plugin
            polls at 5 Hz. Fallback for the case where the Web API refuses to
            write a plugin-created dataref.
``console`` Log the labels; for development only.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from .config import PublishConfig

LOG = logging.getLogger(__name__)

JSON_ENV_VAR = "G1000_SOFTKEY_JSON"
JSON_BASENAME = "g1000_softkey_labels.json"


def default_json_path() -> Path:
    """Shared default location of the JSON fallback file.

    The XPPython3 plugin resolves it exactly the same way, so the two sides
    agree without any configuration.
    """
    override = os.environ.get(JSON_ENV_VAR)
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / JSON_BASENAME


def encode_field(text: str, width: int = 16) -> bytes:
    """UTF-8, truncated to ``width - 1`` bytes, NUL padded to ``width``."""
    raw = text.encode("utf-8", errors="ignore")[: max(0, width - 1)]
    return raw + b"\x00" * (width - len(raw))


def encode_field_b64(text: str, width: int = 16) -> str:
    return base64.b64encode(encode_field(text, width)).decode("ascii")


class Publisher(Protocol):
    name: str

    def publish(self, values: Mapping[str, str]) -> None:
        """Push ``{dataref name: label}``. Must never raise."""

    def close(self) -> None:
        ...


class ConsolePublisher:
    name = "console"

    def __init__(self, config: PublishConfig | None = None) -> None:
        self._last: dict[str, str] = {}

    def publish(self, values: Mapping[str, str]) -> None:
        changed = {k: v for k, v in values.items() if self._last.get(k) != v}
        if not changed:
            return
        self._last.update(values)
        for name in sorted(changed):
            LOG.info("%-24s = %r", name, changed[name])

    def close(self) -> None:
        return None


class FilePublisher:
    """Atomic JSON writer polled by the plugin."""

    name = "file"

    def __init__(self, config: PublishConfig) -> None:
        self.path = Path(config.json_path) if config.json_path else default_json_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last: dict[str, str] = {}
        LOG.info("publishing labels to %s", self.path)

    def publish(self, values: Mapping[str, str]) -> None:
        if dict(values) == self._last:
            return
        payload = {
            "version": 1,
            "updated": time.time(),
            "labels": dict(values),
        }
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)  # atomic within a filesystem
        except OSError as exc:
            LOG.warning("could not write %s: %s", self.path, exc)
            tmp.unlink(missing_ok=True)
            return
        self._last = dict(values)

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
        self._last: dict[str, str] = {}
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
    def publish(self, values: Mapping[str, str]) -> None:
        if not self._ids and time.monotonic() >= self._next_resolve:
            self.resolve()
        stale = False
        for name, text in values.items():
            if self._last.get(name) == text:
                continue
            dataref_id = self._ids.get(name)
            if dataref_id is None:
                continue
            ok, retry = self._write(dataref_id, name, text)
            if ok:
                self._last[name] = text
            elif retry:
                stale = True
                break
        if stale:
            LOG.info("dataref ids look stale, re-resolving")
            self.resolve()

    def _write(self, dataref_id: int, name: str, text: str) -> tuple[bool, bool]:
        """Returns (written, should_re_resolve)."""
        payload = {"data": encode_field_b64(text, self.config.field_width)}
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
        super().__init__(config, dataref_names, session=session)

    @property
    def _ws_url(self) -> str:
        root = self.config.base_url.rstrip("/")
        root = root.replace("https://", "wss://").replace("http://", "ws://")
        return f"{root}/api/{self.config.api_version}"

    def _connect(self) -> bool:
        if self._ws is not None:
            return True
        try:
            from websocket import create_connection
        except ImportError:
            if not self._ws_warned:
                LOG.error(
                    "publish.target = 'websocket' needs the websocket-client package "
                    "(pip install websocket-client). Falling back is not automatic; "
                    "set publish.target = 'webapi' to use REST instead."
                )
                self._ws_warned = True
            return False
        try:
            self._ws = create_connection(self._ws_url, timeout=self.config.timeout)
            # Writes are fire-and-forget; never block the capture loop on a reply.
            self._ws.settimeout(0.0)
        except Exception as exc:  # noqa: BLE001 - many socket error types
            self._log_offline(f"websocket connect failed: {exc}")
            self._ws = None
            return False
        LOG.info("websocket connected to %s", self._ws_url)
        self._ws_warned = False
        return True

    def _drop(self, detail: str) -> None:
        LOG.debug("websocket dropped (%s); will reconnect", detail)
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
        self._ws = None

    def publish(self, values: Mapping[str, str]) -> None:
        if not self._ids and time.monotonic() >= self._next_resolve:
            self.resolve()
        changed = [
            (self._ids[name], text)
            for name, text in values.items()
            if self._last.get(name) != text and name in self._ids
        ]
        if not changed:
            return
        if not self._connect():
            return
        self._req_id += 1
        message = {
            "req_id": self._req_id,
            "type": "dataref_set_values",
            "params": {
                "datarefs": [
                    {"id": ref_id, "value": encode_field_b64(text, self.config.field_width)}
                    for ref_id, text in changed
                ]
            },
        }
        try:
            self._ws.send(json.dumps(message))
        except Exception as exc:  # noqa: BLE001
            self._drop(str(exc))
            return
        for name, text in values.items():
            if name in self._ids:
                self._last[name] = text

    def close(self) -> None:
        self._drop("closing")
        super().close()


def create_publisher(config: PublishConfig, dataref_names: Sequence[str]) -> Publisher:
    if config.target == "console":
        return ConsolePublisher(config)
    if config.target == "file":
        return FilePublisher(config)
    if config.target == "webapi":
        return WebApiPublisher(config, dataref_names)
    if config.target == "websocket":
        return WebSocketPublisher(config, dataref_names)
    raise ValueError(
        f"unknown publish target {config.target!r} (websocket | webapi | file | console)"
    )
