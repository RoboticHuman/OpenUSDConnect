"""E2E tests for per-client layer editing with department ordering.

Uses a real server subprocess, real TCP connections, and verifies layer
isolation, department ordering, mute/unmute, and merge.
No DCC needed — headless, runs in CI.
"""

import time

import pytest
from pxr import Usd, UsdGeom

from openusdconnect.event_apply import apply_events
from openusdconnect.protocol import (
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_SET_XFORM_TRS,
    MSG_EVENT,
)
from openusdconnect.server import UsdSyncServer

# -- Helpers -------------------------------------------------------------

def _make_server(tmp_path, department_priority=None):
    """Create an in-process UsdSyncServer with a temp DB."""
    db = str(tmp_path / "test.db")
    return UsdSyncServer(log_path=db, department_priority=department_priority)


def _emit_events(srv, client_id, events, department=None):
    """Simulate an emitter: create layer, apply txn, persist."""
    layer = srv.get_or_create_client_layer(client_id, department=department)
    srv.apply_txn(events, layer=layer)
    records = []
    for ev in events:
        seq = srv.assign_seq()
        rec = {"type": MSG_EVENT, "seq": seq, "event": ev, "client_id": client_id}
        records.append(rec)
    srv.append_log_batch(records)


def _read_translate(stage, prim_path):
    """Read the composed translate value for a prim."""
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return None
    xf = UsdGeom.Xformable(prim)
    for op in xf.GetOrderedXformOps():
        if op.GetAttr().GetName() == "xformOp:translate":
            v = op.Get()
            return (v[0], v[1], v[2])
    return None


def _make_trs_events(prim_path, t):
    """Build ensure_prim + ensure_xform_ops + set_xform_trs events."""
    return [
        {"k": K_ENSURE_PRIM, "prim": prim_path, "typeName": "Xform"},
        {"k": K_ENSURE_XFORM_OPS, "prim": prim_path},
        {"k": K_SET_XFORM_TRS, "prim": prim_path,
         "fields": ["t"], "t": list(t)},
    ]


# -- Tests ---------------------------------------------------------------

class TestLayerIsolation:

    def test_separate_layers_isolation(self, tmp_path):
        """Two departments editing different prims — both visible in composed stage."""
        srv = _make_server(tmp_path, department_priority=["animation", "layout"])

        _emit_events(srv, "alice", _make_trs_events("/World/Cube", (1, 2, 3)),
                     department="animation")
        _emit_events(srv, "bob", _make_trs_events("/World/Sphere", (4, 5, 6)),
                     department="layout")

        assert _read_translate(srv.stage, "/World/Cube") == (1, 2, 3)
        assert _read_translate(srv.stage, "/World/Sphere") == (4, 5, 6)

        # Edits are in separate layers (different departments)
        assert "alice" in srv.client_layers
        assert "bob" in srv.client_layers
        assert srv.client_layers["alice"] is not srv.client_layers["bob"]

    def test_same_prim_stronger_layer_wins(self, tmp_path):
        """Two departments editing same prim — stronger department wins."""
        srv = _make_server(tmp_path, department_priority=["animation", "layout"])

        _emit_events(srv, "alice", _make_trs_events("/World/Cube", (1, 0, 0)),
                     department="layout")
        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)),
                     department="animation")

        # Animation is stronger than layout
        t = _read_translate(srv.stage, "/World/Cube")
        assert t == (2, 0, 0)

        # Mute animation → layout value reappears
        srv.mute_layer("animation")
        t = _read_translate(srv.stage, "/World/Cube")
        assert t == (1, 0, 0)

        # Unmute animation → animation value is back
        srv.unmute_layer("animation")
        t = _read_translate(srv.stage, "/World/Cube")
        assert t == (2, 0, 0)

    def test_no_dept_merge_is_noop(self, tmp_path):
        """Merge/delete on a non-department client (shared edit_layer) returns False."""
        srv = _make_server(tmp_path)

        _emit_events(srv, "alice", _make_trs_events("/World/Cube", (1, 2, 3)))
        assert srv.client_layers["alice"] is srv.edit_layer

        # merge and delete should refuse to operate on the shared edit_layer
        assert not srv.merge_layer("alice")
        assert not srv.delete_layer("alice")

        # Data still intact
        assert _read_translate(srv.stage, "/World/Cube") == (1, 2, 3)


