"""Headless tests for NoticeEmitter extensions.

Tests creation, deletion, deactivation, rename detection,
suppress flag, and ChangeBlock batching — all DCC-agnostic.
"""

from pxr import Gf, Sdf, Usd, UsdGeom

from openusdconnect.emitter import NoticeEmitter
from openusdconnect.protocol import (
    K_DEACTIVATE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_RENAME_PRIM,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_VARIANT_SELECTIONS,
    K_SET_XFORM_TRS,
)


def _make_stage_and_emitter():
    """Create an in-memory stage with a session layer edit target and a NoticeEmitter."""
    stage = Usd.Stage.CreateInMemory()
    session = stage.GetSessionLayer()
    stage.SetEditTarget(Usd.EditTarget(session))
    emitter = NoticeEmitter(stage)
    return stage, emitter


class TestCreationDetection:
    """DefinePrim triggers ensure_prim + set_xform_trs events."""

    def test_define_prim_emits_creation_events(self):
        stage, emitter = _make_stage_and_emitter()

        stage.DefinePrim("/World", "Xform")
        stage.DefinePrim("/World/Sphere", "Sphere")

        events = emitter.build_events_for_dirty()

        ensure_prims = [e for e in events if e["k"] == K_ENSURE_PRIM]
        assert len(ensure_prims) >= 1
        prim_paths = {e["prim"] for e in ensure_prims}
        assert "/World/Sphere" in prim_paths

    def test_define_xform_prim_gets_trs(self):
        stage, emitter = _make_stage_and_emitter()

        prim = stage.DefinePrim("/World/Box", "Xform")
        xf = UsdGeom.Xformable(prim)
        xf.AddTranslateOp().Set(Gf.Vec3d(1, 2, 3))
        xf.AddOrientOp().Set(Gf.Quatf(1, 0, 0, 0))
        xf.AddScaleOp().Set(Gf.Vec3d(1, 1, 1))

        events = emitter.build_events_for_dirty()

        trs = [e for e in events if e["k"] == K_SET_XFORM_TRS and e["prim"] == "/World/Box"]
        assert len(trs) == 1
        assert "t" in trs[0]["fields"]

    def test_creation_tracks_known_prims(self):
        stage, emitter = _make_stage_and_emitter()

        stage.DefinePrim("/World/A", "Xform")
        emitter.build_events_for_dirty()

        assert "/World/A" in emitter._known_prims

    def test_creation_uses_prim_type_name(self):
        stage, emitter = _make_stage_and_emitter()

        stage.DefinePrim("/World/MySphere", "Sphere")

        events = emitter.build_events_for_dirty()
        ensure = [e for e in events if e["k"] == K_ENSURE_PRIM and e["prim"] == "/World/MySphere"]
        assert len(ensure) == 1
        assert ensure[0]["typeName"] == "Sphere"


class TestDeletionDetection:
    """RemovePrim triggers deactivate_prim event."""

    def test_remove_prim_emits_deactivate(self):
        stage, emitter = _make_stage_and_emitter()

        stage.DefinePrim("/World/Cube", "Xform")
        emitter.build_events_for_dirty()
        assert "/World/Cube" in emitter._known_prims

        stage.RemovePrim("/World/Cube")
        events = emitter.build_events_for_dirty()

        deact = [e for e in events if e["k"] == K_DEACTIVATE_PRIM]
        assert len(deact) == 1
        assert deact[0]["prim"] == "/World/Cube"
        assert deact[0]["active"] is False

    def test_deletion_cleans_caches(self):
        stage, emitter = _make_stage_and_emitter()

        stage.DefinePrim("/World/Cube", "Xform")
        emitter.build_events_for_dirty()

        stage.RemovePrim("/World/Cube")
        emitter.build_events_for_dirty()

        assert "/World/Cube" not in emitter._known_prims
        assert "/World/Cube" not in emitter.last_sent_trs


class TestDeactivationDetection:
    """SetActive(False) triggers deactivate_prim event."""

    def test_set_active_false_emits_deactivate(self):
        stage, emitter = _make_stage_and_emitter()

        prim = stage.DefinePrim("/World/Cone", "Xform")
        emitter.build_events_for_dirty()
        assert "/World/Cone" in emitter._known_prims

        prim.SetActive(False)
        events = emitter.build_events_for_dirty()

        deact = [e for e in events if e["k"] == K_DEACTIVATE_PRIM]
        assert len(deact) == 1
        assert deact[0]["prim"] == "/World/Cone"
        assert deact[0]["active"] is False

    def test_deactivation_cleans_caches(self):
        stage, emitter = _make_stage_and_emitter()

        prim = stage.DefinePrim("/World/Cone", "Xform")
        emitter.build_events_for_dirty()

        prim.SetActive(False)
        emitter.build_events_for_dirty()

        assert "/World/Cone" not in emitter._known_prims


