"""Event-log storage reclaim: store contract, server gating, rewrite hooks."""

import os
import sqlite3
import time

import pytest

from openusdconnect.event_store import SqliteEventStore
from openusdconnect.server.state import UsdSyncServer

BLOB = os.urandom(200_000)


def _disk_size(db):
    return sum(
        os.path.getsize(db + s)
        for s in ("", "-wal", "-shm")
        if os.path.exists(db + s)
    )


def _fill(store, n=50):
    store.append_batch([(i + 1, BLOB, None, "set_gprim_attrs", "/W/M") for i in range(n)])


def test_sqlite_reclaim_shrinks_file(tmp_path):
    db = str(tmp_path / "reclaim.db")
    store = SqliteEventStore(db)
    _fill(store)
    store.clear_and_rewrite([(1, b"tiny", None, "ensure_prim", "/W")])

    grown = _disk_size(db)
    assert grown > 5_000_000, f"expected a grown file, got {grown}"

    reclaimed = store.reclaim_storage()
    shrunk = _disk_size(db)
    assert reclaimed > 4_000_000
    assert shrunk < 1_000_000, f"file still {shrunk} bytes after reclaim"

    rows = store.get_all_asc()
    assert rows == [(1, b"tiny")]
    store.close()


def test_memory_store_reclaim_is_noop():
    store = SqliteEventStore(":memory:")
    _fill(store, n=3)
    assert store.reclaim_storage() == 0
    assert store.get_count() == 3
    store.close()


def test_clear_and_rewrite_rolls_back_failed_replacement():
    store = SqliteEventStore(":memory:")
    store.append(1, b"old", "client", "ensure_prim", "/Old")

    with pytest.raises(sqlite3.IntegrityError):
        store.clear_and_rewrite(
            [
                (2, b"new-a", None, "ensure_prim", "/NewA"),
                (2, b"new-b", None, "ensure_prim", "/NewB"),
            ]
        )

    assert store.get_all_asc() == [(1, b"old")]
    store.close()


def _heavy_server(tmp_path, name, **kw):
    db = str(tmp_path / name)
    srv = UsdSyncServer(log_path=db, **kw)
    events = [{"k": "ensure_prim", "prim": "/World/M", "typeName": "Mesh"}]
    events += [
        {"k": "set_gprim_attrs", "prim": "/World/M",
         "attrs": {"points": [[float(i), 0.0, 0.0]] * 3000}}
        for i in range(40)
    ]
    srv.process_txn(events, client_id="c", origin="o", client_addr="a:1")
    return srv, db


def test_disabled_by_default_keeps_freed_pages(tmp_path):
    srv, db = _heavy_server(tmp_path, "off.db")
    try:
        grown = _disk_size(db)
        srv.compact_log()
        assert srv.get_event_count() == 2
        assert _disk_size(db) > grown / 2, "reclaimed despite being disabled"
    finally:
        srv.shutdown()
        srv.store.close()


def test_compaction_reclaims_when_interval_elapsed(tmp_path):
    srv, db = _heavy_server(tmp_path, "on.db", reclaim_interval=3600)
    try:
        grown = _disk_size(db)
        assert grown > 1_000_000
        srv._last_reclaim = time.monotonic() - 7200
        srv.compact_log()
        assert _disk_size(db) < grown / 4, "compaction did not reclaim"
    finally:
        srv.shutdown()
        srv.store.close()


def test_compaction_skips_reclaim_within_interval(tmp_path):
    srv, db = _heavy_server(tmp_path, "within.db", reclaim_interval=3600)
    try:
        grown = _disk_size(db)
        srv.compact_log()
        assert _disk_size(db) > grown / 2, "reclaimed before the interval elapsed"
    finally:
        srv.shutdown()
        srv.store.close()


def test_purge_reclaims_when_due(tmp_path):
    srv, db = _heavy_server(tmp_path, "purge.db")
    try:
        grown = _disk_size(db)
        srv.set_reclaim_interval(3600)
        srv._last_reclaim = time.monotonic() - 7200
        srv.purge()
        assert srv.get_event_count() == 0
        assert _disk_size(db) < grown / 4, "purge did not reclaim"
    finally:
        srv.shutdown()
        srv.store.close()
