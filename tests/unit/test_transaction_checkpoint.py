"""Optional wire checkpoints and producer acknowledgement ownership."""

from unittest.mock import Mock

import pytest

from openusdconnect import _client_backend
from openusdconnect.checkpoints import MirrorCheckpoint, TransactionCheckpoint
from openusdconnect.codec import decode_envelope, encode_message, message_to_dict, resolve_payload
from openusdconnect.protocol import make_transaction_result
from openusdconnect.sender import EventSender
from openusdconnect.server import UsdSyncServer
from openusdconnect.server.transactions import TransactionRequest


@pytest.mark.parametrize("batch_size", [1, 8])
def test_checkpoint_capture_failure_preserves_durable_ack(tmp_path, monkeypatch, batch_size):
    server = UsdSyncServer(log_path=str(tmp_path / "checkpoint.db"), txn_batch_size=batch_size)
    try:
        def unavailable_head():
            raise RuntimeError("injected checkpoint failure")

        with monkeypatch.context() as patch:
            patch.setattr(server.store, "get_max_seq", unavailable_head)
            commit = server.process_idempotent_txn(
                [{"k": "ensure_prim", "prim": "/Own", "typeName": "Xform"}],
                session_id="checkpoint", txn_id=1, client_id="checkpoint",
            )
        assert commit.status == "committed"
        assert commit.checkpoint is None
        assert server.store.get_max_seq() == 1
        assert server.producer_committed_through("checkpoint", "checkpoint") == 1
    finally:
        server.shutdown()
        server.store.close()


def test_durable_checkpoint_excludes_uncommitted_sequence_reservations(tmp_path, monkeypatch):
    server = UsdSyncServer(log_path=str(tmp_path / "checkpoint.db"))
    try:
        commit = server.process_idempotent_txn(
            [{"k": "ensure_prim", "prim": "/Own", "typeName": "Xform"}],
            session_id="checkpoint", txn_id=1, client_id="checkpoint",
        )
        epoch = commit.checkpoint.epoch
        head = commit.checkpoint.head_seq
        assert head > 0
        observed = []

        def fail_persistence(*args, **kwargs):
            assert server.get_replay_token()[1] > head
            observed.append((server.get_replay_token()[0], server.store.get_max_seq()))
            raise RuntimeError("injected persistence failure")

        monkeypatch.setattr(server.store, "append_batch", fail_persistence)
        with pytest.raises(RuntimeError, match="injected persistence failure"):
            server.process_idempotent_txn(
                [{"k": "ensure_prim", "prim": "/RolledBack", "typeName": "Xform"}],
                session_id="checkpoint", txn_id=2, client_id="checkpoint",
            )
        assert observed and all(checkpoint == (epoch, head) for checkpoint in observed)
        assert server.get_replay_token() == (epoch, head)
        assert commit.checkpoint == TransactionCheckpoint(epoch=epoch, head_seq=head)
    finally:
        server.shutdown()
        server.store.close()


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("capture_fails", [False, True])
def test_batch_shares_one_checkpoint_for_successful_commits(
    tmp_path, monkeypatch, fallback, capture_fails,
):
    server = UsdSyncServer(log_path=str(tmp_path / "checkpoint.db"), txn_batch_size=1)
    try:
        server.process_idempotent_txn(
            [{"k": "ensure_prim", "prim": "/Seed", "typeName": "Xform"}],
            session_id="seed", txn_id=1, client_id="seed",
        )
        requests = [
            TransactionRequest(
                events=[{"k": "ensure_prim", "prim": path, "typeName": "Xform"}],
                session_id=client, txn_id=txn_id, client_id=client,
                origin=None, client_addr=None, layer=None, layer_key="",
            )
            for client, txn_id, path in [
                ("seed", 1, "/Seed"),
                ("first", 1, "/First"),
                ("gap", 3, "/Rejected"),
                ("last", 1, "/Last"),
            ]
        ]
        read_head = Mock(wraps=server.store.get_max_seq)
        if capture_fails:
            read_head.side_effect = RuntimeError("injected checkpoint failure")
        if fallback:
            append = server.store.append_batch
            failed = False

            def fail_group_once(*args, **kwargs):
                nonlocal failed
                if not failed:
                    failed = True
                    raise RuntimeError("injected group failure")
                return append(*args, **kwargs)

            monkeypatch.setattr(server.store, "append_batch", fail_group_once)
        with monkeypatch.context() as patch:
            patch.setattr(server.store, "get_max_seq", read_head)
            for _request in requests:
                server.txn_barrier.acquire_shared()
            server._transactions._execute(requests)

        read_head.assert_called_once_with()
        assert all(request.done.is_set() for request in requests)
        duplicate, first, rejected, last = requests
        assert duplicate.commit.status == "duplicate"
        assert duplicate.commit.checkpoint is None
        assert rejected.error is not None
        assert rejected.commit is None
        assert first.commit.status == last.commit.status == "committed"
        assert first.commit.checkpoint is last.commit.checkpoint
        expected = None if capture_fails else TransactionCheckpoint(0, 3)
        assert first.commit.checkpoint == expected
        assert server.store.get_max_seq() == 3
        assert server.stage.GetPrimAtPath("/First")
        assert server.stage.GetPrimAtPath("/Last")
        assert not server.stage.GetPrimAtPath("/Rejected")
        server.purge()
        assert first.commit.checkpoint == last.commit.checkpoint == expected
    finally:
        server.shutdown()
        server.store.close()


