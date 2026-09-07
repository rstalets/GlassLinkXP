"""Firing X-Plane commands over the Web API.

``publish.py`` writes datarefs; this presses buttons. They are different
enough to keep apart -- a dataref write is a value the daemon owns and rewrites
every time it changes, while a command is a one-shot side effect on the sim
that cannot be taken back -- but they talk to the same web server, so the API
version is negotiated by the same function rather than a second one that could
settle on a different answer.

Only one thing uses this today: opening the G1000 pop-outs (see ``windowmgr``).

The endpoints, as documented for X-Plane 12.1.1 and up:

    GET  /api/{v}/commands?filter[name]=<name>   -> {"data": [{"id": .., "name": ..}]}
    POST /api/{v}/command/{id}/activate          <- {"duration": 0}

A duration of 0 presses and releases the command immediately, which is what a
momentary command like a pop-out wants; holding it down is for axes and trims.
"""

from __future__ import annotations

import logging

from .config import PublishConfig
from .publish import negotiate_api_version

LOG = logging.getLogger(__name__)


class CommandError(Exception):
    """A command could not be fired, with the reason a user can act on."""


class CommandClient:
    """Resolve X-Plane command names to ids and activate them."""

    def __init__(self, config: PublishConfig, session=None) -> None:
        if session is None:
            import requests  # imported here so tests can inject a stub

            session = requests.Session()
        self._session = session
        self.config = config
        self._ids: dict[str, int] = {}
        self._api_version: str | None = None

    # -- plumbing ---------------------------------------------------------
    @property
    def _version(self) -> str:
        return self._api_version or self.config.api_version

    @property
    def _root(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/api/{self._version}"

    def _negotiate(self) -> None:
        if self._api_version:
            return
        found = negotiate_api_version(
            self._session, self.config.base_url, self.config.timeout, self._version
        )
        if found:
            self._api_version = found

    # -- name -> id -------------------------------------------------------
    def _resolve(self, name: str) -> int:
        """This session's numeric id for ``name``.

        The listing is filtered server-side, but the answer is *checked* rather
        than indexed: an X-Plane that does not understand ``filter[name]``
        replies with the whole command list, and taking ``data[0]`` from that
        would activate whichever command happens to sort first. Firing an
        arbitrary command in somebody's cockpit is a far worse failure than
        saying the command could not be found, so an exact name match is
        required either way.
        """
        if name in self._ids:
            return self._ids[name]
        self._negotiate()
        entries = self._list(f"{self._root}/commands", params={"filter[name]": name})
        for entry in entries:
            if entry.get("name") == name and entry.get("id") is not None:
                self._ids[name] = int(entry["id"])
                return self._ids[name]
        raise CommandError(self._not_found(name))

    def _list(self, url: str, params: dict | None = None) -> list[dict]:
        try:
            response = self._session.get(url, params=params, timeout=self.config.timeout)
        except Exception as exc:  # noqa: BLE001 - requests raises many types
            raise CommandError(
                f"X-Plane's web API is not answering at {self.config.base_url} ({exc}). "
                "It is X-Plane that has to be running with its web server enabled -- "
                "Settings -> Network -> Web API."
            ) from exc
        if getattr(response, "status_code", 0) != 200:
            raise CommandError(
                f"HTTP {getattr(response, 'status_code', '?')} listing commands from {url}"
            )
        try:
            return list(response.json().get("data", []))
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"malformed command listing from {url}: {exc}") from exc

    def _not_found(self, name: str) -> str:
        """Say what the sim *does* offer, rather than only what it does not.

        The command names this daemon fires were not read out of X-Plane by
        anyone here, and a neighbouring command with a plausible name exists:
        ``..._popup`` opens the panel *inside* the sim window, where it is
        neither a top-level window nor capturable. So a name that does not
        resolve is exactly the case where the sim's own list is worth printing
        -- guessing at a second name would be the same mistake again.
        """
        stem = name.rsplit("/", 1)[-1].split("_", 1)[0]
        try:
            near = sorted(
                str(entry.get("name"))
                for entry in self._list(f"{self._root}/commands")
                if stem and stem in str(entry.get("name", ""))
            )
        except CommandError:
            near = []
        detail = f" X-Plane does list: {', '.join(near[:8])}." if near else ""
        return f"X-Plane has no command called {name!r}.{detail}"

    # -- firing -----------------------------------------------------------
    def trigger(self, name: str, duration: float = 0.0) -> None:
        """Press and release ``name``. Raises :class:`CommandError` if it did not."""
        command_id = self._resolve(name)
        url = f"{self._root}/command/{command_id}/activate"
        try:
            response = self._session.post(
                url, json={"duration": duration}, timeout=self.config.timeout
            )
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"could not send {name}: {exc}") from exc
        status = getattr(response, "status_code", 0)
        if status not in (200, 201, 204):
            # An id from a previous session of the sim is the likely cause, and
            # it will not become valid again; drop it so a retry re-resolves.
            self._ids.pop(name, None)
            raise CommandError(f"HTTP {status} activating {name}")
        LOG.debug("fired %s (id=%d)", name, command_id)

    def close(self) -> None:
        try:
            self._session.close()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            LOG.debug("ignoring session close error: %s", exc)
