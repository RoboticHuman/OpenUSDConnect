"""Replay identity must describe the captured records and their layer routing."""

import socket
import threading

import pytest
from pxr import Usd

from openusdconnect.checkpoints import MirrorCheckpoint
from openusdconnect.codec import PayloadType, encode_message, message_to_dict
from openusdconnect.framing import recv_framed, send_framed
from openusdconnect.protocol import make_hello
from openusdconnect.sender import EventSender
from openusdconnect.server import connection as connection_mod
from tests.helpers import (
    ReceiverStub,
    ensure_prim_event,
    in_process_server,
    mcp_session_with_receiver,
    receiver_connection,
)


def test_snapshot_replacement_after_capture_cannot_confirm_unapplied_write(monkeypatch):
    with in_process_server() as (state, port):
        session = mcp_session_with_receiver(port)
        session.config.read_after_write_timeout_s = 0.1
        session.sender = EventSender("127.0.0.1", port, client_id="own")
        replay_complete = threading.Event()
        resume_receiver = threading.Event()
        replacement_done = threading.Event()
        replacement_errors = []
        replacement_worker = None
        try:
            assert session.sender.connect()
            assert session.sender.send_events([ensure_prim_event("/Own")])
            assert session.sender.flush(5)
            assert session.sender.acknowledged_checkpoint == MirrorCheckpoint(
                state.server_instance, 0, 1
            )
            state._broadcast_queue.join()
            replacement = Usd.Stage.CreateInMemory()
            replacement.DefinePrim("/Replacement", "Xform")
            epoch, head = state.get_snapshot_token()
            replacement.GetRootLayer().customLayerData = {
                "openusdconnect": {
                    "scene_id": state.scene_id, "epoch": epoch, "snapshot_seq": head,
                },
            }
            send = connection_mod.send_msg
            control = session.receiver.receiver._handle_control_message

            def replace_snapshot():
                try:
                    state.replace_from_stage_snapshot(replacement, reject_ambiguous=False)
                except Exception as exc:
                    replacement_errors.append(exc)
                finally:
                    replacement_done.set()

            def replace_before_hello(sock, message):
                nonlocal replacement_worker
                if message.get("replay_identity"):
                    # Sending has begun: maintenance must be free to replace the
                    # log, even though the receiver's send lock is still held.
                    replacement_worker = threading.Thread(target=replace_snapshot, daemon=True)
                    replacement_worker.start()
                    assert replacement_done.wait(5), "network delivery held the maintenance barrier"
                    assert not replacement_errors, replacement_errors
                    assert state.get_replay_token()[0] == 1
                send(sock, message)

            def pause_after_initial_complete(payload_type, buf, generation):
                result = control(payload_type, buf, generation)
                if payload_type == PayloadType.ReplayComplete and not replay_complete.is_set():
                    replay_complete.set()
                    assert resume_receiver.wait(5)
                return result

            monkeypatch.setattr(connection_mod, "send_msg", replace_before_hello)
            monkeypatch.setattr(
                session.receiver.receiver,
                "_handle_control_message",
                pause_after_initial_complete,
            )
            with receiver_connection(session.receiver.receiver):
                try:
                    assert replay_complete.wait(5)
                    assert session._drain_after_write()
                    assert session.mirror_stage.GetPrimAtPath("/Own")
                    assert not session.mirror_stage.GetPrimAtPath("/Replacement")
                    assert session.receiver.last_seq == 1
                    assert session.receiver.replay_epoch == 0
                finally:
                    resume_receiver.set()
        finally:
            resume_receiver.set()
            session.disconnect()
            if replacement_worker is not None:
                replacement_worker.join(5)
                assert not replacement_worker.is_alive()


