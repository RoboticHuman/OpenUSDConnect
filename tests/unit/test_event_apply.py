"""Tests for openusdconnect.event_apply — apply events to in-memory Usd.Stage.

Requires pxr (OpenUSD Python bindings). Tests are skipped if pxr is not available.
"""

import pytest

try:
    from pxr import Gf, Sdf, Usd, UsdGeom  # noqa: F401

    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")

from openusdconnect.event_apply import (
    apply_event,
    apply_events,
    atomic_apply,
    ensure_canonical_ops,
    get_or_define_prim,
)
from openusdconnect.protocol_constants import (
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_SET_CONNECTABLE_CONNECTION,
    K_SET_GPRIM_ATTRS,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_VARIANT_SELECTIONS,
    K_SET_VISIBILITY,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
)
from openusdconnect.sdf_arc_state import serialize_reference_custom_data


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
        apply_event(stage, {"k": K_ENSURE_PRIM, "prim": "/World/New", "typeName": "Xform"})
        prim = stage.GetPrimAtPath("/World/New")
        assert prim.IsValid()

    def test_ensure_xform_ops(self, stage):
        stage.DefinePrim("/World/Sphere", "Xform")
        apply_event(stage, {"k": K_ENSURE_XFORM_OPS, "prim": "/World/Sphere"})
        xf = UsdGeom.Xformable(stage.GetPrimAtPath("/World/Sphere"))
        ops = xf.GetOrderedXformOps()
        assert len(ops) == 3

    def test_set_xform_trs_full(self, stage):
        stage.DefinePrim("/World/Sphere", "Xform")
        apply_event(stage, {"k": K_ENSURE_XFORM_OPS, "prim": "/World/Sphere"})
        ev = {
            "k": K_SET_XFORM_TRS,
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
        apply_event(stage, {"k": K_ENSURE_XFORM_OPS, "prim": "/World/Sphere"})
        # First set full TRS
        apply_event(
            stage,
            {
                "k": K_SET_XFORM_TRS,
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
                "k": K_SET_XFORM_TRS,
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

    def test_delete_prim(self, stage):
        stage.DefinePrim("/World/ToDelete", "Xform")
        assert stage.GetPrimAtPath("/World/ToDelete").IsValid()
        apply_event(stage, {"k": K_DELETE_PRIM, "prim": "/World/ToDelete"})
        assert not stage.GetPrimAtPath("/World/ToDelete").IsValid()

    def test_deactivate_prim(self, stage):
        stage.DefinePrim("/World/Target", "Xform")
        apply_event(stage, {"k": K_DEACTIVATE_PRIM, "prim": "/World/Target", "active": False})
        prim = stage.GetPrimAtPath("/World/Target")
        # Inactive prims are pruned from the composed view; IsValid returns False
        assert not prim.IsActive()

    def test_reactivate_prim(self, stage):
        stage.DefinePrim("/World/Target", "Xform")
        apply_event(stage, {"k": K_DEACTIVATE_PRIM, "prim": "/World/Target", "active": False})
        apply_event(stage, {"k": K_DEACTIVATE_PRIM, "prim": "/World/Target", "active": True})
        prim = stage.GetPrimAtPath("/World/Target")
        assert prim.IsActive()

    def test_rename_prim(self, stage):
        stage.DefinePrim("/World/OldName", "Xform")
        apply_event(stage, {"k": K_RENAME_PRIM, "prim": "/World/OldName", "new_name": "NewName"})
        assert stage.GetPrimAtPath("/World/NewName").IsValid()
        assert not stage.GetPrimAtPath("/World/OldName").IsValid()

    def test_rename_preserves_transform(self, stage):
        stage.DefinePrim("/World/OldName", "Xform")
        apply_event(stage, {"k": K_ENSURE_XFORM_OPS, "prim": "/World/OldName"})
        apply_event(
            stage,
            {
                "k": K_SET_XFORM_TRS,
                "prim": "/World/OldName",
                "fields": ["t"],
                "t": [3.0, 4.0, 5.0],
            },
        )
        apply_event(stage, {"k": K_RENAME_PRIM, "prim": "/World/OldName", "new_name": "NewName"})
        prim = stage.GetPrimAtPath("/World/NewName")
        assert prim.IsValid()
        xf = UsdGeom.Xformable(prim)
        ops = {op.GetAttr().GetName(): op for op in xf.GetOrderedXformOps()}
        t_val = ops["xformOp:translate"].Get()
        assert abs(t_val[0] - 3.0) < 1e-6


class TestSetVisibility:
    def test_set_invisible(self, stage):
        stage.DefinePrim("/World/Sphere", "Sphere")
        apply_event(stage, {"k": K_SET_VISIBILITY, "prim": "/World/Sphere", "visible": False})
        prim = stage.GetPrimAtPath("/World/Sphere")
        imageable = UsdGeom.Imageable(prim)
        assert imageable.GetVisibilityAttr().Get() == "invisible"

    def test_set_visible(self, stage):
        stage.DefinePrim("/World/Sphere", "Sphere")
        apply_event(stage, {"k": K_SET_VISIBILITY, "prim": "/World/Sphere", "visible": False})
        apply_event(stage, {"k": K_SET_VISIBILITY, "prim": "/World/Sphere", "visible": True})
        prim = stage.GetPrimAtPath("/World/Sphere")
        imageable = UsdGeom.Imageable(prim)
        assert imageable.GetVisibilityAttr().Get() == "inherited"


class TestSetGprimAttrs:
    def test_nonexistent_attr_ignored(self, stage):
        """Setting a non-existent attribute is a no-op."""
        stage.DefinePrim("/World/Sphere", "Sphere")
        # "bogus" is not a real attribute — should not raise
        apply_event(
            stage,
            {"k": K_SET_GPRIM_ATTRS, "prim": "/World/Sphere", "attrs": {"bogus": 1.0}},
        )

    def test_nonexistent_prim_ignored(self, stage):
        """Setting attrs on a missing prim is a no-op."""
        apply_event(
            stage,
            {"k": K_SET_GPRIM_ATTRS, "prim": "/World/Missing", "attrs": {"radius": 1.0}},
        )

    def test_sphere_radius(self, stage):
        stage.DefinePrim("/World/Sphere", "Sphere")
        apply_event(
            stage,
            {
                "k": K_SET_GPRIM_ATTRS,
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
                "k": K_SET_GPRIM_ATTRS,
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
                "k": K_SET_REFERENCE,
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
                "k": K_SET_REFERENCE,
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
                "k": K_SET_REFERENCE,
                "prim": "/World/Clearable",
                "refs": [{"asset_path": ref_stage.GetRootLayer().identifier}],
            },
        )
        prim = stage.GetPrimAtPath("/World/Clearable")
        assert prim.HasAuthoredReferences()

        # Clear with empty refs
        apply_event(
            stage,
            {"k": K_SET_REFERENCE, "prim": "/World/Clearable", "refs": []},
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
                "k": K_SET_REFERENCE,
                "prim": "/World/InternalRef",
                "refs": [{"prim_path": "/Source"}],
            },
        )
        prim = stage.GetPrimAtPath("/World/InternalRef")
        assert prim.IsValid()
        assert prim.HasAuthoredReferences()

    def test_preserves_nonexplicit_list_op_state(self, stage):
        custom_data = {
            "rank": 7,
            "vector": Gf.Vec3d(1.0, 2.0, 3.0),
        }
        apply_event(
            stage,
            {
                "k": K_SET_REFERENCE,
                "prim": "/World/Rich",
                "refs": [
                    {
                        "asset_path": "/tmp/added.usda",
                        "prim_path": "/Added",
                        "list_position": "added",
                        "layer_offset": 7.0,
                        "layer_scale": 2.0,
                        "custom_data_fragment": serialize_reference_custom_data(
                            custom_data,
                        ),
                    },
                    {
                        "asset_path": "/tmp/prepended.usda",
                        "list_position": "prepended",
                    },
                    {
                        "asset_path": "/tmp/appended.usda",
                        "list_position": "appended",
                    },
                    {
                        "asset_path": "/tmp/deleted.usda",
                        "list_position": "deleted",
                    },
                    {
                        "prim_path": "/Internal",
                        "list_position": "ordered",
                    },
                ],
                "list_op_authored": True,
                "list_op_explicit": False,
            },
        )

        op = stage.GetRootLayer().GetPrimAtPath("/World/Rich").referenceList
        assert op.addedItems[0].layerOffset == Sdf.LayerOffset(7.0, 2.0)
        assert op.addedItems[0].customData == custom_data
        assert op.prependedItems[0].assetPath == "/tmp/prepended.usda"
        assert op.appendedItems[0].assetPath == "/tmp/appended.usda"
        assert op.deletedItems[0].assetPath == "/tmp/deleted.usda"
        assert op.orderedItems[0].primPath == Sdf.Path("/Internal")

    def test_distinguishes_explicit_empty_from_clear(self, stage):
        prim_path = "/World/Blocked"
        apply_event(
            stage,
            {
                "k": K_SET_REFERENCE,
                "prim": prim_path,
                "refs": [],
                "list_op_authored": True,
                "list_op_explicit": True,
            },
        )
        spec = stage.GetRootLayer().GetPrimAtPath(prim_path)
        assert spec.HasInfo("references")
        assert spec.referenceList.isExplicit
        assert spec.referenceList.explicitItems == []

        apply_event(
            stage,
            {
                "k": K_SET_REFERENCE,
                "prim": prim_path,
                "refs": [],
                "list_op_authored": False,
                "list_op_explicit": False,
            },
        )
        assert not spec.HasInfo("references")

    def test_unauthored_clear_does_not_create_local_prim_spec(self):
        base = Sdf.Layer.CreateAnonymous("reference-clear-base")
        base_stage = Usd.Stage.Open(base)
        base_stage.DefinePrim("/World/Composed", "Xform")
        session = Sdf.Layer.CreateAnonymous("reference-clear-session")
        stage = Usd.Stage.Open(base, session)
        stage.SetEditTarget(Usd.EditTarget(session))

        apply_event(
            stage,
            {
                "k": K_SET_REFERENCE,
                "prim": "/World/Composed",
                "refs": [],
                "list_op_authored": False,
                "list_op_explicit": False,
            },
        )

        assert session.GetPrimAtPath("/World/Composed") is None


class TestSetPayload:
    def test_add_payload(self, stage):
        pay_stage = Usd.Stage.CreateInMemory("pay.usda")
        pay_stage.DefinePrim("/Model", "Xform")
        pay_path = pay_stage.GetRootLayer().identifier

        apply_event(
            stage,
            {
                "k": K_SET_PAYLOAD,
                "prim": "/World/Asset",
                "payloads": [{"asset_path": pay_path, "prim_path": "/Model"}],
            },
        )
        prim = stage.GetPrimAtPath("/World/Asset")
        assert prim.IsValid()
        assert prim.HasAuthoredPayloads()

    def test_add_multiple_payloads(self, stage):
        pay_a = Usd.Stage.CreateInMemory("pay_a.usda")
        pay_a.DefinePrim("/A", "Xform")
        pay_b = Usd.Stage.CreateInMemory("pay_b.usda")
        pay_b.DefinePrim("/B", "Xform")

        apply_event(
            stage,
            {
                "k": K_SET_PAYLOAD,
                "prim": "/World/Multi",
                "payloads": [
                    {"asset_path": pay_a.GetRootLayer().identifier, "prim_path": "/A"},
                    {"asset_path": pay_b.GetRootLayer().identifier, "prim_path": "/B"},
                ],
            },
        )
        prim = stage.GetPrimAtPath("/World/Multi")
        assert prim.IsValid()
        assert prim.HasAuthoredPayloads()

    def test_clear_payloads(self, stage):
        pay_stage = Usd.Stage.CreateInMemory("pay_clear.usda")
        pay_stage.DefinePrim("/X", "Xform")

        apply_event(
            stage,
            {
                "k": K_SET_PAYLOAD,
                "prim": "/World/Clearable",
                "payloads": [{"asset_path": pay_stage.GetRootLayer().identifier}],
            },
        )
        prim = stage.GetPrimAtPath("/World/Clearable")
        assert prim.HasAuthoredPayloads()

        apply_event(
            stage,
            {"k": K_SET_PAYLOAD, "prim": "/World/Clearable", "payloads": []},
        )
        prim = stage.GetPrimAtPath("/World/Clearable")
        assert not prim.HasAuthoredPayloads()

    def test_add_internal_payload(self, stage):
        stage.DefinePrim("/Source", "Xform")

        apply_event(
            stage,
            {
                "k": K_SET_PAYLOAD,
                "prim": "/World/InternalPay",
                "payloads": [{"prim_path": "/Source"}],
            },
        )
        prim = stage.GetPrimAtPath("/World/InternalPay")
        assert prim.IsValid()
        assert prim.HasAuthoredPayloads()

    def test_preserves_payload_position_and_offset(self, stage):
        apply_event(
            stage,
            {
                "k": K_SET_PAYLOAD,
                "prim": "/World/RichPayload",
                "payloads": [
                    {
                        "asset_path": "/tmp/heavy.usda",
                        "prim_path": "/Asset",
                        "list_position": "appended",
                        "layer_offset": -4.0,
                        "layer_scale": 0.25,
                    }
                ],
                "list_op_authored": True,
                "list_op_explicit": False,
            },
        )

        op = stage.GetRootLayer().GetPrimAtPath(
            "/World/RichPayload",
        ).payloadList
        assert op.prependedItems == []
        assert op.appendedItems[0].layerOffset == Sdf.LayerOffset(-4.0, 0.25)

    def test_load_payload(self):
        """load_payload makes payload children visible on the stage."""
        stage = Usd.Stage.CreateInMemory()
        # Create a payload file
        payload_stage = Usd.Stage.CreateInMemory()
        payload_stage.DefinePrim("/Model", "Xform")
        payload_stage.DefinePrim("/Model/Child", "Mesh")
        payload_path = payload_stage.GetRootLayer().identifier

        # Set payload arc
        apply_event(
            stage,
            {
                "k": K_SET_PAYLOAD,
                "prim": "/World/Asset",
                "payloads": [{"asset_path": payload_path, "prim_path": "/Model"}],
            },
        )
        # Unload by default
        stage.Unload(Sdf.Path("/World/Asset"))
        assert not stage.GetPrimAtPath("/World/Asset").IsLoaded()

        # Load it
        apply_event(stage, {"k": K_LOAD_PAYLOAD, "prim": "/World/Asset"})
        assert stage.GetPrimAtPath("/World/Asset").IsLoaded()
        # Children should be visible
        child = stage.GetPrimAtPath("/World/Asset/Child")
        assert child and child.IsValid()

    def test_unload_payload(self):
        """unload_payload hides payload children."""
        stage = Usd.Stage.CreateInMemory()
        payload_stage = Usd.Stage.CreateInMemory()
        payload_stage.DefinePrim("/Model", "Xform")
        payload_stage.DefinePrim("/Model/Child", "Mesh")
        payload_path = payload_stage.GetRootLayer().identifier

        apply_event(
            stage,
            {
                "k": K_SET_PAYLOAD,
                "prim": "/World/Asset",
                "payloads": [{"asset_path": payload_path, "prim_path": "/Model"}],
            },
        )
        # Load first
        stage.Load(Sdf.Path("/World/Asset"))
        assert stage.GetPrimAtPath("/World/Asset").IsLoaded()

        # Unload via event
        apply_event(stage, {"k": K_UNLOAD_PAYLOAD, "prim": "/World/Asset"})
        assert not stage.GetPrimAtPath("/World/Asset").IsLoaded()


class TestSetVariantSelections:
    def _make_variant_stage(self):
        """Create stage with a prim that has a 'size' variant set."""
        import os

        fixture = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "fixtures", "variant_sphere.usda"
        )
        return Usd.Stage.Open(fixture)

    def test_apply_variant_selection(self):
        stage = self._make_variant_stage()
        prim = stage.GetPrimAtPath("/World/Sphere")
        assert prim.GetVariantSets().GetVariantSelection("size") == "small"

        apply_event(
            stage,
            {
                "k": K_SET_VARIANT_SELECTIONS,
                "prim": "/World/Sphere",
                "selections": {"size": "large"},
            },
        )
        assert prim.GetVariantSets().GetVariantSelection("size") == "large"

    def test_variant_selection_affects_composed_value(self):
        stage = self._make_variant_stage()
        apply_event(
            stage,
            {
                "k": K_SET_VARIANT_SELECTIONS,
                "prim": "/World/Sphere",
                "selections": {"size": "medium"},
            },
        )
        prim = stage.GetPrimAtPath("/World/Sphere")
        radius = prim.GetAttribute("radius").Get()
        assert abs(radius - 5.0) < 1e-6

    def test_nonexistent_variant_set_ignored(self, stage):
        stage.DefinePrim("/World/Plain", "Xform")
        # Should not raise — just skip the non-existent set
        apply_event(
            stage,
            {
                "k": K_SET_VARIANT_SELECTIONS,
                "prim": "/World/Plain",
                "selections": {"bogus": "nope"},
            },
        )


class TestApplyEvents:
    def test_atomic_batch(self, stage):
        events = [
            {"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Xform"},
            {"k": K_ENSURE_PRIM, "prim": "/World/B", "typeName": "Xform"},
            {"k": K_ENSURE_XFORM_OPS, "prim": "/World/A"},
            {"k": K_SET_XFORM_TRS, "prim": "/World/A", "fields": ["t"], "t": [5.0, 0.0, 0.0]},
        ]
        apply_events(stage, events)
        assert stage.GetPrimAtPath("/World/A").IsValid()
        assert stage.GetPrimAtPath("/World/B").IsValid()


# ---------------------------------------------------------------------------
# Material binding
# ---------------------------------------------------------------------------


class TestSetMaterialBinding:
    def test_bind_material(self, stage):
        """Binding creates a material:binding relationship."""
        from pxr import UsdShade

        stage.DefinePrim("/World/Sphere", "Sphere")
        stage.DefinePrim("/Materials/Plastic", "Material")
        apply_event(
            stage,
            {
                "k": "set_material_binding",
                "prim": "/World/Sphere",
                "material_path": "/Materials/Plastic",
            },
        )

        prim = stage.GetPrimAtPath("/World/Sphere")
        binding = UsdShade.MaterialBindingAPI(prim)
        mat, _ = binding.ComputeBoundMaterial()
        assert str(mat.GetPath()) == "/Materials/Plastic"

    def test_unbind_material(self, stage):
        """Empty material_path unbinds."""
        from pxr import UsdShade

        stage.DefinePrim("/World/Sphere", "Sphere")
        stage.DefinePrim("/Materials/Plastic", "Material")
        apply_event(
            stage,
            {
                "k": "set_material_binding",
                "prim": "/World/Sphere",
                "material_path": "/Materials/Plastic",
            },
        )
        apply_event(
            stage,
            {
                "k": "set_material_binding",
                "prim": "/World/Sphere",
                "material_path": "",
            },
        )

        prim = stage.GetPrimAtPath("/World/Sphere")
        binding = UsdShade.MaterialBindingAPI(prim)
        mat, _ = binding.ComputeBoundMaterial()
        assert not mat

    def test_rebind_material(self, stage):
        """Rebinding changes the target."""
        from pxr import UsdShade

        stage.DefinePrim("/World/Sphere", "Sphere")
        stage.DefinePrim("/Materials/A", "Material")
        stage.DefinePrim("/Materials/B", "Material")
        apply_event(
            stage,
            {
                "k": "set_material_binding",
                "prim": "/World/Sphere",
                "material_path": "/Materials/A",
            },
        )
        apply_event(
            stage,
            {
                "k": "set_material_binding",
                "prim": "/World/Sphere",
                "material_path": "/Materials/B",
            },
        )

        prim = stage.GetPrimAtPath("/World/Sphere")
        binding = UsdShade.MaterialBindingAPI(prim)
        mat, _ = binding.ComputeBoundMaterial()
        assert str(mat.GetPath()) == "/Materials/B"


# ---------------------------------------------------------------------------
# Shader inputs
# ---------------------------------------------------------------------------


class TestSetConnectableInput:
    def test_set_preview_surface_inputs(self, stage):
        """Shader inputs are created with correct types and values."""
        from pxr import UsdShade

        stage.DefinePrim("/Materials/Mat", "Material")
        apply_event(
            stage,
            {
                "k": "set_connectable_input",
                "prim": "/Materials/Mat/PBR",
                "info_id": "UsdPreviewSurface",
                "inputs": {
                    "diffuseColor": [0.8, 0.2, 0.2],
                    "metallic": 0.0,
                    "roughness": 0.5,
                },
                "input_types": {
                    "diffuseColor": "color3f",
                    "metallic": "float",
                    "roughness": "float",
                },
            },
        )

        shader = UsdShade.Shader(stage.GetPrimAtPath("/Materials/Mat/PBR"))
        assert shader.GetIdAttr().Get() == "UsdPreviewSurface"
        dc = shader.GetInput("diffuseColor").Get()
        assert abs(dc[0] - 0.8) < 1e-6
        assert abs(dc[1] - 0.2) < 1e-6
        assert abs(shader.GetInput("metallic").Get()) < 1e-6
        assert abs(shader.GetInput("roughness").Get() - 0.5) < 1e-6

    def test_material_output_explicit_connection(self, stage):
        """An explicit set_shader_connection event with port_kind=output
        authors Material.outputs:surface.connect — the only mechanism by
        which a Material terminal gets wired (no auto-wire fallback)."""
        from pxr import UsdShade

        stage.DefinePrim("/Materials/Mat", "Material")
        apply_event(
            stage,
            {
                "k": "set_connectable_input",
                "prim": "/Materials/Mat/PBR",
                "info_id": "UsdPreviewSurface",
                "inputs": {"roughness": 0.3},
                "input_types": {"roughness": "float"},
            },
        )
        apply_event(
            stage,
            {
                "k": "set_connectable_connection",
                "prim": "/Materials/Mat",
                "connections": {
                    "outputs:surface": {
                        "source_prim": "/Materials/Mat/PBR",
                        "source_attr": "outputs:surface",
                    },
                },
            },
        )

        material = UsdShade.Material(stage.GetPrimAtPath("/Materials/Mat"))
        sources, _ = material.GetSurfaceOutput().GetConnectedSources()
        assert len(sources) == 1
        assert str(sources[0].source.GetPath()) == "/Materials/Mat/PBR"
        assert sources[0].sourceName == "surface"

    def test_material_no_phantom_outputs(self, stage):
        """set_shader_input on a surface shader must not synthesize any
        Material output connections — the wire-driven design requires the
        emitter to author them via set_shader_connection.  Verifies the
        receiver stage matches the source-of-truth shape rather than
        guessing at terminals from info_id."""
        from pxr import UsdShade

        stage.DefinePrim("/Materials/Brass", "Material")
        apply_event(
            stage,
            {
                "k": "set_connectable_input",
                "prim": "/Materials/Brass/SS",
                "info_id": "ND_standard_surface_surfaceshader",
                "inputs": {"metalness": 1.0},
                "input_types": {"metalness": "float"},
            },
        )

        material = UsdShade.Material(stage.GetPrimAtPath("/Materials/Brass"))
        assert not material.GetSurfaceOutput().GetAttr().HasAuthoredConnections()
        assert not material.GetDisplacementOutput().GetAttr().HasAuthoredConnections()
        # And no phantom outputs were created on the shader itself.
        ss = UsdShade.Shader(stage.GetPrimAtPath("/Materials/Brass/SS"))
        authored_outputs = [o for o in ss.GetOutputs() if o.GetAttr().IsAuthored()]
        assert authored_outputs == []

    def test_update_existing_input(self, stage):
        """Updating a shader input changes the value."""
        from pxr import UsdShade

        stage.DefinePrim("/Materials/Mat", "Material")
        apply_event(
            stage,
            {
                "k": "set_connectable_input",
                "prim": "/Materials/Mat/PBR",
                "info_id": "UsdPreviewSurface",
                "inputs": {"roughness": 0.3},
                "input_types": {"roughness": "float"},
            },
        )
        apply_event(
            stage,
            {
                "k": "set_connectable_input",
                "prim": "/Materials/Mat/PBR",
                "info_id": "UsdPreviewSurface",
                "inputs": {"roughness": 0.9},
                "input_types": {"roughness": "float"},
            },
        )

        shader = UsdShade.Shader(stage.GetPrimAtPath("/Materials/Mat/PBR"))
        assert abs(shader.GetInput("roughness").Get() - 0.9) < 1e-6

    def test_asset_path_input_applied(self, stage):
        """Asset-typed shader input arrives as a plain string but is authored
        as Sdf.AssetPath on an attribute declared with the asset type.
        pxr auto-converts strings on asset-typed inputs."""
        from pxr import Sdf, UsdShade

        stage.DefinePrim("/Materials/Mat", "Material")
        apply_event(
            stage,
            {
                "k": "set_connectable_input",
                "prim": "/Materials/Mat/Tex",
                "info_id": "UsdUVTexture",
                "inputs": {"file": "./r_normal_map.png"},
                "input_types": {"file": "asset"},
            },
        )

        shader = UsdShade.Shader(stage.GetPrimAtPath("/Materials/Mat/Tex"))
        file_input = shader.GetInput("file")
        assert str(file_input.GetAttr().GetTypeName()) == "asset"
        val = file_input.Get()
        assert isinstance(val, Sdf.AssetPath)
        assert val.path == "./r_normal_map.png"

    def test_float4_and_color4f_inputs_applied(self, stage):
        """float4 (UsdUVTexture bias/scale) and color4f survive apply.
        Previously fell through to inp.Set(list) and raised a type mismatch."""
        from pxr import Gf, UsdShade

        stage.DefinePrim("/Materials/Mat", "Material")
        apply_event(
            stage,
            {
                "k": "set_connectable_input",
                "prim": "/Materials/Mat/Tex",
                "info_id": "UsdUVTexture",
                "inputs": {
                    "bias": [-1.0, -1.0, -1.0, -1.0],
                    "scale": [2.0, 2.0, 2.0, 2.0],
                    "tint": [1.0, 0.5, 0.25, 0.75],
                },
                "input_types": {
                    "bias": "float4",
                    "scale": "float4",
                    "tint": "color4f",
                },
            },
        )

        shader = UsdShade.Shader(stage.GetPrimAtPath("/Materials/Mat/Tex"))
        bias = shader.GetInput("bias").Get()
        assert isinstance(bias, Gf.Vec4f)
        assert list(bias) == [-1.0, -1.0, -1.0, -1.0]
        scale = shader.GetInput("scale").Get()
        assert isinstance(scale, Gf.Vec4f)
        assert list(scale) == [2.0, 2.0, 2.0, 2.0]
        tint = shader.GetInput("tint").Get()
        assert isinstance(tint, Gf.Vec4f)
        assert list(tint) == [1.0, 0.5, 0.25, 0.75]

    def test_full_material_pipeline(self, stage):
        """Full pipeline: create material + shader + bind to geometry +
        explicit Material output wiring via set_shader_connection."""
        from pxr import UsdShade

        events = [
            {"k": "ensure_prim", "prim": "/World", "typeName": "Xform"},
            {"k": "ensure_prim", "prim": "/World/Sphere", "typeName": "Sphere"},
            {"k": "ensure_prim", "prim": "/Materials", "typeName": "Scope"},
            {"k": "ensure_prim", "prim": "/Materials/Red", "typeName": "Material"},
            {
                "k": "set_connectable_input",
                "prim": "/Materials/Red/PBR",
                "info_id": "UsdPreviewSurface",
                "inputs": {
                    "diffuseColor": [1.0, 0.0, 0.0],
                    "roughness": 0.4,
                },
                "input_types": {
                    "diffuseColor": "color3f",
                    "roughness": "float",
                },
            },
            {
                "k": "set_connectable_connection",
                "prim": "/Materials/Red",
                "connections": {
                    "outputs:surface": {
                        "source_prim": "/Materials/Red/PBR",
                        "source_attr": "outputs:surface",
                    },
                },
            },
            {
                "k": "set_material_binding",
                "prim": "/World/Sphere",
                "material_path": "/Materials/Red",
            },
        ]
        apply_events(stage, events)

        # Verify bound material resolves to correct shader values
        sphere = stage.GetPrimAtPath("/World/Sphere")
        binding = UsdShade.MaterialBindingAPI(sphere)
        mat, _ = binding.ComputeBoundMaterial()
        assert str(mat.GetPath()) == "/Materials/Red"

        surface = mat.GetSurfaceOutput()
        sources, _ = surface.GetConnectedSources()
        shader = UsdShade.Shader(sources[0].source)
        dc = shader.GetInput("diffuseColor").Get()
        assert abs(dc[0] - 1.0) < 1e-6
        assert abs(dc[1]) < 1e-6


class TestSetConnectableConnection:
    """Connection events should produce correctly-typed attributes using Sdr."""

    def test_lazy_source_output_uses_sdr_type(self, stage):
        """When the source output doesn't exist yet, its type is resolved
        from the source shader's NodeDef (via Sdr) — UsdUVTexture.outputs:rgb
        is float3, not Token."""
        from pxr import Sdf, UsdShade

        stage.DefinePrim("/Mat", "Material")
        # set_shader_input for the source establishes its info:id but doesn't
        # author outputs.  Connection event then materializes outputs:rgb.
        apply_event(
            stage,
            {
                "k": "set_connectable_input",
                "prim": "/Mat/Tex",
                "info_id": "UsdUVTexture",
                "inputs": {},
                "input_types": {},
            },
        )
        apply_event(
            stage,
            {
                "k": "set_connectable_input",
                "prim": "/Mat/PBR",
                "info_id": "UsdPreviewSurface",
                "inputs": {},
                "input_types": {},
            },
        )
        apply_event(
            stage,
            {
                "k": "set_connectable_connection",
                "prim": "/Mat/PBR",
                "connections": {
                    "inputs:diffuseColor": {
                        "source_prim": "/Mat/Tex",
                        "source_attr": "outputs:rgb",
                    },
                },
            },
        )

        tex = UsdShade.Shader(stage.GetPrimAtPath("/Mat/Tex"))
        rgb = tex.GetOutput("rgb")
        assert rgb
        # Sdr says UsdUVTexture.outputs:rgb is float3.
        assert rgb.GetTypeName() == Sdf.ValueTypeNames.Float3

    def test_lazy_target_input_uses_sdr_type(self, stage):
        """When the target input doesn't exist yet, its type comes from the
        consumer shader's NodeDef — UsdPreviewSurface.inputs:normal is
        normal3f, not whatever Token the source is."""
        from pxr import Sdf, UsdShade

        stage.DefinePrim("/Mat", "Material")
        apply_event(
            stage,
            {
                "k": "set_connectable_input",
                "prim": "/Mat/PBR",
                "info_id": "UsdPreviewSurface",
                "inputs": {},
                "input_types": {},
            },
        )
        apply_event(
            stage,
            {
                "k": "set_connectable_input",
                "prim": "/Mat/NormalMap",
                "info_id": "UsdUVTexture",
                "inputs": {},
                "input_types": {},
            },
        )
        apply_event(
            stage,
            {
                "k": "set_connectable_connection",
                "prim": "/Mat/PBR",
                "connections": {
                    "inputs:normal": {
                        "source_prim": "/Mat/NormalMap",
                        "source_attr": "outputs:rgb",
                    },
                },
            },
        )

        pbr = UsdShade.Shader(stage.GetPrimAtPath("/Mat/PBR"))
        normal_in = pbr.GetInput("normal")
        assert normal_in
        assert normal_in.GetAttr().GetTypeName() == Sdf.ValueTypeNames.Normal3f

    def test_unknown_shader_falls_back_to_token(self, stage):
        """A shader whose info:id isn't in Sdr keeps the old Token behavior
        for outputs — graceful fallback, no exception."""
        from pxr import Sdf, UsdShade

        stage.DefinePrim("/Mat", "Material")
        # Custom shader ID that Sdr doesn't know about.
        apply_event(
            stage,
            {
                "k": "set_connectable_input",
                "prim": "/Mat/CustomSrc",
                "info_id": "MyStudio_UnregisteredNode",
                "inputs": {},
                "input_types": {},
            },
        )
        apply_event(
            stage,
            {
                "k": "set_connectable_input",
                "prim": "/Mat/CustomDst",
                "info_id": "MyStudio_AlsoUnregistered",
                "inputs": {},
                "input_types": {},
            },
        )
        apply_event(
            stage,
            {
                "k": "set_connectable_connection",
                "prim": "/Mat/CustomDst",
                "connections": {
                    "inputs:foo": {
                        "source_prim": "/Mat/CustomSrc",
                        "source_attr": "outputs:out",
                    },
                },
            },
        )

        src = UsdShade.Shader(stage.GetPrimAtPath("/Mat/CustomSrc"))
        out = src.GetOutput("out")
        assert out.GetTypeName() == Sdf.ValueTypeNames.Token


class TestAtomicApply:
    """atomic_apply context manager: commit on success, rollback on failure."""

    def test_commits_on_success(self, stage):
        events = [
            {"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Xform"},
            {"k": K_SET_XFORM_TRS, "prim": "/World/A", "fields": ["t"], "t": [1.0, 2.0, 3.0]},
        ]
        with atomic_apply(stage):
            apply_events(stage, events)

        prim = stage.GetPrimAtPath("/World/A")
        assert prim and prim.IsValid()

    def test_rollback_on_exception(self, stage):
        # Pre-state: /World exists, /World/B does not
        assert not stage.GetPrimAtPath("/World/B").IsValid()

        with pytest.raises(RuntimeError):
            with atomic_apply(stage):
                apply_events(
                    stage,
                    [
                        {"k": K_ENSURE_PRIM, "prim": "/World/B", "typeName": "Xform"},
                    ],
                )
                # Verify B exists mid-transaction
                assert stage.GetPrimAtPath("/World/B").IsValid()
                raise RuntimeError("simulated failure")

        # B should be rolled back
        assert not stage.GetPrimAtPath("/World/B").IsValid()

    def test_exception_propagates(self, stage):
        with pytest.raises(ValueError, match="test error"):
            with atomic_apply(stage):
                raise ValueError("test error")

    def test_partial_batch_rollback(self, stage):
        # Set up a known starting point
        apply_events(
            stage,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/Pre", "typeName": "Xform"},
                {"k": K_ENSURE_XFORM_OPS, "prim": "/World/Pre"},
                {
                    "k": K_SET_XFORM_TRS,
                    "prim": "/World/Pre",
                    "fields": ["t"],
                    "t": [10.0, 20.0, 30.0],
                },
            ],
        )

        # Build a batch where event 3 will fail
        events = [
            {"k": K_ENSURE_PRIM, "prim": "/World/E1", "typeName": "Xform"},
            {"k": K_ENSURE_PRIM, "prim": "/World/E2", "typeName": "Xform"},
        ]

        def failing_apply():
            with atomic_apply(stage):
                apply_events(stage, events)
                raise RuntimeError("fail after partial apply")

        with pytest.raises(RuntimeError):
            failing_apply()

        # E1 and E2 should be rolled back
        assert not stage.GetPrimAtPath("/World/E1").IsValid()
        assert not stage.GetPrimAtPath("/World/E2").IsValid()
        # Pre-existing prim should be untouched
        assert stage.GetPrimAtPath("/World/Pre").IsValid()


