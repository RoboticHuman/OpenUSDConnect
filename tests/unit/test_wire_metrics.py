"""Opt-in per-event-kind wire byte counters on the sync server."""

from openusdconnect.protocol_constants import MSG_RESYNC
from openusdconnect.server.state import UsdSyncServer

EVENTS = [
    {"k": "ensure_prim", "prim": "/World/A", "typeName": "Xform"},
    {"k": "ensure_xform_ops", "prim": "/World/A"},
    {"k": "set_xform_trs", "prim": "/World/A", "fields": ["t"], "t": [1.0, 2.0, 3.0]},
    {"k": "set_xform_trs", "prim": "/World/A", "fields": ["t"], "t": [4.0, 5.0, 6.0]},
]


def test_disabled_by_default():
    s = UsdSyncServer(log_path=":memory:")
    assert s.wire_metrics is None
    s.process_txn(EVENTS, client_id="c1", origin="o1", client_addr="a:1")
    assert s.get_wire_metrics() == {"enabled": False}


def test_records_per_kind_on_txn():
    s = UsdSyncServer(log_path=":memory:", wire_metrics=True)
    s.process_txn(EVENTS, client_id="c1", origin="o1", client_addr="a:1")
    m = s.get_wire_metrics()
    assert m["enabled"] is True
    kinds = m["kinds"]
    assert kinds["ensure_prim"]["count"] == 1
    assert kinds["ensure_xform_ops"]["count"] == 1
    assert kinds["set_xform_trs"]["count"] == 2
    assert all(v["bytes"] > 0 for v in kinds.values())
    assert m["total_count"] == len(EVENTS)
    assert m["total_bytes"] == sum(v["bytes"] for v in kinds.values())


def test_records_server_broadcasts_by_message_type():
    s = UsdSyncServer(log_path=":memory:", wire_metrics=True)
    s.broadcast({"type": MSG_RESYNC, "reason": "test"})
    kinds = s.get_wire_metrics()["kinds"]
    assert kinds[MSG_RESYNC]["count"] == 1
    assert kinds[MSG_RESYNC]["bytes"] > 0
