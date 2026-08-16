import pytest
from pxr import Gf, Sdf, Usd, UsdShade

from openusdconnect.shader_connections import (
    flatten_interface_input_connections,
    resolve_nodegraph_connection,
)
from openusdconnect.usd_state import read_usdshade_connectable


def test_resolve_nodegraph_connection_returns_internal_shader_output():
    stage = Usd.Stage.CreateInMemory()
    nodegraph = UsdShade.NodeGraph.Define(stage, "/Mat/NG")
    shader = UsdShade.Shader.Define(stage, "/Mat/NG/Tex")
    shader.CreateIdAttr("UsdUVTexture")

    shader_out = shader.CreateOutput("rgb", Sdf.ValueTypeNames.Color3f)
    nodegraph_out = nodegraph.CreateOutput("base_color", Sdf.ValueTypeNames.Color3f)
    nodegraph_out.ConnectToSource(shader_out)

    assert resolve_nodegraph_connection(
        stage,
        "/Mat/NG",
        "base_color",
    ) == ("/Mat/NG/Tex", "rgb")


def test_resolve_nodegraph_connection_keeps_unresolved_source():
    stage = Usd.Stage.CreateInMemory()
    shader = UsdShade.Shader.Define(stage, "/Mat/PBR")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateOutput("surface", Sdf.ValueTypeNames.Token)

    assert resolve_nodegraph_connection(
        stage,
        "/Mat/PBR",
        "surface",
    ) == ("/Mat/PBR", "surface")


def _read_flattened_shader(stage, path):
    _kind, _shader_id, inputs, input_types, connections = read_usdshade_connectable(
        stage, path
    )
    shader = UsdShade.Shader(stage.GetPrimAtPath(path))
    flatten_interface_input_connections(shader, inputs, input_types, connections)
    return inputs, input_types, connections


def test_material_interface_value_becomes_shader_input():
    stage = Usd.Stage.CreateInMemory()
    material = UsdShade.Material.Define(stage, "/World/Looks/BubbleGum")
    shader = UsdShade.Shader.Define(stage, "/World/Looks/BubbleGum/Surface")
    shader.CreateIdAttr("ND_standard_surface_surfaceshader")

    interface = material.CreateInput("subsurface_color", Sdf.ValueTypeNames.Color3f)
    interface.Set(Gf.Vec3f(1.0, 0.22, 0.493))
    shader.CreateInput("subsurface_color", Sdf.ValueTypeNames.Color3f).ConnectToSource(
        interface
    )

    inputs, input_types, connections = _read_flattened_shader(stage, str(shader.GetPath()))

    assert inputs["subsurface_color"] == pytest.approx([1.0, 0.22, 0.493])
    assert input_types["subsurface_color"] == "color3f"
    assert "inputs:subsurface_color" not in connections


def test_interface_forwarding_to_shader_output_becomes_direct_connection():
    stage = Usd.Stage.CreateInMemory()
    material = UsdShade.Material.Define(stage, "/World/Looks/M")
    texture = UsdShade.Shader.Define(stage, "/World/Looks/M/Texture")
    texture.CreateIdAttr("UsdUVTexture")
    texture_output = texture.CreateOutput("rgb", Sdf.ValueTypeNames.Color3f)
    surface = UsdShade.Shader.Define(stage, "/World/Looks/M/Surface")
    surface.CreateIdAttr("ND_standard_surface_surfaceshader")

    interface = material.CreateInput("base_color", Sdf.ValueTypeNames.Color3f)
    interface.ConnectToSource(texture_output)
    surface.CreateInput("base_color", Sdf.ValueTypeNames.Color3f).ConnectToSource(interface)

    inputs, input_types, connections = _read_flattened_shader(stage, str(surface.GetPath()))

    assert inputs == {}
    assert input_types == {}
    assert connections["inputs:base_color"] == {
        "source_prim": "/World/Looks/M/Texture",
        "source_attr": "outputs:rgb",
    }