class TestReceiverStageFirstFlow:
    """Simulates the Blender receiver's stage-first flow:
    commit to stage, then dispatch to adapter.  Verifies that on
    failure the stage rolls back and the adapter is never called.
    """

    def test_adapter_only_called_after_stage_commit(self, stage):
        """Adapter dispatch happens only when stage commit succeeds."""
        from openusdconnect.adapters import MockAdapter

        adapter = MockAdapter()
        events = [
            {"k": K_ENSURE_PRIM, "prim": "/World/X", "typeName": "Xform"},
            {"k": K_ENSURE_XFORM_OPS, "prim": "/World/X"},
            {"k": K_SET_XFORM_TRS, "prim": "/World/X", "fields": ["t"], "t": [5.0, 6.0, 7.0]},
        ]

        with atomic_apply(stage):
            apply_events(stage, events)

        for ev in events:
            adapter.apply_event(ev)

        assert stage.GetPrimAtPath("/World/X").IsValid()
        assert "/World/X" in adapter._prims
        assert adapter.get_trs("/World/X").get("t") == [5.0, 6.0, 7.0]

    def test_adapter_not_called_on_stage_failure(self, stage):
        """If stage commit fails, adapter is never touched."""
        from openusdconnect.adapters import MockAdapter

        adapter = MockAdapter()
        events = [
            {"k": K_ENSURE_PRIM, "prim": "/World/Y", "typeName": "Xform"},
            {"k": K_SET_XFORM_TRS, "prim": "/World/Y", "fields": ["t"], "t": [1.0, 2.0, 3.0]},
        ]

        adapter_called = False

        with pytest.raises(RuntimeError):
            with atomic_apply(stage):
                apply_events(stage, events)
                raise RuntimeError("simulated network corruption")

            adapter_called = True

        assert not stage.GetPrimAtPath("/World/Y").IsValid()
        assert not adapter_called
        assert "/World/Y" not in adapter._prims

    def test_stage_rollback_preserves_prior_state(self, stage):
        """Existing stage state survives a failed batch."""
        from openusdconnect.adapters import MockAdapter

        apply_events(
            stage,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/Existing", "typeName": "Xform"},
                {"k": K_ENSURE_XFORM_OPS, "prim": "/World/Existing"},
                {
                    "k": K_SET_XFORM_TRS,
                    "prim": "/World/Existing",
                    "fields": ["t"],
                    "t": [100.0, 200.0, 300.0],
                },
            ],
        )

        adapter = MockAdapter()
        adapter.ensure_prim("/World/Existing", "Xform")
        adapter.ensure_xform_ops("/World/Existing")
        adapter.set_xform_trs("/World/Existing", t=[100.0, 200.0, 300.0])

        with pytest.raises(RuntimeError):
            with atomic_apply(stage):
                apply_events(
                    stage,
                    [
                        {"k": K_ENSURE_PRIM, "prim": "/World/New1", "typeName": "Xform"},
                        {"k": K_ENSURE_PRIM, "prim": "/World/New2", "typeName": "Xform"},
                    ],
                )
                raise RuntimeError("fail")

        assert not stage.GetPrimAtPath("/World/New1").IsValid()
        assert not stage.GetPrimAtPath("/World/New2").IsValid()
        assert stage.GetPrimAtPath("/World/Existing").IsValid()
        assert adapter.get_trs("/World/Existing").get("t") == [100.0, 200.0, 300.0]


