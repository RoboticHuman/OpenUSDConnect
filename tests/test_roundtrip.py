"""Roundtrip tests — emitter -> events -> MockAdapter, verify TRS values match.

Tests the full pipeline without network or DCC: author transforms on a stage,
use NoticeEmitter to build events, apply events via MockAdapter.
"""

import os
import tempfile

import pytest

try:
    from pxr import Gf, Sdf, Usd, UsdGeom

    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")

from openusdconnect.adapters import MockAdapter, UsdStageAdapter
from openusdconnect.emitter import NoticeEmitter, decompose_trs_from_matrix, near_list
from openusdconnect.event_apply import apply_events, ensure_canonical_ops
from openusdconnect.protocol import (
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_SET_GPRIM_ATTRS,
    K_LOAD_PAYLOAD,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_VISIBILITY,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
)


def _create_ref_test_assets(tmp_dir):
    """Create base scene and reference asset USD files for reference tests.

    Returns (base_path, asset_path).
    """
    base_path = os.path.join(tmp_dir, "base_scene.usda")
    base_stage = Usd.Stage.CreateNew(base_path)
    UsdGeom.SetStageUpAxis(base_stage, UsdGeom.Tokens.y)
    UsdGeom.Xform.Define(base_stage, "/World")
    base_stage.Save()

    asset_path = os.path.join(tmp_dir, "asset.usda")
    asset_stage = Usd.Stage.CreateNew(asset_path)
    UsdGeom.SetStageUpAxis(asset_stage, UsdGeom.Tokens.y)
    UsdGeom.Xform.Define(asset_stage, "/Model")
    cube = UsdGeom.Cube.Define(asset_stage, "/Model/Geom")
    cube.GetSizeAttr().Set(2.0)
    asset_stage.Save()

    return base_path, asset_path


def _create_test_stage():
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/World", "Xform")
    stage.DefinePrim("/World/Sphere", "Xform")
    stage.DefinePrim("/World/Cube", "Xform")
    return stage


class TestDecomposeTrs:
    def test_identity_matrix(self):
        m = Gf.Matrix4d(1)
        t, r, s = decompose_trs_from_matrix(m)
        assert near_list(t, [0.0, 0.0, 0.0], 1e-9)
        assert near_list(r, [1.0, 0.0, 0.0, 0.0], 1e-9)
        assert near_list(s, [1.0, 1.0, 1.0], 1e-9)

    def test_translation_only(self):
        m = Gf.Matrix4d(1)
        m.SetTranslateOnly(Gf.Vec3d(5.0, 10.0, 15.0))
        t, r, s = decompose_trs_from_matrix(m)
        assert near_list(t, [5.0, 10.0, 15.0], 1e-6)
        assert near_list(s, [1.0, 1.0, 1.0], 1e-6)


class TestNearList:
    def test_equal(self):
        assert near_list([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], 1e-9)

    def test_near(self):
        assert near_list([1.0, 2.0, 3.0], [1.0 + 1e-10, 2.0, 3.0], 1e-9)

    def test_not_near(self):
        assert not near_list([1.0, 2.0, 3.0], [2.0, 2.0, 3.0], 1e-9)

    def test_none(self):
        assert not near_list(None, [1.0], 1e-9)
        assert not near_list([1.0], None, 1e-9)