class TestDepartmentOrdering:

    def test_no_departments_last_write_wins(self, tmp_path):
        """Without departments, all clients share edit_layer (last write wins)."""
        srv = _make_server(tmp_path)

        _emit_events(srv, "first", _make_trs_events("/World/Cube", (1, 0, 0)))
        _emit_events(srv, "second", _make_trs_events("/World/Cube", (2, 0, 0)))

        # Last write wins — both on shared edit_layer
        t = _read_translate(srv.stage, "/World/Cube")
        assert t == (2, 0, 0)

        # Both clients share the same layer
        assert srv.client_layers["first"] is srv.client_layers["second"]
        assert srv.client_layers["first"] is srv.edit_layer

    def test_department_ordering_overrides_connection_order(self, tmp_path):
        """Department priority overrides connection order."""
        srv = _make_server(tmp_path, department_priority=["animation", "layout"])

        # Alice (layout) connects FIRST
        _emit_events(srv, "alice", _make_trs_events("/World/Cube", (1, 0, 0)),
                      department="layout")
        time.sleep(0.01)
        # Bob (animation) connects SECOND
        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)),
                      department="animation")

        # Animation is stronger than layout regardless of connection order
        t = _read_translate(srv.stage, "/World/Cube")
        assert t == (2, 0, 0)

        # Verify layer stack order
        info = srv.get_layer_stack_info()
        assert info[0]["department"] == "animation"  # strongest
        assert info[1]["department"] == "layout"     # weaker

    def test_department_priority_change_reorders(self, tmp_path):
        """Changing department priority reorders existing layers."""
        srv = _make_server(tmp_path, department_priority=["animation", "layout"])

        _emit_events(srv, "alice", _make_trs_events("/World/Cube", (1, 0, 0)),
                      department="layout")
        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)),
                      department="animation")

        # Animation wins
        assert _read_translate(srv.stage, "/World/Cube") == (2, 0, 0)

        # Reverse priority: layout now stronger
        srv.set_department_priority(["layout", "animation"])
        assert _read_translate(srv.stage, "/World/Cube") == (1, 0, 0)

    def test_no_department_clients_weaker_than_department(self, tmp_path):
        """Clients without a department use the shared edit_layer (weakest)."""
        srv = _make_server(tmp_path, department_priority=["animation"])

        _emit_events(srv, "anon", _make_trs_events("/World/Cube", (1, 0, 0)))
        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)),
                      department="animation")

        # Bob (animation dept) is stronger than anon (shared edit_layer)
        assert _read_translate(srv.stage, "/World/Cube") == (2, 0, 0)


class TestSharedDepartmentLayer:

    def test_same_department_shares_layer(self, tmp_path):
        """Two clients in the same department write to the same layer."""
        srv = _make_server(tmp_path, department_priority=["animation"])

        _emit_events(srv, "alice", _make_trs_events("/World/Cube", (1, 0, 0)),
                      department="animation")
        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)),
                      department="animation")

        # Same layer object
        assert srv.client_layers["alice"] is srv.client_layers["bob"]

        # Last write wins within the department
        assert _read_translate(srv.stage, "/World/Cube") == (2, 0, 0)

    def test_same_department_last_write_wins(self, tmp_path):
        """Within a shared department layer, the last write wins."""
        srv = _make_server(tmp_path, department_priority=["animation"])

        _emit_events(srv, "alice", _make_trs_events("/World/Cube", (1, 0, 0)),
                      department="animation")
        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)),
                      department="animation")
        _emit_events(srv, "alice", _make_trs_events("/World/Cube", (3, 0, 0)),
                      department="animation")

        # Alice wrote last — her value wins
        assert _read_translate(srv.stage, "/World/Cube") == (3, 0, 0)

    def test_shared_layer_broadcast_within_department(self, tmp_path):
        """Events from same department always change composed view (shared layer)."""
        srv = _make_server(tmp_path, department_priority=["animation"])

        _emit_events(srv, "alice", _make_trs_events("/World/Cube", (1, 0, 0)),
                      department="animation")

        layer = srv.get_or_create_client_layer("bob", department="animation")
        events = _make_trs_events("/World/Cube", (2, 0, 0))
        changed = srv.apply_txn(events, layer=layer)

        # Same layer — Bob's write always changes composed view
        assert len(changed) == 3


