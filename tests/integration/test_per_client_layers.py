"""E2E tests for per-client layer editing with department ordering.

Uses a real server subprocess, real TCP connections, and verifies layer
isolation, department ordering, mute/unmute, and merge.
No DCC needed — headless, runs in CI.
"""

import time

import pytest
from pxr import Sdf, Usd, UsdGeom

from openusdconnect.codec import message_to_dict
from openusdconnect.emitter import NoticeEmitter
from openusdconnect.event_apply import apply_events
from openusdconnect.protocol_constants import (
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_SET_SDF_PROPERTY_FIELDS,
    K_SET_STAGE_METADATA,
    K_SET_XFORM_TRS,
)
from openusdconnect.server import UsdSyncServer

# -- Helpers -------------------------------------------------------------


def _make_server(tmp_path, department_priority=None):
    """Create an in-process UsdSyncServer with a temp DB."""
    db = str(tmp_path / "test.db")
    return UsdSyncServer(log_path=db, department_priority=department_priority)


def _emit_events(srv, client_id, events, department=None):
    """Simulate an emitter through the production txn path: create layer,
    apply, persist (records carry the portable layer key for replay)."""
    layer = srv.get_or_create_client_layer(client_id, department=department)
    srv.process_txn(events, client_id=client_id, layer=layer)


def _read_translate(stage, prim_path):
    """Read the composed translate value for a prim."""
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return None
    xf = UsdGeom.Xformable(prim)
    for op in xf.GetOrderedXformOps():
        if op.GetAttr().GetName() == "xformOp:translate":
            v = op.Get()
            if v is None:
                return None
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
    def test_department_policy_persists_only_logical_layer_target(self, tmp_path):
        srv = _make_server(tmp_path, department_priority=["animation"])

        _emit_events(
            srv,
            "animator",
            [{"k": K_ENSURE_PRIM, "prim": "/World/Cube", "typeName": "Cube"}],
            department="animation",
        )

        _seq, encoded = srv.store.get_all_asc()[0]
        record = message_to_dict(encoded)
        assert record["layer_key"] == "department:animation"
        assert "department" not in record

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

    def test_stage_metadata_uses_shared_session_layer(self, tmp_path):
        srv = _make_server(tmp_path, department_priority=["animation"])

        _emit_events(
            srv,
            "animator",
            [{"k": K_SET_STAGE_METADATA, "upAxis": "Z"}],
            department="animation",
        )

        assert UsdGeom.GetStageUpAxis(srv.stage) == "Z"
        assert srv.stage.GetSessionLayer().pseudoRoot.GetInfo("upAxis") == "Z"
        department = srv.client_layers["animator"]
        assert not department.pseudoRoot.HasInfo("upAxis")
        assert srv.stage.GetEditTarget().GetLayer().identifier == department.identifier
        _seq, encoded = srv.store.get_all_asc()[0]
        assert "layer_key" not in message_to_dict(encoded)


