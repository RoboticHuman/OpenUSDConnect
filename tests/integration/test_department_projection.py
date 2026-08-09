"""Network coverage for the flat and layered replay contracts."""

from __future__ import annotations

import threading
import time

import pytest

from openusdconnect.codec import HelloRejectionCode, message_to_dict
from openusdconnect.protocol_constants import MSG_RESYNC
from openusdconnect.receiver import ReceiverThread
from openusdconnect.sender import EventSender
from openusdconnect.server import UsdSyncServer
from openusdconnect.server.connection import ConnectionHandler, ThreadedTCPServer
from openusdconnect.server.types import ReplayModeConflictError


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def server_factory(tmp_path):
    resources = []

    def _create(departments, *, durability="strict"):
        index = len(resources)
        sync_server = UsdSyncServer(
            log_path=str(tmp_path / f"replay-contract-{index}.db"),
            department_priority=departments,
            durability=durability,
        )
        tcp_server = ThreadedTCPServer(
            ("127.0.0.1", 0),
            ConnectionHandler,
            sync_server,
            max_workers=8,
        )
        thread = threading.Thread(target=tcp_server.serve_forever, daemon=True)
        thread.start()
        resources.append((sync_server, tcp_server, thread))
        return sync_server, tcp_server.server_address[1]

    yield _create

    for sync_server, tcp_server, thread in reversed(resources):
        tcp_server.shutdown()
        tcp_server.server_close()
        thread.join(timeout=5)
        sync_server.shutdown()
        sync_server.store.close()


@pytest.mark.parametrize(
    ("departments", "accepted"),
    [([], True), (["animation", "layout"], False)],
)
def test_flat_receiver_is_admitted_only_for_single_layer(
    server_factory,
    departments,
    accepted,
):
    sync_server, port = server_factory(departments)
    receiver = ReceiverThread(
        port=port,
        reconnect=False,
        client_id="flat-observer",
        origin="flat-observer-origin",
        layered_replay=False,
    )
    receiver.start()
    try:
        if not accepted:
            receiver.join(timeout=5)
            assert not receiver.is_alive()
            assert not receiver.connected
            assert receiver.hello_rejected
            assert receiver.rejection_code == HelloRejectionCode.LayeredReplayRequired
            assert "department" in receiver.rejection_reason
            assert not receiver.auth_rejected
            assert _wait_until(lambda: sync_server._flat_receiver_count == 0)
            assert not sync_server.receivers
        else:
            assert _wait_until(lambda: receiver.connected)
            assert not receiver.layered_replay_active
            assert sync_server._flat_receiver_count == 1
    finally:
        receiver.stop()
        receiver.join(timeout=2)


@pytest.mark.parametrize("departments", [[], ["animation", "layout"]])
def test_layered_receiver_is_admitted_for_both_server_modes(server_factory, departments):
    sync_server, port = server_factory(departments)
    receiver = ReceiverThread(
        port=port,
        reconnect=False,
        client_id="layered-observer",
        origin="layered-observer-origin",
    )
    receiver.start()
    try:
        assert _wait_until(lambda: receiver.connected)
        assert receiver.layered_replay_active
        assert sync_server._flat_receiver_count == 0
    finally:
        receiver.stop()
        receiver.join(timeout=2)


def test_single_layer_flat_receiver_gets_live_and_replayed_records(server_factory):
    sync_server, port = server_factory([])

    live = ReceiverThread(
        port=port,
        reconnect=False,
        client_id="live-flat",
        origin="live-flat-origin",
        layered_replay=False,
    )
    sender = EventSender(
        "127.0.0.1",
        port,
        client_id="author",
        origin="author-origin",
    )
    late = None
    live.start()
    try:
        assert _wait_until(lambda: live.connected)
        assert sender.connect()
        event = {"k": "ensure_prim", "prim": "/World/Live", "typeName": "Xform"}
        assert sender.send_events([event])
        assert _wait_until(lambda: live.last_seq == 1)
        live_records = [message_to_dict(raw) for raw in live.drain_queue()]
        assert [record["event"] for record in live_records] == [event]

        live.stop()
        live.join(timeout=2)
        assert _wait_until(lambda: sync_server._flat_receiver_count == 0)

        late = ReceiverThread(
            port=port,
            reconnect=False,
            sync_from=1,
            client_id="late-flat",
            origin="late-flat-origin",
            layered_replay=False,
        )
        late.start()
        assert _wait_until(lambda: late.connected and late.last_seq == 1)
        replay_records = [message_to_dict(raw) for raw in late.drain_queue()]
        assert [record["event"] for record in replay_records] == [event]
    finally:
        sender.disconnect()
        live.stop()
        live.join(timeout=2)
        if late is not None:
            late.stop()
            late.join(timeout=2)


