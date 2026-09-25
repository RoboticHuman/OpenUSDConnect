"""Persistence-worker recovery and shutdown behavior."""

import threading

import pytest

from openusdconnect.event_store import SqliteEventStore
from openusdconnect.server.journal import EventJournal


def test_commit_scope_preserves_order_through_publication_after_rollback(tmp_path):
    store = SqliteEventStore(str(tmp_path / "ordered.db"))
    journal = EventJournal(store, durability="strict", metrics=None)
    attempted = threading.Event()
    entered = threading.Event()
    next_sequences = []

    def next_commit():
        attempted.set()
        with journal.commit_scope():
            next_sequences.append(journal.assign_seq())
            entered.set()

    worker = threading.Thread(target=next_commit, daemon=True)
    try:
        with journal.commit_scope():
            with pytest.raises(ValueError, match="rejected"):
                with journal.reserve_sequences():
                    assert journal.assign_seq() == 1
                    worker.start()
                    assert attempted.wait(5)
                    raise ValueError("rejected")
            # Reservation cleanup must not release the outer publication boundary.
            assert not entered.wait(0.05)
        assert entered.wait(5)
        worker.join(5)
        assert next_sequences == [1]
    finally:
        worker.join(5)
        journal.shutdown()
        store.close()


def test_realtime_shutdown_drains_after_a_failed_write(tmp_path, monkeypatch):
    store = SqliteEventStore(str(tmp_path / "events.db"))
    journal = EventJournal(store, durability="realtime", metrics=None)
    append = store.append_batch
    attempted = []

    def fail_first(rows, **kwargs):
        attempted.append(rows[0][0])
        if len(attempted) == 1:
            raise OSError("injected asynchronous write failure")
        return append(rows, **kwargs)

    monkeypatch.setattr(store, "append_batch", fail_first)
    journal.start()
    try:
        for path in ("/Failed", "/Persisted"):
            encoded = journal.encode_events(
                [("default", {"k": "ensure_prim", "prim": path, "typeName": "Xform"})],
                client_id=None, origin=None, client_addr=None,
            )
            journal.append_batch(encoded.store_rows)
        journal.shutdown()
        journal.shutdown()

        assert attempted == [1, 2]
        assert store.get_count() == journal.event_count == 1
        assert store.get_max_seq() == 2
        assert not journal.running

        drained = threading.Event()

        def drain():
            journal.drain()
            drained.set()

        waiter = threading.Thread(target=drain, daemon=True)
        waiter.start()
        assert drained.wait(2), "shutdown left persistence work permanently pending"
        waiter.join()
    finally:
        journal.shutdown()
        store.close()
