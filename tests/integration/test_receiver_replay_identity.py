"""Reconnect must not reuse a cursor from another server or sequence epoch."""

import socket
import threading
import time
from contextlib import contextmanager

import pytest
from pxr import Usd

from integrations.mcp.config import McpConfig
from integrations.mcp.session import ConnectionSession
from openusdconnect.codec import encode_message, message_to_dict
from openusdconnect.framing import recv_framed, send_framed
from openusdconnect.protocol import make_hello
from openusdconnect.receiver import ReceiverThread
from openusdconnect.sender import EventSender
from openusdconnect.server.connection import ConnectionHandler, ThreadedTCPServer
from openusdconnect.server.state import UsdSyncServer
from openusdconnect.usd_client import UsdReceiver


def _event(path):
    return {"k": "ensure_prim", "prim": path, "typeName": "Xform"}


@contextmanager
def _server():
    state = UsdSyncServer(log_path=":memory:", txn_batch_size=1)
    tcp = ThreadedTCPServer(("127.0.0.1", 0), ConnectionHandler, state, max_workers=8)
    thread = threading.Thread(target=tcp.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, tcp.server_address[1]
    finally:
        tcp.shutdown()
        tcp.server_close()
        thread.join(5)
        state.shutdown()
        state.store.close()


def _wait(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


@contextmanager
def _connection(receiver):
    thread = threading.Thread(target=receiver._connect_and_recv, daemon=True)
    thread.start()
    try:
        _wait(lambda: receiver.connected)
        yield
    finally:
        receiver._close_socket()
        thread.join(5)
        assert not thread.is_alive()
        receiver.connected = False


def _drain_ready(session):
    def ready():
        session.receiver.update()
        return session.receiver.synchronized
    _wait(ready)


def _session_with_receiver(port):
    session = ConnectionSession(McpConfig(read_after_write_timeout_s=1))
    session.mirror_stage = Usd.Stage.CreateInMemory()
    session.receiver = UsdReceiver(
        session.mirror_stage,
        app_name="replay-identity-test",
        host="127.0.0.1",
        port=port,
        persist_token=False,
    )
    # These tests drive one connection attempt directly to control reconnect timing.
    session.receiver._started = True
    return session


@pytest.mark.parametrize("reset", ["compact", "purge", "restart"])
def test_colliding_reconnect_cannot_confirm_missing_own_write(reset):
    with _server() as (state, port), _server() as (replacement, replacement_port):
        state._commit_events([_event("/Before")] * 3)
        session = _session_with_receiver(port)
        try:
            with _connection(session.receiver.receiver):
                _drain_ready(session)
                assert session.receiver.last_seq == 3
                assert session.mirror_stage.GetPrimAtPath("/Before")
            _wait(lambda: not state.receivers)

            if reset == "compact":
                state.compact_log()
                assert state.store.get_max_seq() == 1
            elif reset == "purge":
                state.purge()
            else:
                state, port = replacement, replacement_port
                session.receiver.receiver.port = port
            session.sender = EventSender("127.0.0.1", port, client_id="own")
            assert session.sender.connect()
            assert session.sender.send_events([_event("/Own")])
            assert session.sender.flush(5)
            while state.store.get_max_seq() < 3:
                state._commit_events([_event("/Foreign")])
            assert not session.mirror_stage.GetPrimAtPath("/Own")

            with _connection(session.receiver.receiver):
                assert session._drain_after_write()
                assert session.mirror_stage.GetPrimAtPath("/Own")
                assert session.receiver.last_seq == 3
                assert session.receiver.server_instance == state.server_instance
                if reset != "compact":
                    assert not session.mirror_stage.GetPrimAtPath("/Before")
        finally:
            session.disconnect()


@pytest.mark.parametrize("prefix", ["matching", "epoch", "instance", "unknown", "legacy", "fresh"])
def test_server_validates_prefix_inside_replay_window(prefix):
    with _server() as (state, port):
        state._commit_events([_event("/Own"), _event("/Foreign")])
        epoch, head = state.get_replay_token()
        hello = make_hello("receiver", sync_from=head + 1, layered_replay=True)
        if prefix == "fresh":
            hello["sync_from"] = 1
        if prefix != "legacy":
            hello["replay_server_instance"] = (
                "" if prefix in ("unknown", "fresh") else
                "another-server" if prefix == "instance" else state.server_instance
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


@pytest.mark.parametrize("instance", ["", "older-checkpoint-server"])
def test_old_server_retains_cursor_without_publishing_confirmation_identity(instance):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        listener.settimeout(5)
        receiver = ReceiverThread(
            host="127.0.0.1", port=listener.getsockname()[1],
            sync_from=4, reconnect=False, layered_replay=False,
        )
        receiver.start()
        try:
            with listener.accept()[0] as second:
                second.settimeout(5)
                hello = message_to_dict(recv_framed(second))
                assert hello["sync_from"] == 4
                send_framed(second, encode_message({
                    "type": "hello_ok", "server_instance": instance,
                }))
                assert receiver.wait_connected(5)
                send_framed(second, encode_message({
                    "type": "event", "seq": 4, "event": _event("/Own"),
                }))
                send_framed(second, encode_message({
                    "type": "replay_complete", "head_seq": 4, "epoch": 0,
                }))
                _wait(lambda: receiver.last_seq == 4)
                assert [message_to_dict(raw)["type"] for raw in receiver.drain_queue()] == [
                    "event",
                ]
                _wait(receiver.mark_replay_applied)
                assert receiver.synchronized
                assert receiver.server_instance == ""
                assert receiver._received_replay_identity is None
            receiver.join(5)
            assert not receiver.is_alive()
        finally:
            receiver.stop()
            receiver.join(5)


def test_initial_snapshot_cursor_is_preserved_without_claiming_prefix_proof():
    with _server() as (state, port):
        state._commit_events([_event("/Snapshot"), _event("/PostSnapshot")])
        receiver = ReceiverThread(host="127.0.0.1", port=port, sync_from=2)
        received = []

        def drain_ready():
            received.extend(message_to_dict(raw) for raw in receiver.drain_queue())
            return receiver.mark_replay_applied()

        with _connection(receiver):
            _wait(drain_ready)
            assert not any(msg["type"] == "resync" for msg in received)
            assert [msg["seq"] for msg in received if msg["type"] == "event"] == [2]
            assert receiver.synchronized
            assert receiver.server_instance == ""
            assert receiver._received_replay_identity is None

        # The next connection must validate that externally supplied prefix.
        # With no identity for it, a complete reset/replay establishes proof.
        received.clear()
        with _connection(receiver):
            _wait(drain_ready)
            assert any(msg["type"] == "resync" for msg in received)
            assert [msg["seq"] for msg in received if msg["type"] == "event"] == [1, 2]
            assert receiver.server_instance == state.server_instance


def test_apply_failure_discards_unapplied_replay_identity(monkeypatch):
    with _server() as (state, port):
        state._commit_events([_event("/Before")] * 3)
        session = _session_with_receiver(port)
        try:
            with _connection(session.receiver.receiver):
                _drain_ready(session)
                session.sender = EventSender("127.0.0.1", port, client_id="own")
                assert session.sender.connect()
                state.process_idempotent_txn(
                    [_event("/ApplyFailure")], session_id="foreign", txn_id=1,
                    client_id="foreign",
                )
                _wait(lambda: session.receiver.receiver.last_seq == 4)

                def fail_while_reset_arrives(*args, **kwargs):
                    # The consumer owns an old-epoch batch while the receiver
                    # queues a reset and new-epoch records with colliding IDs.
                    state.purge()
                    assert session.sender.send_events([_event("/Own")])
                    assert session.sender.flush(5)
                    state.process_idempotent_txn(
                        [_event("/Foreign")] * 2, session_id="foreign", txn_id=2,
                        client_id="foreign",
                    )
                    _wait(lambda: (
                        session.receiver.receiver._received_replay_identity
                        == (state.server_instance, 1)
                        and session.receiver.receiver.last_seq == 3
                    ))
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

            with _connection(session.receiver.receiver):
                assert session._drain_after_write()
                assert session.mirror_stage.GetPrimAtPath("/Own")
                assert not session.mirror_stage.GetPrimAtPath("/Before")
                assert session.receiver.last_seq == 3
                assert session.receiver.replay_epoch == 1
                assert session.receiver.server_instance == state.server_instance
        finally:
            session.disconnect()