def test_apply_events_orders_create_before_connect():
    """apply_events must apply prim-creating kinds before connections so
    ensure_prim runs before set_connectable_connection.

    Otherwise a connection whose source_prim names a not-yet-ensured NodeGraph
    falls through _apply_set_connectable_connection's Shader-default branch in
    get_or_define_prim, and the later ensure_prim cannot upgrade the typeName
    (get_or_define_prim only sets typeName when creating a fresh spec). The
    CREATE-before-MODIFY partition closes that gap by applying ensure_prim for
    the source first.

    Pins the invariant: removing the create-before-modify ordering in
    apply_events should fail here. Paired with
    test_receiver_matches_sender_under_shuffled_event_order.
    """
    stage = Usd.Stage.CreateInMemory()
    events = [
        {
            "k": K_SET_CONNECTABLE_CONNECTION,
            "prim": "/Test/MX",
            "connections": {
                "inputs:base_color": {
                    "source_prim": "/Test/NG",
                    "source_attr": "outputs:result",
                },
            },
            "disconnections": [],
        },
        {"k": K_ENSURE_PRIM, "prim": "/Test", "typeName": "Xform"},
        {"k": K_ENSURE_PRIM, "prim": "/Test/NG", "typeName": "NodeGraph"},
        {"k": K_ENSURE_PRIM, "prim": "/Test/MX", "typeName": "Shader"},
    ]
    apply_events(stage, events)

    ng = stage.GetPrimAtPath("/Test/NG")
    assert ng.IsValid()
    assert str(ng.GetTypeName()) == "NodeGraph"
    mx = stage.GetPrimAtPath("/Test/MX")
    assert mx.IsValid()
    assert str(mx.GetTypeName()) == "Shader"


