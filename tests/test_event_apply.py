"""Tests for openusdconnect.event_apply — apply events to in-memory Usd.Stage.

Requires pxr (OpenUSD Python bindings). Tests are skipped if pxr is not available.
"""

import pytest

try:
    from pxr import Usd, UsdGeom, Gf, Sdf
    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")

from openusdconnect.event_apply import (
    get_or_define_prim,
    find_op,
    ensure_canonical_ops,
    quatf_from_wxyz,
    apply_event,
    apply_events,
)


@pytest.fixture
def stage():
    s = Usd.Stage.CreateInMemory()
    s.DefinePrim("/World", "Xform")
    return s


class TestGetOrDefinePrim:
    def test_creates_new_prim(self, stage):
        prim = get_or_define_prim(stage, "/World/Sphere", "Sphere")
        assert prim.IsValid()
        assert prim.GetTypeName() == "Sphere"

    def test_returns_existing_prim(self, stage):
        stage.DefinePrim("/World/Cube", "Cube")
        prim = get_or_define_prim(stage, "/World/Cube", "Cube")
        assert prim.IsValid()

    def test_idempotent(self, stage):
        p1 = get_or_define_prim(stage, "/World/Foo", "Xform")
        p2 = get_or_define_prim(stage, "/World/Foo", "Xform")
        assert p1.GetPath() == p2.GetPath()


class TestEnsureCanonicalOps:
    def test_creates_ops(self, stage):
        stage.DefinePrim("/World/Sphere", "Xform")
        prim, xf, t_op, o_op, s_op = ensure_canonical_ops(stage, "/World/Sphere")
        assert t_op is not None
        assert o_op is not None
        assert s_op is not None
        ops = xf.GetOrderedXformOps()
        names = [op.GetAttr().GetName() for op in ops]
        assert names == ["xformOp:translate", "xformOp:orient", "xformOp:scale"]

    def test_idempotent(self, stage):
        stage.DefinePrim("/World/Sphere", "Xform")
        ensure_canonical_ops(stage, "/World/Sphere")
        _, xf, _, _, _ = ensure_canonical_ops(stage, "/World/Sphere")
        ops = xf.GetOrderedXformOps()
        assert len(ops) == 3  # no duplicates


class TestApplyEvent:
    def test_ensure_prim(self, stage):
        apply_event(stage, {"k": "ensure_prim", "prim": "/World/New", "typeName": "Xform"})
        prim = stage.GetPrimAtPath("/World/New")
        assert prim.IsValid()

    def test_ensure_xform_ops(self, stage):
        stage.DefinePrim("/World/Sphere", "Xform")
        apply_event(stage, {"k": "ensure_xform_ops", "prim": "/World/Sphere"})
        xf = UsdGeom.Xformable(stage.GetPrimAtPath("/World/Sphere"))
        ops = xf.GetOrderedXformOps()
        assert len(ops) == 3

    def test_set_xform_trs_full(self, stage):
        stage.DefinePrim("/World/Sphere", "Xform")
        ev = {
            "k": "set_xform_trs",
            "prim": "/World/Sphere",
            "fields": ["t", "r", "s"],
            "t": [1.0, 2.0, 3.0],
            "r": [1.0, 0.0, 0.0, 0.0],
            "s": [2.0, 2.0, 2.0],
        }
        apply_event(stage, ev)

        prim = stage.GetPrimAtPath("/World/Sphere")
        xf = UsdGeom.Xformable(prim)
        ops = {op.GetAttr().GetName(): op for op in xf.GetOrderedXformOps()}

        t_val = ops["xformOp:translate"].Get()
        assert abs(t_val[0] - 1.0) < 1e-6
        assert abs(t_val[1] - 2.0) < 1e-6
        assert abs(t_val[2] - 3.0) < 1e-6

        s_val = ops["xformOp:scale"].Get()
        assert abs(s_val[0] - 2.0) < 1e-6

    def test_set_xform_trs_partial(self, stage):
        stage.DefinePrim("/World/Sphere", "Xform")
        # First set full TRS
        apply_event(stage, {
            "k": "set_xform_trs", "prim": "/World/Sphere",
            "fields": ["t", "r", "s"],
            "t": [1.0, 2.0, 3.0], "r": [1.0, 0.0, 0.0, 0.0], "s": [1.0, 1.0, 1.0],
        })
        # Then update only translation
        apply_event(stage, {
            "k": "set_xform_trs", "prim": "/World/Sphere",
            "fields": ["t"],
            "t": [10.0, 20.0, 30.0],
        })
        prim = stage.GetPrimAtPath("/World/Sphere")
        xf = UsdGeom.Xformable(prim)
        ops = {op.GetAttr().GetName(): op for op in xf.GetOrderedXformOps()}
        t_val = ops["xformOp:translate"].Get()
        assert abs(t_val[0] - 10.0) < 1e-6
        # Scale should remain unchanged
        s_val = ops["xformOp:scale"].Get()
        assert abs(s_val[0] - 1.0) < 1e-6

    def test_set_xform_matrices_ignored(self, stage):
        # Should not raise
        apply_event(stage, {
            "k": "set_xform_matrices", "prim": "/World/Sphere",
            "local_m": [0.0] * 16, "world_m": [0.0] * 16,
        })

    def test_delete_prim(self, stage):
        stage.DefinePrim("/World/ToDelete", "Xform")
        assert stage.GetPrimAtPath("/World/ToDelete").IsValid()
        apply_event(stage, {"k": "delete_prim", "prim": "/World/ToDelete"})
        assert not stage.GetPrimAtPath("/World/ToDelete").IsValid()


class TestApplyEvents:
    def test_atomic_batch(self, stage):
        events = [
            {"k": "ensure_prim", "prim": "/World/A", "typeName": "Xform"},
            {"k": "ensure_prim", "prim": "/World/B", "typeName": "Xform"},
            {"k": "ensure_xform_ops", "prim": "/World/A"},
            {"k": "set_xform_trs", "prim": "/World/A",
             "fields": ["t"], "t": [5.0, 0.0, 0.0]},
        ]
        apply_events(stage, events)
        assert stage.GetPrimAtPath("/World/A").IsValid()
        assert stage.GetPrimAtPath("/World/B").IsValid()
