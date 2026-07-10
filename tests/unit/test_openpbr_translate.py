"""Receiver-side OpenPBR -> standard_surface translation (pure UsdShade)."""

from __future__ import annotations

import pytest
from pxr import Gf, Sdf, Sdr, Usd, UsdShade

from integrations.openpbr_translate import (
    OPENPBR_ID,
    STANDARD_SURFACE_ID,
    TRANSLATE_ID,
    translate_openpbr_for_paths,
    translate_openpbr_materials,
)

pytestmark = pytest.mark.skipif(
    Sdr.Registry().GetShaderNodeByIdentifier(TRANSLATE_ID) is None,
    reason="MaterialX OpenPBR->standard_surface translation graph not in this USD build",
)


def _author_openpbr(stage, path="/World/Looks/M"):
    mat = UsdShade.Material.Define(stage, path)
    surf = UsdShade.Shader.Define(stage, path + "/Surface")
    surf.CreateIdAttr(OPENPBR_ID)
    surf.CreateInput("base_color", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(0.45, 0.12, 0.7))
    surf.CreateInput("coat_weight", Sdf.ValueTypeNames.Float).Set(1.0)
    surf.CreateInput("specular_roughness", Sdf.ValueTypeNames.Float).Set(0.35)
    out = surf.CreateOutput("out", Sdf.ValueTypeNames.Token)
    mat.CreateSurfaceOutput("mtlx").ConnectToSource(out)
    return mat, surf


def test_translates_openpbr_to_standard_surface():
    stage = Usd.Stage.CreateInMemory()
    mat, _ = _author_openpbr(stage)

    assert translate_openpbr_materials(stage) == 1

    term = mat.GetSurfaceOutput("mtlx")
    sources, _ = term.GetConnectedSources()
    terminal_shader = UsdShade.Shader(sources[0].source.GetPrim())
    assert terminal_shader.GetIdAttr().Get() == STANDARD_SURFACE_ID

    # standard_surface inputs are driven by the translate node
    base_color = terminal_shader.GetInput("base_color")
    assert base_color.HasConnectedSource()

    xlate = UsdShade.Shader(stage.GetPrimAtPath(mat.GetPath().AppendChild("OpenPBRtoStd")))
    assert xlate.GetIdAttr().Get() == TRANSLATE_ID
    # value inputs are copied from the OpenPBR shader at translate time
    assert tuple(xlate.GetInput("base_color").Get()) == pytest.approx((0.45, 0.12, 0.7))
    assert xlate.GetInput("coat_weight").Get() == pytest.approx(1.0)


def test_value_edit_propagates_on_refresh():
    stage = Usd.Stage.CreateInMemory()
    mat, openpbr = _author_openpbr(stage)
    translate_openpbr_materials(stage)
    xlate = UsdShade.Shader(stage.GetPrimAtPath(mat.GetPath().AppendChild("OpenPBRtoStd")))
    assert tuple(xlate.GetInput("base_color").Get()) == pytest.approx((0.45, 0.12, 0.7))

    # a live edit on the OpenPBR shader is picked up by the next translate pass
    openpbr.GetInput("base_color").Set(Gf.Vec3f(0.1, 0.8, 0.2))
    translate_openpbr_materials(stage)
    assert tuple(xlate.GetInput("base_color").Get()) == pytest.approx((0.1, 0.8, 0.2))


def test_idempotent():
    stage = Usd.Stage.CreateInMemory()
    _author_openpbr(stage)
    assert translate_openpbr_materials(stage) == 1
    assert translate_openpbr_materials(stage) == 0


def test_non_openpbr_material_untouched():
    stage = Usd.Stage.CreateInMemory()
    mat = UsdShade.Material.Define(stage, "/World/Looks/P")
    surf = UsdShade.Shader.Define(stage, "/World/Looks/P/Surface")
    surf.CreateIdAttr("UsdPreviewSurface")
    out = surf.CreateOutput("surface", Sdf.ValueTypeNames.Token)
    mat.CreateSurfaceOutput().ConnectToSource(out)
    assert translate_openpbr_materials(stage) == 0
    assert surf.GetIdAttr().Get() == "UsdPreviewSurface"


def test_scoped_translate_and_refresh_by_path():
    stage = Usd.Stage.CreateInMemory()
    mat, openpbr = _author_openpbr(stage)
    surf_path = str(openpbr.GetPath())
    xlate_path = mat.GetPath().AppendChild("OpenPBRtoStd")

    # scoped translate driven by the changed shader path
    assert translate_openpbr_for_paths(stage, [surf_path]) == 1
    xlate = UsdShade.Shader(stage.GetPrimAtPath(xlate_path))
    assert tuple(xlate.GetInput("base_color").Get()) == pytest.approx((0.45, 0.12, 0.7))

    # an unrelated path touches nothing
    assert translate_openpbr_for_paths(stage, ["/World/SomeBall"]) == 0

    # a value edit on the OpenPBR shader propagates on the next scoped pass
    openpbr.GetInput("base_color").Set(Gf.Vec3f(0.1, 0.8, 0.2))
    translate_openpbr_for_paths(stage, [surf_path])
    assert tuple(xlate.GetInput("base_color").Get()) == pytest.approx((0.1, 0.8, 0.2))


def test_colorspace_carried_to_translate_node():
    stage = Usd.Stage.CreateInMemory()
    mat, openpbr = _author_openpbr(stage)
    openpbr.GetInput("base_color").GetAttr().SetColorSpace("srgb_texture")

    translate_openpbr_materials(stage)

    xi = UsdShade.Shader(
        stage.GetPrimAtPath(mat.GetPath().AppendChild("OpenPBRtoStd"))
    ).GetInput("base_color")
    assert xi.GetAttr().HasColorSpace()
    assert xi.GetAttr().GetColorSpace() == "srgb_texture"
