"""Roundtrip tests — emitter -> events -> MockAdapter, verify TRS values match.

Tests the full pipeline without network or DCC: author transforms on a stage,
use NoticeEmitter to build events, apply events via MockAdapter.
"""

import pytest

try:
    from pxr import Usd, UsdGeom, Gf, Sdf
    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")

from openusdconnect.emitter import NoticeEmitter, decompose_trs_from_matrix, near_list
from openusdconnect.event_apply import ensure_canonical_ops, quatf_from_wxyz, apply_events
from openusdconnect.adapters import MockAdapter


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
        prim = stage.GetPrimAtPath("/World/Sphere")
        _, _, t_op, o_op, s_op = ensure_canonical_ops(stage, "/World/Sphere")
        t_op.Set(Gf.Vec3d(3.0, 0.0, 0.0))

        events = emitter.build_events_for_dirty()
        assert len(events) > 0

        # Check that we got a set_xform_trs event
        trs_events = [e for e in events if e.get("k") == "set_xform_trs"]
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
        trs_events = [e for e in events if e.get("k") == "set_xform_trs"]
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
            if k == "ensure_prim":
                adapter.ensure_prim(prim, ev.get("typeName", "Xform"))
            elif k == "ensure_xform_ops":
                adapter.ensure_xform_ops(prim)
            elif k == "set_xform_trs":
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
        trs1 = [e for e in events1 if e.get("k") == "set_xform_trs"]
        assert len(trs1) == 1
        assert set(trs1[0]["fields"]) == {"t", "r", "s"}

        # Change only translation
        t_op.Set(Gf.Vec3d(2.0, 0.0, 0.0))
        events2 = emitter.build_events_for_dirty(include_matrices=False)
        trs2 = [e for e in events2 if e.get("k") == "set_xform_trs"]
        assert len(trs2) == 1
        assert trs2[0]["fields"] == ["t"]