class TestNamespaceEditsInBatch:
    """delete_prim / rename_prim apply outside the value ChangeBlock so each
    sees the composed result of every event before it in the batch."""

    def _stage_with(self, *prims):
        stage = Usd.Stage.CreateInMemory()
        apply_events(stage, [
            {"k": K_ENSURE_PRIM, "prim": "/World", "typeName": "Xform"},
            *({"k": K_ENSURE_PRIM, "prim": p, "typeName": "Xform"} for p in prims),
        ])
        return stage

    def test_chained_renames_in_one_batch(self):
        stage = self._stage_with("/World/A")
        apply_events(stage, [
            {"k": K_RENAME_PRIM, "prim": "/World/A", "new_name": "B"},
            {"k": K_RENAME_PRIM, "prim": "/World/B", "new_name": "C"},
        ])
        assert stage.GetPrimAtPath("/World/C")
        assert not stage.GetPrimAtPath("/World/A")
        assert not stage.GetPrimAtPath("/World/B")

    def test_rename_then_write_at_new_path(self):
        stage = self._stage_with("/World/X")
        apply_events(stage, [
            {"k": K_RENAME_PRIM, "prim": "/World/X", "new_name": "Y"},
            {"k": K_SET_VISIBILITY, "prim": "/World/Y", "visible": False},
        ])
        prim = stage.GetPrimAtPath("/World/Y")
        assert prim
        assert UsdGeom.Imageable(prim).GetVisibilityAttr().Get() == "invisible"

    def test_write_then_delete_same_prim(self):
        stage = self._stage_with("/World/E")
        apply_events(stage, [
            {"k": K_SET_VISIBILITY, "prim": "/World/E", "visible": False},
            {"k": K_DELETE_PRIM, "prim": "/World/E"},
        ])
        assert not stage.GetPrimAtPath("/World/E")

    def test_delete_then_write_is_dropped(self):
        """A value write after a delete in the same batch lands nowhere."""
        stage = self._stage_with("/World/F")
        apply_events(stage, [
            {"k": K_DELETE_PRIM, "prim": "/World/F"},
            {"k": K_SET_VISIBILITY, "prim": "/World/F", "visible": False},
        ])
        assert not stage.GetPrimAtPath("/World/F")

    def test_delete_then_recreate_same_path_survives(self):
        """A delete followed by a same-path recreate (with structural + value
        ops after it) in one batch must leave the recreated prim intact. The
        recreate's structural ops are not hoisted ahead of the delete barrier."""
        stage = self._stage_with("/World/Ball")  # exists as Xform
        apply_events(stage, [
            {"k": K_DELETE_PRIM, "prim": "/World/Ball"},
            {"k": K_ENSURE_PRIM, "prim": "/World/Ball", "typeName": "Sphere"},
            {"k": K_SET_VISIBILITY, "prim": "/World/Ball", "visible": False},
        ])
        prim = stage.GetPrimAtPath("/World/Ball")
        assert prim.IsValid()
        assert str(prim.GetTypeName()) == "Sphere"
        assert UsdGeom.Imageable(prim).GetVisibilityAttr().Get() == "invisible"

    def test_create_delete_recreate_one_batch(self):
        """create -> delete -> recreate-as-different-type in a single batch: the
        shape a fresh client hits when it replays the whole backlog at once. The
        prim must end up at the recreated type, not clobbered by the delete."""
        stage = Usd.Stage.CreateInMemory()
        apply_events(stage, [
            {"k": K_ENSURE_PRIM, "prim": "/World", "typeName": "Xform"},
            {"k": K_ENSURE_PRIM, "prim": "/World/Ball", "typeName": "Sphere"},
            {"k": K_DELETE_PRIM, "prim": "/World/Ball"},
            {"k": K_ENSURE_PRIM, "prim": "/World/Ball", "typeName": "Cube"},
        ])
        prim = stage.GetPrimAtPath("/World/Ball")
        assert prim.IsValid()
        assert str(prim.GetTypeName()) == "Cube"


