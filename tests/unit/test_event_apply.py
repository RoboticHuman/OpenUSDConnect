"""Tests for openusdconnect.event_apply — apply events to in-memory Usd.Stage.

Requires pxr (OpenUSD Python bindings). Tests are skipped if pxr is not available.
"""

import pytest

try:
    from pxr import Sdf, Usd, UsdGeom  # noqa: F401

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
from openusdconnect.protocol import (
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_SET_GPRIM_ATTRS,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_VARIANT_SELECTIONS,
    K_SET_VISIBILITY,
    K_SET_XFORM_MATRICES,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
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

    def test_set_xform_matrices_ignored(self, stage):
        # Should not raise
        apply_event(
            stage,
            {
                "k": K_SET_XFORM_MATRICES,
                "prim": "/World/Sphere",
                "local_m": [0.0] * 16,
                "world_m": [0.0] * 16,
            },
        )

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

    def test_load_payload(self):
        """load_payload makes payload children visible on the stage."""
        stage = Usd.Stage.CreateInMemory()
        # Create a payload file
        payload_stage = Usd.Stage.CreateInMemory()
        payload_stage.DefinePrim("/Model", "Xform")
        payload_stage.DefinePrim("/Model/Child", "Mesh")
        payload_path = payload_stage.GetRootLayer().identifier

        # Set payload arc
        apply_event(stage, {"k": K_SET_PAYLOAD, "prim": "/World/Asset", "payloads": [
            {"asset_path": payload_path, "prim_path": "/Model"}
        ]})
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

        apply_event(stage, {"k": K_SET_PAYLOAD, "prim": "/World/Asset", "payloads": [
            {"asset_path": payload_path, "prim_path": "/Model"}
        ]})
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

        apply_event(stage, {
            "k": K_SET_VARIANT_SELECTIONS,
            "prim": "/World/Sphere",
            "selections": {"size": "large"},
        })
        assert prim.GetVariantSets().GetVariantSelection("size") == "large"

    def test_variant_selection_affects_composed_value(self):
        stage = self._make_variant_stage()
        apply_event(stage, {
            "k": K_SET_VARIANT_SELECTIONS,
            "prim": "/World/Sphere",
            "selections": {"size": "medium"},
        })
        prim = stage.GetPrimAtPath("/World/Sphere")
        radius = prim.GetAttribute("radius").Get()
        assert abs(radius - 5.0) < 1e-6

    def test_nonexistent_variant_set_ignored(self, stage):
        stage.DefinePrim("/World/Plain", "Xform")
        # Should not raise — just skip the non-existent set
        apply_event(stage, {
            "k": K_SET_VARIANT_SELECTIONS,
            "prim": "/World/Plain",
            "selections": {"bogus": "nope"},
        })


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
        apply_event(stage, {
            "k": "set_material_binding",
            "prim": "/World/Sphere",
            "material_path": "/Materials/Plastic",
        })

        prim = stage.GetPrimAtPath("/World/Sphere")
        binding = UsdShade.MaterialBindingAPI(prim)
        mat, _ = binding.ComputeBoundMaterial()
        assert str(mat.GetPath()) == "/Materials/Plastic"

    def test_unbind_material(self, stage):
        """Empty material_path unbinds."""
        from pxr import UsdShade

        stage.DefinePrim("/World/Sphere", "Sphere")
        stage.DefinePrim("/Materials/Plastic", "Material")
        apply_event(stage, {
            "k": "set_material_binding",
            "prim": "/World/Sphere",
            "material_path": "/Materials/Plastic",
        })
        apply_event(stage, {
            "k": "set_material_binding",
            "prim": "/World/Sphere",
            "material_path": "",
        })

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
        apply_event(stage, {
            "k": "set_material_binding",
            "prim": "/World/Sphere",
            "material_path": "/Materials/A",
        })
        apply_event(stage, {
            "k": "set_material_binding",
            "prim": "/World/Sphere",
            "material_path": "/Materials/B",
        })

        prim = stage.GetPrimAtPath("/World/Sphere")
        binding = UsdShade.MaterialBindingAPI(prim)
        mat, _ = binding.ComputeBoundMaterial()
        assert str(mat.GetPath()) == "/Materials/B"


# ---------------------------------------------------------------------------
# Shader inputs
# ---------------------------------------------------------------------------


class TestSetShaderInput:
    def test_set_preview_surface_inputs(self, stage):
        """Shader inputs are created with correct types and values."""
        from pxr import UsdShade

        stage.DefinePrim("/Materials/Mat", "Material")
        apply_event(stage, {
            "k": "set_shader_input",
            "prim": "/Materials/Mat/PBR",
            "shader_id": "UsdPreviewSurface",
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
        })

        shader = UsdShade.Shader(stage.GetPrimAtPath("/Materials/Mat/PBR"))
        assert shader.GetIdAttr().Get() == "UsdPreviewSurface"
        dc = shader.GetInput("diffuseColor").Get()
        assert abs(dc[0] - 0.8) < 1e-6
        assert abs(dc[1] - 0.2) < 1e-6
        assert abs(shader.GetInput("metallic").Get()) < 1e-6
        assert abs(shader.GetInput("roughness").Get() - 0.5) < 1e-6

    def test_shader_connects_to_material_output(self, stage):
        """Shader auto-connects outputs:surface to parent Material."""
        from pxr import UsdShade

        stage.DefinePrim("/Materials/Mat", "Material")
        apply_event(stage, {
            "k": "set_shader_input",
            "prim": "/Materials/Mat/PBR",
            "shader_id": "UsdPreviewSurface",
            "inputs": {"roughness": 0.3},
            "input_types": {"roughness": "float"},
        })

        material = UsdShade.Material(stage.GetPrimAtPath("/Materials/Mat"))
        output = material.GetSurfaceOutput()
        sources, _ = output.GetConnectedSources()
        assert len(sources) == 1
        assert str(sources[0].source.GetPath()) == "/Materials/Mat/PBR"

    def test_update_existing_input(self, stage):
        """Updating a shader input changes the value."""
        from pxr import UsdShade

        stage.DefinePrim("/Materials/Mat", "Material")
        apply_event(stage, {
            "k": "set_shader_input",
            "prim": "/Materials/Mat/PBR",
            "shader_id": "UsdPreviewSurface",
            "inputs": {"roughness": 0.3},
            "input_types": {"roughness": "float"},
        })
        apply_event(stage, {
            "k": "set_shader_input",
            "prim": "/Materials/Mat/PBR",
            "shader_id": "UsdPreviewSurface",
            "inputs": {"roughness": 0.9},
            "input_types": {"roughness": "float"},
        })

        shader = UsdShade.Shader(stage.GetPrimAtPath("/Materials/Mat/PBR"))
        assert abs(shader.GetInput("roughness").Get() - 0.9) < 1e-6

    def test_full_material_pipeline(self, stage):
        """Full pipeline: create material + shader + bind to geometry."""
        from pxr import UsdShade

        events = [
            {"k": "ensure_prim", "prim": "/World", "typeName": "Xform"},
            {"k": "ensure_prim", "prim": "/World/Sphere",
             "typeName": "Sphere"},
            {"k": "ensure_prim", "prim": "/Materials",
             "typeName": "Scope"},
            {"k": "ensure_prim", "prim": "/Materials/Red",
             "typeName": "Material"},
            {"k": "set_shader_input",
             "prim": "/Materials/Red/PBR",
             "shader_id": "UsdPreviewSurface",
             "inputs": {
                 "diffuseColor": [1.0, 0.0, 0.0],
                 "roughness": 0.4,
             },
             "input_types": {
                 "diffuseColor": "color3f",
                 "roughness": "float",
             }},
            {"k": "set_material_binding",
             "prim": "/World/Sphere",
             "material_path": "/Materials/Red"},
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
