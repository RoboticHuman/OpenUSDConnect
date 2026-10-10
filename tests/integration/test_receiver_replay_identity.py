"""Reconnect must not reuse a cursor from another server or sequence epoch."""

import socket
from contextlib import ExitStack

import pytest
from pxr import Usd

from integrations.mcp.config import McpConfig
from integrations.mcp.session import ConnectionSession
from openusdconnect.codec import encode_message, message_to_dict
from openusdconnect.framing import recv_framed, send_framed
from openusdconnect.protocol import make_hello
from openusdconnect.receiver import EventReceiver
from openusdconnect.sender import EventSender
from openusdconnect.usd_client import UsdReceiver
from tests.helpers import (
    ensure_prim_event,
    in_process_server,
    recorded_hellos,
    server_state,
    serving,
    wait_until,
)

# A receiver retries a lost connection after its reconnect delay, and a
# refused attempt takes seconds on some platforms.
RECONNECT_TIMEOUT = 15


def _mcp_session(port):
    """An MCP mirror session whose receiver is connecting to *port*."""
    session = ConnectionSession(McpConfig(read_after_write_timeout_s=1))
    session.mirror_stage = Usd.Stage.CreateInMemory()
    session.receiver = UsdReceiver(
        session.mirror_stage,
        app_name="replay-identity-test",
        host="127.0.0.1",
        port=port,
        persist_token=False,
    )
    session.receiver.start()
    return session


def _drain_ready(session):
    def ready():
        session.receiver.update()
        return session.receiver.status.synchronized

    wait_until(ready, timeout=RECONNECT_TIMEOUT)


@pytest.mark.parametrize("reset", ["compact", "purge", "restart"])
def test_colliding_reconnect_cannot_confirm_missing_own_write(reset):
    with server_state() as state, server_state() as replacement, ExitStack() as cleanup:
        state._commit_events([ensure_prim_event("/Before")] * 3)
        with serving(state) as port:
            session = _mcp_session(port)
            cleanup.callback(session.disconnect)
            _drain_ready(session)
            assert session.receiver.last_seq == 3
            assert session.mirror_stage.GetPrimAtPath("/Before")
        # Closing the listener dropped the receiver; it reconnects once the port serves again.
        assert not state.receivers

        if reset == "compact":
            state.compact_log()
            assert state.store.get_max_seq() == 1
        elif reset == "purge":
            state.purge()
        else:
            state = replacement
        with serving(state) as producer_port:
            session.sender = EventSender("127.0.0.1", producer_port, client_id="own")
            assert session.sender.connect()
            assert session.sender.send_events([ensure_prim_event("/Own")])
            assert session.sender.flush(5)
            while state.store.get_max_seq() < 3:
                state._commit_events([ensure_prim_event("/Foreign")])
            assert not session.mirror_stage.GetPrimAtPath("/Own")

            with serving(state, port):
                wait_until(lambda: session.receiver.receiver.connected, timeout=RECONNECT_TIMEOUT)
                assert session._drain_after_write()
                assert session.mirror_stage.GetPrimAtPath("/Own")
                assert session.receiver.last_seq == 3
                assert session.receiver.server_instance == state.server_instance
                if reset != "compact":
                    assert not session.mirror_stage.GetPrimAtPath("/Before")


@pytest.mark.parametrize("prefix", ["matching", "epoch", "instance", "unknown", "legacy", "fresh"])
def test_server_validates_prefix_inside_replay_window(prefix):
    with in_process_server() as (state, port):
        state._commit_events([ensure_prim_event("/Own"), ensure_prim_event("/Foreign")])
        epoch, head = state.get_replay_token()
        hello = make_hello("receiver", sync_from=head + 1, layered_replay=True)
        if prefix == "fresh":
            hello["sync_from"] = 1
        if prefix != "legacy":
            hello["replay_server_instance"] = (
                ""
                if prefix in ("unknown", "fresh")
                else "another-server"
                if prefix == "instance"
                else state.server_instance
            )
            if prefix not in ("unknown", "fresh"):
                hello["replay_epoch"] = epoch + (prefix == "epoch")
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            send_framed(sock, encode_message(hello))
            messages = []
            while not messages or messages[-1]["type"] != "replay_complete":
                messages.append(message_to_dict(recv_framed(sock)))
        assert messages[0]["replay_identity"] is True
        reset = prefix not in ("matching", "legacy", "fresh")
        assert any(msg["type"] == "resync" for msg in messages) is reset
        assert [msg["seq"] for msg in messages if msg["type"] == "event"] == (
            [1, 2] if reset or prefix == "fresh" else []
        )


