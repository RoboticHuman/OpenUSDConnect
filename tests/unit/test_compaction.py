"""Tests for server event log compaction."""

from openusdconnect.codec import message_to_dict
from openusdconnect.protocol_constants import (
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_RENAME_PRIM,
    K_SET_REFERENCE,
    K_SET_VISIBILITY,
    K_SET_XFORM_TRS,
    MSG_EVENT,
)
from openusdconnect.server import UsdSyncServer


def _make_server(tmp_path):
    db_path = str(tmp_path / "test.db")
    return UsdSyncServer(log_path=db_path)


def _inject_events(server, events):
    """Insert raw events into the server's DB."""
    for ev in events:
        seq = server.assign_seq()
        rec = {"type": MSG_EVENT, "seq": seq, "event": ev}
        server.append_log(rec)


def _read_log(server):
    """Read all events from the DB, return list of event dicts."""
    rows = server.store.get_all_asc()
    result = []
    for _seq, record_bin in rows:
        rec = message_to_dict(record_bin)
        result.append(rec.get("event", rec))
    return result


class TestCompaction:
    def test_trs_merged(self, tmp_path):
        """Multiple set_xform_trs for same prim collapse to one with merged fields."""
        srv = _make_server(tmp_path)
        _inject_events(
            srv,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Xform"},
                {"k": K_SET_XFORM_TRS, "prim": "/World/A", "fields": ["t"], "t": [1, 0, 0]},
                {"k": K_SET_XFORM_TRS, "prim": "/World/A", "fields": ["r"], "r": [1, 0, 0, 0]},
                {"k": K_SET_XFORM_TRS, "prim": "/World/A", "fields": ["t"], "t": [2, 0, 0]},
            ],
        )

        srv.compact_log()
        events = _read_log(srv)

        trs = [e for e in events if e["k"] == K_SET_XFORM_TRS]
        assert len(trs) == 1
        assert set(trs[0]["fields"]) == {"t", "r"}
        assert trs[0]["t"] == [2, 0, 0]
        assert trs[0]["r"] == [1, 0, 0, 0]

    def test_visibility_latest_wins(self, tmp_path):
        """Multiple set_visibility for same prim keeps only the last."""
        srv = _make_server(tmp_path)
        _inject_events(
            srv,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Xform"},
                {"k": K_SET_VISIBILITY, "prim": "/World/A", "visible": False},
                {"k": K_SET_VISIBILITY, "prim": "/World/A", "visible": True},
            ],
        )

        srv.compact_log()
        events = _read_log(srv)

        vis = [e for e in events if e["k"] == K_SET_VISIBILITY]
        assert len(vis) == 1
        assert vis[0]["visible"] is True

    def test_delete_tombstones_prim(self, tmp_path):
        """delete_prim removes all earlier events for that prim."""
        srv = _make_server(tmp_path)
        _inject_events(
            srv,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Xform"},
                {"k": K_ENSURE_XFORM_OPS, "prim": "/World/A"},
                {"k": K_SET_XFORM_TRS, "prim": "/World/A", "fields": ["t"], "t": [1, 0, 0]},
                {"k": K_DELETE_PRIM, "prim": "/World/A"},
            ],
        )

        srv.compact_log()
        events = _read_log(srv)

        # Only the delete event remains
        a_events = [e for e in events if e["prim"] == "/World/A"]
        assert len(a_events) == 1
        assert a_events[0]["k"] == K_DELETE_PRIM

    def test_deactivate_preserves_trs(self, tmp_path):
        """deactivate_prim does NOT tombstone — TRS is kept for payload reload."""
        srv = _make_server(tmp_path)
        _inject_events(
            srv,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Xform"},
                {"k": K_ENSURE_XFORM_OPS, "prim": "/World/A"},
                {"k": K_SET_XFORM_TRS, "prim": "/World/A", "fields": ["t"], "t": [5, 0, 0]},
                {"k": K_DEACTIVATE_PRIM, "prim": "/World/A", "active": False},
            ],
        )

        srv.compact_log()
        events = _read_log(srv)

        # TRS survives — needed for replay_children_after_load
        a_events = [e for e in events if e["prim"] == "/World/A"]
        kinds = {e["k"] for e in a_events}
        assert K_ENSURE_PRIM in kinds
        assert K_SET_XFORM_TRS in kinds
        assert K_DEACTIVATE_PRIM in kinds

    def test_rename_tombstones_old_path(self, tmp_path):
        """rename_prim tombstones the old path."""
        srv = _make_server(tmp_path)
        _inject_events(
            srv,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/Old", "typeName": "Xform"},
                {"k": K_SET_XFORM_TRS, "prim": "/World/Old", "fields": ["t"], "t": [1, 0, 0]},
                {"k": K_RENAME_PRIM, "prim": "/World/Old", "new_name": "New"},
                {"k": K_ENSURE_PRIM, "prim": "/World/New", "typeName": "Xform"},
                {"k": K_SET_XFORM_TRS, "prim": "/World/New", "fields": ["t"], "t": [2, 0, 0]},
            ],
        )

        srv.compact_log()
        events = _read_log(srv)

        old_events = [e for e in events if e["prim"] == "/World/Old"]
        # Only the rename event survives for the old path
        assert len(old_events) == 1
        assert old_events[0]["k"] == K_RENAME_PRIM

        new_events = [e for e in events if e["prim"] == "/World/New"]
        assert any(e["k"] == K_ENSURE_PRIM for e in new_events)
        assert any(e["k"] == K_SET_XFORM_TRS for e in new_events)

    def test_structural_before_values(self, tmp_path):
        """Compacted output orders ensure_prim before set_xform_trs."""
        srv = _make_server(tmp_path)
        _inject_events(
            srv,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Xform"},
                {"k": K_ENSURE_XFORM_OPS, "prim": "/World/A"},
                {"k": K_SET_XFORM_TRS, "prim": "/World/A", "fields": ["t"], "t": [1, 0, 0]},
                {"k": K_SET_VISIBILITY, "prim": "/World/A", "visible": True},
            ],
        )

        srv.compact_log()
        events = _read_log(srv)

        kinds = [e["k"] for e in events if e["prim"] == "/World/A"]
        assert kinds.index(K_ENSURE_PRIM) < kinds.index(K_SET_XFORM_TRS)
        assert kinds.index(K_ENSURE_XFORM_OPS) < kinds.index(K_SET_XFORM_TRS)

    def test_multiple_prims(self, tmp_path):
        """Compaction handles multiple prims independently."""
        srv = _make_server(tmp_path)
        _inject_events(
            srv,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Xform"},
                {"k": K_SET_XFORM_TRS, "prim": "/World/A", "fields": ["t"], "t": [1, 0, 0]},
                {"k": K_ENSURE_PRIM, "prim": "/World/B", "typeName": "Xform"},
                {"k": K_SET_XFORM_TRS, "prim": "/World/B", "fields": ["t"], "t": [2, 0, 0]},
                {"k": K_SET_XFORM_TRS, "prim": "/World/A", "fields": ["t"], "t": [3, 0, 0]},
                {"k": K_SET_XFORM_TRS, "prim": "/World/B", "fields": ["t"], "t": [4, 0, 0]},
            ],
        )

        srv.compact_log()
        events = _read_log(srv)

        a_trs = [e for e in events if e["prim"] == "/World/A" and e["k"] == K_SET_XFORM_TRS]
        b_trs = [e for e in events if e["prim"] == "/World/B" and e["k"] == K_SET_XFORM_TRS]
        assert len(a_trs) == 1
        assert a_trs[0]["t"] == [3, 0, 0]
        assert len(b_trs) == 1
        assert b_trs[0]["t"] == [4, 0, 0]

    def test_significant_reduction(self, tmp_path):
        """Simulates a realistic session and verifies meaningful compaction."""
        srv = _make_server(tmp_path)
        events = [
            {"k": K_ENSURE_PRIM, "prim": "/World/Sphere", "typeName": "Sphere"},
            {"k": K_ENSURE_XFORM_OPS, "prim": "/World/Sphere"},
        ]
        # 100 TRS updates (simulating dragging an object)
        for i in range(100):
            events.append(
                {
                    "k": K_SET_XFORM_TRS,
                    "prim": "/World/Sphere",
                    "fields": ["t"],
                    "t": [float(i), 0.0, 0.0],
                }
            )
        _inject_events(srv, events)
        assert len(_read_log(srv)) == 102

        srv.compact_log()
        compacted = _read_log(srv)
        # 3 events: ensure_prim + ensure_xform_ops + final TRS
        assert len(compacted) == 3
        trs = [e for e in compacted if e["k"] == K_SET_XFORM_TRS][0]
        assert trs["t"] == [99.0, 0.0, 0.0]

    def test_set_reference_latest_wins(self, tmp_path):
        """Multiple set_reference for same prim keeps only the last."""
        srv = _make_server(tmp_path)
        _inject_events(
            srv,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Xform"},
                {"k": K_SET_REFERENCE, "prim": "/World/A", "refs": [{"asset_path": "old.usda"}]},
                {"k": K_SET_REFERENCE, "prim": "/World/A", "refs": [{"asset_path": "new.usda"}]},
            ],
        )

        srv.compact_log()
        events = _read_log(srv)

        refs = [e for e in events if e["k"] == K_SET_REFERENCE]
        assert len(refs) == 1
        assert refs[0]["refs"][0]["asset_path"] == "new.usda"

    def test_timed_events_compact_per_sample(self, tmp_path):
        """Events at distinct time samples must not collapse into each other
        or into the default-time opinion."""
        srv = _make_server(tmp_path)
        _inject_events(
            srv,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Xform"},
                {"k": K_SET_XFORM_TRS, "prim": "/World/A", "fields": ["t"], "t": [1, 0, 0]},
                {"k": K_SET_XFORM_TRS, "prim": "/World/A", "fields": ["t"],
                 "t": [10, 0, 0], "time": 1.0},
                {"k": K_SET_XFORM_TRS, "prim": "/World/A", "fields": ["t"],
                 "t": [20, 0, 0], "time": 2.0},
                {"k": K_SET_XFORM_TRS, "prim": "/World/A", "fields": ["t"],
                 "t": [21, 0, 0], "time": 2.0},
            ],
        )

        srv.compact_log()
        events = _read_log(srv)

        trs = [e for e in events if e["k"] == K_SET_XFORM_TRS]
        by_time = {e.get("time"): e["t"] for e in trs}
        assert len(trs) == 3
        # The default-time opinion is not contaminated by sample values.
        assert by_time[None] == [1.0, 0.0, 0.0]
        assert by_time[1.0] == [10.0, 0.0, 0.0]
        # Same-sample re-author is latest-wins.
        assert by_time[2.0] == [21.0, 0.0, 0.0]
        # Default sorts before samples for replay.
        assert [e.get("time") for e in trs] == [None, 1.0, 2.0]