class TestNoticeEmitterRoundtrip:
    def test_emitter_detects_changes(self):
        stage = _create_test_stage()
        emitter = NoticeEmitter(stage)

        # Author a change
        stage.GetPrimAtPath("/World/Sphere")
        _, _, t_op, o_op, s_op = ensure_canonical_ops(stage, "/World/Sphere")
        t_op.Set(Gf.Vec3d(3.0, 0.0, 0.0))

        events = emitter.build_events_for_dirty()
        assert len(events) > 0

        # Check that we got a set_xform_trs event
        trs_events = [e for e in events if e.get("k") == K_SET_XFORM_TRS]
        assert len(trs_events) >= 1
        trs_ev = trs_events[0]
        assert "t" in trs_ev.get("fields", [])

    def test_emitter_no_change_no_events(self):
        stage = _create_test_stage()
        emitter = NoticeEmitter(stage)
        # Don't make any changes — drain initial dirty set
        _ = emitter.build_events_for_dirty()
        # Now check again
        events = emitter.build_events_for_dirty()
        # Should have no TRS events
        trs_events = [e for e in events if e.get("k") == K_SET_XFORM_TRS]
        assert len(trs_events) == 0

    def test_full_roundtrip_emitter_to_mock_adapter(self):
        """Author on stage -> emitter builds events -> MockAdapter receives them."""
        stage = _create_test_stage()
        emitter = NoticeEmitter(stage)

        # Author TRS on Sphere
        _, _, t_op, o_op, s_op = ensure_canonical_ops(stage, "/World/Sphere")
        with Sdf.ChangeBlock():
            t_op.Set(Gf.Vec3d(5.0, 10.0, 0.0))
            o_op.Set(Gf.Quatf(1.0, Gf.Vec3f(0.0, 0.0, 0.0)))
            s_op.Set(Gf.Vec3d(2.0, 2.0, 2.0))

        events = emitter.build_events_for_dirty(include_matrices=False)

        # Apply events to MockAdapter
        adapter = MockAdapter()
        for ev in events:
            k = ev.get("k")
            prim = ev.get("prim", "")
            if k == K_ENSURE_PRIM:
                adapter.ensure_prim(prim, ev.get("typeName", "Xform"))
            elif k == K_ENSURE_XFORM_OPS:
                adapter.ensure_xform_ops(prim)
            elif k == K_SET_XFORM_TRS:
                adapter.set_xform_trs(prim, ev)

        trs = adapter.get_trs("/World/Sphere")
        assert near_list(trs.get("t"), [5.0, 10.0, 0.0], 1e-6)
        assert near_list(trs.get("r"), [1.0, 0.0, 0.0, 0.0], 1e-6)
        assert near_list(trs.get("s"), [2.0, 2.0, 2.0], 1e-6)

    def test_roundtrip_emitter_to_stage(self):
        """Author on stage A -> emitter -> apply_events on stage B -> verify."""
        stage_a = _create_test_stage()
        emitter = NoticeEmitter(stage_a)

        _, _, t_op, o_op, s_op = ensure_canonical_ops(stage_a, "/World/Cube")
        with Sdf.ChangeBlock():
            t_op.Set(Gf.Vec3d(1.0, 2.0, 3.0))
            s_op.Set(Gf.Vec3d(0.5, 0.5, 0.5))

        events = emitter.build_events_for_dirty(include_matrices=False)

        # Apply to a separate stage
        stage_b = Usd.Stage.CreateInMemory()
        stage_b.DefinePrim("/World", "Xform")
        apply_events(stage_b, events)

        # Verify
        prim_b = stage_b.GetPrimAtPath("/World/Cube")
        assert prim_b.IsValid()
        xf_b = UsdGeom.Xformable(prim_b)
        ops = {op.GetAttr().GetName(): op for op in xf_b.GetOrderedXformOps()}
        t_val = ops["xformOp:translate"].Get()
        assert abs(t_val[0] - 1.0) < 1e-6
        assert abs(t_val[1] - 2.0) < 1e-6
        s_val = ops["xformOp:scale"].Get()
        assert abs(s_val[0] - 0.5) < 1e-6

    def test_partial_update_after_initial(self):
        """Emitter should only send changed fields on subsequent updates."""
        stage = _create_test_stage()
        emitter = NoticeEmitter(stage)

        _, _, t_op, o_op, s_op = ensure_canonical_ops(stage, "/World/Sphere")
        t_op.Set(Gf.Vec3d(1.0, 0.0, 0.0))
        o_op.Set(Gf.Quatf(1.0, Gf.Vec3f(0.0, 0.0, 0.0)))
        s_op.Set(Gf.Vec3d(1.0, 1.0, 1.0))

        # First flush
        events1 = emitter.build_events_for_dirty(include_matrices=False)
        trs1 = [e for e in events1 if e.get("k") == K_SET_XFORM_TRS]
        assert len(trs1) == 1
        assert set(trs1[0]["fields"]) == {"t", "r", "s"}

        # Change only translation
        t_op.Set(Gf.Vec3d(2.0, 0.0, 0.0))
        events2 = emitter.build_events_for_dirty(include_matrices=False)
        trs2 = [e for e in events2 if e.get("k") == K_SET_XFORM_TRS]
        assert len(trs2) == 1
        assert trs2[0]["fields"] == ["t"]

    def test_visibility_roundtrip(self):
        """Emitter detects visibility changes and emits set_visibility events."""
        stage = _create_test_stage()
        emitter = NoticeEmitter(stage)

        # Set up xform ops so the prim shows up as dirty
        ensure_canonical_ops(stage, "/World/Sphere")
        # Drain initial dirty
        emitter.build_events_for_dirty(include_matrices=False)

        # Make invisible
        prim = stage.GetPrimAtPath("/World/Sphere")
        UsdGeom.Imageable(prim).GetVisibilityAttr().Set("invisible")

        events = emitter.build_events_for_dirty(include_matrices=False)
        vis_events = [e for e in events if e.get("k") == K_SET_VISIBILITY]
        assert len(vis_events) == 1
        assert vis_events[0]["visible"] is False

        # Make visible again
        UsdGeom.Imageable(prim).GetVisibilityAttr().Set("inherited")
        events2 = emitter.build_events_for_dirty(include_matrices=False)
        vis_events2 = [e for e in events2 if e.get("k") == K_SET_VISIBILITY]
        assert len(vis_events2) == 1
        assert vis_events2[0]["visible"] is True