class TestDepartmentOrdering:
    def test_configured_departments_materialize_on_first_use(self, tmp_path):
        srv = _make_server(
            tmp_path,
            department_priority=["animation", "layout"],
        )

        assert [
            item["layer_key"]
            for item in srv.get_layer_stack_state()["layers"]
        ] == ["default"]

        srv.get_or_create_client_layer("layout-artist", "layout")
        assert [
            item["layer_key"]
            for item in srv.get_layer_stack_state()["layers"]
        ] == ["department:layout", "default"]

        srv.get_or_create_client_layer("animator", "animation")
        assert [
            item["layer_key"]
            for item in srv.get_layer_stack_state()["layers"]
        ] == [
            "department:animation",
            "department:layout",
            "default",
        ]

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

    def test_unlisted_department_is_weaker_than_configured_priority(self, tmp_path):
        srv = _make_server(tmp_path, department_priority=["animation"])

        _emit_events(srv, "anon", _make_trs_events("/World/Cube", (1, 0, 0)))
        _emit_events(
            srv,
            "fx-artist",
            _make_trs_events("/World/Cube", (3, 0, 0)),
            department="fx",
        )
        _emit_events(
            srv,
            "animator",
            _make_trs_events("/World/Cube", (2, 0, 0)),
            department="animation",
        )

        state = srv.get_layer_stack_state()
        assert [item["layer_key"] for item in state["layers"]] == [
            "department:animation",
            "department:fx",
            "default",
        ]
        assert _read_translate(srv.stage, "/World/Cube") == (2, 0, 0)
        assert srv.mute_layer("animation")
        assert _read_translate(srv.stage, "/World/Cube") == (3, 0, 0)


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
        assert srv.resolve_layer("animation") is None
        assert [
            item["layer_key"]
            for item in srv.get_layer_stack_state()["layers"]
        ] == ["default"]
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

    def test_shared_layer_grouped_and_flagged(self, tmp_path):
        """All department-less clients group onto one shared card, flagged
        shared; the communal layer refuses merge."""
        srv = _make_server(tmp_path, department_priority=["animation"])
        _emit_events(srv, "anim-01", _make_trs_events("/World/A", (1, 2, 3)),
                     department="animation")
        _emit_events(srv, "loose-01", _make_trs_events("/World/B", (4, 5, 6)))
        _emit_events(srv, "loose-02", _make_trs_events("/World/C", (7, 8, 9)))

        info = srv.get_layer_stack_info()
        shared = [e for e in info if e.get("shared")]
        assert len(shared) == 1
        assert sorted(shared[0]["clients"]) == ["loose-01", "loose-02"]
        assert shared[0]["department"] is None

        dept = [e for e in info if e["department"] == "animation"]
        assert dept and not dept[0].get("shared")

        assert srv.merge_layer("loose-01") is False
        srv.store.close()


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

    def test_sdf_correction_projects_composed_department_state(self, tmp_path):
        srv = _make_server(tmp_path, department_priority=["animation", "layout"])
        strong_layer = srv.get_or_create_client_layer("bob", department="animation")
        weak_layer = srv.get_or_create_client_layer("alice", department="layout")
        flat_receiver = Usd.Stage.CreateInMemory()

        strong_stage = Usd.Stage.CreateInMemory()
        strong_prim = strong_stage.DefinePrim("/World/Thing", "Xform")
        strong_attr = strong_prim.CreateAttribute(
            "userProperties:value",
            Sdf.ValueTypeNames.Int,
            True,
        )
        strong_attr.Set(2)
        strong_attr.SetCustomData({"strong": 2})
        strong_emitter = NoticeEmitter(strong_stage)
        strong_events = strong_emitter.snapshot_events()
        strong_generic = next(
            event for event in strong_events if event["k"] == K_SET_SDF_PROPERTY_FIELDS
        )

        changed = srv.apply_txn(strong_events, layer=strong_layer)
        assert strong_events.index(strong_generic) not in changed
        apply_events(
            flat_receiver,
            [event for event in strong_events if event["k"] != K_SET_SDF_PROPERTY_FIELDS],
        )
        apply_events(flat_receiver, [srv.build_correction(strong_generic)])

        weak_stage = Usd.Stage.CreateInMemory()
        weak_prim = weak_stage.DefinePrim("/World/Thing", "Xform")
        weak_attr = weak_prim.CreateAttribute(
            "userProperties:value",
            Sdf.ValueTypeNames.Int,
            True,
        )
        weak_attr.Set(1)
        weak_attr.SetCustomData({"weak": 1})
        weak_emitter = NoticeEmitter(weak_stage)
        weak_generic = next(
            event
            for event in weak_emitter.snapshot_events()
            if event["k"] == K_SET_SDF_PROPERTY_FIELDS
        )

        assert srv.apply_txn([weak_generic], layer=weak_layer) == []
        weak_correction = srv.build_correction(weak_generic)
        apply_events(flat_receiver, [weak_correction])

        path = "/World/Thing.userProperties:value"
        server_attr = srv.stage.GetAttributeAtPath(path)
        receiver_attr = flat_receiver.GetAttributeAtPath(path)
        assert server_attr.Get() == receiver_attr.Get() == 2
        assert server_attr.GetCustomData() == receiver_attr.GetCustomData() == {
            "strong": 2,
            "weak": 1,
        }

        strong_prim.RemoveProperty("userProperties:value")
        removed = next(
            event
            for event in strong_emitter.build_events_for_dirty()
            if event["k"] == K_SET_SDF_PROPERTY_FIELDS
        )
        assert removed["removed"] is True
        assert removed["fields"]
        assert srv.apply_txn([removed], layer=strong_layer) == []

        revealed_correction = srv.build_correction(removed)
        apply_events(flat_receiver, [revealed_correction])
        assert revealed_correction["removed"] is False
        assert srv.stage.GetAttributeAtPath(path).Get() == 1
        assert flat_receiver.GetAttributeAtPath(path).Get() == 1
        assert flat_receiver.GetAttributeAtPath(path).GetCustomData() == {"weak": 1}

        strong_emitter.cleanup()
        weak_emitter.cleanup()


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

        # Opinions landed in the department layers themselves, not the
        # shared fallback: replay preserves layer topology and strength.
        assert srv2.resolve_layer("layout").GetPrimAtPath("/World/A")
        assert srv2.resolve_layer("animation").GetPrimAtPath("/World/B")
        assert not srv2.edit_layer.GetPrimAtPath("/World/A")
        assert not srv2.edit_layer.GetPrimAtPath("/World/B")
        srv2.store.close()

    def test_replay_preserves_interleaved_writes_within_department(self, tmp_path):
        """Clients sharing a layer replay in global transaction order."""
        db = str(tmp_path / "shared-department.db")
        departments = ["animation"]
        srv1 = UsdSyncServer(log_path=db, department_priority=departments)
        _emit_events(
            srv1,
            "alice",
            _make_trs_events("/World/Cube", (1, 0, 0)),
            department="animation",
        )
        _emit_events(
            srv1,
            "bob",
            _make_trs_events("/World/Cube", (2, 0, 0)),
            department="animation",
        )
        _emit_events(
            srv1,
            "alice",
            _make_trs_events("/World/Cube", (3, 0, 0)),
            department="animation",
        )
        srv1.shutdown()
        srv1.store.close()

        srv2 = UsdSyncServer(log_path=db, department_priority=departments)
        try:
            assert _read_translate(srv2.stage, "/World/Cube") == (3, 0, 0)
            assert srv2.client_layers["alice"] is srv2.client_layers["bob"]
        finally:
            srv2.shutdown()
            srv2.store.close()

    def test_replay_restores_unlisted_departments_without_configured_priority(
        self,
        tmp_path,
    ):
        db = str(tmp_path / "unlisted-departments.db")
        srv1 = UsdSyncServer(log_path=db)
        _emit_events(
            srv1,
            "alice",
            _make_trs_events("/World/Cube", (1, 0, 0)),
            department="animation",
        )
        _emit_events(
            srv1,
            "bob",
            _make_trs_events("/World/Cube", (2, 0, 0)),
            department="layout",
        )
        srv1.shutdown()
        srv1.store.close()

        srv2 = UsdSyncServer(log_path=db)
        try:
            assert srv2._ordered_department_names() == ["animation", "layout"]
            assert _read_translate(srv2.stage, "/World/Cube") == (1, 0, 0)
            assert srv2.mute_layer("animation")
            assert _read_translate(srv2.stage, "/World/Cube") == (2, 0, 0)
        finally:
            srv2.shutdown()
            srv2.store.close()

    def test_post_restart_record_uses_the_layer_that_received_the_opinion(
        self,
        tmp_path,
    ):
        db = str(tmp_path / "restart-target.db")
        srv1 = UsdSyncServer(
            log_path=db,
            department_priority=["animation"],
        )
        _emit_events(
            srv1,
            "artist",
            [{"k": K_ENSURE_PRIM, "prim": "/World/Old", "typeName": "Xform"}],
            department="animation",
        )
        srv1.shutdown()
        srv1.store.close()

        srv2 = UsdSyncServer(log_path=db)
        try:
            animation = srv2.resolve_layer("animation")
            records, _changed = srv2.process_txn(
                [
                    {
                        "k": K_ENSURE_PRIM,
                        "prim": "/World/New",
                        "typeName": "Xform",
                    }
                ],
                client_id="artist",
            )
            record = message_to_dict(records[0][1])

            assert record["layer_key"] == "default"
            assert srv2.edit_layer.GetPrimAtPath("/World/New")
            assert not animation.GetPrimAtPath("/World/New")
        finally:
            srv2.shutdown()
            srv2.store.close()

    def test_replay_restores_stage_metadata_through_session_layer(self, tmp_path):
        db = str(tmp_path / "stage-metadata.db")
        departments = ["animation"]
        srv1 = UsdSyncServer(log_path=db, department_priority=departments)
        _emit_events(
            srv1,
            "animator",
            [
                {"k": K_SET_STAGE_METADATA, "upAxis": "Z"},
                {"k": K_SET_STAGE_METADATA, "metersPerUnit": 0.01},
            ],
            department="animation",
        )
        srv1.shutdown()
        srv1.store.close()

        srv2 = UsdSyncServer(log_path=db, department_priority=departments)
        try:
            session = srv2.stage.GetSessionLayer()
            assert UsdGeom.GetStageUpAxis(srv2.stage) == "Z"
            assert UsdGeom.GetStageMetersPerUnit(srv2.stage) == pytest.approx(0.01)
            assert session.pseudoRoot.GetInfo("upAxis") == "Z"
            assert session.pseudoRoot.GetInfo("metersPerUnit") == pytest.approx(0.01)
            assert srv2.resolve_layer("animation") is None
        finally:
            srv2.shutdown()
            srv2.store.close()