class TestRenameDetection:
    """NamespaceEditor.RenamePrim triggers rename_prim event."""

    def test_rename_emits_rename_event(self):
        stage, emitter = _make_stage_and_emitter()

        prim = stage.DefinePrim("/World/OldName", "Xform")
        emitter.build_events_for_dirty()

        editor = Usd.NamespaceEditor(stage)
        editor.RenamePrim(prim, "NewName")
        assert editor.ApplyEdits()

        events = emitter.build_events_for_dirty()

        renames = [e for e in events if e["k"] == K_RENAME_PRIM]
        assert len(renames) == 1
        assert renames[0]["prim"] == "/World/OldName"
        assert renames[0]["new_name"] == "NewName"

    def test_rename_updates_known_prims(self):
        stage, emitter = _make_stage_and_emitter()

        prim = stage.DefinePrim("/World/OldName", "Xform")
        emitter.build_events_for_dirty()
        assert "/World/OldName" in emitter._known_prims

        editor = Usd.NamespaceEditor(stage)
        editor.RenamePrim(prim, "NewName")
        editor.ApplyEdits()
        emitter.build_events_for_dirty()

        assert "/World/OldName" not in emitter._known_prims
        assert "/World/NewName" in emitter._known_prims

    def test_rename_updates_trs_cache(self):
        stage, emitter = _make_stage_and_emitter()

        prim = stage.DefinePrim("/World/OldName", "Xform")
        xf = UsdGeom.Xformable(prim)
        xf.AddTranslateOp().Set(Gf.Vec3d(5, 0, 0))
        xf.AddOrientOp().Set(Gf.Quatf(1, 0, 0, 0))
        xf.AddScaleOp().Set(Gf.Vec3d(1, 1, 1))
        emitter.build_events_for_dirty()
        assert "/World/OldName" in emitter.last_sent_trs

        editor = Usd.NamespaceEditor(stage)
        editor.RenamePrim(prim, "NewName")
        editor.ApplyEdits()
        emitter.build_events_for_dirty()

        assert "/World/OldName" not in emitter.last_sent_trs
        assert "/World/NewName" in emitter.last_sent_trs


class TestSuppressFlag:
    """Suppress flag blocks event collection."""

    def test_suppress_blocks_collection(self):
        stage, emitter = _make_stage_and_emitter()

        emitter.suppress()
        stage.DefinePrim("/World/Hidden", "Xform")

        events = emitter.build_events_for_dirty()
        assert len(events) == 0

    def test_unsuppress_resumes_collection(self):
        stage, emitter = _make_stage_and_emitter()

        emitter.suppress()
        stage.DefinePrim("/World/Hidden", "Xform")

        emitter.unsuppress()
        stage.DefinePrim("/World/Visible", "Xform")

        events = emitter.build_events_for_dirty()
        prim_paths = {e["prim"] for e in events if e["k"] == K_ENSURE_PRIM}
        assert "/World/Visible" in prim_paths
        assert "/World/Hidden" not in prim_paths


class TestChangeBlockBatching:
    """Multiple changes in one ChangeBlock produce a single batch of events."""

    def test_changeblock_batches_multiple_changes(self):
        stage, emitter = _make_stage_and_emitter()

        # Create prims outside ChangeBlock (structural ops)
        prim_a = stage.DefinePrim("/World/A", "Xform")
        prim_b = stage.DefinePrim("/World/B", "Xform")

        # Flush creation events
        emitter.build_events_for_dirty()

        # Set up xform ops
        xf_a = UsdGeom.Xformable(prim_a)
        t_a = xf_a.AddTranslateOp()
        xf_a.AddOrientOp().Set(Gf.Quatf(1, 0, 0, 0))
        xf_a.AddScaleOp().Set(Gf.Vec3d(1, 1, 1))

        xf_b = UsdGeom.Xformable(prim_b)
        t_b = xf_b.AddTranslateOp()
        xf_b.AddOrientOp().Set(Gf.Quatf(1, 0, 0, 0))
        xf_b.AddScaleOp().Set(Gf.Vec3d(1, 1, 1))

        # Flush ops setup events
        emitter.build_events_for_dirty()

        # Now batch value changes inside a ChangeBlock
        with Sdf.ChangeBlock():
            t_a.Set(Gf.Vec3d(10, 0, 0))
            t_b.Set(Gf.Vec3d(0, 20, 0))

        events = emitter.build_events_for_dirty()

        trs = [e for e in events if e["k"] == K_SET_XFORM_TRS]
        trs_prims = {e["prim"] for e in trs}
        assert "/World/A" in trs_prims
        assert "/World/B" in trs_prims


