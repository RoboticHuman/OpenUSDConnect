"""Tests for UsdSyncServer — in-process, no TCP.

Instantiates UsdSyncServer directly and exercises its core logic:
sequence assignment, event log, compaction, replay, apply_txn, etc.
"""

import json

import pytest

from openusdconnect.server import UsdSyncServer


@pytest.fixture
def srv(tmp_path):
    """Create a UsdSyncServer with a temp SQLite DB."""
    db = str(tmp_path / "test.db")
    s = UsdSyncServer(log_path=db)
    yield s
    s.db_conn.close()


# ---------------------------------------------------------------------------
# Sequence assignment
# ---------------------------------------------------------------------------


class TestAssignSeq:
    def test_starts_at_one(self, srv):
        assert srv.assign_seq() == 1

    def test_monotonic(self, srv):
        a = srv.assign_seq()
        b = srv.assign_seq()
        c = srv.assign_seq()
        assert (a, b, c) == (1, 2, 3)


# ---------------------------------------------------------------------------
# Event log (append + read back)
# ---------------------------------------------------------------------------


class TestAppendLog:
    def test_append_and_read(self, srv):
        rec = {"type": "event", "seq": 1, "event": {"k": "ensure_prim", "prim": "/A"}}
        srv.append_log(rec)

        rows = srv.db_conn.execute("SELECT seq, event FROM events ORDER BY seq").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 1
        assert json.loads(rows[0][1])["event"]["prim"] == "/A"

    def test_multiple_appends(self, srv):
        for i in range(5):
            srv.append_log({
                "type": "event", "seq": i + 1,
                "event": {"k": "ensure_prim", "prim": f"/P{i}"},
            })
        rows = srv.db_conn.execute("SELECT COUNT(*) FROM events").fetchone()
        assert rows[0] == 5


# ---------------------------------------------------------------------------
# Stage creation
# ---------------------------------------------------------------------------


class TestStageCreation:
    def test_in_memory_stage_has_root(self, srv):
        prim = srv.stage.GetPrimAtPath("/Root")
        assert prim.IsValid()

    def test_invalid_base_path_raises(self, tmp_path):
        with pytest.raises(RuntimeError):
            UsdSyncServer(
                base_usd_path=str(tmp_path / "nonexistent.usda"),
                log_path=str(tmp_path / "err.db"),
            )


# ---------------------------------------------------------------------------
# apply_txn
# ---------------------------------------------------------------------------


class TestApplyTxn:
    def test_applies_events_to_stage(self, srv):
        events = [
            {"k": "ensure_prim", "prim": "/World/Cube", "typeName": "Cube"},
            {"k": "ensure_xform_ops", "prim": "/World/Cube"},
            {"k": "set_xform_trs", "prim": "/World/Cube",
             "fields": ["t"], "t": [1.0, 2.0, 3.0]},
        ]
        srv.apply_txn(events)

        prim = srv.stage.GetPrimAtPath("/World/Cube")
        assert prim.IsValid()
        assert prim.GetTypeName() == "Cube"


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


