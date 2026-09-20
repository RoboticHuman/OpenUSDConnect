"""Tests for ConnectionSession reconnect/teardown lifecycle."""

from types import SimpleNamespace

import pytest

from integrations.mcp import session as session_mod
from integrations.mcp.config import McpConfig
from integrations.mcp.errors import ToolError
from openusdconnect import usd_client
from openusdconnect.checkpoints import MirrorCheckpoint


class _FakeSender:
    def __init__(self, *args, **kwargs):
        self.options = kwargs
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
        connected = True
        layered_replay_active = True
        server_instance = "test-server"
        replay_epoch = 0

        def __init__(self, **kwargs):
            self.options = kwargs
            self.token = kwargs["token"]
            self.joined = False

        def start(self):
            started.append(self)

        def stop(self):
            stopped.append(self)

        def is_alive(self):
            return not self.joined

        def join(self, timeout=None):
            assert self in stopped
            self.joined = True

    monkeypatch.setattr(session_mod, "EventSender", _FakeSender)
    monkeypatch.setattr(usd_client, "ReceiverThread", _FakeReceiver)
    monkeypatch.setattr(session_mod.token_client, "load_token", lambda host, port: None)


def test_reconnect_stops_previous_receiver(monkeypatch):
    """A dropped-then-reconnected session must stop the old receiver, not leak it."""
    started, stopped = [], []
    _patch_net(monkeypatch, started, stopped)
    session = session_mod.ConnectionSession(McpConfig())

    session.connect()
    first = session.receiver
    assert first.receiver in started
    assert first.receiver not in stopped

    # A dropped sender leaves the mirror alive until reconnect tears it down.
    session.sender.is_connected = False
    assert not session.connected

    session.connect()
    assert first.receiver in stopped
    assert first.receiver.joined
    assert session.receiver is not first  # replaced by a fresh one
    assert session.receiver.receiver in started
    session.disconnect()


def test_connect_is_noop_while_connected(monkeypatch):
    """Calling connect() when already connected must not build a second receiver."""
    started, stopped = [], []
    _patch_net(monkeypatch, started, stopped)
    session = session_mod.ConnectionSession(McpConfig())

    session.connect()
    session.connect()
    assert len(started) == 1
    assert stopped == []
    session.disconnect()


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
    session.disconnect()


def test_disconnect_stops_receiver(monkeypatch):
    started, stopped = [], []
    _patch_net(monkeypatch, started, stopped)
    session = session_mod.ConnectionSession(McpConfig())

    session.connect()
    receiver = session.receiver
    session.disconnect()
    assert receiver.receiver in stopped
    assert receiver.receiver.joined
    assert session.receiver is None
    assert session.sender is None
    assert session.mirror_stage is None
    assert session._dirty == {}
    assert session._playback_state is None
    session.disconnect()
    assert stopped == [receiver.receiver]


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
        session.receiver.dispatcher.last_seq += 1
        return 1

    monkeypatch.setattr(session.receiver, "update", apply_foreign_write)
    try:
        result = session.send([{"k": "ensure_prim", "prim": "/Own", "typeName": "Xform"}])
        assert session.mirror_stage.GetPrimAtPath("/Foreign")
        assert not session.mirror_stage.GetPrimAtPath("/Own")
        assert result["mirror_synced"] is False
    finally:
        session.disconnect()


@pytest.mark.parametrize(
    "instance,epoch,head,ready,expected",
    [
        ("test-server", 0, 1, True, True),
        ("test-server", 0, 8, True, False),
        ("other-server", 0, 1, True, False),
        ("test-server", 1, 1, True, False),
        ("test-server", 0, 1, False, False),
    ],
)
def test_confirmation_requires_matching_applied_checkpoint(
    monkeypatch, instance, epoch, head, ready, expected
):
    _patch_net(monkeypatch, [], [])
    session = session_mod.ConnectionSession(McpConfig(read_after_write_timeout_s=0.02))
    session.connect()
    session.sender.acknowledged_checkpoint = MirrorCheckpoint(instance, epoch, head)
    monkeypatch.setattr(session.sender, "send_events", lambda events: True, raising=False)

    def apply():
        session.receiver.dispatcher.last_seq = 1
        session.receiver.receiver.synchronized = ready
        return 0

    monkeypatch.setattr(session.receiver, "update", apply)
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
    updates = []

    def flush(timeout):
        assert timeout == 0
        polls.append(timeout)
        if len(polls) >= 2:
            session.sender.acknowledged_checkpoint = MirrorCheckpoint("test-server", 0, 50)
            return True
        return False

    def apply():
        updates.append(True)
        session.receiver.dispatcher.last_seq = 50 if len(updates) >= 2 else 1
        return 1

    monkeypatch.setattr(session.sender, "flush", flush)
    monkeypatch.setattr(session.receiver, "update", apply)
    monkeypatch.setattr(session_mod, "time", SimpleNamespace(
        monotonic=lambda: 0.0,
        sleep=lambda seconds: pytest.fail("slept while mirror was making progress"),
    ))
    try:
        assert session.send([{}])["mirror_synced"]
        assert len(updates) == 2
    finally:
        session.disconnect()


