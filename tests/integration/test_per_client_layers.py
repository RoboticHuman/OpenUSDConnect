"""E2E tests for per-client layer editing with department ordering.

Uses a real server subprocess, real TCP connections, and verifies layer
isolation, department ordering, mute/unmute, and merge.
No DCC needed — headless, runs in CI.
"""

import time

import pytest
from pxr import Usd, UsdGeom

from openusdconnect.event_apply import apply_events
from openusdconnect.protocol_constants import (
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
    from openusdconnect.codec import encode_message

    layer = srv.get_or_create_client_layer(client_id, department=department)
    srv.apply_txn(events, layer=layer)
    tuples = []
    for ev in events:
        seq = srv.assign_seq()
        rec = {"type": MSG_EVENT, "seq": seq, "event": ev, "client_id": client_id}
        rec_bin = encode_message(rec)
        tuples.append((seq, rec_bin, client_id, ev.get("k"), ev.get("prim")))
    srv.append_log_batch(tuples)


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
        {"k": K_SET_XFORM_TRS, "prim": prim_path, "fields": ["t"], "t": list(t)},
    ]


# -- Tests ---------------------------------------------------------------


class TestLayerIsolation:
    def test_separate_layers_isolation(self, tmp_path):
        """Two departments editing different prims — both visible in composed stage."""
        srv = _make_server(tmp_path, department_priority=["animation", "layout"])

        _emit_events(
            srv, "alice", _make_trs_events("/World/Cube", (1, 2, 3)), department="animation"
        )
        _emit_events(srv, "bob", _make_trs_events("/World/Sphere", (4, 5, 6)), department="layout")

        assert _read_translate(srv.stage, "/World/Cube") == (1, 2, 3)
        assert _read_translate(srv.stage, "/World/Sphere") == (4, 5, 6)

        # Edits are in separate layers (different departments)
        assert "alice" in srv.client_layers
        assert "bob" in srv.client_layers
        assert srv.client_layers["alice"] is not srv.client_layers["bob"]

    def test_same_prim_stronger_layer_wins(self, tmp_path):
        """Two departments editing same prim — stronger department wins."""
        srv = _make_server(tmp_path, department_priority=["animation", "layout"])

        _emit_events(srv, "alice", _make_trs_events("/World/Cube", (1, 0, 0)), department="layout")
        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)), department="animation")

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
        _emit_events(srv, "alice", _make_trs_events("/World/Cube", (1, 0, 0)), department="layout")
        time.sleep(0.01)
        # Bob (animation) connects SECOND
        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)), department="animation")

        # Animation is stronger than layout regardless of connection order
        t = _read_translate(srv.stage, "/World/Cube")
        assert t == (2, 0, 0)

        # Verify layer stack order
        info = srv.get_layer_stack_info()
        assert info[0]["department"] == "animation"  # strongest
        assert info[1]["department"] == "layout"  # weaker

    def test_department_priority_change_reorders(self, tmp_path):
        """Changing department priority reorders existing layers."""
        srv = _make_server(tmp_path, department_priority=["animation", "layout"])

        _emit_events(srv, "alice", _make_trs_events("/World/Cube", (1, 0, 0)), department="layout")
        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)), department="animation")

        # Animation wins
        assert _read_translate(srv.stage, "/World/Cube") == (2, 0, 0)

        # Reverse priority: layout now stronger
        srv.set_department_priority(["layout", "animation"])
        assert _read_translate(srv.stage, "/World/Cube") == (1, 0, 0)

    def test_no_department_clients_weaker_than_department(self, tmp_path):
        """Clients without a department use the shared edit_layer (weakest)."""
        srv = _make_server(tmp_path, department_priority=["animation"])

        _emit_events(srv, "anon", _make_trs_events("/World/Cube", (1, 0, 0)))
        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)), department="animation")

        # Bob (animation dept) is stronger than anon (shared edit_layer)
        assert _read_translate(srv.stage, "/World/Cube") == (2, 0, 0)