class TestParentBeforeChildOrdering:
    """Events for parent prims are emitted before child prims."""

    def test_parent_emitted_before_child(self):
        stage, emitter = _make_stage_and_emitter()

        # Create a deep hierarchy in one batch
        stage.DefinePrim("/World", "Xform")
        stage.DefinePrim("/World/Parent", "Xform")
        stage.DefinePrim("/World/Parent/Child", "Xform")
        stage.DefinePrim("/World/Parent/Child/Grandchild", "Sphere")

        events = emitter.build_events_for_dirty()

        # Extract ensure_prim events and verify ordering
        ensure_prims = [e for e in events if e["k"] == K_ENSURE_PRIM]
        ensure_paths = [e["prim"] for e in ensure_prims]

        # Each path should appear after its parent
        for i, path in enumerate(ensure_paths):
            parent = path.rsplit("/", 1)[0]
            if parent and parent in ensure_paths:
                parent_idx = ensure_paths.index(parent)
                assert parent_idx < i, (
                    f"Parent {parent} (idx={parent_idx}) should come before child {path} (idx={i})"
                )


class TestClearAll:
    """clear_all() flushes without building events."""

    def test_clear_all_discards_pending(self):
        stage, emitter = _make_stage_and_emitter()

        stage.DefinePrim("/World/Temp", "Xform")
        emitter.clear_all()

        events = emitter.build_events_for_dirty()
        assert len(events) == 0


class TestRotationRoundTrip:
    """Rotation authored via xform ops appears in NoticeEmitter events."""

    def test_rotation_round_trip(self):
        stage, emitter = _make_stage_and_emitter()

        prim = stage.DefinePrim("/World/Rot", "Xform")
        xf = UsdGeom.Xformable(prim)
        xf.AddTranslateOp().Set(Gf.Vec3d(0, 0, 0))
        xf.AddOrientOp().Set(Gf.Quatf(0.707, 0.707, 0, 0))  # ~90° X
        xf.AddScaleOp().Set(Gf.Vec3d(1, 1, 1))

        events = emitter.build_events_for_dirty()

        trs = [e for e in events if e["k"] == K_SET_XFORM_TRS and e["prim"] == "/World/Rot"]
        assert len(trs) == 1
        assert "r" in trs[0]["fields"]
        r = trs[0]["r"]
        assert abs(r[0] - 0.707) < 0.01  # w
        assert abs(r[1] - 0.707) < 0.01  # x


class TestScaleRoundTrip:
    """Non-uniform scale authored via xform ops appears in NoticeEmitter events."""

    def test_scale_round_trip(self):
        stage, emitter = _make_stage_and_emitter()

        prim = stage.DefinePrim("/World/Scaled", "Xform")
        xf = UsdGeom.Xformable(prim)
        xf.AddTranslateOp().Set(Gf.Vec3d(0, 0, 0))
        xf.AddOrientOp().Set(Gf.Quatf(1, 0, 0, 0))
        xf.AddScaleOp().Set(Gf.Vec3d(2, 3, 4))

        events = emitter.build_events_for_dirty()

        trs = [e for e in events if e["k"] == K_SET_XFORM_TRS and e["prim"] == "/World/Scaled"]
        assert len(trs) == 1
        assert "s" in trs[0]["fields"]
        s = trs[0]["s"]
        assert abs(s[0] - 2.0) < 1e-6
        assert abs(s[1] - 3.0) < 1e-6
        assert abs(s[2] - 4.0) < 1e-6


