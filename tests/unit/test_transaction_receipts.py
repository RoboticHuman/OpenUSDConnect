"""Cumulative producer progress and atomic failure-boundary coverage."""

import sqlite3
import threading

import pytest

from openusdconnect.event_store import LayerIdentity, ProducerProgress, SqliteEventStore
from openusdconnect.server.state import UsdSyncServer
from openusdconnect.server.transactions import TransactionRequest
from openusdconnect.server.types import TransactionRejectedError


def _event(path: str) -> dict:
    return {"k": "ensure_prim", "prim": path, "typeName": "Xform"}


def _commit(server, path: str, *, client: str, session: str, txn_id: int):
    return server.process_idempotent_txn(
        [_event(path)],
        client_id=client,
        session_id=session,
        txn_id=txn_id,
    )


def _run_concurrently(calls):
    barrier = threading.Barrier(len(calls) + 1)
    results = [None] * len(calls)
    errors = [None] * len(calls)

    def run(index, call):
        barrier.wait()
        try:
            results[index] = call()
        except BaseException as exc:
            errors[index] = exc

    threads = [
        threading.Thread(target=run, args=(index, call))
        for index, call in enumerate(calls)
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    return results, errors


def test_event_rows_and_producer_progress_commit_atomically_and_survive_reopen(tmp_path):
    db = str(tmp_path / "progress.db")
    progress = ProducerProgress("client", "session-a", 1)
    store = SqliteEventStore(db)
    store.append_batch(
        [(1, b"event-one", "client", "ensure_prim", "/A")],
        producer_progress=(progress,),
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.append_batch(
            [(1, b"duplicate-seq", "client", "ensure_prim", "/B")],
            producer_progress=(ProducerProgress("client", "session-a", 2),),
        )
    assert store.get_all_asc() == [(1, b"event-one")]
    assert store.get_producer_progress("client", "session-a") == 1
    store.close()

    reopened = SqliteEventStore(db)
    assert reopened.get_producer_progress("client", "session-a") == 1
    reopened.close()


def test_failed_single_append_does_not_poison_subsequent_writes(tmp_path):
    db = str(tmp_path / "append-rollback.db")
    store = SqliteEventStore(db)
    try:
        store.append(1, b"one", "client", "ensure_prim", "/A")
        with pytest.raises(sqlite3.IntegrityError):
            store.append(1, b"duplicate", "client", "ensure_prim", "/B")

        store.append_batch(
            [(2, b"two", "client", "ensure_prim", "/B")],
            producer_progress=(ProducerProgress("client", "session", 2),),
        )
        store.append(3, b"three", "client", "ensure_prim", "/C")
    finally:
        store.close()

    reopened = SqliteEventStore(db)
    try:
        assert reopened.get_all_asc() == [(1, b"one"), (2, b"two"), (3, b"three")]
        assert reopened.get_producer_progress("client", "session") == 2
        assert reopened.query(kind="ensure_prim", prim_contains="/C") == ([b"three"], 1)
    finally:
        reopened.close()


def test_grouped_records_and_multiple_producers_commit_atomically(tmp_path):
    store = SqliteEventStore(str(tmp_path / "group.db"))
    store.append_batch(
        [
            (1, b"one", "a", "ensure_prim", "/A"),
            (2, b"two", "b", "ensure_prim", "/B"),
        ],
        producer_progress=(
            ProducerProgress("a", "session", 1),
            ProducerProgress("b", "session", 1),
        ),
    )
    assert store.get_all_asc() == [(1, b"one"), (2, b"two")]
    assert store.get_producer_progress("a", "session") == 1
    assert store.get_producer_progress("b", "session") == 1

    with pytest.raises(sqlite3.IntegrityError):
        store.append_batch(
            [
                (3, b"rolled-back", "c", "ensure_prim", "/C"),
                (2, b"duplicate", "a", "ensure_prim", "/D"),
            ],
            producer_progress=(ProducerProgress("c", "session", 1),),
        )
    assert store.get_all_asc() == [(1, b"one"), (2, b"two")]
    assert store.get_producer_progress("c", "session") == 0
    store.close()


def test_layer_identity_updates_are_atomic_and_survive_log_rewrite(tmp_path):
    store = SqliteEventStore(str(tmp_path / "layer-identities.db"))
    first = LayerIdentity("/show/asset.usda", "layer:asset")
    store.append_batch(
        [(1, b"one", None, "layer_graph_state", None)],
        layer_identities=(first,),
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.append_batch(
            [(1, b"duplicate", None, "layer_graph_state", None)],
            layer_identities=(LayerIdentity("/show/other.usda", "layer:other"),),
        )

    assert store.get_layer_identities() == (first,)
    with pytest.raises(sqlite3.IntegrityError):
        store.append_batch(
            [],
            layer_identities=(LayerIdentity("/show/alias.usda", "layer:asset"),),
        )
    assert store.get_layer_identities() == (first,)
    with pytest.raises(sqlite3.IntegrityError, match="cannot be remapped"):
        store.append_batch(
            [],
            layer_identities=(LayerIdentity("/show/asset.usda", "layer:replacement"),),
        )
    assert store.get_layer_identities() == (first,)
    store.clear_and_rewrite([(1, b"compacted", None, "layer_graph_state", None)])
    assert store.get_layer_identities() == (first,)
    store.close()


def test_progress_is_scoped_by_authenticated_client(tmp_path):
    store = SqliteEventStore(str(tmp_path / "scope.db"))
    store.append_batch([], producer_progress=(ProducerProgress("a", "same", 7),))
    assert store.get_producer_progress("a", "same") == 7
    assert store.get_producer_progress("b", "same") == 0
    store.close()


def test_one_row_per_producer_session_not_one_row_per_transaction(tmp_path):
    store = SqliteEventStore(str(tmp_path / "bounded.db"))
    for txn_id in range(1, 101):
        store.append_batch(
            [],
            producer_progress=(ProducerProgress("client", "session", txn_id),),
        )
    row_count = store._conn.execute("SELECT COUNT(*) FROM producer_sessions").fetchone()[0]
    assert row_count == 1
    assert store.get_producer_progress("client", "session") == 100
    store.close()


def test_producer_progress_cannot_move_backwards(tmp_path):
    store = SqliteEventStore(str(tmp_path / "monotonic.db"))
    store.append_batch(
        [], producer_progress=(ProducerProgress("client", "session", 7),)
    )
    store.append_batch(
        [], producer_progress=(ProducerProgress("client", "session", 3),)
    )
    assert store.get_producer_progress("client", "session") == 7
    store.close()


def test_duplicate_returns_cumulative_highwater_without_reapplying(tmp_path):
    server = UsdSyncServer(log_path=str(tmp_path / "duplicate.db"), txn_batch_size=1)
    try:
        first = _commit(
            server, "/World/Once", client="client", session="producer", txn_id=1
        )
        duplicate = _commit(
            server, "/World/DifferentPayload", client="client", session="producer", txn_id=1
        )
        assert first.status == "committed"
        assert first.txn_id == 1
        assert duplicate.status == "duplicate"
        assert duplicate.txn_id == 1
        assert server.store.get_count() == 1
        assert not server.stage.GetPrimAtPath("/World/DifferentPayload").IsValid()
    finally:
        server.shutdown()
        server.store.close()


def test_gap_is_rejected_with_expected_transaction(tmp_path):
    server = UsdSyncServer(log_path=str(tmp_path / "gap.db"), txn_batch_size=1)
    try:
        with pytest.raises(TransactionRejectedError) as caught:
            _commit(server, "/World/Gap", client="client", session="producer", txn_id=2)
        assert caught.value.code == "unexpected_id"
        assert caught.value.expected_txn_id == 1
        assert server.store.get_count() == 0
    finally:
        server.shutdown()
        server.store.close()


@pytest.mark.parametrize("grouped", [False, True])
def test_producer_order_and_metadata_survive_duplicate_and_gap_requests(tmp_path, grouped):
    """A rejected gap cannot advance progress or change neighboring writes."""
    from openusdconnect.codec import message_to_dict

    server = UsdSyncServer(log_path=str(tmp_path / "ordering.db"), txn_batch_size=1)
    try:
        layer = server.get_or_create_client_layer("artist", "layout")
        requests = [
            TransactionRequest(
                events=[_event(f"/World/Edit{index}")], client_id="artist",
                session_id="session", txn_id=txn_id, layer=layer,
                origin="dcc", client_addr="127.0.0.1:7200",
            )
            for index, txn_id in enumerate((1, 1, 3, 2, 1))
        ]
        if grouped:
            outcomes = server._commit_managed_transaction_group(requests)
        else:
            outcomes = []
            for request in requests:
                try:
                    outcomes.append(server._process_idempotent_txn_now(request))
                except TransactionRejectedError as exc:
                    outcomes.append(exc)
        assert [outcomes[i].status for i in (0, 1, 3, 4)] == [
            "committed", "duplicate", "committed", "duplicate",
        ]
        assert [outcomes[i].txn_id for i in (0, 1, 3, 4)] == [1, 1, 2, 2]
        assert isinstance(outcomes[2], TransactionRejectedError)
        assert outcomes[2].expected_txn_id == 2
        assert server.store.get_producer_progress("artist", "session") == 2
        records = [message_to_dict(blob) for _seq, blob in server.store.get_all_asc()]
        assert [record["event"]["prim"] for record in records] == ["/World/Edit0", "/World/Edit3"]
        assert [record["seq"] for record in records] == [1, 2]
        for record in records:
            assert record["client_id"] == "artist"
            assert record["origin"] == "dcc"
            assert record["client"] == "127.0.0.1:7200"
            assert record["layer_key"] == server.layer_stack.key_for_layer(layer)
        assert layer.GetPrimAtPath("/World/Edit0") and layer.GetPrimAtPath("/World/Edit3")
        assert not any(server.stage.GetPrimAtPath(f"/World/Edit{i}") for i in (1, 2, 4))
    finally:
        server.shutdown()
        server.store.close()


@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("broadcast_fails", [False, True])
def test_payload_publication_keeps_commit_order_and_durable_outcomes(
    tmp_path, monkeypatch, grouped, broadcast_fails,
):
    server = UsdSyncServer(log_path=str(tmp_path / "publication.db"), txn_batch_size=1)
    try:
        server._commit_events([_event("/Payload")])
        observed = []
        broadcast = server.broadcast_transaction_group_views

        def publish(transactions):
            for records in transactions:
                for record, _encoded in records:
                    observed.append(record["event"]["prim"])
                    if broadcast_fails and record["event"]["k"] == "load_payload":
                        raise OSError("injected broadcast failure")
            broadcast(transactions)

        monkeypatch.setattr(server, "broadcast_transaction_group_views", publish)
        monkeypatch.setattr(
            server, "replay_children_after_load", lambda path: observed.append(path + "/Child"),
        )
        requests = [
            TransactionRequest(events=[event], client_id="client", session_id="s", txn_id=index)
            for index, event in enumerate([
                _event("/Before"), {"k": "load_payload", "prim": "/Payload"}, _event("/After"),
            ], start=1)
        ]
        outcomes = (
            # The coordinator closes each batch at a payload load.
            server._commit_managed_transaction_group(requests[:2])
            + server._commit_managed_transaction_group(requests[2:]) if grouped
            else [server._process_idempotent_txn_now(request) for request in requests]
        )
        assert observed == [
            "/Before", "/Payload", *([] if broadcast_fails else ["/Payload/Child"]), "/After",
        ]
        assert [outcome.status for outcome in outcomes] == ["committed"] * 3
        assert server.store.get_producer_progress("client", "s") == 3
        assert server.stage.GetPrimAtPath("/After")
    finally:
        server.shutdown()
        server.store.close()


@pytest.mark.parametrize("durability", ["strict", "realtime"])
def test_child_replay_preserves_routing_and_persists_before_publication(tmp_path, durability):
    from openusdconnect.codec import message_to_dict

    server = UsdSyncServer(
        log_path=str(tmp_path / "children.db"), durability=durability, wire_metrics=True,
    )
    try:
        for name in ("A", "B"):
            layer = server.get_or_create_client_layer(name, department=name)
            server._commit_events(
                [_event(f"/Payload/{name}")], client_id=name, origin=f"origin-{name}", layer=layer,
            )
        observed = []
        server.add_event_listener(
            lambda record: observed.append((record, server.store.get_max_seq())),
        )
        before_metrics = server.get_wire_metrics()["total_count"]

        server.replay_children_after_load("/Payload")

        replayed = [message_to_dict(blob) for blob in server.store.get_from_seq_bin(3)]
        assert [record["seq"] for record in replayed] == [3, 4]
        assert [record["event"]["prim"] for record in replayed] == ["/Payload/A", "/Payload/B"]
        assert [record["origin"] for record in replayed] == ["origin-A", "origin-B"]
        assert [record["layer_key"] for record in replayed] == ["department:A", "department:B"]
        assert all("client_id" not in record for record in replayed)
        assert [(record["seq"], head) for record, head in observed] == [(3, 4), (4, 4)]
        assert server.get_event_count() == 4
        assert server.get_wire_metrics()["total_count"] == before_metrics + 2
    finally:
        server.shutdown()
        server.store.close()


@pytest.mark.parametrize("batch_size", [1, 8])
@pytest.mark.parametrize("durability", ["strict", "realtime"])
def test_child_replay_storage_failure_preserves_parent_commit_and_sequence(
    tmp_path, monkeypatch, batch_size, durability,
):
    server = UsdSyncServer(
        log_path=str(tmp_path / "replay-failure.db"),
        txn_batch_size=batch_size, durability=durability,
    )
    try:
        server._commit_events([_event("/Payload"), _event("/Payload/Child")])
        append = server.store.append_batch
        observed = []
        server.add_event_listener(observed.append)

        def fail_replay(rows, **kwargs):
            if not kwargs.get("producer_progress"):
                raise OSError("replay persistence failed")
            return append(rows, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(server.store, "append_batch", fail_replay)
            outcome = server.process_idempotent_txn(
                [{"k": "load_payload", "prim": "/Payload"}],
                client_id="client", session_id="session", txn_id=1,
            )
        assert outcome.status == "committed"
        assert outcome.checkpoint.head_seq == 3
        assert server.producer_committed_through("client", "session") == 1
        assert server.get_replay_token()[1] == server.store.get_max_seq() == 3
        assert server.get_event_count() == server.store.get_count() == 3
        assert [record["seq"] for record in observed] == [3]

        following = _commit(server, "/Following", client="client", session="session", txn_id=2)
        assert following.status == "committed"
        assert following.records[0][0]["seq"] == 4
    finally:
        server.shutdown()
        server.store.close()


@pytest.mark.parametrize("paths,expected", [
    (["/Before", "/Payload", "/After"], ["/Before", "/Payload", "/Payload/Child", "/After"]),
    (["/Payload", "/After"], ["/Payload", "/Payload/Child", "/After"]),
    (["/Payload", "/Payload", "/After"],
     ["/Payload", "/Payload/Child", "/Payload", "/Payload/Child", "/After"]),
])
def test_queued_payload_replay_keeps_wire_sequences_in_commit_order(
    tmp_path, monkeypatch, paths, expected,
):
    import io

    from openusdconnect.codec import message_to_dict
    from openusdconnect.framing import recv_framed_rfile
    from openusdconnect.server.transactions import TransactionCoordinator
    from tests.helpers import ReceiverStub

    # Admit the whole batch before starting the worker, without a timing race.
    start = TransactionCoordinator.start
    with monkeypatch.context() as patch:
        patch.setattr(TransactionCoordinator, "start", lambda self: None)
        server = UsdSyncServer(log_path=str(tmp_path / "replay-order.db"), txn_batch_size=8)
    try:
        server._commit_events([_event("/Payload"), _event("/Payload/Child")])
        observed = []
        server.add_event_listener(observed.append)
        receiver = ReceiverStub()
        receiver.send_lock = threading.Lock()
        receiver.client_address = ("replay-order", 0)
        receiver.request = io.BytesIO()
        receiver.request.sendall = receiver.request.write
        server.receivers.add(receiver)
        requests = [
            server.submit_idempotent_txn(
                [{"k": "load_payload", "prim": path} if path == "/Payload" else _event(path)],
                client_id="client", session_id="session", txn_id=index,
            )
            for index, path in enumerate(paths, start=1)
        ]
        start(server._transactions)
        outcomes = [request.wait() for request in requests]
        assert [outcome.status for outcome in outcomes] == ["committed"] * len(paths)
        assert [record["event"]["prim"] for record in observed] == expected
        sequences = list(range(3, 3 + len(expected)))
        assert [record["seq"] for record in observed] == sequences
        assert server.store.get_max_seq() == sequences[-1]
        server._broadcast_queue.join()
        receiver.request.seek(0)
        wire = [message_to_dict(recv_framed_rfile(receiver.request)) for _ in expected]
        assert [record["seq"] for record in wire] == sequences
        assert receiver.request.read() == b""
    finally:
        server.shutdown()
        server.store.close()


def test_invalid_transaction_is_rejected_before_queue_or_sequence_reservation(tmp_path):
    server = UsdSyncServer(log_path=str(tmp_path / "invalid.db"), txn_batch_size=1)
    try:
        with pytest.raises(ValueError, match="transform fields"):
            server.process_idempotent_txn(
                [{"k": "set_xform_trs", "prim": "/World/X", "fields": ["bogus"]}],
                client_id="client",
                session_id="producer",
                txn_id=1,
            )
        assert server.store.get_count() == 0
        assert server.store.get_producer_progress("client", "producer") == 0
        assert server.get_replay_token()[1] == 0
    finally:
        server.shutdown()
        server.store.close()


def test_progress_and_authoritative_stage_survive_restart(tmp_path):
    db = str(tmp_path / "restart.db")
    first = UsdSyncServer(log_path=db, txn_batch_size=1)
    try:
        _commit(first, "/World/Restarted", client="client", session="producer", txn_id=1)
    finally:
        first.shutdown()
        first.store.close()

    restarted = UsdSyncServer(log_path=db, txn_batch_size=1)
    try:
        duplicate = _commit(
            restarted,
            "/World/ShouldNotApply",
            client="client",
            session="producer",
            txn_id=1,
        )
        assert duplicate.status == "duplicate"
        assert duplicate.txn_id == 1
        assert restarted.stage.GetPrimAtPath("/World/Restarted").IsValid()
        assert not restarted.stage.GetPrimAtPath("/World/ShouldNotApply").IsValid()
    finally:
        restarted.shutdown()
        restarted.store.close()


def test_compaction_and_purge_preserve_progress_without_stale_sequence_tokens(tmp_path):
    server = UsdSyncServer(log_path=str(tmp_path / "maintenance.db"), txn_batch_size=1)
    try:
        _commit(server, "/World/Before", client="client", session="producer", txn_id=1)
        server.compact_log()
        duplicate = _commit(
            server, "/World/NoCompactReplay", client="client", session="producer", txn_id=1
        )
        assert duplicate.txn_id == 1

        server.purge()
        assert not server.stage.GetPrimAtPath("/World/Before").IsValid()
        duplicate = _commit(
            server, "/World/NoPurgeReplay", client="client", session="producer", txn_id=1
        )
        assert duplicate.status == "duplicate"
        committed = _commit(
            server, "/World/After", client="client", session="producer", txn_id=2
        )
        assert committed.status == "committed"
        assert server.stage.GetPrimAtPath("/World/After").IsValid()
    finally:
        server.shutdown()
        server.store.close()


def test_store_failure_rolls_back_usd_sequence_and_progress(tmp_path, monkeypatch):
    server = UsdSyncServer(log_path=str(tmp_path / "rollback.db"), txn_batch_size=1)
    append_batch = server.store.append_batch
    failed = False

    def fail_once(records, *, producer_progress=()):
        nonlocal failed
        if producer_progress and not failed:
            failed = True
            raise sqlite3.OperationalError("injected commit failure")
        return append_batch(records, producer_progress=producer_progress)

    monkeypatch.setattr(server.store, "append_batch", fail_once)
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            _commit(server, "/World/Rollback", client="client", session="producer", txn_id=1)
        assert not server.stage.GetPrimAtPath("/World/Rollback").IsValid()
        assert server.store.get_count() == 0
        assert server.store.get_producer_progress("client", "producer") == 0
        assert server.get_replay_token()[1] == 0

        committed = _commit(
            server, "/World/Rollback", client="client", session="producer", txn_id=1
        )
        assert committed.status == "committed"
    finally:
        server.shutdown()
        server.store.close()


def test_store_failure_does_not_publish_prim_or_instancing_indexes(
    tmp_path, monkeypatch
):
    server = UsdSyncServer(
        log_path=str(tmp_path / "tracking-rollback.db"),
        txn_batch_size=1,
    )
    append_batch = server.store.append_batch
    failed = False

    def fail_once(records, *, producer_progress=()):
        nonlocal failed
        if producer_progress and not failed:
            failed = True
            raise sqlite3.OperationalError("injected tracking persistence failure")
        return append_batch(records, producer_progress=producer_progress)

    events = [
        {"k": "ensure_prim", "prim": "/World/Rollback", "typeName": "PointInstancer"},
        {"k": "set_instanceable", "prim": "/World/Rollback", "instanceable": True},
    ]
    before = (server.get_prim_tree(), server.get_prim_count(), server.get_instance_count())
    monkeypatch.setattr(server.store, "append_batch", fail_once)
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected tracking"):
            server.process_idempotent_txn(
                events,
                client_id="client",
                session_id="producer",
                txn_id=1,
            )

        assert not server.stage.GetPrimAtPath("/World/Rollback").IsValid()
        assert (
            server.get_prim_tree(), server.get_prim_count(), server.get_instance_count()
        ) == before

        committed = server.process_idempotent_txn(
            events,
            client_id="client",
            session_id="producer",
            txn_id=1,
        )
        assert committed.status == "committed"
        prim = next(row for row in server.get_prim_tree() if row["path"] == "/World/Rollback")
        assert prim["typeName"] == "PointInstancer"
        assert prim["instanceable"] and prim["is_point_instancer"]
        assert server.get_instance_count() == 1
    finally:
        server.shutdown()
        server.store.close()


def test_private_commit_helper_rolls_back_usd_and_sequence(tmp_path, monkeypatch):
    server = UsdSyncServer(log_path=str(tmp_path / "private-rollback.db"))
    append_batch = server.store.append_batch
    try:
        server._commit_events([_event("/World/Old")], client_id="seed")
        before = server.edit_layer.ExportToString()

        def fail_once(records, *, producer_progress=()):
            raise sqlite3.OperationalError("injected private commit failure")

        monkeypatch.setattr(server.store, "append_batch", fail_once)
        with pytest.raises(sqlite3.OperationalError, match="injected private"):
            server._commit_events(
                [{"k": "rename_prim", "prim": "/World/Old", "new_name": "New"}],
                client_id="maintenance",
            )

        assert server.edit_layer.ExportToString() == before
        assert server.stage.GetPrimAtPath("/World/Old").IsValid()
        assert not server.stage.GetPrimAtPath("/World/New").IsValid()
        assert server.store.get_count() == 1
        assert server.get_replay_token()[1] == 1

        monkeypatch.setattr(server.store, "append_batch", append_batch)
        records = server._commit_events([_event("/World/After")], client_id="maintenance")
        assert records[0][0]["seq"] == 2
    finally:
        server.shutdown()
        server.store.close()


def test_private_commit_cannot_overtake_failed_identified_commit(tmp_path, monkeypatch):
    server = UsdSyncServer(
        log_path=str(tmp_path / "private-serialized.db"),
        txn_batch_size=1,
    )
    append_batch = server.store.append_batch
    identified_persisting = threading.Event()
    release_failure = threading.Event()
    private_started = threading.Event()
    private_persisting = threading.Event()
    identified_errors = []
    private_errors = []
    private_records = []

    def controlled_append(records, *, producer_progress=()):
        if producer_progress:
            identified_persisting.set()
            assert release_failure.wait(timeout=5)
            raise sqlite3.OperationalError("injected identified commit failure")
        private_persisting.set()
        return append_batch(records, producer_progress=producer_progress)

    def commit_identified():
        try:
            _commit(
                server,
                "/World/Rejected",
                client="client",
                session="session",
                txn_id=1,
            )
        except BaseException as exc:
            identified_errors.append(exc)

    def commit_private():
        private_started.set()
        try:
            private_records.extend(
                server._commit_events([_event("/World/Private")], client_id="maintenance")
            )
        except BaseException as exc:
            private_errors.append(exc)

    monkeypatch.setattr(server.store, "append_batch", controlled_append)
    identified = threading.Thread(target=commit_identified)
    private = threading.Thread(target=commit_private)
    try:
        identified.start()
        assert identified_persisting.wait(timeout=5)
        private.start()
        assert private_started.wait(timeout=5)
        assert not private_persisting.wait(timeout=0.1)

        release_failure.set()
        identified.join(timeout=5)
        private.join(timeout=5)
        assert not identified.is_alive()
        assert not private.is_alive()

        assert len(identified_errors) == 1
        assert isinstance(identified_errors[0], sqlite3.OperationalError)
        assert private_errors == []
        assert private_records[0][0]["seq"] == 1
        assert [seq for seq, _payload in server.store.get_all_asc()] == [1]
        assert not server.stage.GetPrimAtPath("/World/Rejected").IsValid()
        assert server.stage.GetPrimAtPath("/World/Private").IsValid()
        assert server.get_replay_token()[1] == 1
    finally:
        release_failure.set()
        identified.join(timeout=5)
        private.join(timeout=5)
        server.shutdown()
        server.store.close()


def test_store_failure_rolls_back_both_rename_paths(tmp_path, monkeypatch):
    server = UsdSyncServer(log_path=str(tmp_path / "rename-rollback.db"), txn_batch_size=1)
    append_batch = server.store.append_batch

    try:
        _commit(server, "/World/Old", client="client", session="producer", txn_id=1)
        before = server.edit_layer.ExportToString()

        def fail_rename(records, *, producer_progress=()):
            if producer_progress:
                raise sqlite3.OperationalError("injected rename persistence failure")
            return append_batch(records, producer_progress=producer_progress)

        monkeypatch.setattr(server.store, "append_batch", fail_rename)
        with pytest.raises(sqlite3.OperationalError, match="injected rename"):
            server.process_idempotent_txn(
                [{"k": "rename_prim", "prim": "/World/Old", "new_name": "New"}],
                client_id="client",
                session_id="producer",
                txn_id=2,
            )

        assert server.edit_layer.ExportToString() == before
        assert server.stage.GetPrimAtPath("/World/Old").IsValid()
        assert not server.stage.GetPrimAtPath("/World/New").IsValid()
        assert server.store.get_count() == 1
        assert server.store.get_producer_progress("client", "producer") == 1
        assert server.get_replay_token()[1] == 1
    finally:
        server.shutdown()
        server.store.close()


def test_group_store_failure_rolls_back_both_rename_paths(tmp_path, monkeypatch):
    server = UsdSyncServer(log_path=str(tmp_path / "group-rename-rollback.db"))
    append_batch = server.store.append_batch
    server.apply_txn([_event("/World/Old")])
    before = server.edit_layer.ExportToString()
    before_tracking = (server.get_prim_tree(), server.get_prim_count(), server.get_instance_count())

    def fail_group(_records, *, producer_progress=()):
        raise sqlite3.OperationalError("injected grouped persistence failure")

    monkeypatch.setattr(server.store, "append_batch", fail_group)
    requests = [
        TransactionRequest(
            events=[{"k": "rename_prim", "prim": "/World/Old", "new_name": "New"}],
            session_id="session",
            txn_id=1,
            client_id="rename-client",
            origin=None,
            client_addr=None,
            layer=None,
            layer_key="",
        ),
        TransactionRequest(
            events=[_event("/World/Other")],
            session_id="session",
            txn_id=1,
            client_id="other-client",
            origin=None,
            client_addr=None,
            layer=None,
            layer_key="",
        ),
    ]
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected grouped"):
            server._commit_managed_transaction_group(requests)

        assert server.edit_layer.ExportToString() == before
        assert server.stage.GetPrimAtPath("/World/Old").IsValid()
        assert not server.stage.GetPrimAtPath("/World/New").IsValid()
        assert not server.stage.GetPrimAtPath("/World/Other").IsValid()
        assert (
            server.get_prim_tree(), server.get_prim_count(), server.get_instance_count()
        ) == before_tracking
        assert server.store.get_count() == 0
        assert server.get_replay_token()[1] == 0
        for request in requests:
            assert server.producer_committed_through(request.client_id, request.session_id) == 0

        monkeypatch.setattr(server.store, "append_batch", append_batch)
        outcomes = server._commit_managed_transaction_group(requests)
        assert [outcome.status for outcome in outcomes] == ["committed", "committed"]
        assert all(request.commit is None and request.error is None for request in requests)
        assert all(not request.done.is_set() for request in requests)
        assert not server.stage.GetPrimAtPath("/World/Old").IsValid()
        assert server.stage.GetPrimAtPath("/World/New").IsValid()
        assert server.stage.GetPrimAtPath("/World/Other").IsValid()
        assert server.store.get_count() == 2
        for request in requests:
            assert server.producer_committed_through(request.client_id, request.session_id) == 1
            assert server.store.get_producer_progress(request.client_id, request.session_id) == 1

        outcomes = server._commit_managed_transaction_group(requests)
        assert [outcome.status for outcome in outcomes] == ["duplicate", "duplicate"]
        assert server.store.get_count() == 2
        assert server.get_replay_token()[1] == 2
    finally:
        server.shutdown()
        server.store.close()


def test_concurrent_producers_share_one_durable_group(tmp_path, monkeypatch):
    server = UsdSyncServer(
        log_path=str(tmp_path / "batched.db"),
        txn_batch_size=8,
        txn_batch_delay=0.05,
    )
    grouped_progress_counts = []
    append_batch = server.store.append_batch

    def observe(records, *, producer_progress=()):
        if producer_progress:
            grouped_progress_counts.append(len(producer_progress))
        return append_batch(records, producer_progress=producer_progress)

    monkeypatch.setattr(server.store, "append_batch", observe)
    try:
        results, errors = _run_concurrently(
            [
                lambda: _commit(server, "/World/A", client="a", session="s", txn_id=1),
                lambda: _commit(server, "/World/B", client="b", session="s", txn_id=1),
            ]
        )
        assert errors == [None, None]
        assert [result.status for result in results] == ["committed", "committed"]
        assert 2 in grouped_progress_counts
        assert server.store.get_count() == 2
    finally:
        server.shutdown()
        server.store.close()


def test_same_session_pipeline_advances_one_cumulative_progress_value(tmp_path):
    server = UsdSyncServer(
        log_path=str(tmp_path / "pipeline.db"),
        txn_batch_size=8,
        txn_batch_delay=0.05,
    )
    try:
        first = server.submit_idempotent_txn(
            [_event("/World/One")], client_id="client", session_id="session", txn_id=1
        )
        second = server.submit_idempotent_txn(
            [_event("/World/Two")], client_id="client", session_id="session", txn_id=2
        )
        assert server.wait_for_transaction(first).txn_id == 1
        assert server.wait_for_transaction(second).txn_id == 2
        assert server.store.get_producer_progress("client", "session") == 2
        assert server.store._conn.execute(
            "SELECT COUNT(*) FROM producer_sessions"
        ).fetchone()[0] == 1
    finally:
        server.shutdown()
        server.store.close()


def test_server_caches_durable_progress_after_first_store_lookup(tmp_path, monkeypatch):
    server = UsdSyncServer(log_path=str(tmp_path / "cached.db"), txn_batch_size=1)
    lookup = server.store.get_producer_progress
    lookup_count = 0

    def counted(client_id, session_id):
        nonlocal lookup_count
        lookup_count += 1
        return lookup(client_id, session_id)

    monkeypatch.setattr(server.store, "get_producer_progress", counted)
    try:
        _commit(server, "/World/One", client="client", session="session", txn_id=1)
        _commit(server, "/World/Two", client="client", session="session", txn_id=2)
        duplicate = _commit(
            server, "/World/Ignored", client="client", session="session", txn_id=1
        )
        assert duplicate.txn_id == 2
        assert server.producer_committed_through("client", "session") == 2
        assert lookup_count == 1
    finally:
        server.shutdown()
        server.store.close()