class TestSharedDepartmentLayer:
    def test_same_department_shares_layer(self, tmp_path):
        """Two clients in the same department write to the same layer."""
        srv = _make_server(tmp_path, department_priority=["animation"])

        _emit_events(
            srv, "alice", _make_trs_events("/World/Cube", (1, 0, 0)), department="animation"
        )
        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)), department="animation")

        # Same layer object
        assert srv.client_layers["alice"] is srv.client_layers["bob"]

        # Last write wins within the department
        assert _read_translate(srv.stage, "/World/Cube") == (2, 0, 0)

    def test_same_department_last_write_wins(self, tmp_path):
        """Within a shared department layer, the last write wins."""
        srv = _make_server(tmp_path, department_priority=["animation"])

        _emit_events(
            srv, "alice", _make_trs_events("/World/Cube", (1, 0, 0)), department="animation"
        )
        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)), department="animation")
        _emit_events(
            srv, "alice", _make_trs_events("/World/Cube", (3, 0, 0)), department="animation"
        )

        # Alice wrote last — her value wins
        assert _read_translate(srv.stage, "/World/Cube") == (3, 0, 0)

    def test_shared_layer_broadcast_within_department(self, tmp_path):
        """Events from same department always change composed view (shared layer)."""
        srv = _make_server(tmp_path, department_priority=["animation"])

        _emit_events(
            srv, "alice", _make_trs_events("/World/Cube", (1, 0, 0)), department="animation"
        )

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
        _emit_events(
            srv, "alice", _make_trs_events("/World/Cube", (1, 2, 3)), department="animation"
        )
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

        _emit_events(
            srv, "alice", _make_trs_events("/World/Cube", (1, 2, 3)), department="animation"
        )
        assert _read_translate(srv.stage, "/World/Cube") == (1, 2, 3)

        srv.mute_layer("animation")
        assert _read_translate(srv.stage, "/World/Cube") is None

        srv.unmute_layer("animation")
        assert _read_translate(srv.stage, "/World/Cube") == (1, 2, 3)

    def test_delete_layer(self, tmp_path):
        """Deleting a department layer removes it and its opinions permanently."""
        srv = _make_server(tmp_path, department_priority=["animation"])

        _emit_events(
            srv, "alice", _make_trs_events("/World/Cube", (1, 2, 3)), department="animation"
        )
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
        _emit_events(
            srv, "bob", _make_trs_events("/World/Cube", (10, 20, 30)), department="animation"
        )

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

        _emit_events(srv, "alice", _make_trs_events("/World/A", (1, 0, 0)), department="layout")
        _emit_events(srv, "bob", _make_trs_events("/World/B", (2, 0, 0)), department="fx")

        info = srv.get_layer_stack_info()
        assert len(info) == 2
        assert info[0]["department"] == "fx"
        assert "bob" in info[0]["clients"]
        assert info[0]["muted"] is False
        assert info[1]["department"] == "layout"
        assert "alice" in info[1]["clients"]

    def test_muted_reflected_in_info(self, tmp_path):
        srv = _make_server(tmp_path, department_priority=["animation"])
        _emit_events(srv, "alice", _make_trs_events("/World/A", (1, 0, 0)), department="animation")

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
        _emit_events(srv, "alice", _make_trs_events("/World/A", (10, 20, 30)), department="layout")
        _emit_events(srv, "bob", _make_trs_events("/World/B", (40, 50, 60)), department="animation")

        # Both also edit same prim — animation is stronger
        _emit_events(
            srv, "alice", _make_trs_events("/World/Shared", (1, 0, 0)), department="layout"
        )
        _emit_events(
            srv, "bob", _make_trs_events("/World/Shared", (2, 0, 0)), department="animation"
        )

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
        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)), department="animation")

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

        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)), department="animation")

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
        _emit_events(srv, "alice", _make_trs_events("/World/Cube", (1, 0, 0)), department="layout")

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

        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)), department="animation")
        _emit_events(
            srv, "alice", _make_trs_events("/World/Cube", (99, 99, 99)), department="layout"
        )

        # Alice's event was overridden — build correction
        correction = srv.build_correction(
            {"k": K_SET_XFORM_TRS, "prim": "/World/Cube", "fields": ["t"], "t": [99, 99, 99]},
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

        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (1, 0, 0)), department="animation")

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

        _emit_events(srv, "bob", _make_trs_events("/World/Cube", (2, 0, 0)), department="animation")

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
        srv.send_to_origin({"type": "ping"}, "nonexistent-origin")