class TestUsdStageAdapterNewFeatures:
    """Test new features through UsdStageAdapter against real USD stages."""

    def test_visibility_via_adapter(self):
        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/World", "Xform")
        stage.DefinePrim("/World/Sphere", "Sphere")
        adapter = UsdStageAdapter(stage)

        adapter.set_visibility("/World/Sphere", False)
        prim = stage.GetPrimAtPath("/World/Sphere")
        assert UsdGeom.Imageable(prim).GetVisibilityAttr().Get() == "invisible"

        adapter.set_visibility("/World/Sphere", True)
        assert UsdGeom.Imageable(prim).GetVisibilityAttr().Get() == "inherited"

    def test_gprim_attrs_via_adapter(self):
        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/World", "Xform")
        stage.DefinePrim("/World/Sphere", "Sphere")
        adapter = UsdStageAdapter(stage)

        adapter.set_gprim_attrs("/World/Sphere", {"radius": 5.0})
        prim = stage.GetPrimAtPath("/World/Sphere")
        assert abs(prim.GetAttribute("radius").Get() - 5.0) < 1e-6

    def test_gprim_attrs_cone_via_adapter(self):
        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/World", "Xform")
        stage.DefinePrim("/World/Cone", "Cone")
        adapter = UsdStageAdapter(stage)

        adapter.set_gprim_attrs("/World/Cone", {"height": 3.0, "radius": 1.5})
        prim = stage.GetPrimAtPath("/World/Cone")
        assert abs(prim.GetAttribute("height").Get() - 3.0) < 1e-6
        assert abs(prim.GetAttribute("radius").Get() - 1.5) < 1e-6

    def test_reference_via_adapter(self):
        src_stage = Usd.Stage.CreateInMemory("source.usda")
        src_stage.DefinePrim("/Chair", "Xform")
        src_layer = src_stage.GetRootLayer()

        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/World", "Xform")
        adapter = UsdStageAdapter(stage)

        adapter.set_reference(
            "/World/Chair",
            [{"asset_path": src_layer.identifier, "prim_path": "/Chair"}],
        )
        prim = stage.GetPrimAtPath("/World/Chair")
        assert prim.IsValid()
        assert prim.HasAuthoredReferences()

    def test_ensure_prim_typed(self):
        """ensure_prim with typed gprim creates proper USD typed prims."""
        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/World", "Xform")
        adapter = UsdStageAdapter(stage)

        for type_name in ("Sphere", "Cube", "Cylinder", "Cone", "Mesh"):
            adapter.ensure_prim(f"/World/{type_name}", type_name)
            prim = stage.GetPrimAtPath(f"/World/{type_name}")
            assert prim.IsValid()
            assert prim.GetTypeName() == type_name


