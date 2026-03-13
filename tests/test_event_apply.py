"""Tests for openusdconnect.event_apply — apply events to in-memory Usd.Stage.

Requires pxr (OpenUSD Python bindings). Tests are skipped if pxr is not available.
"""

import pytest

try:
    from pxr import Usd, UsdGeom  # noqa: F401

    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")

from openusdconnect.event_apply import (
    apply_event,
    apply_events,
    ensure_canonical_ops,
    get_or_define_prim,
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
        apply_event(
            stage,
            {
                "k": "set_xform_trs",
                "prim": "/World/Sphere",
                "fields": ["t", "r", "s"],
                "t": [1.0, 2.0, 3.0],
                "r": [1.0, 0.0, 0.0, 0.0],
                "s": [1.0, 1.0, 1.0],
            },
        )
        # Then update only translation
        apply_event(
            stage,
            {
                "k": "set_xform_trs",
                "prim": "/World/Sphere",
                "fields": ["t"],
                "t": [10.0, 20.0, 30.0],
            },
        )
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
        apply_event(
            stage,
            {
                "k": "set_xform_matrices",
                "prim": "/World/Sphere",
                "local_m": [0.0] * 16,
                "world_m": [0.0] * 16,
            },
        )

    def test_delete_prim(self, stage):
        stage.DefinePrim("/World/ToDelete", "Xform")
        assert stage.GetPrimAtPath("/World/ToDelete").IsValid()
        apply_event(stage, {"k": "delete_prim", "prim": "/World/ToDelete"})
        assert not stage.GetPrimAtPath("/World/ToDelete").IsValid()

    def test_deactivate_prim(self, stage):
        stage.DefinePrim("/World/Target", "Xform")
        apply_event(stage, {"k": "deactivate_prim", "prim": "/World/Target", "active": False})
        prim = stage.GetPrimAtPath("/World/Target")
        # Inactive prims are pruned from the composed view; IsValid returns False
        assert not prim.IsActive()

    def test_reactivate_prim(self, stage):
        stage.DefinePrim("/World/Target", "Xform")
        apply_event(stage, {"k": "deactivate_prim", "prim": "/World/Target", "active": False})
        apply_event(stage, {"k": "deactivate_prim", "prim": "/World/Target", "active": True})
        prim = stage.GetPrimAtPath("/World/Target")
        assert prim.IsActive()

    def test_rename_prim(self, stage):
        stage.DefinePrim("/World/OldName", "Xform")
        apply_event(stage, {"k": "rename_prim", "prim": "/World/OldName", "new_name": "NewName"})
        assert stage.GetPrimAtPath("/World/NewName").IsValid()
        assert not stage.GetPrimAtPath("/World/OldName").IsValid()

    def test_rename_preserves_transform(self, stage):
        stage.DefinePrim("/World/OldName", "Xform")
        apply_event(
            stage,
            {
                "k": "set_xform_trs",
                "prim": "/World/OldName",
                "fields": ["t"],
                "t": [3.0, 4.0, 5.0],
            },
        )
        apply_event(stage, {"k": "rename_prim", "prim": "/World/OldName", "new_name": "NewName"})
        prim = stage.GetPrimAtPath("/World/NewName")
        assert prim.IsValid()
        xf = UsdGeom.Xformable(prim)
        ops = {op.GetAttr().GetName(): op for op in xf.GetOrderedXformOps()}
        t_val = ops["xformOp:translate"].Get()
        assert abs(t_val[0] - 3.0) < 1e-6


class TestSetVisibility:
    def test_set_invisible(self, stage):
        stage.DefinePrim("/World/Sphere", "Sphere")
        apply_event(stage, {"k": "set_visibility", "prim": "/World/Sphere", "visible": False})
        prim = stage.GetPrimAtPath("/World/Sphere")
        imageable = UsdGeom.Imageable(prim)
        assert imageable.GetVisibilityAttr().Get() == "invisible"

    def test_set_visible(self, stage):
        stage.DefinePrim("/World/Sphere", "Sphere")
        apply_event(stage, {"k": "set_visibility", "prim": "/World/Sphere", "visible": False})
        apply_event(stage, {"k": "set_visibility", "prim": "/World/Sphere", "visible": True})
        prim = stage.GetPrimAtPath("/World/Sphere")
        imageable = UsdGeom.Imageable(prim)
        assert imageable.GetVisibilityAttr().Get() == "inherited"


class TestSetGprimAttrs:
    def test_sphere_radius(self, stage):
        stage.DefinePrim("/World/Sphere", "Sphere")
        apply_event(
            stage,
            {
                "k": "set_gprim_attrs",
                "prim": "/World/Sphere",
                "attrs": {"radius": 3.0},
            },
        )
        prim = stage.GetPrimAtPath("/World/Sphere")
        assert abs(prim.GetAttribute("radius").Get() - 3.0) < 1e-6

    def test_cone_height_and_radius(self, stage):
        stage.DefinePrim("/World/Cone", "Cone")
        apply_event(
            stage,
            {
                "k": "set_gprim_attrs",
                "prim": "/World/Cone",
                "attrs": {"height": 1.4, "radius": 0.6},
            },
        )
        prim = stage.GetPrimAtPath("/World/Cone")
        assert abs(prim.GetAttribute("height").Get() - 1.4) < 1e-6
        assert abs(prim.GetAttribute("radius").Get() - 0.6) < 1e-6


class TestSetReference:
    def test_add_reference(self, stage):
        ref_stage = Usd.Stage.CreateInMemory("ref.usda")
        ref_stage.DefinePrim("/Chair", "Xform")
        ref_path = ref_stage.GetRootLayer().identifier

        apply_event(
            stage,
            {
                "k": "set_reference",
                "prim": "/World/Chair",
                "refs": [{"asset_path": ref_path, "prim_path": "/Chair"}],
            },
        )
        prim = stage.GetPrimAtPath("/World/Chair")
        assert prim.IsValid()
        assert prim.HasAuthoredReferences()


    def test_add_multiple_references(self, stage):
        ref_stage_1 = Usd.Stage.CreateInMemory("ref1.usda")
        ref_stage_1.DefinePrim("/A", "Xform")
        ref_stage_2 = Usd.Stage.CreateInMemory("ref2.usda")
        ref_stage_2.DefinePrim("/B", "Xform")

        apply_event(
            stage,
            {
                "k": "set_reference",
                "prim": "/World/Multi",
                "refs": [
                    {"asset_path": ref_stage_1.GetRootLayer().identifier, "prim_path": "/A"},
                    {"asset_path": ref_stage_2.GetRootLayer().identifier, "prim_path": "/B"},
                ],
            },
        )
        prim = stage.GetPrimAtPath("/World/Multi")
        assert prim.IsValid()
        assert prim.HasAuthoredReferences()

    def test_clear_references(self, stage):
        ref_stage = Usd.Stage.CreateInMemory("ref_clear.usda")
        ref_stage.DefinePrim("/X", "Xform")

        # Add a reference first
        apply_event(
            stage,
            {
                "k": "set_reference",
                "prim": "/World/Clearable",
                "refs": [{"asset_path": ref_stage.GetRootLayer().identifier}],
            },
        )
        prim = stage.GetPrimAtPath("/World/Clearable")
        assert prim.HasAuthoredReferences()

        # Clear with empty refs
        apply_event(
            stage,
            {"k": "set_reference", "prim": "/World/Clearable", "refs": []},
        )
        prim = stage.GetPrimAtPath("/World/Clearable")
        assert not prim.HasAuthoredReferences()

    def test_add_internal_reference(self, stage):
        """Same-file reference (no asset_path, only prim_path)."""
        # Define a source prim to reference
        stage.DefinePrim("/Source", "Xform")

        apply_event(
            stage,
            {
                "k": "set_reference",
                "prim": "/World/InternalRef",
                "refs": [{"prim_path": "/Source"}],
            },
        )
        prim = stage.GetPrimAtPath("/World/InternalRef")
        assert prim.IsValid()
        assert prim.HasAuthoredReferences()


class TestApplyEvents:
    def test_atomic_batch(self, stage):
        events = [
            {"k": "ensure_prim", "prim": "/World/A", "typeName": "Xform"},
            {"k": "ensure_prim", "prim": "/World/B", "typeName": "Xform"},
            {"k": "ensure_xform_ops", "prim": "/World/A"},
            {"k": "set_xform_trs", "prim": "/World/A", "fields": ["t"], "t": [5.0, 0.0, 0.0]},
        ]
        apply_events(stage, events)
        assert stage.GetPrimAtPath("/World/A").IsValid()
        assert stage.GetPrimAtPath("/World/B").IsValid()
