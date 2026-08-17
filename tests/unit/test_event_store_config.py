"""SQLite event-store configuration coverage."""

from openusdconnect.event_store import SqliteEventStore


def test_sqlite_keeps_strict_durability_and_defers_wal_checkpoints(tmp_path):
    store = SqliteEventStore(str(tmp_path / "events.db"))
    connection = store._conn

    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    assert connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == max(
        1000,
        (32 * 1024 * 1024) // page_size,
    )
    store.close()


def test_sqlite_creates_only_the_queried_event_indexes(tmp_path):
    store = SqliteEventStore(str(tmp_path / "events.db"))
    indexes = {
        row[1] for row in store._conn.execute("PRAGMA index_list(events)").fetchall()
    }

    assert indexes == {"idx_events_kind", "idx_events_prim"}
    store.close()