def test_stale_cursor_resyncs_against_an_empty_log(server_factory):
    _sync_server, port = server_factory([])
    receiver = ReceiverThread(
        port=port,
        reconnect=False,
        sync_from=5,
        client_id="stale-layered-observer",
    )
    receiver.start()
    received = []
    try:
        assert receiver.wait_connected(timeout=5)

        def _received_resync():
            received.extend(receiver.drain_queue())
            return any(
                message_to_dict(raw).get("type") == MSG_RESYNC
                for raw in received
            )

        assert _wait_until(_received_resync)
    finally:
        receiver.stop()
        receiver.join(timeout=2)


def test_flat_receiver_blocks_enabling_department_policy(server_factory):
    sync_server, port = server_factory([])

    receiver = ReceiverThread(
        port=port,
        reconnect=False,
        client_id="flat-policy-guard",
        layered_replay=False,
    )
    receiver.start()
    try:
        assert _wait_until(lambda: receiver.connected)
        with pytest.raises(ReplayModeConflictError, match="layer-stack changes"):
            sync_server.set_department_priority(["animation", "layout"])
    finally:
        receiver.stop()
        receiver.join(timeout=2)

    assert _wait_until(lambda: sync_server._flat_receiver_count == 0)
    sync_server.set_department_priority(["animation", "layout"])
    assert sync_server.department_priority == ["animation", "layout"]


def test_replay_failure_unregisters_layered_receiver(server_factory, monkeypatch):
    sync_server, port = server_factory(["animation"])

    def _fail_replay(_handler, _seq_start, *, seq_end=None):
        raise OSError("injected replay failure")

    monkeypatch.setattr(sync_server, "replay_from", _fail_replay)
    receiver = ReceiverThread(
        port=port,
        reconnect=False,
        client_id="failing-replay",
        origin="failing-replay-origin",
    )
    receiver.start()
    receiver.join(timeout=5)
    try:
        assert not receiver.is_alive()
        assert _wait_until(lambda: not sync_server.receivers)
        assert _wait_until(lambda: not sync_server.clients)
    finally:
        receiver.stop()
        receiver.join(timeout=2)


def test_compaction_replay_failure_releases_flat_reservation(server_factory, monkeypatch):
    sync_server, port = server_factory([])
    receiver = ReceiverThread(
        port=port,
        reconnect=False,
        client_id="failing-flat-compaction",
        layered_replay=False,
    )
    sender = EventSender(
        "127.0.0.1",
        port,
        client_id="compaction-author",
        origin="compaction-author-origin",
    )
    receiver.start()
    try:
        assert _wait_until(lambda: receiver.connected)
        assert sender.connect()
        assert sender.send_events(
            [{"k": "ensure_prim", "prim": "/World/Compaction", "typeName": "Xform"}]
        )
        assert _wait_until(lambda: receiver.last_seq == 1)
        assert _wait_until(lambda: sync_server.store.get_count() == 1)

        def _fail_replay(_handler, _seq_start, *, seq_end=None):
            raise OSError("injected compaction replay failure")

        monkeypatch.setattr(sync_server, "replay_from", _fail_replay)
        sync_server.compact_log()

        assert not sync_server.receivers
        assert sync_server._flat_receiver_count == 0
    finally:
        sender.disconnect()
        receiver.stop()
        receiver.join(timeout=2)


def test_realtime_receiver_boundary_waits_for_pending_persistence(
    server_factory,
    monkeypatch,
):
    sync_server, port = server_factory([], durability="realtime")
    persist_started = threading.Event()
    allow_persist = threading.Event()
    append_batch = sync_server.store.append_batch

    def _blocked_append(records, **kwargs):
        persist_started.set()
        if not allow_persist.wait(timeout=5):
            raise TimeoutError("test did not release persistence")
        append_batch(records, **kwargs)

    monkeypatch.setattr(sync_server.store, "append_batch", _blocked_append)
    sender = EventSender(
        "127.0.0.1",
        port,
        client_id="realtime-author",
        origin="realtime-author-origin",
    )
    receiver = ReceiverThread(
        port=port,
        reconnect=False,
        client_id="realtime-observer",
        layered_replay=False,
    )
    try:
        assert sender.connect()
        event = {"k": "ensure_prim", "prim": "/World/Realtime", "typeName": "Xform"}
        assert sender.send_events([event])
        assert persist_started.wait(timeout=5)

        receiver.start()
        time.sleep(0.2)
        assert not receiver.drain_queue()

        allow_persist.set()
        assert receiver.wait_connected(timeout=5)
        assert _wait_until(lambda: receiver.last_seq == 1)
        records = [message_to_dict(raw) for raw in receiver.drain_queue()]
        assert [record["event"] for record in records] == [event]
    finally:
        allow_persist.set()
        sender.disconnect()
        receiver.stop()
        if receiver.ident is not None:
            receiver.join(timeout=2)
