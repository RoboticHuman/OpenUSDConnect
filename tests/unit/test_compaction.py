"""Tests for server event log compaction."""

from openusdconnect.codec import message_to_dict
from openusdconnect.protocol_constants import (
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_RENAME_PRIM,
    K_SET_CONNECTABLE_CONNECTION,
    K_SET_CONNECTABLE_INPUT,
    K_SET_MATERIAL_BINDING,
    K_SET_REFERENCE,
    K_SET_VARIANT_SELECTIONS,
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

    def test_point_instancer_partial_fields_merge(self, tmp_path):
        """A later partial set_point_instancer must not drop earlier fields."""
        from openusdconnect.protocol_constants import K_SET_POINT_INSTANCER

        srv = _make_server(tmp_path)
        _inject_events(
            srv,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/PI", "typeName": "PointInstancer"},
                {"k": K_SET_POINT_INSTANCER, "prim": "/World/PI",
                 "fields": ["prototypes", "proto_indices", "positions"],
                 "prototypes": ["/Protos/A"],
                 "proto_indices": [0, 0],
                 "positions": [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]},
                {"k": K_SET_POINT_INSTANCER, "prim": "/World/PI",
                 "fields": ["positions"],
                 "positions": [[5.0, 5.0, 5.0], [6.0, 6.0, 6.0]]},
            ],
        )

        srv.compact_log()
        events = _read_log(srv)

        pi = [e for e in events if e["k"] == K_SET_POINT_INSTANCER]
        assert len(pi) == 1
        assert set(pi[0]["fields"]) == {"prototypes", "proto_indices", "positions"}
        assert pi[0]["prototypes"] == ["/Protos/A"]
        assert pi[0]["positions"] == [[5.0, 5.0, 5.0], [6.0, 6.0, 6.0]]

    def test_point_instancer_animated_samples_survive(self, tmp_path):
        """Animated PI arrays keep one merged event per time sample."""
        from openusdconnect.protocol_constants import K_SET_POINT_INSTANCER

        srv = _make_server(tmp_path)
        _inject_events(
            srv,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/PI", "typeName": "PointInstancer"},
                {"k": K_SET_POINT_INSTANCER, "prim": "/World/PI",
                 "fields": ["prototypes", "positions"],
                 "prototypes": ["/Protos/A"],
                 "positions": [[0.0, 0.0, 0.0]]},
                {"k": K_SET_POINT_INSTANCER, "prim": "/World/PI",
                 "fields": ["positions"], "positions": [[1.0, 0.0, 0.0]], "time": 1.0},
                {"k": K_SET_POINT_INSTANCER, "prim": "/World/PI",
                 "fields": ["positions"], "positions": [[2.0, 0.0, 0.0]], "time": 2.0},
                {"k": K_SET_POINT_INSTANCER, "prim": "/World/PI",
                 "fields": ["scales"], "scales": [[2.0, 2.0, 2.0]], "time": 2.0},
            ],
        )

        srv.compact_log()
        events = _read_log(srv)

        pi = [e for e in events if e["k"] == K_SET_POINT_INSTANCER]
        assert len(pi) == 3
        by_time = {e.get("time"): e for e in pi}
        assert by_time[None]["prototypes"] == ["/Protos/A"]
        assert by_time[1.0]["fields"] == ["positions"]
        # Same-time events for different arrays merge into one.
        assert set(by_time[2.0]["fields"]) == {"positions", "scales"}

    def test_instanceable_latest_wins(self, tmp_path):
        """Multiple set_instanceable for same prim keeps only the last."""
        from openusdconnect.protocol_constants import K_SET_INSTANCEABLE

        srv = _make_server(tmp_path)
        _inject_events(
            srv,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Xform"},
                {"k": K_SET_INSTANCEABLE, "prim": "/World/A", "instanceable": True},
                {"k": K_SET_INSTANCEABLE, "prim": "/World/A", "instanceable": False},
                {"k": K_SET_INSTANCEABLE, "prim": "/World/A", "instanceable": True},
            ],
        )

        srv.compact_log()
        events = _read_log(srv)

        inst = [e for e in events if e["k"] == K_SET_INSTANCEABLE]
        assert len(inst) == 1
        assert inst[0]["instanceable"] is True