class TestScopedAtomicApply:
    """atomic_apply(stage, prim_paths=...) backs up only the touched specs."""

    def _stage(self):
        stage = Usd.Stage.CreateInMemory()
        apply_events(stage, [
            {"k": K_ENSURE_PRIM, "prim": "/World", "typeName": "Xform"},
            {"k": K_ENSURE_PRIM, "prim": "/World/Keep", "typeName": "Xform"},
            {"k": K_SET_VISIBILITY, "prim": "/World/Keep", "visible": False},
        ])
        return stage

    def test_rollback_restores_modified_spec(self):
        stage = self._stage()
        with pytest.raises(RuntimeError):
            with atomic_apply(stage, prim_paths=["/World/Keep"]):
                apply_events(stage, [
                    {"k": K_SET_VISIBILITY, "prim": "/World/Keep", "visible": True},
                ])
                raise RuntimeError("boom")
        prim = stage.GetPrimAtPath("/World/Keep")
        assert UsdGeom.Imageable(prim).GetVisibilityAttr().Get() == "invisible"

    def test_rollback_removes_created_prims_and_ancestors(self):
        stage = self._stage()
        with pytest.raises(RuntimeError):
            with atomic_apply(stage, prim_paths=["/World/New/Deep/Child"]):
                apply_events(stage, [
                    {"k": K_ENSURE_PRIM, "prim": "/World/New/Deep/Child", "typeName": "Xform"},
                ])
                raise RuntimeError("boom")
        assert not stage.GetPrimAtPath("/World/New")
        assert stage.GetPrimAtPath("/World/Keep")

    def test_success_persists(self):
        stage = self._stage()
        with atomic_apply(stage, prim_paths=["/World/Keep"]):
            apply_events(stage, [
                {"k": K_SET_VISIBILITY, "prim": "/World/Keep", "visible": True},
            ])
        prim = stage.GetPrimAtPath("/World/Keep")
        assert UsdGeom.Imageable(prim).GetVisibilityAttr().Get() == "inherited"

    def test_rollback_with_mixed_existing_and_created(self):
        stage = self._stage()
        with pytest.raises(RuntimeError):
            with atomic_apply(stage, prim_paths=["/World/Keep", "/World/Mat/Shader"]):
                apply_events(stage, [
                    {"k": K_SET_VISIBILITY, "prim": "/World/Keep", "visible": True},
                    {
                        "k": K_SET_CONNECTABLE_CONNECTION,
                        "prim": "/World/Mat/Shader",
                        "connections": {},
                    },
                ])
                raise RuntimeError("boom")
        prim = stage.GetPrimAtPath("/World/Keep")
        assert UsdGeom.Imageable(prim).GetVisibilityAttr().Get() == "invisible"
        assert not stage.GetPrimAtPath("/World/Mat")