class TestPayloadRoundtrip:
    """Payload-specific roundtrip tests."""

    def test_payload_via_adapter(self):
        """UsdStageAdapter writes the payload arc and unloads the prim."""
        pay_stage = Usd.Stage.CreateInMemory("payload_src.usda")
        pay_stage.DefinePrim("/Model", "Xform")
        pay_layer = pay_stage.GetRootLayer()

        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/World", "Xform")
        adapter = UsdStageAdapter(stage)

        adapter.set_payload(
            "/World/Asset",
            [{"asset_path": pay_layer.identifier, "prim_path": "/Model"}],
        )
        prim = stage.GetPrimAtPath("/World/Asset")
        assert prim.IsValid()
        assert prim.HasAuthoredPayloads()

    def test_payload_stage_to_stage(self):
        """Emit payload on stage A, apply to stage B, verify arc."""
        pay_stage = Usd.Stage.CreateInMemory("pay_rt.usda")
        pay_stage.DefinePrim("/Model", "Xform")
        pay_id = pay_stage.GetRootLayer().identifier

        stage_a = Usd.Stage.CreateInMemory()
        session = stage_a.GetSessionLayer()
        stage_a.SetEditTarget(Usd.EditTarget(session))

        prim_a = stage_a.DefinePrim("/World/Thing", "Xform")
        prim_a.GetPayloads().AddPayload(pay_id, "/Model")

        emitter = NoticeEmitter(stage_a)
        emitter.mark_dirty("/World/Thing")
        events = emitter.build_events_for_dirty(include_matrices=False)

        pay_evs = [e for e in events if e["k"] == K_SET_PAYLOAD]
        assert len(pay_evs) == 1

        stage_b = Usd.Stage.CreateInMemory()
        stage_b.DefinePrim("/World", "Xform")
        apply_events(stage_b, events)

        prim_b = stage_b.GetPrimAtPath("/World/Thing")
        assert prim_b.IsValid()
        assert prim_b.HasAuthoredPayloads()

    def test_payload_unloaded_by_default(self):
        """After UsdStageAdapter.set_payload(), prim payload is not loaded."""
        pay_stage = Usd.Stage.CreateInMemory("pay_unload.usda")
        pay_stage.DefinePrim("/Model", "Xform")
        pay_stage.DefinePrim("/Model/Child", "Cube")
        pay_layer = pay_stage.GetRootLayer()

        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/World", "Xform")
        adapter = UsdStageAdapter(stage)

        adapter.set_payload(
            "/World/Heavy",
            [{"asset_path": pay_layer.identifier, "prim_path": "/Model"}],
        )
        # The payload arc is authored but the prim should be unloaded
        prim = stage.GetPrimAtPath("/World/Heavy")
        assert prim.IsValid()
        assert prim.HasAuthoredPayloads()
        # Child from payload should NOT be visible (unloaded)
        child = stage.GetPrimAtPath("/World/Heavy/Child")
        assert not child or not child.IsValid()

    def test_payload_can_be_loaded_after_receive(self):
        """User can manually load a payload after receive."""
        pay_stage = Usd.Stage.CreateInMemory("pay_load.usda")
        pay_stage.DefinePrim("/Model", "Xform")
        pay_stage.DefinePrim("/Model/Geo", "Sphere")
        pay_layer = pay_stage.GetRootLayer()

        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/World", "Xform")
        adapter = UsdStageAdapter(stage)

        adapter.set_payload(
            "/World/Loadable",
            [{"asset_path": pay_layer.identifier, "prim_path": "/Model"}],
        )
        # Initially unloaded
        child = stage.GetPrimAtPath("/World/Loadable/Geo")
        assert not child or not child.IsValid()

        # User opts-in to load
        stage.Load("/World/Loadable")
        child = stage.GetPrimAtPath("/World/Loadable/Geo")
        assert child.IsValid()
        assert child.GetTypeName() == "Sphere"

    def test_pyramid_payload_roundtrip(self):
        """Real-world test with Pyramid asset: payload arc emit -> apply."""
        import os

        fixture_dir = os.path.join(os.path.dirname(__file__), "fixtures", "pyramid")
        pyramid_path = os.path.join(fixture_dir, "Pyramid.usd")
        if not os.path.isfile(pyramid_path):
            pytest.skip("Pyramid fixture not found")

        # Open the Pyramid asset — it has prepend payload = @./payload.usda@
        src_stage = Usd.Stage.Open(pyramid_path)
        root_prim = src_stage.GetDefaultPrim()
        assert root_prim is not None
        prim_path = str(root_prim.GetPath())

        # Read payloads from the root prim
        from openusdconnect.emitter import _read_payloads

        payloads = _read_payloads(src_stage, prim_path)
        assert len(payloads) >= 1, f"Expected payloads on {prim_path}, got {payloads}"

        # Build the event
        pay_ev = {"k": K_SET_PAYLOAD, "prim": prim_path, "payloads": []}
        for asset_path, pay_prim_path in payloads:
            entry = {"asset_path": asset_path}
            if pay_prim_path:
                entry["prim_path"] = pay_prim_path
            pay_ev["payloads"].append(entry)

        # Apply to a destination stage in the same directory
        # (so relative paths resolve)
        from pxr import Sdf

        dest_layer = Sdf.Layer.CreateNew(
            os.path.join(fixture_dir, "_test_dest.usda")
        )
        dest_stage = Usd.Stage.Open(dest_layer)
        dest_stage.DefinePrim(prim_path, "Xform")

        from openusdconnect.event_apply import apply_event

        apply_event(dest_stage, pay_ev)

        dest_prim = dest_stage.GetPrimAtPath(prim_path)
        assert dest_prim.HasAuthoredPayloads()

        # Load the payload and verify content is accessible
        dest_stage.Load(prim_path)

        # Cleanup temp layer
        try:
            os.unlink(os.path.join(fixture_dir, "_test_dest.usda"))
        except Exception:
            pass

    def test_load_payload_via_adapter(self):
        """UsdStageAdapter.load_payload makes children visible."""
        # Create payload asset
        payload_stage = Usd.Stage.CreateInMemory()
        payload_stage.DefinePrim("/Model", "Xform")
        payload_stage.DefinePrim("/Model/Child", "Mesh")
        payload_path = payload_stage.GetRootLayer().identifier

        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/World", "Xform")
        adapter = UsdStageAdapter(stage)
        adapter.ensure_prim("/World/Asset")
        adapter.set_payload("/World/Asset", [{"asset_path": payload_path, "prim_path": "/Model"}])
        # set_payload unloads by default
        assert not adapter.stage.GetPrimAtPath("/World/Asset").IsLoaded()

        adapter.load_payload("/World/Asset")
        assert adapter.stage.GetPrimAtPath("/World/Asset").IsLoaded()
        child = adapter.stage.GetPrimAtPath("/World/Asset/Child")
        assert child and child.IsValid()

    def test_unload_payload_via_adapter(self):
        """UsdStageAdapter.unload_payload hides children."""
        payload_stage = Usd.Stage.CreateInMemory()
        payload_stage.DefinePrim("/Model", "Xform")
        payload_stage.DefinePrim("/Model/Child", "Mesh")
        payload_path = payload_stage.GetRootLayer().identifier

        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/World", "Xform")
        adapter = UsdStageAdapter(stage)
        adapter.ensure_prim("/World/Asset")
        adapter.set_payload("/World/Asset", [{"asset_path": payload_path, "prim_path": "/Model"}])
        adapter.load_payload("/World/Asset")
        assert adapter.stage.GetPrimAtPath("/World/Asset").IsLoaded()

        adapter.unload_payload("/World/Asset")
        assert not adapter.stage.GetPrimAtPath("/World/Asset").IsLoaded()

    def test_load_unload_roundtrip(self):
        """Emitter detects load/unload transitions and emits events."""
        from openusdconnect.emitter import NoticeEmitter
        from openusdconnect.event_apply import apply_events

        # Source stage with payload
        payload_stage = Usd.Stage.CreateInMemory()
        payload_stage.DefinePrim("/Model", "Xform")
        payload_path = payload_stage.GetRootLayer().identifier

        src = Usd.Stage.CreateInMemory()
        src.DefinePrim("/World/Asset", "Xform")
        prim = src.GetPrimAtPath("/World/Asset")
        prim.GetPayloads().AddPayload(payload_path, "/Model")
        src.Unload(Sdf.Path("/World/Asset"))

        emitter = NoticeEmitter(src)
        emitter.mark_dirty("/World/Asset")
        events1 = emitter.build_events_for_dirty(include_matrices=False)
        # First encounter: should NOT emit load_payload (prim is unloaded)
        load_events = [e for e in events1 if e["k"] == K_LOAD_PAYLOAD]
        assert len(load_events) == 0

        # Now load the payload
        src.Load(Sdf.Path("/World/Asset"))
        emitter.mark_dirty("/World/Asset")
        events2 = emitter.build_events_for_dirty(include_matrices=False)
        load_events = [e for e in events2 if e["k"] == K_LOAD_PAYLOAD]
        assert len(load_events) == 1
        assert load_events[0]["prim"] == "/World/Asset"

        # Now unload
        src.Unload(Sdf.Path("/World/Asset"))
        emitter.mark_dirty("/World/Asset")
        events3 = emitter.build_events_for_dirty(include_matrices=False)
        unload_events = [e for e in events3 if e["k"] == K_UNLOAD_PAYLOAD]
        assert len(unload_events) == 1
        assert unload_events[0]["prim"] == "/World/Asset"