class TestCompactionReplayOrder:
    """The compacted log must replay causally for strict one-event-at-a-time
    receivers: creates before the events that reference the created prims,
    deletes before recreates, no zombie descendants."""

    def test_connection_replays_after_referenced_prim_create(self, tmp_path):
        srv = _make_server(tmp_path)
        _inject_events(
            srv,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/Looks/M", "typeName": "Material"},
                {"k": K_ENSURE_PRIM, "prim": "/World/Looks/M/Surface", "typeName": "Shader"},
                {"k": K_SET_CONNECTABLE_INPUT, "prim": "/World/Looks/M/Surface",
                 "info_id": "ND_standard_surface_surfaceshader",
                 "inputs": {"metalness": 1.0}, "input_types": {"metalness": "float"}},
                {"k": K_SET_CONNECTABLE_CONNECTION, "prim": "/World/Looks/M",
                 "connections": {"outputs:mtlx:surface": {
                     "source_prim": "/World/Looks/M/Surface",
                     "source_attr": "outputs:surface"}}},
            ],
        )

        srv.compact_log()
        events = _read_log(srv)

        keys = [(e["k"], e["prim"]) for e in events]
        create_idx = keys.index((K_ENSURE_PRIM, "/World/Looks/M/Surface"))
        conn_idx = keys.index((K_SET_CONNECTABLE_CONNECTION, "/World/Looks/M"))
        assert create_idx < conn_idx

    def test_delete_tombstones_subtree(self, tmp_path):
        srv = _make_server(tmp_path)
        _inject_events(
            srv,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/Looks/M", "typeName": "Material"},
                {"k": K_ENSURE_PRIM, "prim": "/World/Looks/M/Surface", "typeName": "Shader"},
                {"k": K_SET_CONNECTABLE_INPUT, "prim": "/World/Looks/M/Surface",
                 "info_id": "ND_standard_surface_surfaceshader",
                 "inputs": {"metalness": 1.0}, "input_types": {"metalness": "float"}},
                {"k": K_DELETE_PRIM, "prim": "/World/Looks/M"},
            ],
        )

        srv.compact_log()
        events = _read_log(srv)

        subtree = [e for e in events if e["prim"].startswith("/World/Looks/M")]
        assert [e["k"] for e in subtree] == [K_DELETE_PRIM]

    def test_delete_then_recreate_survives(self, tmp_path):
        srv = _make_server(tmp_path)
        _inject_events(
            srv,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Xform"},
                {"k": K_SET_XFORM_TRS, "prim": "/World/A", "fields": ["t"], "t": [1, 0, 0]},
                {"k": K_DELETE_PRIM, "prim": "/World/A"},
                {"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Sphere"},
                {"k": K_SET_XFORM_TRS, "prim": "/World/A", "fields": ["t"], "t": [2, 0, 0]},
            ],
        )

        srv.compact_log()
        events = _read_log(srv)

        kinds = [e["k"] for e in events if e["prim"] == "/World/A"]
        assert kinds == [K_DELETE_PRIM, K_ENSURE_PRIM, K_SET_XFORM_TRS]
        ensure = [e for e in events if e["k"] == K_ENSURE_PRIM][0]
        assert ensure["typeName"] == "Sphere"
        trs = [e for e in events if e["k"] == K_SET_XFORM_TRS][0]
        assert trs["t"] == [2.0, 0.0, 0.0]

    def test_reensure_keeps_create_before_dependents(self, tmp_path):
        """A late api_schemas re-ensure must not push the create past a
        connection that references the prim."""
        srv = _make_server(tmp_path)
        _inject_events(
            srv,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/Looks/M", "typeName": "Material"},
                {"k": K_ENSURE_PRIM, "prim": "/World/Looks/M/Surface", "typeName": "Shader"},
                {"k": K_SET_CONNECTABLE_CONNECTION, "prim": "/World/Looks/M",
                 "connections": {"outputs:mtlx:surface": {
                     "source_prim": "/World/Looks/M/Surface",
                     "source_attr": "outputs:surface"}}},
                {"k": K_ENSURE_PRIM, "prim": "/World/Looks/M/Surface",
                 "typeName": "Shader", "api_schemas": ["NodeDefAPI"]},
            ],
        )

        srv.compact_log()
        events = _read_log(srv)

        keys = [(e["k"], e["prim"]) for e in events]
        create_idx = keys.index((K_ENSURE_PRIM, "/World/Looks/M/Surface"))
        conn_idx = keys.index((K_SET_CONNECTABLE_CONNECTION, "/World/Looks/M"))
        assert create_idx < conn_idx
        assert events[create_idx]["api_schemas"] == ["NodeDefAPI"]

    def test_connection_events_merge(self, tmp_path):
        """Connection events for different inputs on one prim merge instead of
        latest-wins clobbering."""
        srv = _make_server(tmp_path)
        _inject_events(
            srv,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/Looks/M/Surface", "typeName": "Shader"},
                {"k": K_SET_CONNECTABLE_CONNECTION, "prim": "/World/Looks/M/Surface",
                 "connections": {"inputs:base_color": {
                     "source_prim": "/World/Looks/M/Img",
                     "source_attr": "outputs:out"}}},
                {"k": K_SET_CONNECTABLE_CONNECTION, "prim": "/World/Looks/M/Surface",
                 "connections": {"inputs:roughness": {
                     "source_prim": "/World/Looks/M/Rough",
                     "source_attr": "outputs:out"}}},
            ],
        )

        srv.compact_log()
        events = _read_log(srv)

        conns = [e for e in events if e["k"] == K_SET_CONNECTABLE_CONNECTION]
        assert len(conns) == 1
        assert set(conns[0]["connections"]) == {"inputs:base_color", "inputs:roughness"}

    def test_binding_purposes_survive_independently(self, tmp_path):
        srv = _make_server(tmp_path)
        _inject_events(
            srv,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Sphere"},
                {"k": K_SET_MATERIAL_BINDING, "prim": "/World/A",
                 "material_path": "/World/Looks/M"},
                {"k": K_SET_MATERIAL_BINDING, "prim": "/World/A",
                 "material_path": "/World/Looks/Preview",
                 "material_purpose": "preview"},
            ],
        )

        srv.compact_log()
        events = _read_log(srv)

        binds = [e for e in events if e["k"] == K_SET_MATERIAL_BINDING]
        by_purpose = {
            e.get("material_purpose") or "": e["material_path"] for e in binds
        }
        assert by_purpose == {
            "": "/World/Looks/M",
            "preview": "/World/Looks/Preview",
        }

    def test_variant_selections_merge(self, tmp_path):
        srv = _make_server(tmp_path)
        _inject_events(
            srv,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Xform"},
                {"k": K_SET_VARIANT_SELECTIONS, "prim": "/World/A",
                 "selections": {"size": "large"}},
                {"k": K_SET_VARIANT_SELECTIONS, "prim": "/World/A",
                 "selections": {"color": "red"}},
                {"k": K_SET_VARIANT_SELECTIONS, "prim": "/World/A",
                 "selections": {"size": "small"}},
            ],
        )

        srv.compact_log()
        events = _read_log(srv)

        sels = [e for e in events if e["k"] == K_SET_VARIANT_SELECTIONS]
        assert len(sels) == 1
        assert sels[0]["selections"] == {"size": "small", "color": "red"}

    def test_per_event_replay_equivalence(self, tmp_path):
        """Golden property: applying the compacted log one event at a time
        composes the same stage as applying the original log one event at a
        time — the contract strict sequential receivers rely on."""
        import pytest

        pxr = pytest.importorskip("pxr")
        from pxr import Usd

        from openusdconnect.event_apply import apply_events

        original = [
            {"k": K_ENSURE_PRIM, "prim": "/World/Looks/M", "typeName": "Material"},
            {"k": K_ENSURE_PRIM, "prim": "/World/Looks/M/Surface", "typeName": "Shader"},
            {"k": K_SET_CONNECTABLE_INPUT, "prim": "/World/Looks/M/Surface",
             "info_id": "ND_standard_surface_surfaceshader",
             "inputs": {"metalness": 0.2}, "input_types": {"metalness": "float"}},
            {"k": K_SET_CONNECTABLE_CONNECTION, "prim": "/World/Looks/M",
             "connections": {"outputs:mtlx:surface": {
                 "source_prim": "/World/Looks/M/Surface",
                 "source_attr": "outputs:surface"}}},
            {"k": K_ENSURE_PRIM, "prim": "/World/Ball", "typeName": "Sphere"},
            {"k": K_ENSURE_XFORM_OPS, "prim": "/World/Ball"},
            {"k": K_SET_XFORM_TRS, "prim": "/World/Ball", "fields": ["t"], "t": [1, 0, 0]},
            {"k": K_SET_MATERIAL_BINDING, "prim": "/World/Ball",
             "material_path": "/World/Looks/M"},
            {"k": K_SET_XFORM_TRS, "prim": "/World/Ball", "fields": ["t"], "t": [3, 0, 0]},
            {"k": K_SET_VISIBILITY, "prim": "/World/Ball", "visible": False},
            {"k": K_SET_VISIBILITY, "prim": "/World/Ball", "visible": True},
            {"k": K_ENSURE_PRIM, "prim": "/World/Temp", "typeName": "Xform"},
            {"k": K_ENSURE_PRIM, "prim": "/World/Temp/Child", "typeName": "Sphere"},
            {"k": K_DELETE_PRIM, "prim": "/World/Temp"},
            {"k": K_SET_CONNECTABLE_INPUT, "prim": "/World/Looks/M/Surface",
             "info_id": "", "inputs": {"metalness": 1.0},
             "input_types": {"metalness": "float"}},
        ]

        srv = _make_server(tmp_path)
        _inject_events(srv, original)
        srv.compact_log()
        compacted = _read_log(srv)
        assert len(compacted) < len(original)

        def replay_one_at_a_time(events):
            stage = Usd.Stage.CreateInMemory()
            for ev in events:
                apply_events(stage, [ev])
            return stage.Flatten().ExportToString()

        assert replay_one_at_a_time(compacted) == replay_one_at_a_time(original)


