from pxr import Sdf, Usd, UsdShade

from openusdconnect.shader_connections import resolve_nodegraph_connection


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
