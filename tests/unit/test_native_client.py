from __future__ import annotations

import struct
from pathlib import Path

import pytest

native = pytest.importorskip("openusdconnect._native_client")
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_frame_decoder_handles_fragmented_and_coalesced_input():
    decoder = native.FrameDecoder()
    stream = native.encode_frame(b"alpha") + native.encode_frame(b"beta")

    assert decoder.feed(stream[:3]) == []
    assert decoder.buffered_bytes == 3
    assert decoder.feed(stream[3:8]) == []
    assert decoder.feed(stream[8:]) == [b"alpha", b"beta"]
    assert decoder.buffered_bytes == 0


def test_frame_decoder_rejects_invalid_size_at_header_boundary():
    decoder = native.FrameDecoder(8)

    with pytest.raises(native.FrameError):
        decoder.feed(struct.pack(">I", 9))


def test_python_boundary_preserves_frame_validation_exceptions():
    with pytest.raises(ValueError):
        native.FrameDecoder(0)
    with pytest.raises(native.FrameError):
        native.encode_frame(b"")
    with pytest.raises(native.FrameError):
        native.encode_frame(b"oversized", max_frame_size=4)


def test_receiver_inbox_preserves_replay_live_boundary():
    inbox = native.ReceiverInbox(initial_sync_from=1, max_messages=8)
    connection = inbox.begin_connection()

    assert connection.sync_from == 1
    assert (
        inbox.accept(connection.generation, native.ReceiverMessageKind.EVENT, 1, b"event-1")
        == native.AcceptResult.ACCEPTED
    )
    assert (
        inbox.accept_replay_complete(connection.generation, head_seq=1, epoch=7)
        == native.AcceptResult.ACCEPTED
    )
    assert (
        inbox.accept(connection.generation, native.ReceiverMessageKind.EVENT, 2, b"event-2")
        == native.AcceptResult.ACCEPTED
    )

    assert inbox.mark_replay_applied() is False
    assert inbox.drain(1) == [b"event-1"]
    assert inbox.mark_replay_applied() is True
    assert inbox.replay_head_sequence == 1
    assert inbox.replay_epoch == 7
    assert inbox.drain() == [b"event-2"]


def test_receiver_inbox_retains_python_bytes_without_copying():
    inbox = native.ReceiverInbox(initial_sync_from=1, max_messages=2)
    connection = inbox.begin_connection()
    payload = b"immutable-receiver-payload"

    assert (
        inbox.accept(connection.generation, native.ReceiverMessageKind.EVENT, 1, payload)
        == native.AcceptResult.ACCEPTED
    )
    # The binding queues the immutable Python object itself; it does not copy its bytes.
    drained = inbox.drain()
    assert drained == [payload]
    assert drained[0] is payload
    assert inbox.drain() == []


def test_receiver_inbox_rejects_stale_generation_without_mutation():
    inbox = native.ReceiverInbox(initial_sync_from=4, max_messages=8)
    first = inbox.begin_connection()
    second = inbox.begin_connection()

    assert (
        inbox.accept(first.generation, native.ReceiverMessageKind.EVENT, 4, b"stale")
        == native.AcceptResult.STALE_GENERATION
    )
    assert inbox.size == 0
    assert second.sync_from == 4


def test_receiver_inbox_overflow_is_bounded_and_replayable():
    inbox = native.ReceiverInbox(initial_sync_from=1, max_messages=1)
    connection = inbox.begin_connection()

    assert (
        inbox.accept(connection.generation, native.ReceiverMessageKind.EVENT, 1, b"one")
        == native.AcceptResult.ACCEPTED
    )
    assert (
        inbox.accept(connection.generation, native.ReceiverMessageKind.EVENT, 2, b"two")
        == native.AcceptResult.QUEUE_FULL
    )
    assert inbox.overflowed is True
    assert inbox.drain() == [b"one"]

    inbox.request_replay_from(2)
    replay = inbox.begin_connection()
    assert replay.sync_from == 2
    assert inbox.overflowed is False


def test_receiver_session_can_enforce_contiguous_delivery():
    inbox = native.ReceiverInbox(
        initial_sync_from=1,
        max_messages=4,
        require_contiguous=True,
    )
    connection = inbox.begin_connection()

    assert (
        inbox.accept(connection.generation, native.ReceiverMessageKind.EVENT, 2, b"gap")
        == native.AcceptResult.SEQUENCE_GAP
    )
    assert (
        inbox.accept(connection.generation, native.ReceiverMessageKind.EVENT, 1, b"one")
        == native.AcceptResult.ACCEPTED
    )
    assert (
        inbox.accept(connection.generation, native.ReceiverMessageKind.EVENT, 1, b"duplicate")
        == native.AcceptResult.DUPLICATE
    )
    assert inbox.drain() == [b"one"]
    assert inbox.mark_applied_through(connection.generation, 1) is True
    assert inbox.last_applied_sequence == 1