@pytest.mark.parametrize("applied", [0, 1])
def test_ack_arriving_during_apply_is_confirmed_without_sleep(monkeypatch, applied):
    _patch_net(monkeypatch, [], [])
    session = session_mod.ConnectionSession(McpConfig())
    session.connect()
    acknowledged = False

    def flush(timeout):
        assert timeout == 0
        return acknowledged

    def apply():
        nonlocal acknowledged
        acknowledged = True
        session.sender.acknowledged_checkpoint = MirrorCheckpoint("test-server", 0, 1)
        session.receiver.dispatcher.last_seq = 1
        return applied

    monkeypatch.setattr(session.sender, "send_events", lambda events: True, raising=False)
    monkeypatch.setattr(session.sender, "flush", flush)
    monkeypatch.setattr(session.receiver, "update", apply)
    monkeypatch.setattr(session_mod, "time", SimpleNamespace(
        monotonic=lambda: 0.0,
        sleep=lambda seconds: pytest.fail("slept after acknowledgement and mirror were ready"),
    ))
    try:
        assert session.send([{}])["mirror_synced"]
    finally:
        session.disconnect()


@pytest.mark.parametrize("applied", [0, 1])
def test_confirmation_respects_budget_with_or_without_progress(monkeypatch, applied):
    _patch_net(monkeypatch, [], [])
    session = session_mod.ConnectionSession(McpConfig(read_after_write_timeout_s=0.01))
    session.connect()
    elapsed = 0.0
    sleeps = []

    def apply():
        nonlocal elapsed
        elapsed += 0.001
        session.receiver.dispatcher.last_seq += applied
        return applied

    def sleep(seconds):
        nonlocal elapsed
        assert 0 < seconds <= min(0.005, 0.01 - elapsed)
        sleeps.append(seconds)
        elapsed += seconds

    monkeypatch.setattr(session.sender, "send_events", lambda events: True, raising=False)
    monkeypatch.setattr(session.sender, "flush", lambda timeout: False)
    monkeypatch.setattr(session.receiver, "update", apply)
    monkeypatch.setattr(session_mod, "time", SimpleNamespace(
        monotonic=lambda: elapsed, sleep=sleep,
    ))
    try:
        assert not session.send([{}])["mirror_synced"]
        # One update can straddle the deadline; progress must not extend the loop.
        assert 0.01 <= elapsed <= 0.011 + 1e-9
        assert bool(sleeps) is (applied == 0)
    finally:
        session.disconnect()


def test_pending_ack_times_out_without_false_confirmation(monkeypatch):
    _patch_net(monkeypatch, [], [])
    session = session_mod.ConnectionSession(McpConfig(read_after_write_timeout_s=0.02))
    session.connect()
    monkeypatch.setattr(session.sender, "send_events", lambda events: True, raising=False)
    monkeypatch.setattr(session.sender, "flush", lambda timeout: False)
    session.sender.acknowledged_checkpoint = MirrorCheckpoint("test-server", 0, 1)
    monkeypatch.setattr(session.receiver, "update", lambda: 0)
    session.receiver.dispatcher.last_seq = 100
    try:
        assert not session.send([{}])["mirror_synced"]
    finally:
        session.disconnect()


@pytest.mark.parametrize("after_apply", [False, True])
def test_rejected_transaction_is_reported_as_tool_error(monkeypatch, after_apply):
    from openusdconnect.recovery import TransactionFailure
    from openusdconnect.sender import TransactionRejectedError

    _patch_net(monkeypatch, [], [])
    session = session_mod.ConnectionSession(McpConfig())
    session.connect()
    monkeypatch.setattr(session.sender, "send_events", lambda events: True, raising=False)
    applied = False

    def apply():
        nonlocal applied
        applied = True
        return 0

    def reject(timeout):
        if after_apply and not applied:
            return False
        raise TransactionRejectedError(TransactionFailure(txn_id=1, code=4, reason="invalid"))

    monkeypatch.setattr(session.sender, "flush", reject)
    monkeypatch.setattr(session.receiver, "update", apply)
    try:
        with pytest.raises(ToolError) as error:
            session.send([{}])
        assert error.value.code == "transaction_rejected"
    finally:
        session.disconnect()