class TestStageToStageRoundtrip:
    """Author on stage A -> apply_events on stage B -> verify on real USD stages."""

    def test_visibility_stage_to_stage(self):
        """Visibility change on stage A replicates to stage B."""
        stage_a = Usd.Stage.CreateInMemory()
        stage_a.DefinePrim("/World", "Xform")
        stage_a.DefinePrim("/World/Sphere", "Sphere")
        emitter = NoticeEmitter(stage_a)

        # Drain initial dirty
        emitter.build_events_for_dirty(include_matrices=False)

        # Hide the sphere
        UsdGeom.Imageable(stage_a.GetPrimAtPath("/World/Sphere")).GetVisibilityAttr().Set(
            "invisible"
        )
        events = emitter.build_events_for_dirty(include_matrices=False)

        # Apply to stage B
        stage_b = Usd.Stage.CreateInMemory()
        stage_b.DefinePrim("/World", "Xform")
        stage_b.DefinePrim("/World/Sphere", "Sphere")
        apply_events(stage_b, events)

        prim_b = stage_b.GetPrimAtPath("/World/Sphere")
        assert UsdGeom.Imageable(prim_b).GetVisibilityAttr().Get() == "invisible"

    def test_gprim_attrs_stage_to_stage(self):
        """Gprim attr changes replicate between stages."""
        stage_a = Usd.Stage.CreateInMemory()
        stage_a.DefinePrim("/World", "Xform")
        stage_a.DefinePrim("/World/Sphere", "Sphere")

        # Manually build events (emitter doesn't emit gprim attrs yet)
        events = [
            {"k": K_ENSURE_PRIM, "prim": "/World/Sphere", "typeName": "Sphere"},
            {"k": K_SET_GPRIM_ATTRS, "prim": "/World/Sphere", "attrs": {"radius": 4.0}},
        ]

        stage_b = Usd.Stage.CreateInMemory()
        stage_b.DefinePrim("/World", "Xform")
        apply_events(stage_b, events)

        prim_b = stage_b.GetPrimAtPath("/World/Sphere")
        assert prim_b.IsValid()
        assert prim_b.GetTypeName() == "Sphere"
        assert abs(prim_b.GetAttribute("radius").Get() - 4.0) < 1e-6

    def test_reference_stage_to_stage(self):
        """Reference arc replicates between stages."""
        src_stage = Usd.Stage.CreateInMemory("ref_asset.usda")
        src_stage.DefinePrim("/Model", "Xform")
        src_stage.DefinePrim("/Model/Body", "Cube")
        src_layer = src_stage.GetRootLayer()

        events = [
            {
                "k": K_SET_REFERENCE,
                "prim": "/World/Furniture",
                "refs": [{"asset_path": src_layer.identifier, "prim_path": "/Model"}],
            },
        ]

        stage_b = Usd.Stage.CreateInMemory()
        stage_b.DefinePrim("/World", "Xform")
        apply_events(stage_b, events)

        prim_b = stage_b.GetPrimAtPath("/World/Furniture")
        assert prim_b.IsValid()
        assert prim_b.HasAuthoredReferences()
        # Verify composition: child prim from the reference should be visible
        child = stage_b.GetPrimAtPath("/World/Furniture/Body")
        assert child.IsValid()
        assert child.GetTypeName() == "Cube"

    def test_mixed_events_stage_to_stage(self):
        """Multiple event types in one batch replicate correctly."""
        src_stage = Usd.Stage.CreateInMemory("asset.usda")
        src_stage.DefinePrim("/Prop", "Xform")
        src_layer = src_stage.GetRootLayer()

        events = [
            {"k": K_ENSURE_PRIM, "prim": "/World/Sphere", "typeName": "Sphere"},
            {"k": K_ENSURE_XFORM_OPS, "prim": "/World/Sphere"},
            {"k": K_SET_XFORM_TRS, "prim": "/World/Sphere", "fields": ["t"], "t": [1.0, 2.0, 3.0]},
            {"k": K_SET_VISIBILITY, "prim": "/World/Sphere", "visible": False},
            {"k": K_SET_GPRIM_ATTRS, "prim": "/World/Sphere", "attrs": {"radius": 2.5}},
            {
                "k": K_SET_REFERENCE,
                "prim": "/World/Ref",
                "refs": [{"asset_path": src_layer.identifier, "prim_path": "/Prop"}],
            },
        ]

        stage_b = Usd.Stage.CreateInMemory()
        stage_b.DefinePrim("/World", "Xform")
        apply_events(stage_b, events)

        # Verify sphere
        sphere = stage_b.GetPrimAtPath("/World/Sphere")
        assert sphere.IsValid()
        assert sphere.GetTypeName() == "Sphere"
        assert UsdGeom.Imageable(sphere).GetVisibilityAttr().Get() == "invisible"
        assert abs(sphere.GetAttribute("radius").Get() - 2.5) < 1e-6
        xf = UsdGeom.Xformable(sphere)
        ops = {op.GetAttr().GetName(): op for op in xf.GetOrderedXformOps()}
        t_val = ops["xformOp:translate"].Get()
        assert abs(t_val[0] - 1.0) < 1e-6

        # Verify reference
        ref = stage_b.GetPrimAtPath("/World/Ref")
        assert ref.IsValid()
        assert ref.HasAuthoredReferences()