class TestGeometryHeavyLog:
    def test_mesh_points_compact_fast_and_replay_intact(self, tmp_path):
        """Large float-array attrs must take the numpy decode path during
        compaction (the per-element path is ~100x slower and stalls the
        periodic thread for minutes) and survive the re-encode on commit."""
        import time

        import numpy as np

        db = str(tmp_path / "heavy.db")
        srv = UsdSyncServer(log_path=db)
        rng = np.random.default_rng(0)
        base = rng.random((9216, 3), dtype=np.float32)
        events = [{"k": K_ENSURE_PRIM, "prim": "/World/M", "typeName": "Mesh"}]
        for i in range(30):
            events.append({
                "k": "set_gprim_attrs", "prim": "/World/M",
                "attrs": {"points": (base + np.float32(i)).tolist()},
            })
        srv.process_txn(events, client_id="c", origin="o", client_addr="a:1")

        t0 = time.perf_counter()
        srv.compact_log()
        elapsed = time.perf_counter() - t0
        assert elapsed < 10, f"compaction of 30 mesh events took {elapsed:.1f}s"
        assert srv.get_event_count() == 2
        srv.store.close()

        srv2 = UsdSyncServer(log_path=db)
        got = np.array(srv2.stage.GetPrimAtPath("/World/M").GetAttribute("points").Get())
        assert got.shape == (9216, 3)
        assert np.allclose(got, base + np.float32(29), atol=1e-5)
        srv2.shutdown()
        srv2.store.close()
        srv.shutdown()
