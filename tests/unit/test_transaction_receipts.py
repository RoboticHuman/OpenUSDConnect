"""Cumulative producer progress and atomic failure-boundary coverage."""

import sqlite3
import threading

import pytest

from openusdconnect.event_store import LayerIdentity, ProducerProgress, SqliteEventStore
from openusdconnect.server.state import UsdSyncServer, _TransactionRequest
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
        assert server._next_seq == 1

        committed = _commit(
            server, "/World/Rollback", client="client", session="producer", txn_id=1
        )
        assert committed.status == "committed"
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
        assert server._next_seq == 2

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
        assert server._next_seq == 2
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
        assert server._next_seq == 2
    finally:
        server.shutdown()
        server.store.close()


def test_group_store_failure_rolls_back_both_rename_paths(tmp_path, monkeypatch):
    server = UsdSyncServer(log_path=str(tmp_path / "group-rename-rollback.db"))
    server.apply_txn([_event("/World/Old")])
    before = server.edit_layer.ExportToString()

    def fail_group(_records, *, producer_progress=()):
        raise sqlite3.OperationalError("injected grouped persistence failure")

    monkeypatch.setattr(server.store, "append_batch", fail_group)
    requests = [
        _TransactionRequest(
            events=[{"k": "rename_prim", "prim": "/World/Old", "new_name": "New"}],
            session_id="session",
            txn_id=1,
            client_id="rename-client",
            origin=None,
            client_addr=None,
            layer=None,
            layer_key="",
        ),
        _TransactionRequest(
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
        assert server.store.get_count() == 0
        assert server._next_seq == 1
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
