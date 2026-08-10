"""End-to-end ambiguous-commit and reconnect transaction coverage."""

from __future__ import annotations

import socket
import threading
import time

import pytest

import openusdconnect.server.connection as connection_module
from openusdconnect.codec import message_to_dict
from openusdconnect.framing import recv_framed
from openusdconnect.protocol import make_hello
from openusdconnect.protocol_constants import MSG_TRANSACTION_RESULT
from openusdconnect.receiver import ReceiverThread
from openusdconnect.sender import EventSender
from openusdconnect.server import UsdSyncServer
from openusdconnect.server.connection import ConnectionHandler, ThreadedTCPServer
from openusdconnect.transport import send_msg


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


@pytest.fixture
def transaction_server(tmp_path):
    state = UsdSyncServer(
        log_path=str(tmp_path / "transactions.db"),
        txn_batch_delay=0.02,
    )
    tcp = ThreadedTCPServer(("127.0.0.1", 0), ConnectionHandler, state, max_workers=8)
    thread = threading.Thread(target=tcp.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, tcp.server_address[1]
    finally:
        tcp.shutdown()
        tcp.server_close()
        thread.join(timeout=5)
        state.shutdown()
        state.store.close()


def test_lost_commit_ack_reconnects_as_duplicate_without_reapplying(
    transaction_server, monkeypatch
):
    state, port = transaction_server
    original_send = connection_module.send_msg
    dropped = threading.Event()

    def drop_first_committed_result(sock, message):
        if message.get("type") == MSG_TRANSACTION_RESULT and not dropped.is_set():
            dropped.set()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            raise OSError("injected lost acknowledgement")
        return original_send(sock, message)

    monkeypatch.setattr(connection_module, "send_msg", drop_first_committed_result)
    sender = EventSender(
        "127.0.0.1",
        port,
        client_id="reconnecting-producer",
        session_id="reconnecting-session",
    )
    try:
        assert sender.connect()
        event = {"k": "ensure_prim", "prim": "/World/Once", "typeName": "Xform"}
        assert sender.send_events([event])
        assert dropped.wait(timeout=5)
        assert _wait_until(lambda: not sender.connected)
        assert sender.pending_transaction_count == 1
        assert state.store.get_count() == 1

        assert sender.connect()
        assert sender.flush(timeout=5)
        assert sender.drain_acknowledged_event_count() == 1
        assert state.store.get_count() == 1
        assert state.store.get_producer_progress(
            "reconnecting-producer", "reconnecting-session"
        ) == 1
        assert state.stage.GetPrimAtPath("/World/Once").IsValid()
    finally:
        sender.disconnect()


def test_lost_ack_from_group_commit_reconnects_without_reapplying(
    transaction_server, monkeypatch
):
    state, port = transaction_server
    group_sizes = []
    append_batch = state.store.append_batch

    def observe_group(records, *, producer_progress=()):
        if producer_progress:
            group_sizes.append(len(records))
        return append_batch(records, producer_progress=producer_progress)

    monkeypatch.setattr(state.store, "append_batch", observe_group)
    original_send = connection_module.send_msg
    dropped = threading.Event()

    def drop_group_member_ack(sock, message):
        if (
            message.get("type") == MSG_TRANSACTION_RESULT
            and message.get("status") == "acknowledged"
            and not dropped.is_set()
        ):
            dropped.set()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            raise OSError("injected grouped acknowledgement loss")
        return original_send(sock, message)

    monkeypatch.setattr(connection_module, "send_msg", drop_group_member_ack)
    first = EventSender("127.0.0.1", port, client_id="group-a", session_id="group-a")
    second = EventSender("127.0.0.1", port, client_id="group-b", session_id="group-b")
    try:
        assert first.connect()
        assert second.connect()
        barrier = threading.Barrier(3)

        def send(sender, prim):
            barrier.wait()
            assert sender.send_events(
                [{"k": "ensure_prim", "prim": prim, "typeName": "Xform"}]
            )

        threads = [
            threading.Thread(target=send, args=(first, "/World/A")),
            threading.Thread(target=send, args=(second, "/World/B")),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        assert dropped.wait(timeout=5)
        assert _wait_until(lambda: not first.connected or not second.connected)
        victim = first if not first.connected else second
        survivor = second if victim is first else first
        assert survivor.flush(timeout=5)
        assert group_sizes == [2]
        assert state.store.get_count() == 2

        assert victim.connect()
        assert victim.flush(timeout=5)
        assert victim.drain_acknowledged_event_count() == 1
        assert state.store.get_count() == 2
        assert state.stage.GetPrimAtPath("/World/A").IsValid()
        assert state.stage.GetPrimAtPath("/World/B").IsValid()
    finally:
        first.disconnect()
        second.disconnect()


def test_group_acknowledgements_wait_for_durable_store_commit(
    transaction_server,
    monkeypatch,
):
    state, port = transaction_server
    entered = threading.Event()
    release = threading.Event()
    append_batch = state.store.append_batch

    def block_group(records, *, producer_progress=()):
        entered.set()
        assert release.wait(timeout=5)
        return append_batch(records, producer_progress=producer_progress)

    monkeypatch.setattr(state.store, "append_batch", block_group)
    senders = [
        EventSender("127.0.0.1", port, client_id=f"blocked-{index}", session_id=f"blocked-{index}")
        for index in range(2)
    ]
    try:
        assert all(sender.connect() for sender in senders)
        barrier = threading.Barrier(3)

        def send(index):
            barrier.wait()
            assert senders[index].send_events(
                [
                    {
                        "k": "ensure_prim",
                        "prim": f"/World/Blocked{index}",
                        "typeName": "Xform",
                    }
                ]
            )

        threads = [threading.Thread(target=send, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        assert entered.wait(timeout=5)
        time.sleep(0.05)
        assert state.store.get_count() == 0
        assert [sender.pending_transaction_count for sender in senders] == [1, 1]
        assert [sender.drain_acknowledged_event_count() for sender in senders] == [0, 0]

        release.set()
        assert all(sender.flush(timeout=5) for sender in senders)
        assert state.store.get_count() == 2
        assert [sender.drain_acknowledged_event_count() for sender in senders] == [1, 1]
    finally:
        release.set()
        for sender in senders:
            sender.disconnect()


def test_one_producer_pipelines_into_group_commit_and_receives_ordered_results(
    transaction_server,
    monkeypatch,
):
    state, port = transaction_server
    group_sizes = []
    result_batch_sizes = []
    append_batch = state.store.append_batch
    send_transaction_results = connection_module._send_transaction_results

    def observe_group(records, *, producer_progress=()):
        if producer_progress:
            group_sizes.append(len(records))
        return append_batch(records, producer_progress=producer_progress)

    def observe_result_batch(sock, results):
        result_batch_sizes.append(len(results))
        return send_transaction_results(sock, results)

    monkeypatch.setattr(state.store, "append_batch", observe_group)
    monkeypatch.setattr(
        connection_module,
        "_send_transaction_results",
        observe_result_batch,
    )
    sender = EventSender(
        "127.0.0.1",
        port,
        client_id="single-pipeline",
        session_id="single-pipeline",
    )
    try:
        assert sender.connect()
        for txn_id in range(1, 65):
            assert sender.send_events(
                [
                    {
                        "k": "ensure_prim",
                        "prim": f"/World/Pipelined{txn_id}",
                        "typeName": "Xform",
                    }
                ]
            )

        assert sender.flush(timeout=10)
        assert sender.drain_acknowledged_event_count() == 64
        assert state.store.get_count() == 64
        assert group_sizes
        assert max(group_sizes) > 1
        assert max(result_batch_sizes) == 1
    finally:
        sender.disconnect()


def test_lost_first_pipeline_ack_replays_whole_outbox_as_duplicates(
    transaction_server,
    monkeypatch,
):
    state, port = transaction_server
    original_send = connection_module._send_transaction_results
    dropped = threading.Event()

    def drop_first_result_batch(sock, messages):
        if messages and not dropped.is_set():
            dropped.set()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            raise OSError("injected first pipeline acknowledgement batch loss")
        return original_send(sock, messages)

    monkeypatch.setattr(
        connection_module,
        "_send_transaction_results",
        drop_first_result_batch,
    )
    sender = EventSender(
        "127.0.0.1",
        port,
        client_id="pipeline-loss",
        session_id="pipeline-loss",
    )
    try:
        assert sender.connect()
        for txn_id in range(1, 65):
            assert sender.send_events(
                [
                    {
                        "k": "ensure_prim",
                        "prim": f"/World/Lost{txn_id}",
                        "typeName": "Xform",
                    }
                ]
            )

        assert dropped.wait(timeout=5)
        assert _wait_until(lambda: not sender.connected)
        assert _wait_until(lambda: state.store.get_count() == 64)
        assert sender.pending_transaction_count == 64

        assert sender.connect()
        assert sender.flush(timeout=10)
        assert sender.drain_acknowledged_event_count() == 64
        assert state.store.get_count() == 64
    finally:
        sender.disconnect()


def test_pipeline_rejection_preserves_result_order_and_quarantines_suffix(
    transaction_server,
    monkeypatch,
):
    state, port = transaction_server
    apply_txn = state.apply_txn

    def reject_second(events, *args, **kwargs):
        if any(event.get("prim") == "/World/Reject2" for event in events):
            raise ValueError("injected invalid transaction")
        return apply_txn(events, *args, **kwargs)

    monkeypatch.setattr(state, "apply_txn", reject_second)
    sender = EventSender(
        "127.0.0.1",
        port,
        client_id="pipeline-reject",
        session_id="pipeline-reject",
    )
    try:
        assert sender.connect()
        for txn_id in range(1, 4):
            assert sender.send_events(
                [
                    {
                        "k": "ensure_prim",
                        "prim": f"/World/Reject{txn_id}",
                        "typeName": "Xform",
                    }
                ]
            )

        assert _wait_until(lambda: sender.recovery_required)
        assert sender.drain_acknowledged_event_count() == 1
        assert sender.pending_transaction_count == 1
        assert state.store.get_count() == 1
        assert state.stage.GetPrimAtPath("/World/Reject1")
        assert not state.stage.GetPrimAtPath("/World/Reject2")
        assert not state.stage.GetPrimAtPath("/World/Reject3")
    finally:
        sender.disconnect()


def test_live_receiver_observes_grouped_records_in_sequence_order(transaction_server):
    state, port = transaction_server
    group_sizes = []
    append_batch = state.store.append_batch

    def observe_group(records, *, producer_progress=()):
        if producer_progress:
            group_sizes.append(len(records))
        return append_batch(records, producer_progress=producer_progress)

    state.store.append_batch = observe_group
    receiver = ReceiverThread(
        host="127.0.0.1",
        port=port,
        reconnect=False,
        client_id="ordered-receiver",
        origin="ordered-receiver",
        layered_replay=True,
    )
    senders = [
        EventSender("127.0.0.1", port, client_id=f"ordered-{index}", session_id=f"ordered-{index}")
        for index in range(6)
    ]
    receiver.start()
    try:
        assert _wait_until(lambda: receiver.connected)
        receiver.drain_queue()
        assert all(sender.connect() for sender in senders)
        barrier = threading.Barrier(len(senders) + 1)

        def send(index):
            barrier.wait()
            assert senders[index].send_events(
                [
                    {
                        "k": "ensure_prim",
                        "prim": f"/World/Ordered{index}",
                        "typeName": "Xform",
                    }
                ]
            )

        threads = [threading.Thread(target=send, args=(index,)) for index in range(len(senders))]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        assert all(sender.flush(timeout=5) for sender in senders)

        records = []

        def received_everything():
            for raw in receiver.drain_queue():
                message = message_to_dict(raw)
                if message.get("type") == "event":
                    records.append(message)
            return len(records) >= len(senders)

        assert _wait_until(received_everything)
        sequences = [record["seq"] for record in records]
        assert sequences == sorted(sequences)
        assert sequences == list(range(1, len(senders) + 1))
        assert group_sizes == [len(senders)]
    finally:
        receiver.stop()
        receiver.join(timeout=5)
        for sender in senders:
            sender.disconnect()


def test_single_commits_publish_in_durable_sequence_order(
    transaction_server,
    monkeypatch,
):
    """A delayed connection thread cannot publish seq 1 after seq 2."""
    state, port = transaction_server
    first_commit_returned = threading.Event()
    second_published = threading.Event()
    original_wait = state.wait_for_transaction
    original_broadcast = state.broadcast_transaction_views
    original_group_broadcast = state.broadcast_transaction_group_views

    def delay_first_connection(request):
        commit = original_wait(request)
        if commit.records and commit.records[0][0]["seq"] == 1:
            first_commit_returned.set()
            assert second_published.wait(timeout=5)
        return commit

    def observe_publication(records, **kwargs):
        original_broadcast(records, **kwargs)
        if records and records[0][0]["seq"] == 2:
            second_published.set()

    def observe_group_publication(transactions):
        original_group_broadcast(transactions)
        if any(
            records and records[0][0]["seq"] == 2
            for records in transactions
        ):
            second_published.set()

    monkeypatch.setattr(state, "wait_for_transaction", delay_first_connection)
    monkeypatch.setattr(state, "broadcast_transaction_views", observe_publication)
    monkeypatch.setattr(
        state, "broadcast_transaction_group_views", observe_group_publication
    )
    receiver = ReceiverThread(
        host="127.0.0.1",
        port=port,
        reconnect=False,
        client_id="forced-order-receiver",
        origin="forced-order-receiver",
        layered_replay=True,
    )
    first = EventSender("127.0.0.1", port, client_id="forced-a", session_id="forced-a")
    second = EventSender("127.0.0.1", port, client_id="forced-b", session_id="forced-b")
    receiver.start()
    try:
        assert _wait_until(lambda: receiver.connected)
        receiver.drain_queue()
        assert first.connect()
        assert second.connect()
        assert first.send_events(
            [{"k": "ensure_prim", "prim": "/World/First", "typeName": "Xform"}]
        )
        assert first_commit_returned.wait(timeout=5)
        assert second.send_events(
            [{"k": "ensure_prim", "prim": "/World/Second", "typeName": "Xform"}]
        )
        assert first.flush(timeout=5)
        assert second.flush(timeout=5)

        records = []

        def received_both():
            for raw in receiver.drain_queue():
                message = message_to_dict(raw)
                if message.get("type") == "event":
                    records.append(message)
            return len(records) >= 2

        assert _wait_until(received_both)
        assert [record["seq"] for record in records] == [1, 2]
    finally:
        second_published.set()
        receiver.stop()
        receiver.join(timeout=5)
        first.disconnect()
        second.disconnect()


def test_replay_complete_is_the_wire_boundary_between_replay_and_live_events(
    transaction_server,
):
    _state, port = transaction_server
    sender = EventSender(
        "127.0.0.1",
        port,
        client_id="watermark-producer",
        session_id="watermark-session",
    )
    receiver = None
    try:
        assert sender.connect()
        assert sender.send_events(
            [{"k": "ensure_prim", "prim": "/World/Replayed", "typeName": "Xform"}]
        )
        assert sender.flush(timeout=5)

        receiver = socket.create_connection(("127.0.0.1", port), timeout=5)
        receiver.settimeout(5)
        send_msg(
            receiver,
            make_hello(
                "receiver",
                sync_from=1,
                client_id="watermark-receiver",
                origin="watermark-receiver",
            ),
        )
        replay = []
        while True:
            message = message_to_dict(recv_framed(receiver))
            replay.append(message)
            if message["type"] == "replay_complete":
                break

        marker = replay[-1]
        assert marker == {"type": "replay_complete", "head_seq": 1, "epoch": 0}
        assert [message["seq"] for message in replay if message["type"] == "event"] == [1]

        assert sender.send_events(
            [{"k": "ensure_prim", "prim": "/World/Live", "typeName": "Xform"}]
        )
        assert sender.flush(timeout=5)
        live = message_to_dict(recv_framed(receiver))
        assert live["type"] == "event"
        assert live["seq"] == 2
    finally:
        if receiver is not None:
            receiver.close()
        sender.disconnect()
