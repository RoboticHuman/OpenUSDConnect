"""Round-trip tests for the UsdShade -> MaterialX document exporter.

A network authored through the real event pipeline exports to XML, the XML
re-opens through usdMtlx, and the recomposed network carries the same values
and connections — the property consumers of materialized documents rely on.
"""

import os
import tempfile

import pytest

pxr = pytest.importorskip("pxr")
from pxr import Gf, Sdf, Usd, UsdShade  # noqa: E402

from openusdconnect.event_apply import apply_events  # noqa: E402
from openusdconnect.mtlx_export import material_to_mtlx  # noqa: E402

BRASS = [0.56, 0.57, 0.58]
TEXTURE = "D:/textures/brass_color.jpg"

EVENTS = [
    {"k": "ensure_prim", "prim": "/World/Looks/Brushed", "typeName": "Material"},
    {"k": "ensure_prim", "prim": "/World/Looks/Brushed/Surface", "typeName": "Shader"},
    {"k": "ensure_prim", "prim": "/World/Looks/Brushed/Img", "typeName": "Shader"},
    {
        "k": "set_connectable_input",
        "prim": "/World/Looks/Brushed/Surface",
        "info_id": "ND_standard_surface_surfaceshader",
        "inputs": {"base_color": BRASS, "metalness": 1.0, "specular_roughness": 0.35},
        "input_types": {
            "base_color": "color3f",
            "metalness": "float",
            "specular_roughness": "float",
        },
    },
    {
        "k": "set_connectable_input",
        "prim": "/World/Looks/Brushed/Img",
        "info_id": "ND_image_color3",
        "inputs": {"file": TEXTURE},
        "input_types": {"file": "asset"},
    },
    {
        "k": "set_connectable_connection",
        "prim": "/World/Looks/Brushed/Surface",
        "connections": {
            "inputs:base_color": {
                "source_prim": "/World/Looks/Brushed/Img",
                "source_attr": "outputs:out",
            }
        },
    },
    {
        "k": "set_connectable_connection",
        "prim": "/World/Looks/Brushed",
        "connections": {
            "outputs:mtlx:surface": {
                "source_prim": "/World/Looks/Brushed/Surface",
                "source_attr": "outputs:surface",
            }
        },
    },
]


def _authored_stage():
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/World", "Xform")
    apply_events(stage, EVENTS)
    return stage


def _roundtrip_material(doc: str, name: str):
    path = os.path.join(tempfile.mkdtemp(), "export.mtlx")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    stage = Usd.Stage.CreateInMemory()
    prim = stage.DefinePrim(f"/RT/{name}")
    prim.GetReferences().AddReference(
        path.replace("\\", "/"), f"/MaterialX/Materials/{name}"
    )
    return stage, prim


def _resolved(shader: UsdShade.Shader, name: str):
    """Resolve an input like a renderer: through interface forwarding.

    usdMtlx authors document values on the Material's interface and connects
    shader inputs to it, so direct Get() on the shader input returns nothing.
    """
    attrs = shader.GetInput(name).GetValueProducingAttributes()
    return attrs[0].Get() if attrs else None


def _find_shader(stage, name: str) -> UsdShade.Shader:
    for prim in stage.Traverse():
        if prim.GetName() == name and prim.IsA(UsdShade.Shader):
            return UsdShade.Shader(prim)
    raise AssertionError(f"no Shader named {name} composed")


class TestMaterialToMtlx:
    def test_roundtrip_values_and_connections(self):
        doc = material_to_mtlx(_authored_stage(), "/World/Looks/Brushed")
        stage, prim = _roundtrip_material(doc, "Brushed")

        assert prim.IsA(UsdShade.Material)
        # Node names are material-prefixed so documents generated from the
        # same scene never collide in name-keyed consumers.
        surface = _find_shader(stage, "Brushed_Surface")
        assert surface.GetIdAttr().Get() == "ND_standard_surface_surfaceshader"
        assert _resolved(surface, "metalness") == pytest.approx(1.0)
        assert _resolved(surface, "specular_roughness") == pytest.approx(0.35)

        sources, _ = surface.GetInput("base_color").GetConnectedSources()
        assert sources and sources[0].source.GetPrim().GetName() == "Brushed_Img"

        img = _find_shader(stage, "Brushed_Img")
        assert _resolved(img, "file").path == TEXTURE

        mat_out = UsdShade.Material(prim).GetSurfaceOutput("mtlx")
        srcs, _ = mat_out.GetConnectedSources()
        assert srcs and srcs[0].source.GetPrim().GetName() == "Brushed_Surface"

    def test_asset_paths_normalized_to_forward_slashes(self):
        stage = _authored_stage()
        apply_events(
            stage,
            [{
                "k": "set_connectable_input",
                "prim": "/World/Looks/Brushed/Img",
                "info_id": "",
                "inputs": {"file": "D:\\textures\\brass_color.jpg"},
                "input_types": {"file": "asset"},
            }],
        )
        doc = material_to_mtlx(stage, "/World/Looks/Brushed")
        assert 'value="D:/textures/brass_color.jpg"' in doc
        assert "\\" not in doc

    def test_namespaced_inputs_excluded(self):
        stage = _authored_stage()
        surface = UsdShade.Shader(
            stage.GetPrimAtPath("/World/Looks/Brushed/Surface")
        )
        surface.CreateInput("openpbr:base_color", Sdf.ValueTypeNames.Color3f).Set(
            Gf.Vec3f(1, 0, 1)
        )
        doc = material_to_mtlx(stage, "/World/Looks/Brushed")
        assert "openpbr:" not in doc

    def test_material_name_override(self):
        doc = material_to_mtlx(
            _authored_stage(), "/World/Looks/Brushed", material_name="Renamed"
        )
        _stage, prim = _roundtrip_material(doc, "Renamed")
        assert prim.IsA(UsdShade.Material)

    def test_rejects_non_material(self):
        stage = _authored_stage()
        with pytest.raises(ValueError):
            material_to_mtlx(stage, "/World/Looks/Brushed/Surface")

    def test_rejects_unconnected_material(self):
        stage = _authored_stage()
        stage.DefinePrim("/World/Looks/Empty", "Material")
        with pytest.raises(ValueError):
            material_to_mtlx(stage, "/World/Looks/Empty")
