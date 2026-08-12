"""Periodic event-log compaction: startup interval, runtime control, idle skip."""

import time

import pytest

from openusdconnect.server.state import UsdSyncServer

# Every test here exercises the background compaction thread against real
# tick intervals, so the file is inherently timing-dependent.
pytestmark = pytest.mark.slow

EVENTS = [
    {"k": "ensure_prim", "prim": "/World/A", "typeName": "Xform"},
    {"k": "ensure_xform_ops", "prim": "/World/A"},
] + [
    {"k": "set_xform_trs", "prim": "/World/A", "fields": ["t"],
     "t": [float(i), 0.0, 0.0]}
    for i in range(30)
]


def _instrument(srv):
    """Count COMPLETED compact_log runs while preserving behavior.

    Recording after the call returns lets tests assert post-compaction
    state as soon as the counter moves.
    """
    calls = []
    original = srv.compact_log

    def counting():
        original()
        calls.append(time.monotonic())

    srv.compact_log = counting
    return calls


def _poll(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_periodic_compaction_compacts_and_then_idles(tmp_path):
    srv = UsdSyncServer(log_path=str(tmp_path / "pc.db"), compact_interval=0.2)
    calls = _instrument(srv)
    try:
        srv._commit_events(EVENTS, client_id="c", origin="o", client_addr="a:1")
        before = srv.get_event_count()
        assert before == len(EVENTS)

        assert _poll(lambda: srv.get_event_count() < before), "never compacted"
        # 30 TRS on one prim collapse into one merged event.
        assert srv.get_event_count() == 3

        # Idle: no new events, so further ticks must skip.
        settled = len(calls)
        time.sleep(0.45)
        assert len(calls) == settled
    finally:
        srv.shutdown()
        srv.store.close()


def test_runtime_enable_and_disable(tmp_path):
    srv = UsdSyncServer(log_path=str(tmp_path / "rt.db"))
    calls = _instrument(srv)
    try:
        assert srv._compact_thread is None
        srv._commit_events(EVENTS, client_id="c", origin="o", client_addr="a:1")

        srv.set_compact_interval(0.2)
        assert _poll(lambda: len(calls) >= 1), "runtime enable never compacted"
        assert srv.get_event_count() == 3

        srv.set_compact_interval(0)
        time.sleep(0.25)  # drain a tick already in flight before the disable
        settled = len(calls)
        srv._commit_events(
            [{"k": "set_xform_trs", "prim": "/World/A", "fields": ["t"],
              "t": [99.0, 0.0, 0.0]}],
            client_id="c", origin="o", client_addr="a:1",
        )
        time.sleep(0.45)
        assert len(calls) == settled, "compacted while disabled"
    finally:
        srv.shutdown()
        srv.store.close()


def test_txn_during_compaction_survives_via_delta_merge(tmp_path):
    """An event arriving while phase 1 runs is merged in phase 2, not lost."""
    import threading

    srv = UsdSyncServer(log_path=str(tmp_path / "delta.db"))
    try:
        srv._commit_events(EVENTS, client_id="c", origin="o", client_addr="a:1")

        phase1_started = threading.Event()
        original_build = srv._build_compacted

        def slow_build(rows):
            phase1_started.set()
            time.sleep(0.4)
            return original_build(rows)

        srv._build_compacted = slow_build

        compactor = threading.Thread(target=srv.compact_log)
        compactor.start()
        assert phase1_started.wait(timeout=5)

        # The private in-process helper owns the same barrier as network work.
        srv._commit_events(
            [{"k": "set_xform_trs", "prim": "/World/A", "fields": ["t"],
              "t": [123.0, 0.0, 0.0]}],
            client_id="c", origin="o", client_addr="a:1",
        )
        compactor.join(timeout=10)
        assert not compactor.is_alive(), "compaction deadlocked"

        assert srv.get_event_count() == 3
        srv.store.close()

        # The compacted log must replay to the mid-compaction value.
        srv2 = UsdSyncServer(log_path=str(tmp_path / "delta.db"))
        from pxr import UsdGeom

        prim = srv2.stage.GetPrimAtPath("/World/A")
        for op in UsdGeom.Xformable(prim).GetOrderedXformOps():
            if op.GetAttr().GetName() == "xformOp:translate":
                assert op.Get()[0] == 123.0
                break
        else:
            raise AssertionError("no translate op after replay")
        srv2.shutdown()
        srv2.store.close()
    finally:
        srv.shutdown()


def test_shorter_interval_takes_effect_promptly(tmp_path):
    """Shortening a long interval must not wait out the old period."""
    srv = UsdSyncServer(log_path=str(tmp_path / "short.db"), compact_interval=300)
    calls = _instrument(srv)
    try:
        srv._commit_events(EVENTS, client_id="c", origin="o", client_addr="a:1")
        srv.set_compact_interval(0.2)
        assert _poll(lambda: len(calls) >= 1, timeout=3), (
            "still waiting out the previous 300s period"
        )
    finally:
        srv.shutdown()
        srv.store.close()


def test_startup_compacts_preexisting_log(tmp_path):
    """A server started with an interval compacts an inherited backlog."""
    db = str(tmp_path / "pre.db")
    srv1 = UsdSyncServer(log_path=db)
    srv1._commit_events(EVENTS, client_id="c", origin="o", client_addr="a:1")
    srv1.shutdown()
    srv1.store.close()

    srv2 = UsdSyncServer(log_path=db, compact_interval=0.2)
    try:
        assert _poll(lambda: srv2.get_event_count() == 3), (
            f"backlog never compacted (count={srv2.get_event_count()})"
        )
    finally:
        srv2.shutdown()
        srv2.store.close()


def test_shutdown_is_fast_with_long_interval(tmp_path):
    srv = UsdSyncServer(log_path=str(tmp_path / "slow.db"), compact_interval=3600)
    t0 = time.monotonic()
    srv.shutdown()
    assert time.monotonic() - t0 < 5, "shutdown blocked on the compaction wait"
    srv.store.close()


def test_negative_interval_clamps_to_disabled(tmp_path):
    srv = UsdSyncServer(log_path=str(tmp_path / "neg.db"))
    try:
        srv.set_compact_interval(-5)
        assert srv.get_compact_interval() == 0
    finally:
        srv.shutdown()
        srv.store.close()


def test_purge_resets_idle_marker(tmp_path):
    """After purge nothing is pending, so periodic ticks keep skipping."""
    srv = UsdSyncServer(log_path=str(tmp_path / "purge.db"), compact_interval=0.2)
    calls = _instrument(srv)
    try:
        srv._commit_events(EVENTS, client_id="c", origin="o", client_addr="a:1")
        assert _poll(lambda: len(calls) >= 1)
        srv.purge()
        time.sleep(0.25)  # drain a tick already in flight before the purge
        settled = len(calls)
        time.sleep(0.45)
        assert len(calls) == settled, "compacted an empty purged log"
        assert srv.get_event_count() == 0
    finally:
        srv.shutdown()
        srv.store.close()