@pytest.mark.parametrize("saved_token", [None, "saved-token"])
def test_mirror_preserves_identity_token_and_callbacks(monkeypatch, saved_token):
    started, stopped = [], []
    _patch_net(monkeypatch, started, stopped)
    monkeypatch.setattr(session_mod.token_client, "load_token", lambda *args: saved_token)
    saved = []
    monkeypatch.setattr(session_mod.token_client, "save_token", lambda *args: saved.append(args))

    def connect(sender):
        sender.token = "issued-token"
        sender.options["on_token_issued"](sender.token)
        return True

    monkeypatch.setattr(_FakeSender, "connect", connect)
    session = session_mod.ConnectionSession(McpConfig(client_id="mcp-identity"))
    session.connect()
    try:
        options = session.receiver.receiver.options
        assert options["token"] == (saved_token or "issued-token")
        assert saved == [(session.config.host, session.config.port, "issued-token")]
        assert session.sender.options["origin"] == f"{session._origin_base}-emit"
        assert session.sender.options["token"] == saved_token
        assert options["client_id"] == "mcp-identity"
        assert options["origin"] == f"{session._origin_base}-recv"
        assert options["layered_replay"] is True
        options["on_playback_state"]({"playing": True})
        assert session.playback_status()["playing"] is True
        dispatcher = session.receiver.dispatcher
        dispatcher.last_seq = 3
        dispatcher._applying_seq = 7
        dispatcher.on_applied(["/World"])
        assert session._dirty == {"/World": 7}
        assert session.receiver.last_seq == 3
    finally:
        session.disconnect()


@pytest.mark.parametrize("auth_rejected", [False, True])
def test_sender_rejection_preserves_error_code(monkeypatch, auth_rejected):
    started, stopped = [], []
    _patch_net(monkeypatch, started, stopped)

    def reject(sender):
        sender.auth_rejected = auth_rejected
        return False

    monkeypatch.setattr(_FakeSender, "connect", reject)
    session = session_mod.ConnectionSession(McpConfig())
    with pytest.raises(ToolError) as error:
        session.connect()
    assert error.value.code == ("auth_rejected" if auth_rejected else "connect_failed")
    assert session.status()["auth_rejected"] is auth_rejected
    assert session.sender is None
    assert session.receiver is None
    assert started == []


def test_failed_mirror_start_closes_partial_connection(monkeypatch):
    started, stopped = [], []
    _patch_net(monkeypatch, started, stopped)

    def fail_start(self):
        raise RuntimeError("start failed")

    monkeypatch.setattr(usd_client.UsdReceiver, "start", fail_start)
    session = session_mod.ConnectionSession(McpConfig())
    with pytest.raises(RuntimeError, match="start failed"):
        session.connect()
    assert len(stopped) == 1
    assert stopped[0].joined
    assert session.sender is None
    assert session.receiver is None
    assert session.mirror_stage is None


def test_failed_send_closes_and_joins_mirror(monkeypatch):
    started, stopped = [], []
    _patch_net(monkeypatch, started, stopped)
    session = session_mod.ConnectionSession(McpConfig())
    session.connect()
    sender = session.sender
    monkeypatch.setattr(sender, "send_events", lambda events: False, raising=False)
    with pytest.raises(ToolError) as error:
        session.send([{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
    assert error.value.code == "disconnected"
    assert not sender.is_connected
    assert stopped == started
    assert stopped[0].joined


def test_no_mirror_send_result_is_unchanged(monkeypatch):
    _patch_net(monkeypatch, [], [])
    session = session_mod.ConnectionSession(McpConfig(mirror_enabled=False))
    session.connect()
    monkeypatch.setattr(session.sender, "send_events", lambda events: True, raising=False)
    monkeypatch.setattr(session.sender, "flush", lambda **kwargs: pytest.fail("unexpected wait"))
    try:
        assert session.send([{}]) == {
            "sent": True,
            "event_count": 1,
            "last_seq": None,
            "mirror_synced": False,
        }
    finally:
        session.disconnect()