def test_initial_snapshot_cursor_is_preserved_without_claiming_prefix_proof(monkeypatch):
    hellos = recorded_hellos(monkeypatch)
    with server_state() as state, ExitStack() as cleanup:
        state._commit_events([ensure_prim_event("/Snapshot"), ensure_prim_event("/PostSnapshot")])
        received = []

        def drain_ready():
            received.extend(message_to_dict(raw) for raw in receiver.drain_queue())
            return receiver.mark_replay_applied()

        with serving(state) as port:
            receiver = EventReceiver(host="127.0.0.1", port=port, sync_from=2)
            cleanup.callback(receiver.close, 5)
            receiver.start()
            wait_until(drain_ready)
            assert not any(msg["type"] == "resync" for msg in received)
            assert [msg["seq"] for msg in received if msg["type"] == "event"] == [2]
            assert receiver.synchronized
            assert receiver.server_instance == ""

        # The next connection must validate that externally supplied prefix.
        # With no identity for it, a complete reset/replay establishes proof.
        received.clear()
        with serving(state, port):
            wait_until(drain_ready, timeout=RECONNECT_TIMEOUT)
            assert len(hellos) == 2
            assert hellos[1]["replay_server_instance"] == ""
            assert "replay_epoch" not in hellos[1]
            assert any(msg["type"] == "resync" for msg in received)
            assert [msg["seq"] for msg in received if msg["type"] == "event"] == [1, 2]
            assert receiver.server_instance == state.server_instance


def test_apply_failure_discards_unapplied_replay_identity(monkeypatch):
    with in_process_server() as (state, port):
        state._commit_events([ensure_prim_event("/Before")] * 3)
        session = _mcp_session(port)
        try:
            _drain_ready(session)
            session.sender = EventSender("127.0.0.1", port, client_id="own")
            assert session.sender.connect()
            state.process_idempotent_txn(
                [ensure_prim_event("/ApplyFailure")],
                session_id="foreign",
                txn_id=1,
                client_id="foreign",
            )
            wait_until(lambda: session.receiver.receiver.last_seq == 4)

            def fail_while_reset_arrives(*args, **kwargs):
                # The consumer owns an old-epoch batch while the receiver
                # queues a reset and new-epoch records with colliding IDs.
                state.purge()
                assert session.sender.send_events([ensure_prim_event("/Own")])
                assert session.sender.flush(5)
                state.process_idempotent_txn(
                    [ensure_prim_event("/Foreign")] * 2,
                    session_id="foreign",
                    txn_id=2,
                    client_id="foreign",
                )
                # The purge's reset and new-epoch marker precede these records.
                wait_until(lambda: session.receiver.receiver.last_seq == 3)
                assert session.receiver.receiver.replay_epoch == 0
                raise RuntimeError("injected apply failure")

            with monkeypatch.context() as patch:
                patch.setattr(
                    session.receiver.dispatcher, "_apply_layered", fail_while_reset_arrives
                )
                with pytest.raises(RuntimeError, match="injected apply failure"):
                    session.receiver.update()
            assert session.receiver.last_seq == 3
            assert session.mirror_stage.GetPrimAtPath("/Before")
            assert not session.mirror_stage.GetPrimAtPath("/Own")

            # The failed batch requested a replay, which reconnects.
            wait_until(lambda: session.receiver.receiver.connected, timeout=RECONNECT_TIMEOUT)
            assert session._drain_after_write()
            assert session.mirror_stage.GetPrimAtPath("/Own")
            assert not session.mirror_stage.GetPrimAtPath("/Before")
            assert session.receiver.last_seq == 3
            assert session.receiver.replay_epoch == 1
            assert session.receiver.server_instance == state.server_instance
        finally:
            session.disconnect()