class TestBaseLayerPrimTRS:
    """Prims on the base layer with only translate get full TRS after ops are added."""

    def test_trs_round_trip_on_base_layer_prim(self):
        """Simulates a base file with only xformOp:translate.

        After adding orient/scale ops and authoring values, all three
        TRS components must appear in events.
        """
        stage, emitter = _make_stage_and_emitter()

        # Simulate a base-layer prim with only translate
        prim = stage.DefinePrim("/World/Cube", "Xform")
        xf = UsdGeom.Xformable(prim)
        xf.AddTranslateOp().Set(Gf.Vec3d(1, 2, 3))

        # Flush initial creation events
        emitter.build_events_for_dirty()

        # Now add orient/scale ops (simulating _ensure_xform_ops)
        from openusdconnect.event_apply import ensure_canonical_ops

        ensure_canonical_ops(stage, "/World/Cube")

        # Author rotation and scale values
        from openusdconnect.event_apply import find_op

        xf = UsdGeom.Xformable(prim)
        find_op(xf, "orient").Set(Gf.Quatf(0.707, 0, 0.707, 0))  # ~90° Y
        find_op(xf, "scale").Set(Gf.Vec3d(2, 2, 2))

        events = emitter.build_events_for_dirty()

        trs = [e for e in events if e["k"] == K_SET_XFORM_TRS and e["prim"] == "/World/Cube"]
        assert len(trs) == 1
        fields = trs[0]["fields"]
        assert "r" in fields, "Rotation field missing from events"
        assert "s" in fields, "Scale field missing from events"

    def test_existing_prim_gets_full_trs(self):
        """Prim exists on base layer with only translate op.

        After ensure_canonical_ops and writing values, NoticeEmitter
        must emit all TRS fields (not just translate).
        """
        stage, emitter = _make_stage_and_emitter()

        # Create prim with only translate (mimics base layer)
        prim = stage.DefinePrim("/World/Box", "Xform")
        xf = UsdGeom.Xformable(prim)
        xf.AddTranslateOp().Set(Gf.Vec3d(5, 0, 0))

        # Flush creation
        emitter.build_events_for_dirty()

        # Add canonical ops and set non-identity values
        from openusdconnect.event_apply import ensure_canonical_ops, find_op

        ensure_canonical_ops(stage, "/World/Box")

        xf = UsdGeom.Xformable(prim)
        find_op(xf, "translate").Set(Gf.Vec3d(10, 0, 0))
        find_op(xf, "orient").Set(Gf.Quatf(0.5, 0.5, 0.5, 0.5))
        find_op(xf, "scale").Set(Gf.Vec3d(3, 3, 3))

        events = emitter.build_events_for_dirty()

        trs = [e for e in events if e["k"] == K_SET_XFORM_TRS and e["prim"] == "/World/Box"]
        assert len(trs) == 1
        assert "t" in trs[0]["fields"]
        assert "r" in trs[0]["fields"]
        assert "s" in trs[0]["fields"]


class TestRootPrimRotationNotDoubled:
    """Root prim rotation should not be doubled when applied to receiver."""

    def test_root_prim_rotation_not_doubled(self):
        """Simulate emitter/receiver: root prim with 90° rotation.

        Events from the emitter applied to a receiver stage that already
        has the same prim should result in the same transform, not doubled.
        """
        from openusdconnect.event_apply import apply_event, ensure_canonical_ops

        # Emitter stage: root prim with 90° X rotation
        emitter_stage = Usd.Stage.CreateInMemory()
        session = emitter_stage.GetSessionLayer()
        emitter_stage.SetEditTarget(Usd.EditTarget(session))

        emitter_stage.DefinePrim("/World", "Xform")
        _, xf, t, o, s = ensure_canonical_ops(emitter_stage, "/World")
        t.Set(Gf.Vec3d(0, 0, 0))
        o.Set(Gf.Quatf(0.707, 0.707, 0, 0))  # ~90° X
        s.Set(Gf.Vec3d(1, 1, 1))

        emitter = NoticeEmitter(emitter_stage)
        emitter.mark_dirty("/World")
        events = emitter.build_events_for_dirty()

        # Receiver stage: same prim already exists with same rotation
        receiver_stage = Usd.Stage.CreateInMemory()
        recv_session = receiver_stage.GetSessionLayer()
        receiver_stage.SetEditTarget(Usd.EditTarget(recv_session))

        receiver_stage.DefinePrim("/World", "Xform")
        _, rxf, rt, ro, rs = ensure_canonical_ops(receiver_stage, "/World")
        rt.Set(Gf.Vec3d(0, 0, 0))
        ro.Set(Gf.Quatf(0.707, 0.707, 0, 0))  # same rotation
        rs.Set(Gf.Vec3d(1, 1, 1))

        # Apply emitter events to receiver
        for ev in events:
            apply_event(receiver_stage, ev)

        # Verify rotation is the same (not doubled)
        rxf2 = UsdGeom.Xformable(receiver_stage.GetPrimAtPath("/World"))
        from openusdconnect.event_apply import find_op

        orient = find_op(rxf2, "orient")
        q = orient.Get()
        assert abs(float(q.GetReal()) - 0.707) < 0.01
        assert abs(float(q.GetImaginary()[0]) - 0.707) < 0.01
        assert abs(float(q.GetImaginary()[1])) < 0.01
        assert abs(float(q.GetImaginary()[2])) < 0.01


