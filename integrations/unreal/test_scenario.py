"""Build the USD fixture used by the Unreal integration harness."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class UnrealScenario:
    base_stage: Path
    expected_initial: Path
    expected_final: Path
    result_path: Path
    database_path: Path
    config_path: Path
    initial_events: tuple[dict, ...]
    update_events: tuple[dict, ...]


def _preview_surface_events(
    texture_color: str,
    texture_roughness: str,
) -> tuple[list[dict], list[dict], list[dict]]:
    baseline = [
        {
            "k": "set_stage_metadata",
            "upAxis": "Y",
            "metersPerUnit": 1.0,
            "timeCodesPerSecond": 24.0,
            "framesPerSecond": 24.0,
            "startTimeCode": 1.0,
            "endTimeCode": 48.0,
        },
        {"k": "ensure_prim", "prim": "/World/Looks", "typeName": "Scope"},
        {"k": "ensure_prim", "prim": "/World/PreviewBall", "typeName": "Sphere"},
        {"k": "set_gprim_attrs", "prim": "/World/PreviewBall", "attrs": {"radius": 1.25}},
        {"k": "ensure_xform_ops", "prim": "/World/PreviewBall"},
        {
            "k": "set_xform_trs",
            "prim": "/World/PreviewBall",
            "fields": ["t", "r", "s"],
            "t": [-2.0, 1.5, 0.5],
            "r": [0.9238795, 0.0, 0.3826834, 0.0],
            "s": [1.0, 1.0, 1.0],
        },
        {"k": "ensure_prim", "prim": "/World/Looks/Preview", "typeName": "Material"},
        {
            "k": "ensure_prim",
            "prim": "/World/Looks/Preview/Surface",
            "typeName": "Shader",
        },
        {
            "k": "set_connectable_input",
            "prim": "/World/Looks/Preview/Surface",
            "info_id": "UsdPreviewSurface",
            "inputs": {
                "diffuseColor": [0.12, 0.48, 0.9],
                "emissiveColor": [0.01, 0.02, 0.04],
                "metallic": 0.2,
                "opacity": 0.95,
                "roughness": 0.38,
            },
            "input_types": {
                "diffuseColor": "color3f",
                "emissiveColor": "color3f",
                "metallic": "float",
                "opacity": "float",
                "roughness": "float",
            },
        },
        {
            "k": "set_connectable_connection",
            "prim": "/World/Looks/Preview",
            "connections": {
                "outputs:surface": {
                    "source_prim": "/World/Looks/Preview/Surface",
                    "source_attr": "outputs:surface",
                }
            },
        },
        {
            "k": "set_material_binding",
            "prim": "/World/PreviewBall",
            "material_path": "/World/Looks/Preview",
        },
        {"k": "ensure_prim", "prim": "/World/TexturedPanel", "typeName": "Mesh"},
        {
            "k": "set_gprim_attrs",
            "prim": "/World/TexturedPanel",
            "attrs": {
                "faceVertexCounts": [4],
                "faceVertexIndices": [0, 1, 2, 3],
                "points": [
                    [-1.0, -1.0, 0.0],
                    [1.0, -1.0, 0.0],
                    [1.0, 1.0, 0.0],
                    [-1.0, 1.0, 0.0],
                ],
                "primvars:st": [
                    [0.0, 0.0],
                    [1.0, 0.0],
                    [1.0, 1.0],
                    [0.0, 1.0],
                ],
                "subdivisionScheme": "none",
            },
            "primvar_meta": {
                "primvars:st": {
                    "typeName": "texCoord2f[]",
                    "interpolation": "faceVarying",
                }
            },
        },
        {"k": "ensure_xform_ops", "prim": "/World/TexturedPanel"},
        {
            "k": "set_xform_trs",
            "prim": "/World/TexturedPanel",
            "fields": ["t"],
            "t": [0.5, 1.5, 0.5],
        },
        {"k": "ensure_prim", "prim": "/World/Looks/Textured", "typeName": "Material"},
        {
            "k": "ensure_prim",
            "prim": "/World/Looks/Textured/Surface",
            "typeName": "Shader",
        },
        {
            "k": "set_connectable_input",
            "prim": "/World/Looks/Textured/Surface",
            "info_id": "UsdPreviewSurface",
            "inputs": {"metallic": 0.15, "roughness": 0.42},
            "input_types": {"metallic": "float", "roughness": "float"},
        },
        {
            "k": "ensure_prim",
            "prim": "/World/Looks/Textured/Texture",
            "typeName": "Shader",
        },
        {
            "k": "set_connectable_input",
            "prim": "/World/Looks/Textured/Texture",
            "info_id": "UsdUVTexture",
            "inputs": {"file": texture_color, "sourceColorSpace": "sRGB"},
            "input_types": {"file": "asset", "sourceColorSpace": "token"},
        },
        {
            "k": "ensure_prim",
            "prim": "/World/Looks/Textured/Primvar",
            "typeName": "Shader",
        },
        {
            "k": "set_connectable_input",
            "prim": "/World/Looks/Textured/Primvar",
            "info_id": "UsdPrimvarReader_float2",
            "inputs": {"varname": "st"},
            "input_types": {"varname": "string"},
        },
        {
            "k": "set_connectable_connection",
            "prim": "/World/Looks/Textured/Texture",
            "connections": {
                "inputs:st": {
                    "source_prim": "/World/Looks/Textured/Primvar",
                    "source_attr": "outputs:result",
                }
            },
        },
        {
            "k": "set_connectable_connection",
            "prim": "/World/Looks/Textured/Surface",
            "connections": {
                "inputs:diffuseColor": {
                    "source_prim": "/World/Looks/Textured/Texture",
                    "source_attr": "outputs:rgb",
                }
            },
        },
        {
            "k": "set_connectable_connection",
            "prim": "/World/Looks/Textured",
            "connections": {
                "outputs:surface": {
                    "source_prim": "/World/Looks/Textured/Surface",
                    "source_attr": "outputs:surface",
                }
            },
        },
        {
            "k": "set_material_binding",
            "prim": "/World/TexturedPanel",
            "material_path": "/World/Looks/Textured",
        },
        {"k": "ensure_prim", "prim": "/World/MaterialXBall", "typeName": "Sphere"},
        {"k": "ensure_xform_ops", "prim": "/World/MaterialXBall"},
        {
            "k": "set_xform_trs",
            "prim": "/World/MaterialXBall",
            "fields": ["t"],
            "t": [3.0, 1.5, 0.5],
        },
        {"k": "ensure_prim", "prim": "/World/Looks/MaterialX", "typeName": "Material"},
        {
            "k": "ensure_prim",
            "prim": "/World/Looks/MaterialX/Surface",
            "typeName": "Shader",
        },
        {
            "k": "set_connectable_input",
            "prim": "/World/Looks/MaterialX/Surface",
            "info_id": "ND_standard_surface_surfaceshader",
            "inputs": {
                "base": 1.0,
                "base_color": [0.72, 0.18, 0.08],
                "metalness": 0.35,
                "specular_roughness": 0.27,
            },
            "input_types": {
                "base": "float",
                "base_color": "color3f",
                "metalness": "float",
                "specular_roughness": "float",
            },
        },
        {
            "k": "set_connectable_connection",
            "prim": "/World/Looks/MaterialX",
            "connections": {
                "outputs:mtlx:surface": {
                    "source_prim": "/World/Looks/MaterialX/Surface",
                    "source_attr": "outputs:out",
                }
            },
        },
        {
            "k": "set_material_binding",
            "prim": "/World/MaterialXBall",
            "material_path": "/World/Looks/MaterialX",
        },
    ]
    initial = [
        {
            "k": "set_connectable_input",
            "prim": "/World/Looks/Preview/Surface",
            "info_id": "UsdPreviewSurface",
            "inputs": {
                "diffuseColor": [0.82, 0.08, 0.2],
                "metallic": 0.7,
                "roughness": 0.16,
            },
            "input_types": {
                "diffuseColor": "color3f",
                "metallic": "float",
                "roughness": "float",
            },
        },
        {
            "k": "set_connectable_input",
            "prim": "/World/Looks/Textured/Texture",
            "info_id": "UsdUVTexture",
            "inputs": {"file": texture_roughness, "sourceColorSpace": "raw"},
            "input_types": {"file": "asset", "sourceColorSpace": "token"},
        },
        {
            "k": "set_connectable_input",
            "prim": "/World/Looks/MaterialX/Surface",
            "info_id": "ND_standard_surface_surfaceshader",
            "inputs": {
                "base_color": [0.1, 0.68, 0.24],
                "specular_roughness": 0.08,
            },
            "input_types": {"base_color": "color3f", "specular_roughness": "float"},
        },
        {
            "k": "set_xform_trs",
            "prim": "/World/PreviewBall",
            "fields": ["t"],
            "t": [-1.5, 2.25, 1.0],
        },
        {"k": "ensure_prim", "prim": "/World/LiveSphere", "typeName": "Sphere"},
        {
            "k": "set_gprim_attrs",
            "prim": "/World/LiveSphere",
            "attrs": {"primvars:displayColor": [[0.9, 0.25, 0.08]], "radius": 0.75},
            "primvar_meta": {
                "primvars:displayColor": {
                    "typeName": "color3f[]",
                    "interpolation": "constant",
                }
            },
        },
        {"k": "ensure_xform_ops", "prim": "/World/LiveSphere"},
        {
            "k": "set_xform_trs",
            "prim": "/World/LiveSphere",
            "fields": ["t"],
            "t": [1.5, 0.75, -1.0],
        },
        {"k": "ensure_prim", "prim": "/World/Looks/Live", "typeName": "Material"},
        {
            "k": "ensure_prim",
            "prim": "/World/Looks/Live/Surface",
            "typeName": "Shader",
        },
        {
            "k": "set_connectable_input",
            "prim": "/World/Looks/Live/Surface",
            "info_id": "UsdPreviewSurface",
            "inputs": {"metallic": 0.05, "roughness": 0.3},
            "input_types": {"metallic": "float", "roughness": "float"},
        },
        {
            "k": "ensure_prim",
            "prim": "/World/Looks/Live/Texture",
            "typeName": "Shader",
        },
        {
            "k": "set_connectable_input",
            "prim": "/World/Looks/Live/Texture",
            "info_id": "UsdUVTexture",
            "inputs": {"file": texture_color, "sourceColorSpace": "sRGB"},
            "input_types": {"file": "asset", "sourceColorSpace": "token"},
        },
        {
            "k": "ensure_prim",
            "prim": "/World/Looks/Live/Primvar",
            "typeName": "Shader",
        },
        {
            "k": "set_connectable_input",
            "prim": "/World/Looks/Live/Primvar",
            "info_id": "UsdPrimvarReader_float2",
            "inputs": {"varname": "st"},
            "input_types": {"varname": "string"},
        },
        {
            "k": "set_connectable_connection",
            "prim": "/World/Looks/Live/Texture",
            "connections": {
                "inputs:st": {
                    "source_prim": "/World/Looks/Live/Primvar",
                    "source_attr": "outputs:result",
                }
            },
        },
        {
            "k": "set_connectable_connection",
            "prim": "/World/Looks/Live/Surface",
            "connections": {
                "inputs:diffuseColor": {
                    "source_prim": "/World/Looks/Live/Texture",
                    "source_attr": "outputs:rgb",
                }
            },
        },
        {
            "k": "set_connectable_connection",
            "prim": "/World/Looks/Live",
            "connections": {
                "outputs:surface": {
                    "source_prim": "/World/Looks/Live/Surface",
                    "source_attr": "outputs:surface",
                }
            },
        },
        {
            "k": "set_material_binding",
            "prim": "/World/LiveSphere",
            "material_path": "/World/Looks/Live",
        },
        {"k": "set_visibility", "prim": "/World/TexturedPanel", "visible": False},
    ]
    updates = [
        {
            "k": "set_connectable_input",
            "prim": "/World/Looks/Preview/Surface",
            "info_id": "UsdPreviewSurface",
            "inputs": {
                "diffuseColor": [0.18, 0.62, 0.95],
                "metallic": 0.45,
                "roughness": 0.24,
            },
            "input_types": {
                "diffuseColor": "color3f",
                "metallic": "float",
                "roughness": "float",
            },
        },
        {
            "k": "set_connectable_input",
            "prim": "/World/Looks/MaterialX/Surface",
            "info_id": "ND_standard_surface_surfaceshader",
            "inputs": {
                "base_color": [0.8, 0.42, 0.12],
                "specular_roughness": 0.18,
            },
            "input_types": {"base_color": "color3f", "specular_roughness": "float"},
        },
        {
            "k": "set_xform_trs",
            "prim": "/World/PreviewBall",
            "fields": ["t"],
            "t": [-1.0, 2.0, 1.25],
        },
        {
            "k": "set_gprim_attrs",
            "prim": "/World/LiveSphere",
            "attrs": {"primvars:displayColor": [[0.12, 0.8, 0.32]]},
            "primvar_meta": {
                "primvars:displayColor": {
                    "typeName": "color3f[]",
                    "interpolation": "constant",
                }
            },
        },
        {
            "k": "set_xform_trs",
            "prim": "/World/LiveSphere",
            "fields": ["t"],
            "t": [2.25, 1.0, -0.5],
        },
        {"k": "set_visibility", "prim": "/World/TexturedPanel", "visible": True},
    ]
    return baseline, initial, updates


def create_scenario(work_dir: Path, *, port: int, python_executable: Path) -> UnrealScenario:
    """Create the source stage, expected layers, and editor-driver configuration."""
    from pxr import Kind, Usd, UsdGeom

    from openusdconnect.codec import encode_message, message_to_dict
    from openusdconnect.event_apply import apply_events
    from openusdconnect.protocol import make_txn

    work_dir.mkdir(parents=True, exist_ok=True)
    for name in ("events.db", "events.db-shm", "events.db-wal", "unreal-result.json"):
        (work_dir / name).unlink(missing_ok=True)
    texture_dir = work_dir / "textures"
    texture_dir.mkdir(exist_ok=True)
    source_texture_dir = REPO_ROOT / "assets" / "test_assets" / "MaterialXTest" / "textures"
    shutil.copy2(source_texture_dir / "brass_color.jpg", texture_dir / "brass_color.jpg")
    shutil.copy2(source_texture_dir / "brass_roughness.jpg", texture_dir / "brass_roughness.jpg")

    base_stage = work_dir / "scene.usda"
    stage = Usd.Stage.CreateNew(str(base_stage))
    stage.SetDefaultPrim(stage.DefinePrim("/World", "Xform"))
    Usd.ModelAPI(stage.GetDefaultPrim()).SetKind(Kind.Tokens.group)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    stage.GetRootLayer().Save()

    baseline, initial, updates = _preview_surface_events(
        "./textures/brass_color.jpg",
        "./textures/brass_roughness.jpg",
    )
    baseline = message_to_dict(encode_message(make_txn(baseline)))["events"]
    initial = message_to_dict(encode_message(make_txn(initial)))["events"]
    updates = message_to_dict(encode_message(make_txn(updates)))["events"]
    apply_events(stage, baseline)
    stage.GetRootLayer().Save()
    expected = Usd.Stage.Open(str(base_stage))
    apply_events(expected, initial)
    expected_initial = work_dir / "expected-initial.usda"
    expected.GetRootLayer().Export(str(expected_initial))
    apply_events(expected, updates)
    expected_final = work_dir / "expected-final.usda"
    expected.GetRootLayer().Export(str(expected_final))

    result_path = work_dir / "unreal-result.json"
    database_path = work_dir / "events.db"
    config_path = work_dir / "unreal-driver.json"
    config_path.write_text(
        json.dumps(
            {
                "base_stage": str(base_stage),
                "database_path": str(database_path),
                "expected_final": str(expected_final),
                "expected_initial": str(expected_initial),
                "initial_events": initial,
                "interactive": False,
                "material_expectations": {
                    "baseline": {
                        "color": [0.12, 0.48, 0.9],
                        "metallic": 0.2,
                        "roughness": 0.38,
                        "texture": "brass_color",
                    },
                    "final": {
                        "color": [0.18, 0.62, 0.95],
                        "metallic": 0.45,
                        "roughness": 0.24,
                        "texture": "brass_roughness",
                    },
                    "initial": {
                        "color": [0.82, 0.08, 0.2],
                        "metallic": 0.7,
                        "roughness": 0.16,
                        "texture": "brass_roughness",
                    },
                },
                "port": port,
                "python_executable": str(python_executable),
                "repo_root": str(REPO_ROOT),
                "result_path": str(result_path),
                "timeout": 120.0,
                "update_events": updates,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return UnrealScenario(
        base_stage=base_stage,
        expected_initial=expected_initial,
        expected_final=expected_final,
        result_path=result_path,
        database_path=database_path,
        config_path=config_path,
        initial_events=tuple(initial),
        update_events=tuple(updates),
    )


__all__ = ["UnrealScenario", "create_scenario"]
