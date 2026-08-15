"""Interface-input forwarding in the usdview receive path.

Edits that only touch a Material's interface inputs don't dirty the shader
prims Hydra watches; the connection module forwards the edited values onto
the consuming shader inputs after apply so the network re-resolves.
Forwarding is scoped to the inputs each event edited untouched interface
values (e.g. the dozens composed from a referenced .mtlx document) are
never authored onto consumers.
"""

import pytest

pxr = pytest.importorskip("pxr")
from pxr import Gf, Sdf, Usd, UsdShade  # noqa: E402

from integrations.usdview.connection import _forward_interface_edits  # noqa: E402


def _edit(prim: str, **inputs) -> dict:
    return {"k": "set_connectable_input", "prim": prim, "inputs": inputs}


def _material_with_interface(stage):
    material = UsdShade.Material.Define(stage, "/World/Looks/M")
    shader = UsdShade.Shader.Define(stage, "/World/Looks/M/Surface")
    shader.CreateIdAttr("ND_standard_surface_surfaceshader")

    for name in ("base_color", "specular_color"):
        iface = material.CreateInput(name, Sdf.ValueTypeNames.Color3f)
        consumer = shader.CreateInput(name, Sdf.ValueTypeNames.Color3f)
        consumer.ConnectToSource(iface)

    material.CreateSurfaceOutput("mtlx").ConnectToSource(
        shader.CreateOutput("surface", Sdf.ValueTypeNames.Token)
    )
    return material, shader


class TestForwardInterfaceEdits:
    def test_edited_value_lands_on_consumer(self):
        stage = Usd.Stage.CreateInMemory()
        material, shader = _material_with_interface(stage)
        material.GetInput("base_color").Set(Gf.Vec3f(0.9, 0.1, 0.1))

        _forward_interface_edits(stage, [_edit("/World/Looks/M", base_color=[0.9, 0.1, 0.1])])

        authored = shader.GetInput("base_color").GetAttr().Get()
        assert authored == Gf.Vec3f(0.9, 0.1, 0.1)

    def test_untouched_interface_inputs_not_forwarded(self):
        stage = Usd.Stage.CreateInMemory()
        material, shader = _material_with_interface(stage)
        material.GetInput("base_color").Set(Gf.Vec3f(0.9, 0.1, 0.1))
        material.GetInput("specular_color").Set(Gf.Vec3f(1, 1, 1))

        _forward_interface_edits(stage, [_edit("/World/Looks/M", base_color=[0.9, 0.1, 0.1])])

        assert shader.GetInput("specular_color").GetAttr().Get() is None

    def test_unchanged_value_not_reauthored(self):
        stage = Usd.Stage.CreateInMemory()
        material, shader = _material_with_interface(stage)
        material.GetInput("base_color").Set(Gf.Vec3f(0.5, 0.5, 0.5))
        ev = _edit("/World/Looks/M", base_color=[0.5, 0.5, 0.5])
        _forward_interface_edits(stage, [ev])

        layer = stage.GetRootLayer()
        spec_before = layer.GetAttributeAtPath(
            "/World/Looks/M/Surface.inputs:base_color"
        ).default
        _forward_interface_edits(stage, [ev])
        spec_after = layer.GetAttributeAtPath(
            "/World/Looks/M/Surface.inputs:base_color"
        ).default
        assert spec_before == spec_after

    def test_shader_and_other_events_ignored(self):
        stage = Usd.Stage.CreateInMemory()
        _material_with_interface(stage)
        stage.DefinePrim("/World/Ball", "Sphere")

        _forward_interface_edits(stage, [
            _edit("/World/Looks/M/Surface", base_color=[1, 0, 0]),
            _edit("/World/Ball", radius=2.0),
            _edit("/World/Missing", base_color=[1, 0, 0]),
            {"k": "set_visibility", "prim": "/World/Looks/M", "visible": True},
        ])

    def test_unauthored_interface_skipped(self):
        stage = Usd.Stage.CreateInMemory()
        material, shader = _material_with_interface(stage)

        _forward_interface_edits(stage, [_edit("/World/Looks/M", base_color=[1, 0, 0])])

        assert shader.GetInput("base_color").GetAttr().Get() is None