class TestFirstEncounterStructuralEvents:
    """ensure_prim/ensure_xform_ops only emitted on first encounter."""

    def test_no_structural_events_on_second_dirty(self):
        stage, emitter = _make_stage_and_emitter()

        prim = stage.DefinePrim("/World/Obj", "Xform")
        xf = UsdGeom.Xformable(prim)
        xf.AddTranslateOp().Set(Gf.Vec3d(1, 0, 0))
        xf.AddOrientOp().Set(Gf.Quatf(1, 0, 0, 0))
        xf.AddScaleOp().Set(Gf.Vec3d(1, 1, 1))

        # First encounter — should have ensure_prim
        events1 = emitter.build_events_for_dirty()
        ensure1 = [e for e in events1 if e["k"] == K_ENSURE_PRIM and e["prim"] == "/World/Obj"]
        assert len(ensure1) == 1

        # Change translate
        xf.GetOrderedXformOps()[0].Set(Gf.Vec3d(5, 0, 0))
        events2 = emitter.build_events_for_dirty()

        ensure2 = [e for e in events2 if e["k"] == K_ENSURE_PRIM and e["prim"] == "/World/Obj"]
        assert len(ensure2) == 0, "ensure_prim should not be emitted on subsequent updates"

        ops2 = [e for e in events2 if e["k"] == K_ENSURE_XFORM_OPS and e["prim"] == "/World/Obj"]
        assert len(ops2) == 0, "ensure_xform_ops should not be emitted on subsequent updates"

        trs2 = [e for e in events2 if e["k"] == K_SET_XFORM_TRS and e["prim"] == "/World/Obj"]
        assert len(trs2) == 1


class TestSnapshotPrim:
    """NoticeEmitter.snapshot_prim edge cases."""

    def test_invalid_prim_returns_none(self):
        stage, emitter = _make_stage_and_emitter()
        assert emitter.snapshot_prim("/World/Missing") is None

    def test_valid_prim_returns_trs(self):
        stage, emitter = _make_stage_and_emitter()
        from openusdconnect.event_apply import ensure_canonical_ops

        stage.DefinePrim("/World/Obj", "Xform")
        ensure_canonical_ops(stage, "/World/Obj")
        snap = emitter.snapshot_prim("/World/Obj")
        assert snap is not None
        assert "t" in snap and "r" in snap and "s" in snap
        assert "local_m16" in snap and "world_m16" in snap


class TestMarkDirty:
    def test_mark_dirty_adds_to_dirty_set(self):
        stage, emitter = _make_stage_and_emitter()
        emitter.mark_dirty("/World/Foo")
        assert "/World/Foo" in emitter.dirty


class TestDirtyPrimSkippedIfInvalid:
    """build_events_for_dirty skips prims that snapshot returns None for."""

    def test_dirty_invalid_prim_produces_no_events(self):
        stage, emitter = _make_stage_and_emitter()
        # Mark a non-existent prim as dirty
        emitter.dirty.add("/World/Ghost")
        events = emitter.build_events_for_dirty()
        ghost_events = [e for e in events if e.get("prim") == "/World/Ghost"]
        assert len(ghost_events) == 0


class TestPrimPathFromNoticePath:
    """Test the _prim_path_from_notice_path helper."""

    def test_property_path_extracted(self):
        from openusdconnect.emitter import _prim_path_from_notice_path

        assert _prim_path_from_notice_path("/World/Sphere.xformOp:translate") == "/World/Sphere"

    def test_prim_path_passthrough(self):
        from openusdconnect.emitter import _prim_path_from_notice_path

        assert _prim_path_from_notice_path("/World/Sphere") == "/World/Sphere"

    def test_non_absolute_returns_none(self):
        from openusdconnect.emitter import _prim_path_from_notice_path

        assert _prim_path_from_notice_path("relative/path") is None


class TestMatrixHelpers:
    """Test mat_to_16 and decompose_trs_from_matrix."""

    def test_mat_to_16_identity(self):
        from openusdconnect.emitter import mat_to_16

        m = Gf.Matrix4d(1)
        result = mat_to_16(m)
        assert len(result) == 16
        assert result[0] == 1.0
        assert result[5] == 1.0
        assert result[10] == 1.0
        assert result[15] == 1.0

    def test_as_matrix_tuple(self):
        from openusdconnect.emitter import as_matrix

        assert as_matrix((42, "extra")) == 42

    def test_as_matrix_direct(self):
        from openusdconnect.emitter import as_matrix

        assert as_matrix(42) == 42