class TestCompaction:
    def _insert_events(self, srv, events):
        """Helper: assign seqs, append to log, apply to stage."""
        for ev in events:
            seq = srv.assign_seq()
            rec = {"type": "event", "seq": seq, "event": ev}
            srv.append_log(rec)
        srv.apply_txn(events)

    def test_trs_merged(self, srv):
        """Two TRS events for the same prim should merge into one."""
        self._insert_events(srv, [
            {"k": "ensure_prim", "prim": "/A", "typeName": "Xform"},
            {"k": "ensure_xform_ops", "prim": "/A"},
            {"k": "set_xform_trs", "prim": "/A", "fields": ["t"], "t": [1, 0, 0]},
        ])
        self._insert_events(srv, [
            {"k": "set_xform_trs", "prim": "/A", "fields": ["t", "r"],
             "t": [5, 0, 0], "r": [1, 0, 0, 0]},
        ])
        count_before = srv.db_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count_before == 4

        srv.compact_log()

        rows = srv.db_conn.execute("SELECT event FROM events ORDER BY seq").fetchall()
        events = [json.loads(r[0])["event"] for r in rows]
        trs = [e for e in events if e["k"] == "set_xform_trs"]
        assert len(trs) == 1
        assert trs[0]["t"] == [5, 0, 0]
        assert trs[0]["r"] == [1, 0, 0, 0]
        assert "t" in trs[0]["fields"] and "r" in trs[0]["fields"]

    def test_delete_tombstones(self, srv):
        """delete_prim removes all prior events for that prim."""
        self._insert_events(srv, [
            {"k": "ensure_prim", "prim": "/A", "typeName": "Xform"},
            {"k": "ensure_xform_ops", "prim": "/A"},
            {"k": "set_xform_trs", "prim": "/A", "fields": ["t"], "t": [1, 0, 0]},
        ])
        self._insert_events(srv, [
            {"k": "delete_prim", "prim": "/A"},
        ])

        srv.compact_log()

        rows = srv.db_conn.execute("SELECT event FROM events ORDER BY seq").fetchall()
        events = [json.loads(r[0])["event"] for r in rows]
        assert len(events) == 1
        assert events[0]["k"] == "delete_prim"

    def test_rename_tombstones_old(self, srv):
        """rename_prim removes prior events for the old path."""
        self._insert_events(srv, [
            {"k": "ensure_prim", "prim": "/A", "typeName": "Xform"},
            {"k": "set_xform_trs", "prim": "/A", "fields": ["t"], "t": [1, 0, 0]},
        ])
        self._insert_events(srv, [
            {"k": "rename_prim", "prim": "/A", "new_name": "B"},
        ])

        srv.compact_log()

        rows = srv.db_conn.execute("SELECT event FROM events ORDER BY seq").fetchall()
        events = [json.loads(r[0])["event"] for r in rows]
        prims = [e["prim"] for e in events]
        # Only the rename should remain for /A
        assert all(p != "/A" or e["k"] == "rename_prim" for p, e in zip(prims, events))

    def test_visibility_latest_wins(self, srv):
        """Only the latest visibility event per prim survives compaction."""
        self._insert_events(srv, [
            {"k": "ensure_prim", "prim": "/A", "typeName": "Xform"},
            {"k": "set_visibility", "prim": "/A", "visible": False},
        ])
        self._insert_events(srv, [
            {"k": "set_visibility", "prim": "/A", "visible": True},
        ])

        srv.compact_log()

        rows = srv.db_conn.execute("SELECT event FROM events ORDER BY seq").fetchall()
        events = [json.loads(r[0])["event"] for r in rows]
        vis = [e for e in events if e["k"] == "set_visibility"]
        assert len(vis) == 1
        assert vis[0]["visible"] is True

    def test_load_unload_latest_wins(self, srv):
        """load/unload are mutually exclusive — only last one kept."""
        self._insert_events(srv, [
            {"k": "ensure_prim", "prim": "/A", "typeName": "Xform"},
            {"k": "load_payload", "prim": "/A"},
        ])
        self._insert_events(srv, [
            {"k": "unload_payload", "prim": "/A"},
        ])

        srv.compact_log()

        rows = srv.db_conn.execute("SELECT event FROM events ORDER BY seq").fetchall()
        events = [json.loads(r[0])["event"] for r in rows]
        load_events = [e for e in events if e["k"] in ("load_payload", "unload_payload")]
        assert len(load_events) == 1
        assert load_events[0]["k"] == "unload_payload"

    def test_compact_empty_log_noop(self, srv):
        """Compacting an empty log doesn't crash."""
        srv.compact_log()
        rows = srv.db_conn.execute("SELECT COUNT(*) FROM events").fetchone()
        assert rows[0] == 0

    def test_seq_resets_after_compact(self, srv):
        """After compaction, sequence numbers restart from 1."""
        self._insert_events(srv, [
            {"k": "ensure_prim", "prim": "/A", "typeName": "Xform"},
            {"k": "ensure_prim", "prim": "/B", "typeName": "Xform"},
        ])
        assert srv.assign_seq() == 3  # next would be 3

        srv.compact_log()

        # After compact, seqs were reassigned starting from 1
        rows = srv.db_conn.execute("SELECT seq FROM events ORDER BY seq").fetchall()
        seqs = [r[0] for r in rows]
        assert seqs[0] == 1


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


