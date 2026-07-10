"""Tests for ConnectionSession reconnect/teardown lifecycle."""

from integrations.mcp import session as session_mod
from integrations.mcp.config import McpConfig


class _FakeSender:
    def __init__(self, *args, **kwargs):
        self.is_connected = True
        self.auth_rejected = False
        self.stage_metadata = {}
        self.token = None

    def connect(self):
        return True

    def disconnect(self):
        self.is_connected = False


def _patch_net(monkeypatch, started, stopped):
    class _FakeReceiver:
        def start(self):
            started.append(self)

        def stop(self):
            stopped.append(self)

    monkeypatch.setattr(session_mod, "EventSender", _FakeSender)
    monkeypatch.setattr(session_mod, "ReceiverThread", lambda *a, **k: _FakeReceiver())
    monkeypatch.setattr(session_mod.token_client, "load_token", lambda host, port: None)


def test_reconnect_stops_previous_receiver(monkeypatch):
    """A dropped-then-reconnected session must stop the old receiver, not leak it."""
    started, stopped = [], []
    _patch_net(monkeypatch, started, stopped)
    session = session_mod.ConnectionSession(McpConfig())

    session.connect()
    first = session.receiver
    assert first in started
    assert first not in stopped

    # Simulate the drop: send() nulls only self.sender; the receiver keeps running.
    session.sender = None
    assert not session.connected

    session.connect()
    assert first in stopped  # the fix: the previous receiver is torn down
    assert session.receiver is not first  # replaced by a fresh one
    assert session.receiver in started


def test_connect_is_noop_while_connected(monkeypatch):
    """Calling connect() when already connected must not build a second receiver."""
    started, stopped = [], []
    _patch_net(monkeypatch, started, stopped)
    session = session_mod.ConnectionSession(McpConfig())

    session.connect()
    session.connect()
    assert len(started) == 1
    assert stopped == []


def test_playback_status_reflects_broadcast(monkeypatch):
    """playback_status reports the latest PlaybackState and leadership."""
    started, stopped = [], []
    _patch_net(monkeypatch, started, stopped)
    session = session_mod.ConnectionSession(McpConfig(client_id="mcp-x"))
    session.connect()

    assert session.playback_status()["observed"] is False  # nothing broadcast yet

    session._on_playback_state(
        {"playing": True, "time": 12.0, "rate": 2.0, "leader_client_id": "mcp-x"}
    )
    st = session.playback_status()
    assert st["observed"] and st["playing"] is True
    assert st["time"] == 12.0 and st["rate"] == 2.0
    assert st["has_leader"] is True and st["is_leader"] is True

    session._on_playback_state(
        {"playing": False, "time": 0.0, "rate": 1.0, "leader_client_id": "someone-else"}
    )
    st2 = session.playback_status()
    assert st2["is_leader"] is False
    assert st2["leader_client_id"] == "someone-else"


def test_disconnect_stops_receiver(monkeypatch):
    started, stopped = [], []
    _patch_net(monkeypatch, started, stopped)
    session = session_mod.ConnectionSession(McpConfig())

    session.connect()
    receiver = session.receiver
    session.disconnect()
    assert receiver in stopped
    assert session.receiver is None
    assert session.sender is None