def test_layer_stack_is_captured_before_replay_delivery(monkeypatch):
    with in_process_server() as (state, port):
        state._commit_events([ensure_prim_event("/Own")])
        state._broadcast_queue.join()
        original_stack = state.get_layer_stack_state()
        send = connection_mod.send_msg

        def mute_before_hello(sock, message):
            if message.get("replay_identity"):
                assert state.mute_layer("default")
            send(sock, message)

        monkeypatch.setattr(connection_mod, "send_msg", mute_before_hello)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            send_framed(sock, encode_message(make_hello("receiver", layered_replay=True)))
            messages = []
            while not messages or messages[-1]["type"] != "replay_complete":
                messages.append(message_to_dict(recv_framed(sock)))
            stack = next(message for message in messages if message["type"] == "layer_stack_state")
            assert stack == original_stack
            assert [message["seq"] for message in messages if message["type"] == "event"] == [1]
            # The concurrent live update must follow, rather than replace, the
            # captured routing state used by the initial replay.
            updated_stack = message_to_dict(recv_framed(sock))
            assert updated_stack == state.get_layer_stack_state()
            assert updated_stack["layers"][0]["muted"] is True


class _CaptureHandler(ReceiverStub):
    _layered_replay = True

    def __init__(self):
        self.send_lock = threading.Lock()


@pytest.mark.parametrize("cursor,instance,epoch,start,reason", [
    (1, None, None, 1, None),
    (2, None, None, 2, None),
    (2, "same", 0, 2, None),
    (4, "same", 0, 4, None),
    (2, "same", 1, 1, "replay_identity_changed"),
    (2, "other", 0, 1, "replay_identity_changed"),
    (2, "", None, 1, "replay_identity_changed"),
    (6, "same", 0, 1, "seq_overflow"),
])
def test_capture_selects_only_required_records_without_copying_bytes(
    monkeypatch, cursor, instance, epoch, start, reason,
):
    with in_process_server() as (state, _port):
        state._commit_events([ensure_prim_event(f"/P{i}") for i in range(3)])
        state._broadcast_queue.join()
        read = state.store.get_from_seq_bin
        queries = []

        def capture_read(first, last):
            records = read(first, last)
            queries.append((first, last, records))
            return records

        monkeypatch.setattr(state.store, "get_from_seq_bin", capture_read)
        handler = _CaptureHandler()
        try:
            with state.receiver_replay_window(
                handler, cursor,
                replay_server_instance=state.server_instance if instance == "same" else instance,
                replay_epoch=epoch,
            ) as replay:
                assert handler.send_lock.locked()
                assert (replay.epoch, replay.head_seq) == (0, 3)
                assert replay.resync_reason == reason
                assert len(queries) == 1
                assert queries[0][:2] == (start, 3)
                assert isinstance(replay.records, tuple)
                assert all(
                    captured is original
                    for captured, original in zip(replay.records, queries[0][2], strict=True)
                )
                assert [message_to_dict(record)["seq"] for record in replay.records] == list(
                    range(start, 4)
                )
                assert message_to_dict(replay.layer_stack_state) == state.get_layer_stack_state()
            assert not handler.send_lock.locked()
        finally:
            with state.clients_lock:
                state.receivers.discard(handler)


def test_failed_capture_releases_registration_and_locks(monkeypatch):
    with in_process_server() as (state, _port):
        handler = _CaptureHandler()

        def fail_read(*_args):
            raise RuntimeError("injected replay capture failure")

        with monkeypatch.context() as patch:
            patch.setattr(state.store, "get_from_seq_bin", fail_read)
            with pytest.raises(RuntimeError, match="injected replay capture failure"):
                with state.receiver_replay_window(handler):
                    pytest.fail("failed capture must not start delivery")
        assert handler not in state.receivers
        assert not handler.send_lock.locked()
        barrier_released = threading.Event()

        def acquire_barrier():
            state.txn_barrier.acquire_exclusive()
            state.txn_barrier.release_exclusive()
            barrier_released.set()

        worker = threading.Thread(target=acquire_barrier, daemon=True)
        worker.start()
        worker.join(5)
        assert barrier_released.is_set()
        assert not worker.is_alive()