class TestPayloadEmission:
    """NoticeEmitter detects and emits set_payload events."""

    @staticmethod
    def _make_pay_stage():
        """Create a small in-memory stage suitable for use as a payload."""
        pay_stage = Usd.Stage.CreateInMemory()
        pay_stage.DefinePrim("/Model", "Xform")
        return pay_stage

    def test_payload_emitted_on_first_encounter(self):
        stage, emitter = _make_stage_and_emitter()
        pay_stage = self._make_pay_stage()
        pay_id = pay_stage.GetRootLayer().identifier

        prim = stage.DefinePrim("/World/Asset", "Xform")
        prim.GetPayloads().AddPayload(pay_id, "/Model")

        events = emitter.build_events_for_dirty()
        pay_evs = [e for e in events if e["k"] == K_SET_PAYLOAD and e["prim"] == "/World/Asset"]
        assert len(pay_evs) == 1
        assert len(pay_evs[0]["payloads"]) == 1
        assert pay_evs[0]["payloads"][0]["asset_path"] == pay_id
        assert pay_evs[0]["payloads"][0]["prim_path"] == "/Model"

    def test_payload_change_emits_new_event(self):
        stage, emitter = _make_stage_and_emitter()
        pay_a = self._make_pay_stage()
        pay_b = self._make_pay_stage()

        prim = stage.DefinePrim("/World/Obj", "Xform")
        prim.GetPayloads().AddPayload(pay_a.GetRootLayer().identifier, "/Model")
        emitter.build_events_for_dirty()

        prim.GetPayloads().ClearPayloads()
        prim.GetPayloads().AddPayload(pay_b.GetRootLayer().identifier, "/Model")

        events = emitter.build_events_for_dirty()
        pay_evs = [e for e in events if e["k"] == K_SET_PAYLOAD and e["prim"] == "/World/Obj"]
        assert len(pay_evs) == 1
        assert pay_evs[0]["payloads"][0]["asset_path"] == pay_b.GetRootLayer().identifier

    def test_no_payload_no_event(self):
        stage, emitter = _make_stage_and_emitter()
        stage.DefinePrim("/World/Plain", "Xform")

        events = emitter.build_events_for_dirty()
        pay_evs = [e for e in events if e["k"] == K_SET_PAYLOAD and e["prim"] == "/World/Plain"]
        assert len(pay_evs) == 0

    def test_unchanged_payload_no_event(self):
        stage, emitter = _make_stage_and_emitter()
        pay_stage = self._make_pay_stage()

        prim = stage.DefinePrim("/World/Stable", "Xform")
        prim.GetPayloads().AddPayload(pay_stage.GetRootLayer().identifier, "/Model")
        emitter.build_events_for_dirty()

        # Dirty the prim via TRS change (no payload change)
        xf = UsdGeom.Xformable(prim)
        xf.AddTranslateOp().Set(Gf.Vec3d(1, 0, 0))

        events = emitter.build_events_for_dirty()
        pay_evs = [e for e in events if e["k"] == K_SET_PAYLOAD and e["prim"] == "/World/Stable"]
        assert len(pay_evs) == 0

    def test_payload_cache_cleaned_on_deletion(self):
        stage, emitter = _make_stage_and_emitter()
        pay_stage = self._make_pay_stage()

        prim = stage.DefinePrim("/World/Del", "Xform")
        prim.GetPayloads().AddPayload(pay_stage.GetRootLayer().identifier)
        emitter.build_events_for_dirty()
        assert "/World/Del" in emitter.last_sent_payloads

        stage.RemovePrim("/World/Del")
        emitter.build_events_for_dirty()
        assert "/World/Del" not in emitter.last_sent_payloads

    def test_payload_cache_updated_on_rename(self):
        stage, emitter = _make_stage_and_emitter()
        pay_stage = self._make_pay_stage()

        prim = stage.DefinePrim("/World/OldPay", "Xform")
        prim.GetPayloads().AddPayload(pay_stage.GetRootLayer().identifier)
        emitter.build_events_for_dirty()
        assert "/World/OldPay" in emitter.last_sent_payloads

        editor = Usd.NamespaceEditor(stage)
        editor.RenamePrim(prim, "NewPay")
        editor.ApplyEdits()
        emitter.build_events_for_dirty()

        assert "/World/OldPay" not in emitter.last_sent_payloads
        assert "/World/NewPay" in emitter.last_sent_payloads

    def test_payload_cleared_emits_empty(self):
        stage, emitter = _make_stage_and_emitter()
        pay_stage = self._make_pay_stage()

        prim = stage.DefinePrim("/World/Clr", "Xform")
        prim.GetPayloads().AddPayload(pay_stage.GetRootLayer().identifier)
        emitter.build_events_for_dirty()

        prim.GetPayloads().ClearPayloads()

        events = emitter.build_events_for_dirty()
        pay_evs = [e for e in events if e["k"] == K_SET_PAYLOAD and e["prim"] == "/World/Clr"]
        assert len(pay_evs) == 1
        assert pay_evs[0]["payloads"] == []

    def test_multiple_payloads_emitted(self):
        stage, emitter = _make_stage_and_emitter()
        pay_a = self._make_pay_stage()
        pay_b = self._make_pay_stage()

        prim = stage.DefinePrim("/World/Multi", "Xform")
        prim.GetPayloads().AddPayload(pay_a.GetRootLayer().identifier, "/Model")
        prim.GetPayloads().AddPayload(pay_b.GetRootLayer().identifier, "/Model")

        events = emitter.build_events_for_dirty()
        pay_evs = [e for e in events if e["k"] == K_SET_PAYLOAD and e["prim"] == "/World/Multi"]
        assert len(pay_evs) == 1
        assert len(pay_evs[0]["payloads"]) == 2


