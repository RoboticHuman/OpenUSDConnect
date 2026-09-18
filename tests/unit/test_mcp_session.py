"""Tests for ConnectionSession reconnect/teardown lifecycle."""

import pytest

from integrations.mcp import session as session_mod
from integrations.mcp.config import McpConfig


class _FakeSender:
    def __init__(self, *args, **kwargs):
        self.is_connected = True
        self.auth_rejected = False
        self.stage_metadata = {}
        self.token = None
        self.acknowledged_checkpoint = None

    def flush(self, timeout=None):
        return True

    def connect(self):
        return True

    def disconnect(self):
        self.is_connected = False


def _patch_net(monkeypatch, started, stopped):
    class _FakeReceiver:
        synchronized = True
        server_instance = "test-server"
        replay_epoch = 0

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


def test_status_reports_mirror_synchronization(monkeypatch):
    started, stopped = [], []
    _patch_net(monkeypatch, started, stopped)
    session = session_mod.ConnectionSession(McpConfig())

    status = session.connect()

    assert status["mirror_synchronized"] is True
    session.disconnect()


def test_concurrent_foreign_write_cannot_confirm_own_transaction(monkeypatch):
    _patch_net(monkeypatch, [], [])
    session = session_mod.ConnectionSession(McpConfig(read_after_write_timeout_s=0.02))
    session.connect()
    monkeypatch.setattr(session.sender, "send_events", lambda events: True, raising=False)

    def apply_foreign_write():
        session.mirror_stage.DefinePrim("/Foreign", "Xform")
        session.dispatcher.last_seq += 1
        return 1

    monkeypatch.setattr(session.dispatcher, "drain_and_apply", apply_foreign_write)
    try:
        result = session.send([{"k": "ensure_prim", "prim": "/Own", "typeName": "Xform"}])
        assert session.mirror_stage.GetPrimAtPath("/Foreign")
        assert not session.mirror_stage.GetPrimAtPath("/Own")
        assert result["mirror_synced"] is False
    finally:
        session.disconnect()


@pytest.mark.parametrize("instance,epoch,head,ready,expected", [
    ("test-server", 0, 1, True, True),
    ("test-server", 0, 8, True, False),
    ("other-server", 0, 1, True, False),
    ("test-server", 1, 1, True, False),
    ("test-server", 0, 1, False, False),
])
def test_confirmation_requires_matching_applied_checkpoint(
    monkeypatch, instance, epoch, head, ready, expected,
):
    _patch_net(monkeypatch, [], [])
    session = session_mod.ConnectionSession(McpConfig(read_after_write_timeout_s=0.02))
    session.connect()
    session.sender.acknowledged_checkpoint = (instance, epoch, head)
    monkeypatch.setattr(session.sender, "send_events", lambda events: True, raising=False)

    def apply():
        session.dispatcher.last_seq = 1
        session.receiver.synchronized = ready
        return 0

    monkeypatch.setattr(session.dispatcher, "drain_and_apply", apply)
    try:
        # Input count is deliberately unrelated to the server's committed head.
        assert session.send([{}] * 10)["mirror_synced"] is expected
    finally:
        session.disconnect()


def test_confirmation_waits_for_ack_and_mirror(monkeypatch):
    _patch_net(monkeypatch, [], [])
    session = session_mod.ConnectionSession(McpConfig(read_after_write_timeout_s=0.2))
    session.connect()
    monkeypatch.setattr(session.sender, "send_events", lambda events: True, raising=False)
    polls = []

    def flush(timeout):
        assert timeout == 0
        polls.append(timeout)
        if len(polls) >= 2:
            session.sender.acknowledged_checkpoint = ("test-server", 0, 50)
            return True
        return False

    def apply():
        session.dispatcher.last_seq = 50 if len(polls) >= 3 else 1
        return 1

    monkeypatch.setattr(session.sender, "flush", flush)
    monkeypatch.setattr(session.dispatcher, "drain_and_apply", apply)
    try:
        assert session.send([{}])["mirror_synced"]
        assert len(polls) == 3
    finally:
        session.disconnect()


def test_no_mirror_does_not_wait_for_confirmation(monkeypatch):
    _patch_net(monkeypatch, [], [])
    session = session_mod.ConnectionSession(McpConfig(mirror_enabled=False))
    session.connect()
    monkeypatch.setattr(session.sender, "send_events", lambda events: True, raising=False)
    monkeypatch.setattr(session.sender, "flush", lambda **kwargs: pytest.fail("unexpected wait"))
    try:
        assert not session.send([{}])["mirror_synced"]
    finally:
        session.disconnect()


def test_pending_ack_times_out_without_false_confirmation(monkeypatch):
    _patch_net(monkeypatch, [], [])
    session = session_mod.ConnectionSession(McpConfig(read_after_write_timeout_s=0.02))
    session.connect()
    monkeypatch.setattr(session.sender, "send_events", lambda events: True, raising=False)
    monkeypatch.setattr(session.sender, "flush", lambda timeout: False)
    session.sender.acknowledged_checkpoint = ("test-server", 0, 1)
    monkeypatch.setattr(session.dispatcher, "drain_and_apply", lambda: 0)
    session.dispatcher.last_seq = 100
    try:
        assert not session.send([{}])["mirror_synced"]
    finally:
        session.disconnect()


def test_rejected_transaction_is_reported_as_tool_error(monkeypatch):
    from integrations.mcp.errors import ToolError
    from openusdconnect.recovery import TransactionFailure
    from openusdconnect.sender import TransactionRejectedError

    _patch_net(monkeypatch, [], [])
    session = session_mod.ConnectionSession(McpConfig())
    session.connect()
    monkeypatch.setattr(session.sender, "send_events", lambda events: True, raising=False)

    def reject(timeout):
        raise TransactionRejectedError(TransactionFailure(txn_id=1, code=4, reason="invalid"))

    monkeypatch.setattr(session.sender, "flush", reject)
    try:
        with pytest.raises(ToolError) as error:
            session.send([{}])
        assert error.value.code == "transaction_rejected"
    finally:
        session.disconnect()