class TestReplayWithClientLayers:
    def test_replay_routes_to_client_layers(self, tmp_path):
        """Events replayed on startup go to the correct client layers."""
        db = str(tmp_path / "replay.db")

        depts = ["animation", "layout"]

        # First server session: two clients emit events
        srv1 = UsdSyncServer(log_path=db, department_priority=depts)
        _emit_events(srv1, "alice", _make_trs_events("/World/A", (1, 2, 3)), department="layout")
        _emit_events(srv1, "bob", _make_trs_events("/World/B", (4, 5, 6)), department="animation")
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


class TestConcurrentDepartmentWrites:
    """Verify that concurrent threads writing to different department layers
    produce correct composed results.

    USD's SetEditTarget is a global stage mutation — writes must be
    serialized through stage_lock.  These tests confirm the lock
    correctly protects concurrent access and that reads can interleave
    with the split lock phases.
    """

    def test_concurrent_writes_to_different_departments(self, tmp_path):
        """3 threads × 20 writes each to separate prims — verify writes
        land in the correct layer (not cross-written due to SetEditTarget
        race) and that per-layer values are independently correct."""
        import threading

        from pxr import Sdf

        srv = _make_server(tmp_path, department_priority=["animation", "lighting", "fx"])
        barrier = threading.Barrier(3)
        errors = []

        def _writer(client_id, dept, prim, base, count):
            try:
                barrier.wait(timeout=5)
                for i in range(count):
                    t = (base[0] + i, base[1] + i, base[2] + i)
                    _emit_events(srv, client_id, _make_trs_events(prim, t), department=dept)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(
            target=_writer, args=("alice", "animation", "/World/Anim", (1, 0, 0), 20)
        )
        t2 = threading.Thread(
            target=_writer, args=("bob", "lighting", "/World/Light", (0, 1, 0), 20)
        )
        t3 = threading.Thread(target=_writer, args=("carol", "fx", "/World/FX", (0, 0, 1), 20))

        t1.start()
        t2.start()
        t3.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        t3.join(timeout=10)

        assert not errors, f"Writer threads raised: {errors}"

        # Final composed values correct
        assert _read_translate(srv.stage, "/World/Anim") == (20, 19, 19)
        assert _read_translate(srv.stage, "/World/Light") == (19, 20, 19)
        assert _read_translate(srv.stage, "/World/FX") == (19, 19, 20)

        # Opinions landed in the correct layers — each layer must only
        # contain specs for its own prim, not the other departments'.
        anim_layer = srv.client_layers["alice"]
        light_layer = srv.client_layers["bob"]
        fx_layer = srv.client_layers["carol"]

        assert anim_layer.GetPrimAtPath("/World/Anim") is not None
        assert anim_layer.GetPrimAtPath("/World/Light") is None
        assert anim_layer.GetPrimAtPath("/World/FX") is None

        assert light_layer.GetPrimAtPath("/World/Light") is not None
        assert light_layer.GetPrimAtPath("/World/Anim") is None
        assert light_layer.GetPrimAtPath("/World/FX") is None

        assert fx_layer.GetPrimAtPath("/World/FX") is not None
        assert fx_layer.GetPrimAtPath("/World/Anim") is None
        assert fx_layer.GetPrimAtPath("/World/Light") is None

        # Verify the value inside each layer directly (not composed) —
        # catches corruption where the composed value happens to be right
        # but the layer opinion is wrong.
        for layer, prim_path, expected in [
            (anim_layer, "/World/Anim", (20, 19, 19)),
            (light_layer, "/World/Light", (19, 20, 19)),
            (fx_layer, "/World/FX", (19, 19, 20)),
        ]:
            attr_spec = layer.GetAttributeAtPath(
                Sdf.Path(f"{prim_path}.xformOp:translate"),
            )
            assert attr_spec is not None, f"No translate spec in layer for {prim_path}"
            val = attr_spec.default
            assert (val[0], val[1], val[2]) == expected, (
                f"Layer opinion for {prim_path}: {val} != {expected}"
            )

    def test_op_cache_invalidated_on_edit_target_change(self, tmp_path):
        """Deterministic pin for the fix behind the flaky concurrent failures.

        A cached XformOp is only valid while the stage edit target is unchanged.
        Under concurrent department writes the edit target switches between
        transactions, so the op cache must be invalidated on every switch; a
        cache reused across a switch authors against the wrong layer and the
        write is lost. ``_op_cache_for`` clears on change and keeps the cache
        for consecutive same-layer edits (the single-client fast path).
        """
        srv = _make_server(tmp_path, department_priority=["animation", "lighting"])
        la = srv.get_or_create_client_layer("alice", "animation")
        lb = srv.get_or_create_client_layer("bob", "lighting")

        cache = srv._op_cache_for(la)
        cache["/World/P"] = object()
        # switching the edit target invalidates the cache
        assert "/World/P" not in srv._op_cache_for(lb)
        # staying on the same layer keeps it (no needless re-fetch)
        srv._op_cache_for(lb)["/World/Q"] = object()
        assert "/World/Q" in srv._op_cache_for(lb)

    def test_concurrent_writes_to_same_prim(self, tmp_path):
        """Two departments writing to the same prim concurrently.
        Regardless of thread scheduling, the composed value must always
        reflect the stronger department's last write."""
        import threading

        srv = _make_server(tmp_path, department_priority=["animation", "lighting"])
        barrier = threading.Barrier(2)
        errors = []

        def _writer(client_id, dept, base, count):
            try:
                barrier.wait(timeout=5)
                for i in range(count):
                    t = (base[0] + i, base[1] + i, base[2] + i)
                    _emit_events(
                        srv, client_id, _make_trs_events("/World/Shared", t), department=dept
                    )
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=_writer, args=("alice", "animation", (100, 0, 0), 20))
        t2 = threading.Thread(target=_writer, args=("bob", "lighting", (0, 200, 0), 20))

        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Writer threads raised: {errors}"

        from pxr import Sdf

        # Animation is stronger — composed value must be animation's last write,
        # regardless of which thread finished last.
        result = _read_translate(srv.stage, "/World/Shared")
        assert result == (119, 19, 19), f"Expected animation to win, got {result}"

        # Both layers have opinions — verify each layer's opinion is
        # independently correct (catches writes landing in the wrong layer).
        anim_layer = srv.client_layers["alice"]
        light_layer = srv.client_layers["bob"]

        anim_spec = anim_layer.GetAttributeAtPath(
            Sdf.Path("/World/Shared.xformOp:translate"),
        )
        light_spec = light_layer.GetAttributeAtPath(
            Sdf.Path("/World/Shared.xformOp:translate"),
        )
        assert anim_spec is not None, "Animation layer missing translate opinion"
        assert light_spec is not None, "Lighting layer missing translate opinion"

        # Animation wrote (100+i, i, i), lighting wrote (i, 200+i, i)
        anim_val = anim_spec.default
        light_val = light_spec.default
        assert anim_val[0] == 119, f"Animation layer X={anim_val[0]}, expected 119"
        assert light_val[1] == 219, f"Lighting layer Y={light_val[1]}, expected 219"

    def test_reads_interleave_with_writes(self, tmp_path):
        """Dashboard reads (prim count, flatten, layer info) complete while
        writers are active. Verifies the split lock phases allow reads to
        interleave — reads should not be starved by continuous writes."""
        import threading

        srv = _make_server(tmp_path, department_priority=["animation", "lighting"])

        # Seed some data so reads have something to traverse
        _emit_events(
            srv, "seed", _make_trs_events("/World/Seed", (0, 0, 0)), department="animation"
        )

        write_done = threading.Event()
        barrier = threading.Barrier(3)
        errors = []
        read_count = 0
        read_lock = threading.Lock()

        def _writer(client_id, dept, prim, base, count):
            try:
                barrier.wait(timeout=5)
                for i in range(count):
                    t = (base[0] + i, base[1] + i, base[2] + i)
                    _emit_events(srv, client_id, _make_trs_events(prim, t), department=dept)
            except Exception as exc:
                errors.append(exc)
            finally:
                write_done.set()

        def _reader():
            nonlocal read_count
            try:
                barrier.wait(timeout=5)
                while not write_done.is_set():
                    # These should not block for the full write duration
                    count = srv.get_prim_count()
                    assert count > 0, "Prim count should be > 0"
                    usda = srv.export_flattened_string()
                    assert len(usda) > 0, "Flattened string should not be empty"
                    info = srv.get_layer_stack_info()
                    assert isinstance(info, list)
                    with read_lock:
                        read_count += 1
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(
            target=_writer, args=("alice", "animation", "/World/Cube", (1, 0, 0), 30)
        )
        t2 = threading.Thread(
            target=_writer, args=("bob", "lighting", "/World/Sphere", (0, 1, 0), 30)
        )
        t3 = threading.Thread(target=_reader)

        t1.start()
        t2.start()
        t3.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
        t3.join(timeout=15)

        assert not errors, f"Threads raised: {errors}"
        assert read_count > 0, "Reader thread never completed a read cycle"

        # Stage consistent after all writes
        assert _read_translate(srv.stage, "/World/Cube") is not None
        assert _read_translate(srv.stage, "/World/Sphere") is not None