def test_python_boundary_validates_receiver_preconditions():
    with pytest.raises(ValueError):
        native.ReceiverInbox(initial_sync_from=0, max_messages=1)
    with pytest.raises(ValueError):
        native.ReceiverInbox(initial_sync_from=1, max_messages=0)

    inbox = native.ReceiverInbox(initial_sync_from=1, max_messages=1)
    connection = inbox.begin_connection()
    with pytest.raises(ValueError):
        inbox.accept(connection.generation, native.ReceiverMessageKind.EVENT, 0, b"event")
    with pytest.raises(ValueError):
        inbox.accept_replay_complete(connection.generation, -1, 0)
    with pytest.raises(ValueError):
        inbox.drain(0)
    with pytest.raises(ValueError):
        inbox.request_replay_from(0)


def test_producer_session_replays_only_the_unacknowledged_suffix():
    session = native.ProducerSession(capacity=4)
    first = session.begin_connection()
    assert session.accept_hello(first.generation, 0) == native.ProducerResult.ACCEPTED
    assert (
        session.append(first.generation, 1, b"txn-1", 2, "layout") == native.ProducerResult.ACCEPTED
    )
    assert (
        session.append(first.generation, 2, b"txn-2", 3, "animation")
        == native.ProducerResult.ACCEPTED
    )
    assert session.claim_next_unsent(first.generation) == (1, b"txn-1", 2, "layout")
    assert session.claim_next_unsent(first.generation) == (2, b"txn-2", 3, "animation")

    assert session.disconnect(first.generation) == native.ProducerResult.ACCEPTED
    second = session.begin_connection()
    assert session.accept_hello(second.generation, 1) == native.ProducerResult.ACCEPTED
    assert session.claim_next_unsent(second.generation) == (2, b"txn-2", 3, "animation")
    assert session.claim_next_unsent(second.generation) is None
    assert session.pending_transaction_count == 1
    assert session.pending_event_count == 3
    assert session.acknowledged_transaction_count == 1
    assert session.acknowledged_event_count == 2
    assert session.drain_acknowledged_event_count() == 2
    assert session.drain_acknowledged_event_count() == 0


def test_producer_session_retains_python_bytes_without_copying():
    session = native.ProducerSession(capacity=1)
    connection = session.begin_connection()
    payload = b"immutable-producer-payload"

    assert session.accept_hello(connection.generation, 0) == native.ProducerResult.ACCEPTED
    assert session.append(connection.generation, 1, payload, 1) == native.ProducerResult.ACCEPTED
    claimed = session.claim_next_unsent(connection.generation)
    assert claimed is not None
    assert claimed[1] is payload


def test_producer_session_quarantines_highwater_contradictions():
    session = native.ProducerSession(capacity=2)
    connection = session.begin_connection()

    assert session.accept_hello(connection.generation, 1) == native.ProducerResult.HIGHWATER_AHEAD
    assert session.phase == native.ProducerPhase.RECOVERY_REQUIRED
    assert session.recovery_required is True
    assert session.begin_connection() is None


def test_producer_session_repairs_recoverable_transaction_at_same_id():
    session = native.ProducerSession(capacity=2)
    first = session.begin_connection()
    assert session.accept_hello(first.generation, 0) == native.ProducerResult.ACCEPTED
    assert session.append(first.generation, 1, b"stale", 1) == native.ProducerResult.ACCEPTED
    assert (
        session.reject(
            first.generation,
            1,
            native.ProducerRecoveryDisposition.RECOVERABLE_CONFLICT,
        )
        == native.ProducerResult.ACCEPTED
    )

    assert session.repair_rejected(b"repaired", 2) == native.ProducerResult.ACCEPTED
    second = session.begin_connection()
    assert session.accept_hello(second.generation, 0) == native.ProducerResult.ACCEPTED
    assert session.claim_next_unsent(second.generation) == (1, b"repaired", 2, "")


def test_python_boundary_validates_producer_preconditions():
    with pytest.raises(ValueError):
        native.ProducerSession(capacity=0)

    session = native.ProducerSession(capacity=1)
    connection = session.begin_connection()
    assert session.accept_hello(connection.generation, 0) == native.ProducerResult.ACCEPTED
    with pytest.raises(ValueError):
        session.append(connection.generation, 1, b"txn", 0)


def test_shared_core_and_unreal_module_do_not_enable_cpp_exceptions():
    core = REPO_ROOT / "native" / "client_core"
    sources = [*core.rglob("*.h"), *core.rglob("*.cpp")]

    assert sources
    assert all("throw" not in source.read_text(encoding="utf-8") for source in sources)

    build_rules = (
        REPO_ROOT
        / "integrations"
        / "unreal"
        / "OpenUSDConnect"
        / "Source"
        / "OpenUSDConnect"
        / "OpenUSDConnect.Build.cs"
    ).read_text(encoding="utf-8")
    assert "bEnableExceptions" not in build_rules
