"""The X-Plane command client.

Only one thing fires commands -- opening the G1000 pop-outs -- but a command is
a side effect on somebody's running sim that cannot be taken back, so the
interesting tests here are about not firing the *wrong* one.
"""

from __future__ import annotations

import pytest

from glasslinkxp.command import CommandClient, CommandError
from glasslinkxp.config import PublishConfig

PFD = "sim/GPS/g1000n1_popout"
MFD = "sim/GPS/g1000n3_popout"


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeSession:
    """A stand-in X-Plane web server.

    ``honour_filter`` is the interesting knob: an X-Plane that does not
    understand ``filter[name]`` answers with the entire command list instead of
    an empty one, and every command it has ever heard of is then a candidate.
    """

    def __init__(self, commands=None, honour_filter=True, activate_status=200, fail=False):
        self.commands = dict(commands or {PFD: 101, MFD: 103})
        self.honour_filter = honour_filter
        self.activate_status = activate_status
        self.fail = fail
        self.gets: list[tuple[str, dict | None]] = []
        self.posts: list[tuple[str, dict]] = []
        self.closed = False

    def get(self, url, params=None, timeout=None):
        if url.endswith("/api/capabilities"):
            return FakeResponse(200, {"api": {"versions": ["v1", "v2", "v3"]}})
        self.gets.append((url, params))
        if self.fail:
            raise ConnectionError("connection refused")
        wanted = (params or {}).get("filter[name]")
        items = self.commands.items()
        if wanted is not None and self.honour_filter:
            items = [(n, i) for n, i in items if n == wanted]
        return FakeResponse(200, {"data": [{"id": i, "name": n} for n, i in items]})

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        if self.fail:
            raise ConnectionError("connection refused")
        return FakeResponse(self.activate_status)

    def close(self):
        self.closed = True


def client(session, **kwargs):
    return CommandClient(PublishConfig(**kwargs), session=session)


def test_a_command_is_resolved_then_activated():
    session = FakeSession()
    client(session).trigger(PFD)

    url, body = session.posts[0]
    assert url == "http://localhost:8086/api/v3/command/101/activate"
    assert body == {"duration": 0.0}, "a momentary press, not a held one"


def test_the_version_x_plane_advertises_is_the_one_used():
    """Negotiated by the same function the publishers use, not a second one.

    A daemon writing datarefs over v3 while firing commands over v1 would be
    two clients of two APIs that only happen to share a port.
    """
    session = FakeSession()
    client(session).trigger(PFD)

    assert all("/api/v3/" in url for url, _ in session.gets)
    assert "/api/v3/" in session.posts[0][0]


def test_an_ignored_filter_does_not_activate_whatever_came_back_first():
    """The safety property this client exists to have.

    An X-Plane too old for ``filter[name]`` replies with its whole command
    list. Taking ``data[0]`` from that would activate an arbitrary command in
    somebody's cockpit -- a far worse outcome than saying the command could not
    be found -- so the name is matched rather than the position trusted.
    """
    session = FakeSession(
        commands={"sim/autopilot/servos_off": 1, "sim/engines/mixture_cut": 2},
        honour_filter=False,
    )
    with pytest.raises(CommandError):
        client(session).trigger(PFD)

    assert session.posts == [], "nothing may be activated when the name was not found"


def test_a_missing_command_says_what_the_sim_does_have():
    """Because the neighbouring name is a plausible mistake.

    ``_popup`` opens the panel inside the sim window rather than as a window of
    its own, so it is both easy to reach for and useless here. Printing what
    X-Plane actually lists beats a second guess at the name.
    """
    session = FakeSession(commands={"sim/GPS/g1000n1_popup": 55})
    with pytest.raises(CommandError) as raised:
        client(session).trigger(PFD)

    assert "sim/GPS/g1000n1_popup" in str(raised.value)


def test_an_id_is_resolved_once_and_reused():
    session = FakeSession()
    command = client(session)
    command.trigger(PFD)
    command.trigger(PFD)

    assert len(session.gets) == 1
    assert len(session.posts) == 2


def test_a_rejected_activation_forgets_the_id():
    """Ids are session-scoped, so the likely cause is a sim that restarted."""
    session = FakeSession(activate_status=404)
    command = client(session)
    with pytest.raises(CommandError):
        command.trigger(PFD)
    with pytest.raises(CommandError):
        command.trigger(PFD)

    assert len(session.gets) == 2, "the second attempt re-resolved rather than reusing 101"


def test_a_sim_that_is_not_answering_says_so_in_terms_of_x_plane():
    session = FakeSession(fail=True)
    with pytest.raises(CommandError) as raised:
        client(session).trigger(PFD)

    message = str(raised.value)
    assert "web server" in message, "the fix is in X-Plane, so the message has to point there"
    assert "http://localhost:8086" in message, "and say which address went unanswered"


def test_closing_closes_the_session():
    session = FakeSession()
    command = client(session)
    command.close()
    assert session.closed