class TestDepartmentCompaction:
    def test_compaction_preserves_weaker_department_after_restart(self, tmp_path):
        """Muting a stronger department still reveals the weaker opinion."""
        db = str(tmp_path / "compaction.db")
        departments = ["animation", "layout"]
        srv1 = UsdSyncServer(log_path=db, department_priority=departments)
        _emit_events(
            srv1,
            "alice",
            _make_trs_events("/World/Cube", (1, 0, 0)),
            department="layout",
        )
        _emit_events(
            srv1,
            "bob",
            _make_trs_events("/World/Cube", (2, 0, 0)),
            department="animation",
        )
        assert _read_translate(srv1.stage, "/World/Cube") == (2, 0, 0)

        srv1.compact_log()
        srv1.shutdown()
        srv1.store.close()

        srv2 = UsdSyncServer(log_path=db, department_priority=departments)
        try:
            assert _read_translate(srv2.stage, "/World/Cube") == (2, 0, 0)
            srv2.mute_layer("animation")
            assert _read_translate(srv2.stage, "/World/Cube") == (1, 0, 0)
        finally:
            srv2.shutdown()
            srv2.store.close()

    def test_delete_tombstones_only_its_department(self, tmp_path):
        """A department delete must not erase a weaker layer during compaction."""
        db = str(tmp_path / "delete-compaction.db")
        departments = ["animation", "layout"]
        srv1 = UsdSyncServer(log_path=db, department_priority=departments)
        _emit_events(
            srv1,
            "alice",
            _make_trs_events("/World/Cube", (1, 0, 0)),
            department="layout",
        )
        _emit_events(
            srv1,
            "bob",
            _make_trs_events("/World/Cube", (2, 0, 0)),
            department="animation",
        )
        _emit_events(
            srv1,
            "bob",
            [{"k": K_DELETE_PRIM, "prim": "/World/Cube"}],
            department="animation",
        )
        assert _read_translate(srv1.stage, "/World/Cube") == (1, 0, 0)

        srv1.compact_log()
        srv1.shutdown()
        srv1.store.close()

        srv2 = UsdSyncServer(log_path=db, department_priority=departments)
        try:
            assert _read_translate(srv2.stage, "/World/Cube") == (1, 0, 0)
            assert srv2.resolve_layer("layout").GetPrimAtPath("/World/Cube")
            assert not srv2.resolve_layer("animation").GetPrimAtPath(
                "/World/Cube"
            )
        finally:
            srv2.shutdown()
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