class TestReferenceEmission:
    """NoticeEmitter detects and emits set_reference events."""

    @staticmethod
    def _make_ref_stage():
        """Create a small in-memory stage suitable for referencing."""
        ref_stage = Usd.Stage.CreateInMemory()
        ref_stage.DefinePrim("/Model", "Xform")
        return ref_stage

    def test_reference_emitted_on_first_encounter(self):
        stage, emitter = _make_stage_and_emitter()
        ref_stage = self._make_ref_stage()
        ref_id = ref_stage.GetRootLayer().identifier

        prim = stage.DefinePrim("/World/Chair", "Xform")
        prim.GetReferences().AddReference(ref_id, "/Model")

        events = emitter.build_events_for_dirty()
        ref_evs = [e for e in events if e["k"] == K_SET_REFERENCE and e["prim"] == "/World/Chair"]
        assert len(ref_evs) == 1
        assert len(ref_evs[0]["refs"]) == 1
        assert ref_evs[0]["refs"][0]["asset_path"] == ref_id
        assert ref_evs[0]["refs"][0]["prim_path"] == "/Model"

    def test_reference_change_emits_new_event(self):
        stage, emitter = _make_stage_and_emitter()
        ref_a = self._make_ref_stage()
        ref_b = self._make_ref_stage()

        prim = stage.DefinePrim("/World/Obj", "Xform")
        prim.GetReferences().AddReference(ref_a.GetRootLayer().identifier, "/Model")
        emitter.build_events_for_dirty()

        # Change reference
        prim.GetReferences().ClearReferences()
        prim.GetReferences().AddReference(ref_b.GetRootLayer().identifier, "/Model")

        events = emitter.build_events_for_dirty()
        ref_evs = [e for e in events if e["k"] == K_SET_REFERENCE and e["prim"] == "/World/Obj"]
        assert len(ref_evs) == 1
        assert ref_evs[0]["refs"][0]["asset_path"] == ref_b.GetRootLayer().identifier

    def test_no_reference_no_event(self):
        stage, emitter = _make_stage_and_emitter()
        stage.DefinePrim("/World/Plain", "Xform")

        events = emitter.build_events_for_dirty()
        ref_evs = [e for e in events if e["k"] == K_SET_REFERENCE and e["prim"] == "/World/Plain"]
        assert len(ref_evs) == 0

    def test_unchanged_reference_no_event(self):
        stage, emitter = _make_stage_and_emitter()
        ref_stage = self._make_ref_stage()

        prim = stage.DefinePrim("/World/Stable", "Xform")
        prim.GetReferences().AddReference(ref_stage.GetRootLayer().identifier, "/Model")

        # First flush
        emitter.build_events_for_dirty()

        # Dirty the prim via TRS change (no ref change)
        xf = UsdGeom.Xformable(prim)
        xf.AddTranslateOp().Set(Gf.Vec3d(1, 0, 0))

        events = emitter.build_events_for_dirty()
        ref_evs = [e for e in events if e["k"] == K_SET_REFERENCE and e["prim"] == "/World/Stable"]
        assert len(ref_evs) == 0

    def test_reference_cache_cleaned_on_deletion(self):
        stage, emitter = _make_stage_and_emitter()
        ref_stage = self._make_ref_stage()

        prim = stage.DefinePrim("/World/Del", "Xform")
        prim.GetReferences().AddReference(ref_stage.GetRootLayer().identifier)
        emitter.build_events_for_dirty()
        assert "/World/Del" in emitter.last_sent_references

        stage.RemovePrim("/World/Del")
        emitter.build_events_for_dirty()
        assert "/World/Del" not in emitter.last_sent_references

    def test_reference_cache_updated_on_rename(self):
        stage, emitter = _make_stage_and_emitter()
        ref_stage = self._make_ref_stage()

        prim = stage.DefinePrim("/World/OldRef", "Xform")
        prim.GetReferences().AddReference(ref_stage.GetRootLayer().identifier)
        emitter.build_events_for_dirty()
        assert "/World/OldRef" in emitter.last_sent_references

        editor = Usd.NamespaceEditor(stage)
        editor.RenamePrim(prim, "NewRef")
        editor.ApplyEdits()
        emitter.build_events_for_dirty()

        assert "/World/OldRef" not in emitter.last_sent_references
        assert "/World/NewRef" in emitter.last_sent_references

    def test_reference_without_prim_path(self):
        stage, emitter = _make_stage_and_emitter()
        ref_stage = self._make_ref_stage()
        ref_id = ref_stage.GetRootLayer().identifier

        prim = stage.DefinePrim("/World/NoPP", "Xform")
        prim.GetReferences().AddReference(ref_id)

        events = emitter.build_events_for_dirty()
        ref_evs = [e for e in events if e["k"] == K_SET_REFERENCE and e["prim"] == "/World/NoPP"]
        assert len(ref_evs) == 1
        assert "prim_path" not in ref_evs[0]["refs"][0]

    def test_reference_cleared_emits_empty_refs(self):
        stage, emitter = _make_stage_and_emitter()
        ref_stage = self._make_ref_stage()

        prim = stage.DefinePrim("/World/Clr", "Xform")
        prim.GetReferences().AddReference(ref_stage.GetRootLayer().identifier)
        emitter.build_events_for_dirty()

        # Clear references
        prim.GetReferences().ClearReferences()

        events = emitter.build_events_for_dirty()
        ref_evs = [e for e in events if e["k"] == K_SET_REFERENCE and e["prim"] == "/World/Clr"]
        assert len(ref_evs) == 1
        assert ref_evs[0]["refs"] == []

    def test_multiple_references_emitted(self):
        stage, emitter = _make_stage_and_emitter()
        ref_a = self._make_ref_stage()
        ref_b = self._make_ref_stage()

        prim = stage.DefinePrim("/World/Multi", "Xform")
        prim.GetReferences().AddReference(ref_a.GetRootLayer().identifier, "/Model")
        prim.GetReferences().AddReference(ref_b.GetRootLayer().identifier, "/Model")

        events = emitter.build_events_for_dirty()
        ref_evs = [e for e in events if e["k"] == K_SET_REFERENCE and e["prim"] == "/World/Multi"]
        assert len(ref_evs) == 1
        assert len(ref_evs[0]["refs"]) == 2