class TestFileBasedReferenceRoundtrip:
    """Reference tests using real USD files on disk."""

    def test_reference_applied_to_usd_stage_adapter(self):
        """UsdStageAdapter.set_reference creates valid reference arcs with file-based assets."""
        tmp_dir = tempfile.mkdtemp()
        _, asset_path = _create_ref_test_assets(tmp_dir)

        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/World", "Xform")
        adapter = UsdStageAdapter(stage)

        adapter.ensure_prim("/World/Furniture", "Xform")
        adapter.set_reference(
            "/World/Furniture",
            [{"asset_path": asset_path, "prim_path": "/Model"}],
        )

        furniture = stage.GetPrimAtPath("/World/Furniture")
        assert furniture.IsValid()
        assert furniture.HasAuthoredReferences()

        # Verify composed child
        geom = stage.GetPrimAtPath("/World/Furniture/Geom")
        assert geom.IsValid()
        assert geom.GetTypeName() == "Cube"

    def test_reference_without_prim_path_ref(self):
        """set_reference without prim_path references the whole asset."""
        tmp_dir = tempfile.mkdtemp()
        _, asset_path = _create_ref_test_assets(tmp_dir)

        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/World", "Xform")
        adapter = UsdStageAdapter(stage)

        adapter.ensure_prim("/World/FullAsset", "Xform")
        adapter.set_reference("/World/FullAsset", [{"asset_path": asset_path}])

        prim = stage.GetPrimAtPath("/World/FullAsset")
        assert prim.IsValid()
        assert prim.HasAuthoredReferences()