@pytest.mark.parametrize(
    "checkpoint", [None, TransactionCheckpoint(0, 0), TransactionCheckpoint(3, 42)]
)
def test_optional_checkpoint_roundtrip(checkpoint):
    msg = make_transaction_result(1, checkpoint=checkpoint)
    decoded = message_to_dict(encode_message(msg))
    if checkpoint is not None:
        assert decoded["checkpoint"] == {
            "epoch": checkpoint.epoch,
            "head_seq": checkpoint.head_seq,
        }
    else:
        assert "checkpoint" not in decoded


def test_checkpoint_requires_current_ack_and_no_pending_transactions(monkeypatch):
    sender = EventSender("127.0.0.1", 1, client_id="checkpoint")
    connection = sender._session.begin_connection()
    generation = connection.generation
    assert sender._session.accept_hello(generation, 0) == _client_backend.ProducerResult.ACCEPTED
    sender._socket_generation = generation
    sender._server_instance = "server"
    sender.sock = object()
    monkeypatch.setattr("openusdconnect.sender.send_raw", lambda *args: None)

    def ack(txn_id, checkpoint=None):
        envelope = decode_envelope(
            encode_message(make_transaction_result(txn_id, checkpoint=checkpoint))
        )
        return resolve_payload(envelope)[1]

    event = {"k": "ensure_prim", "prim": "/Own", "typeName": "Xform"}
    assert sender.send_events([event])
    assert sender.acknowledged_checkpoint is None
    sender._accept_result(ack(1, TransactionCheckpoint(2, 8)), generation)
    assert sender.acknowledged_checkpoint == MirrorCheckpoint("server", 2, 8)
    sender._accept_result(ack(1, TransactionCheckpoint(9, 999)), generation + 1)
    assert sender.acknowledged_checkpoint == MirrorCheckpoint("server", 2, 8)
    assert sender.send_events([event])
    assert sender.acknowledged_checkpoint is None
    sender._accept_result(ack(2), generation)
    assert sender.flush(timeout=0)
    assert sender.acknowledged_checkpoint is None
    sender.sock = None


def test_hello_highwater_recovery_does_not_confirm_mirror(monkeypatch):
    sender = EventSender("127.0.0.1", 1, client_id="checkpoint")
    generation = sender._session.begin_connection().generation
    sender._session.accept_hello(generation, 0)
    sender._socket_generation = generation
    sender.sock = Mock()
    monkeypatch.setattr("openusdconnect.sender.send_raw", lambda *args: None)
    try:
        assert sender.send_events([{"k": "ensure_prim", "prim": "/Own", "typeName": "Xform"}])
        sender._acknowledged_checkpoint = MirrorCheckpoint("old-instance", 0, 10)
        generation = sender._session.begin_connection().generation
        envelope = decode_envelope(encode_message({
            "type": "hello_ok", "server_instance": "new-instance", "committed_through": 1,
        }))
        assert sender._accept_handshake_response(
            sender.sock, envelope, envelope.PayloadType(), generation,
        )
        assert sender._session.empty
        assert sender.acknowledged_checkpoint is None
    finally:
        sender.sock = None


def test_duplicate_after_purge_has_no_original_visibility_proof(tmp_path, monkeypatch):
    state = UsdSyncServer(log_path=str(tmp_path / "checkpoint.db"))
    try:
        transaction = dict(session_id="producer", txn_id=1, client_id="producer")
        events = [{"k": "ensure_prim", "prim": "/Own", "typeName": "Xform"}]
        committed = state.process_idempotent_txn(events, **transaction)
        assert committed.checkpoint == TransactionCheckpoint(0, 1)
        state.purge()
        read_head = Mock(wraps=state.store.get_max_seq)
        monkeypatch.setattr(state.store, "get_max_seq", read_head)
        duplicate = state.process_idempotent_txn(events, **transaction)
        assert duplicate.status == "duplicate"
        assert duplicate.checkpoint is None
        read_head.assert_not_called()
    finally:
        state.shutdown()
        state.store.close()