class TestVariantSelectionEmission:
    """NoticeEmitter detects and emits set_variant_selections events."""

    @staticmethod
    def _make_variant_stage_and_emitter():
        """Open the variant_sphere fixture with a session-layer emitter."""
        import os

        fixture = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "fixtures", "variant_sphere.usda"
        )
        stage = Usd.Stage.Open(fixture)
        session = stage.GetSessionLayer()
        stage.SetEditTarget(Usd.EditTarget(session))
        emitter = NoticeEmitter(stage)
        return stage, emitter

    def test_variant_emitted_on_first_encounter(self):
        stage, emitter = self._make_variant_stage_and_emitter()
        # Dirty the prim so it gets processed
        emitter.mark_dirty("/World/Sphere")
        events = emitter.build_events_for_dirty()
        vsel = [e for e in events if e["k"] == K_SET_VARIANT_SELECTIONS]
        assert len(vsel) == 1
        assert vsel[0]["selections"] == {"size": "small"}

    def test_variant_change_detected(self):
        stage, emitter = self._make_variant_stage_and_emitter()
        emitter.mark_dirty("/World/Sphere")
        emitter.build_events_for_dirty()  # first flush

        # Change selection
        prim = stage.GetPrimAtPath("/World/Sphere")
        prim.GetVariantSets().GetVariantSet("size").SetVariantSelection("large")
        events = emitter.build_events_for_dirty()
        vsel = [e for e in events if e["k"] == K_SET_VARIANT_SELECTIONS]
        assert len(vsel) == 1
        assert vsel[0]["selections"]["size"] == "large"

    def test_unchanged_variant_no_event(self):
        stage, emitter = self._make_variant_stage_and_emitter()
        emitter.mark_dirty("/World/Sphere")
        emitter.build_events_for_dirty()  # cache the selection

        # Dirty again without changing selection
        emitter.mark_dirty("/World/Sphere")
        events = emitter.build_events_for_dirty()
        vsel = [e for e in events if e["k"] == K_SET_VARIANT_SELECTIONS]
        assert len(vsel) == 0

    def test_variant_cache_cleaned_on_deletion(self):
        stage, emitter = _make_stage_and_emitter()
        prim = stage.DefinePrim("/World/VarObj", "Xform")
        vset = prim.GetVariantSets().AddVariantSet("color")
        vset.AddVariant("red")
        vset.SetVariantSelection("red")

        emitter.build_events_for_dirty()
        assert "/World/VarObj" in emitter.last_sent_variant_selections

        stage.RemovePrim("/World/VarObj")
        emitter.build_events_for_dirty()
        assert "/World/VarObj" not in emitter.last_sent_variant_selections

    def test_no_variants_no_event(self):
        stage, emitter = _make_stage_and_emitter()
        stage.DefinePrim("/World/Plain", "Xform")
        events = emitter.build_events_for_dirty()
        vsel = [e for e in events if e["k"] == K_SET_VARIANT_SELECTIONS]
        assert len(vsel) == 0