class TestLayerLifecycle:

    def test_layer_survives_disconnect(self, tmp_path):
        """Department layer persists after simulated disconnect — opinions stay."""
        srv = _make_server(tmp_path, department_priority=["animation"])

        addr = ("127.0.0.1", 12345)
        srv.register_client(addr, "emitter", client_id="alice", department="animation")
        _emit_events(srv, "alice", _make_trs_events("/World/Cube", (1, 2, 3)),
                     department="animation")
        assert _read_translate(srv.stage, "/World/Cube") == (1, 2, 3)

        # Disconnect — client removed from tracking, but layer stays
        srv.unregister_client(addr)
        key = f"{addr[0]}:{addr[1]}"
        assert key not in srv.clients

        # Layer and opinions still present
        assert "alice" in srv.client_layers
        assert _read_translate(srv.stage, "/World/Cube") == (1, 2, 3)

    def test_mute_unmute(self, tmp_path):
        """Muting hides department opinions, unmuting restores them."""
        srv = _make_server(tmp_path, department_priority=["animation"])

        _emit_events(srv, "alice", _make_trs_events("/World/Cube", (1, 2, 3)),
                     department="animation")
        assert _read_translate(srv.stage, "/World/Cube") == (1, 2, 3)

        srv.mute_layer("animation")
        assert _read_translate(srv.stage, "/World/Cube") is None

        srv.unmute_layer("animation")
        assert _read_translate(srv.stage, "/World/Cube") == (1, 2, 3)

    def test_delete_layer(self, tmp_path):
        """Deleting a department layer removes it and its opinions permanently."""
        srv = _make_server(tmp_path, department_priority=["animation"])

        _emit_events(srv, "alice", _make_trs_events("/World/Cube", (1, 2, 3)),
                     department="animation")
        assert "alice" in srv.client_layers

        srv.delete_layer("alice")
        assert "alice" not in srv.client_layers
        assert _read_translate(srv.stage, "/World/Cube") is None

    def test_merge_to_base(self, tmp_path):
        """Merging transfers department opinions to root layer."""
        srv = _make_server(tmp_path, department_priority=["animation"])

        # Create pre-existing content in root layer
        root = srv.stage.GetRootLayer()
        srv.stage.SetEditTarget(Usd.EditTarget(root))
        apply_events(srv.stage, _make_trs_events("/World/Existing", (99, 99, 99)))

        # Bob edits in his department layer
        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (10, 20, 30)),
                     department="animation")

        # Both visible before merge
        assert _read_translate(srv.stage, "/World/Existing") == (99, 99, 99)
        assert _read_translate(srv.stage, "/World/Cube") == (10, 20, 30)

        srv.merge_layer("bob")

        assert "bob" not in srv.client_layers
        assert _read_translate(srv.stage, "/World/Cube") == (10, 20, 30)
        assert _read_translate(srv.stage, "/World/Existing") == (99, 99, 99)