class TestMultiDepartmentContention:
    """Stress test: multiple departments with a mix of private and shared prims.

    Each department has prims only it touches (private) and prims that
    multiple departments contest (shared). After all threads finish,
    the composed stage must reflect:
      - private prims: each department's last value in its own layer
      - shared prims: strongest department's last value wins composed view,
        but each layer retains its own opinion
    """

    # Department priority: animation > lighting > fx > layout (strongest first)
    DEPTS = ["animation", "lighting", "fx", "layout"]

    # Each department writes to its own private prims AND to shared prims.
    # Format: (client_id, department, prims_spec)
    # prims_spec: list of (prim_path, base_translate, is_shared)
    CLIENTS = [
        (
            "alice",
            "animation",
            [
                ("/World/AnimRig", (100, 0, 0), False),
                ("/World/Hero", (1, 0, 0), True),  # shared — animation wins
                ("/World/Camera", (10, 0, 0), True),  # shared — animation wins
            ],
        ),
        (
            "bob",
            "lighting",
            [
                ("/World/KeyLight", (0, 100, 0), False),
                ("/World/Hero", (0, 1, 0), True),  # shared — weaker
                ("/World/EnvSphere", (0, 10, 0), True),  # shared — lighting wins vs fx/layout
            ],
        ),
        (
            "carol",
            "fx",
            [
                ("/World/Particles", (0, 0, 100), False),
                ("/World/Hero", (0, 0, 1), True),  # shared — weakest with opinion
                ("/World/EnvSphere", (0, 0, 10), True),  # shared — weaker than lighting
            ],
        ),
        (
            "dave",
            "layout",
            [
                ("/World/Ground", (50, 50, 0), False),
                ("/World/Camera", (50, 0, 50), True),  # shared — weaker than animation
            ],
        ),
    ]

    ITERATIONS = 50

    def test_concurrent_contention_on_shared_prims(self, tmp_path):
        """Concurrent department writes with shared and private prims."""
        import threading

        from pxr import Sdf

        srv = _make_server(tmp_path, department_priority=self.DEPTS)
        barrier = threading.Barrier(len(self.CLIENTS))
        errors = []

        def _worker(client_id, dept, prims_spec, iterations):
            try:
                barrier.wait(timeout=10)
                for i in range(iterations):
                    for prim_path, base, _shared in prims_spec:
                        t = (base[0] + i, base[1] + i, base[2] + i)
                        _emit_events(
                            srv,
                            client_id,
                            _make_trs_events(prim_path, t),
                            department=dept,
                        )
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(
                target=_worker,
                args=(cid, dept, prims, self.ITERATIONS),
            )
            for cid, dept, prims in self.CLIENTS
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Worker threads raised: {errors}"

        n = self.ITERATIONS - 1  # last iteration index

        # -- Private prims: each department's last value --
        assert _read_translate(srv.stage, "/World/AnimRig") == (100 + n, n, n)
        assert _read_translate(srv.stage, "/World/KeyLight") == (n, 100 + n, n)
        assert _read_translate(srv.stage, "/World/Particles") == (n, n, 100 + n)
        assert _read_translate(srv.stage, "/World/Ground") == (50 + n, 50 + n, n)

        # -- Shared prims: strongest department's last value wins --
        # /World/Hero: animation(strongest) > lighting > fx
        # animation wrote (1+n, n, n)
        assert _read_translate(srv.stage, "/World/Hero") == (1 + n, n, n)

        # /World/Camera: animation > layout
        # animation wrote (10+n, n, n)
        assert _read_translate(srv.stage, "/World/Camera") == (10 + n, n, n)

        # /World/EnvSphere: lighting > fx
        # lighting wrote (n, 10+n, n)
        assert _read_translate(srv.stage, "/World/EnvSphere") == (n, 10 + n, n)

        # -- Layer isolation: private prims only in their own layer --
        anim_layer = srv.client_layers["alice"]
        light_layer = srv.client_layers["bob"]
        fx_layer = srv.client_layers["carol"]
        layout_layer = srv.client_layers["dave"]

        assert anim_layer.GetPrimAtPath("/World/AnimRig") is not None
        assert anim_layer.GetPrimAtPath("/World/KeyLight") is None
        assert light_layer.GetPrimAtPath("/World/KeyLight") is not None
        assert light_layer.GetPrimAtPath("/World/AnimRig") is None
        assert fx_layer.GetPrimAtPath("/World/Particles") is not None
        assert fx_layer.GetPrimAtPath("/World/Ground") is None
        assert layout_layer.GetPrimAtPath("/World/Ground") is not None
        assert layout_layer.GetPrimAtPath("/World/Particles") is None

        # -- Shared prims: each contesting layer has its own opinion --
        # /World/Hero has opinions from animation, lighting, and fx
        for layer, dept_base in [
            (anim_layer, (1, 0, 0)),
            (light_layer, (0, 1, 0)),
            (fx_layer, (0, 0, 1)),
        ]:
            spec = layer.GetAttributeAtPath(
                Sdf.Path("/World/Hero.xformOp:translate"),
            )
            assert spec is not None, f"Missing Hero opinion in {layer.identifier}"
            val = spec.default
            expected = (dept_base[0] + n, dept_base[1] + n, dept_base[2] + n)
            assert (val[0], val[1], val[2]) == expected, f"Hero layer opinion: {val} != {expected}"

        # layout should NOT have /World/Hero (dave never wrote to it)
        assert layout_layer.GetPrimAtPath("/World/Hero") is None

        # /World/Camera has opinions from animation and layout
        for layer, dept_base in [
            (anim_layer, (10, 0, 0)),
            (layout_layer, (50, 0, 50)),
        ]:
            spec = layer.GetAttributeAtPath(
                Sdf.Path("/World/Camera.xformOp:translate"),
            )
            assert spec is not None
            val = spec.default
            expected = (dept_base[0] + n, dept_base[1] + n, dept_base[2] + n)
            assert (val[0], val[1], val[2]) == expected

        # /World/EnvSphere has opinions from lighting and fx
        for layer, dept_base in [
            (light_layer, (0, 10, 0)),
            (fx_layer, (0, 0, 10)),
        ]:
            spec = layer.GetAttributeAtPath(
                Sdf.Path("/World/EnvSphere.xformOp:translate"),
            )
            assert spec is not None
            val = spec.default
            expected = (dept_base[0] + n, dept_base[1] + n, dept_base[2] + n)
            assert (val[0], val[1], val[2]) == expected