class TestReplay:
    def test_replay_from_sends_events(self, srv):
        """replay_from writes events to a handler's request socket."""
        # Insert some events
        for i in range(3):
            seq = srv.assign_seq()
            rec = {"type": "event", "seq": seq, "event": {"k": "ensure_prim", "prim": f"/P{i}"}}
            srv.append_log(rec)

        # Mock handler with a writable buffer
        import io

        class FakeHandler:
            def __init__(self):
                self.request = io.BytesIO()

        handler = FakeHandler()
        # BytesIO doesn't have sendall — monkey-patch it
        handler.request.sendall = handler.request.write

        srv.replay_from(handler, 1)

        output = handler.request.getvalue().decode("utf-8")
        lines = [json.loads(l) for l in output.strip().split("\n") if l.strip()]
        assert len(lines) == 3
        assert lines[0]["seq"] == 1
        assert lines[2]["seq"] == 3

    def test_replay_from_with_offset(self, srv):
        """replay_from with seq_start=2 skips earlier events."""
        for i in range(3):
            seq = srv.assign_seq()
            srv.append_log({
                "type": "event", "seq": seq,
                "event": {"k": "ensure_prim", "prim": f"/P{i}"},
            })

        import io

        class FakeHandler:
            def __init__(self):
                self.request = io.BytesIO()

        handler = FakeHandler()
        handler.request.sendall = handler.request.write

        srv.replay_from(handler, 2)

        output = handler.request.getvalue().decode("utf-8")
        lines = [json.loads(l) for l in output.strip().split("\n") if l.strip()]
        assert len(lines) == 2
        assert lines[0]["seq"] == 2


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------


class TestBroadcast:
    def test_broadcast_to_receivers(self, srv):
        """broadcast sends to all registered receivers."""
        import io

        class FakeHandler:
            def __init__(self):
                self.request = io.BytesIO()
                self.client_address = ("fake", 0)

        h1 = FakeHandler()
        h1.request.sendall = h1.request.write
        h2 = FakeHandler()
        h2.request.sendall = h2.request.write

        srv.receivers.add(h1)
        srv.receivers.add(h2)

        srv.broadcast({"type": "event", "seq": 1, "event": {"k": "ensure_prim", "prim": "/A"}})

        for h in (h1, h2):
            data = h.request.getvalue().decode("utf-8").strip()
            msg = json.loads(data)
            assert msg["type"] == "event"
            assert msg["event"]["prim"] == "/A"

    def test_broadcast_removes_dead_receivers(self, srv):
        """broadcast discards receivers whose socket is broken."""

        class DeadHandler:
            client_address = ("dead", 0)

            class request:
                @staticmethod
                def sendall(data):
                    raise OSError("broken pipe")

        h = DeadHandler()
        srv.receivers.add(h)
        srv.broadcast({"type": "event", "seq": 1, "event": {}})
        assert h not in srv.receivers


# ---------------------------------------------------------------------------
# DB resume
# ---------------------------------------------------------------------------


class TestDBResume:
    def test_resumes_seq_from_existing_db(self, tmp_path):
        """Server resumes sequence counter from existing DB."""
        db = str(tmp_path / "resume.db")

        # First server writes events
        s1 = UsdSyncServer(log_path=db)
        for i in range(5):
            seq = s1.assign_seq()
            s1.append_log({"type": "event", "seq": seq, "event": {"k": "ensure_prim", "prim": f"/P{i}"}})
        s1.db_conn.close()

        # Second server should resume from seq 6
        s2 = UsdSyncServer(log_path=db)
        assert s2.assign_seq() == 6
        s2.db_conn.close()