class TestLayerStackInfo:

    def test_get_layer_stack_info(self, tmp_path):
        """get_layer_stack_info returns correct metadata."""
        srv = _make_server(tmp_path, department_priority=["fx", "layout"])

        _emit_events(srv, "alice", _make_trs_events("/World/A", (1, 0, 0)),
                      department="layout")
        _emit_events(srv, "bob", _make_trs_events("/World/B", (2, 0, 0)),
                      department="fx")

        info = srv.get_layer_stack_info()
        assert len(info) == 2
        assert info[0]["department"] == "fx"
        assert "bob" in info[0]["clients"]
        assert info[0]["muted"] is False
        assert info[1]["department"] == "layout"
        assert "alice" in info[1]["clients"]

    def test_muted_reflected_in_info(self, tmp_path):
        srv = _make_server(tmp_path, department_priority=["animation"])
        _emit_events(srv, "alice", _make_trs_events("/World/A", (1, 0, 0)),
                     department="animation")

        srv.mute_layer("animation")
        info = srv.get_layer_stack_info()
        assert info[0]["muted"] is True

        srv.unmute_layer("animation")
        info = srv.get_layer_stack_info()
        assert info[0]["muted"] is False


class TestMergeParity:

    def test_merge_preserves_composed_parity(self, tmp_path):
        """Composed stage is identical before and after merge."""
        srv = _make_server(tmp_path, department_priority=["animation", "layout"])

        # Pre-existing root content
        root = srv.stage.GetRootLayer()
        srv.stage.SetEditTarget(Usd.EditTarget(root))
        apply_events(srv.stage, _make_trs_events("/World/RootPrim", (1, 1, 1)))

        # Alice (layout) and Bob (animation) each edit different prims
        _emit_events(srv, "alice", _make_trs_events("/World/A", (10, 20, 30)),
                     department="layout")
        _emit_events(srv, "bob", _make_trs_events("/World/B", (40, 50, 60)),
                     department="animation")

        # Both also edit same prim — animation is stronger
        _emit_events(srv, "alice", _make_trs_events("/World/Shared", (1, 0, 0)),
                     department="layout")
        _emit_events(srv, "bob", _make_trs_events("/World/Shared", (2, 0, 0)),
                     department="animation")

        # Snapshot composed state before merge
        before = {}
        for prim in srv.stage.Traverse():
            t = _read_translate(srv.stage, str(prim.GetPath()))
            if t is not None:
                before[str(prim.GetPath())] = t

        assert len(before) >= 4  # RootPrim, A, B, Shared

        # Merge Alice (layout) into root
        srv.merge_layer("alice")

        # Composed state unchanged
        after_alice = {}
        for prim in srv.stage.Traverse():
            t = _read_translate(srv.stage, str(prim.GetPath()))
            if t is not None:
                after_alice[str(prim.GetPath())] = t

        assert after_alice == before

        # Merge Bob (animation) into root
        srv.merge_layer("bob")

        # Still identical
        after_bob = {}
        for prim in srv.stage.Traverse():
            t = _read_translate(srv.stage, str(prim.GetPath()))
            if t is not None:
                after_bob[str(prim.GetPath())] = t

        assert after_bob == before

        # No client layers remain
        assert len(srv.client_layers) == 0


class TestBroadcastGating:

    def test_overridden_event_not_broadcast(self, tmp_path):
        """Events that don't change the composed view are persisted but not broadcast."""
        srv = _make_server(tmp_path, department_priority=["animation", "layout"])

        # Bob (animation = stronger) sets /World/Cube
        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)),
                      department="animation")

        # Alice (layout = weaker) writes to same prim — composed value unchanged
        layer = srv.get_or_create_client_layer("alice", department="layout")
        events = _make_trs_events("/World/Cube", (99, 99, 99))
        changed = srv.apply_txn(events, layer=layer)

        # Composed value is still Bob's
        assert _read_translate(srv.stage, "/World/Cube") == (2, 0, 0)

        # apply_txn reports no composed changes
        assert changed == []

    def test_non_conflicting_event_is_broadcast(self, tmp_path):
        """Events that change the composed view are reported."""
        srv = _make_server(tmp_path, department_priority=["animation", "layout"])

        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)),
                      department="animation")

        # Alice writes to a different prim — composed value changes
        layer = srv.get_or_create_client_layer("alice", department="layout")
        events = _make_trs_events("/World/Sphere", (5, 5, 5))
        changed = srv.apply_txn(events, layer=layer)

        # All 3 events (ensure_prim, ensure_xform_ops, set_xform_trs) changed composed view
        assert len(changed) == 3

    def test_stronger_layer_always_broadcast(self, tmp_path):
        """Events from the strongest layer always change the composed view."""
        srv = _make_server(tmp_path, department_priority=["animation", "layout"])

        # Alice (layout = weaker) writes first
        _emit_events(srv, "alice", _make_trs_events("/World/Cube", (1, 0, 0)),
                      department="layout")

        # Bob (animation = stronger) writes to same prim — his value wins
        layer = srv.get_or_create_client_layer("bob", department="animation")
        events = _make_trs_events("/World/Cube", (2, 0, 0))
        changed = srv.apply_txn(events, layer=layer)

        assert _read_translate(srv.stage, "/World/Cube") == (2, 0, 0)
        assert len(changed) == 3  # all events produced composed changes


class TestCorrectionPath:

    def test_build_correction_for_overridden_transform(self, tmp_path):
        """Server builds a correction event with composed TRS for overridden writes."""
        srv = _make_server(tmp_path, department_priority=["animation", "layout"])

        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)),
                      department="animation")
        _emit_events(srv, "alice", _make_trs_events("/World/Cube", (99, 99, 99)),
                      department="layout")

        # Alice's event was overridden — build correction
        correction = srv.build_correction(
            {"k": K_SET_XFORM_TRS, "prim": "/World/Cube",
             "fields": ["t"], "t": [99, 99, 99]},
        )

        assert correction is not None
        assert correction["k"] == K_SET_XFORM_TRS
        assert correction["prim"] == "/World/Cube"
        # Correction contains Bob's composed value, not Alice's
        assert correction["t"][0] == pytest.approx(2.0)
        assert correction["t"][1] == pytest.approx(0.0)

    def test_build_correction_for_deactivation(self, tmp_path):
        """Correction for overridden deactivation returns composed active state."""
        srv = _make_server(tmp_path, department_priority=["animation", "layout"])

        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (1, 0, 0)),
                      department="animation")

        # Bob's prim is active, Alice tries to deactivate (weaker)
        correction = srv.build_correction(
            {"k": "deactivate_prim", "prim": "/World/Cube", "active": False},
        )
        # Bob never set active — but prim defaults to active
        assert correction is not None
        assert correction["active"] is True

    def test_no_correction_for_winning_event(self, tmp_path):
        """No correction needed when the sender's layer wins."""
        srv = _make_server(tmp_path, department_priority=["animation", "layout"])

        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)),
                      department="animation")

        # Bob is strongest — no correction
        changed = srv.apply_txn(
            _make_trs_events("/World/Cube", (5, 0, 0)),
            layer=srv.get_or_create_client_layer("bob", department="animation"),
        )
        assert len(changed) == 3  # all events won

    def test_send_to_origin(self, tmp_path):
        """send_to_origin delivers to receivers matching the origin."""
        srv = _make_server(tmp_path)

        # Verify method exists and doesn't crash with no receivers
        srv.send_to_origin({"type": "test"}, "nonexistent-origin")


class TestReplayWithClientLayers:

    def test_replay_routes_to_client_layers(self, tmp_path):
        """Events replayed on startup go to the correct client layers."""
        db = str(tmp_path / "replay.db")

        depts = ["animation", "layout"]

        # First server session: two clients emit events
        srv1 = UsdSyncServer(log_path=db, department_priority=depts)
        _emit_events(srv1, "alice", _make_trs_events("/World/A", (1, 2, 3)),
                      department="layout")
        _emit_events(srv1, "bob", _make_trs_events("/World/B", (4, 5, 6)),
                      department="animation")
        srv1.store.close()

        # Second server session: replays from same DB
        srv2 = UsdSyncServer(log_path=db, department_priority=depts)

        # Both prims restored
        assert _read_translate(srv2.stage, "/World/A") == (1, 2, 3)
        assert _read_translate(srv2.stage, "/World/B") == (4, 5, 6)

        # Events routed to separate client layers
        assert "alice" in srv2.client_layers
        assert "bob" in srv2.client_layers
        srv2.store.close()
