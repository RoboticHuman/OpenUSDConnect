"""Supported USD authoring helpers shared by events and integrations."""

from __future__ import annotations

from pxr import Gf, Sdf, Sdr, Usd, UsdShade, Vt

from .connectable_attrs import ConnectableAttr

_TIME_DEFAULT = Usd.TimeCode.Default()


def set_connectable_input_value(
    connectable: UsdShade.ConnectableAPI,
    name: str,
    value,
    type_name: str,
    time: Usd.TimeCode = _TIME_DEFAULT,
) -> None:
    """Set a single input on a UsdShade connectable, creating it if needed.

    Works on Shader, NodeGraph, Material, and UsdLux lights, which all share
    GetInput / CreateInput through ConnectableAPI. ``time`` selects the USD
    time sample; default writes the static opinion. Unknown type names are
    ignored. Existing inputs retain their declared type.
    """
    sdf_type = Sdf.ValueTypeNames.Find(type_name)
    if not sdf_type:
        return
    inp = connectable.GetInput(name)
    if not inp:
        inp = connectable.CreateInput(name, sdf_type)

    if type_name == "asset" and isinstance(value, str):
        # Empty string clears the asset path.
        inp.Set(Sdf.AssetPath(value) if value else Sdf.AssetPath(), time)
        return

    if isinstance(value, list):
        if type_name in ("color3f", "float3", "normal3f", "point3f", "vector3f"):
            inp.Set(Gf.Vec3f(*value), time)
        elif type_name in ("color3d", "double3", "normal3d", "point3d", "vector3d"):
            inp.Set(Gf.Vec3d(*value), time)
        elif type_name in ("float2", "texCoord2f", "double2"):
            inp.Set(Gf.Vec2f(*value) if type_name != "double2" else Gf.Vec2d(*value), time)
        elif type_name in ("float4", "color4f", "double4"):
            inp.Set(Gf.Vec4f(*value) if type_name != "double4" else Gf.Vec4d(*value), time)
        elif type_name == "matrix4d" and len(value) == 16:
            m = Gf.Matrix4d(*value)
            inp.Set(m, time)
        elif type_name == "matrix3d" and len(value) == 9:
            inp.Set(Gf.Matrix3d(*value), time)
        elif type_name == "matrix2d" and len(value) == 4:
            inp.Set(Gf.Matrix2d(*value), time)
        elif type_name == "int[]":
            inp.Set(Vt.IntArray([int(v) for v in value]), time)
        elif type_name == "float[]":
            inp.Set(Vt.FloatArray([float(v) for v in value]), time)
        elif type_name == "token[]":
            inp.Set(Vt.TokenArray([str(v) for v in value]), time)
        elif type_name == "string[]":
            inp.Set(Vt.StringArray([str(v) for v in value]), time)
        else:
            inp.Set(value, time)
    else:
        if type_name == "float":
            inp.Set(float(value), time)
        elif type_name == "int":
            inp.Set(int(value), time)
        else:
            inp.Set(value, time)


def resolve_shader_port_type(prim: Usd.Prim, attr: ConnectableAttr):
    """Resolve a Shader input/output type from its Sdr NodeDef.

    Untyped overrides may carry ``info:id`` without redundantly authoring a
    Shader type opinion. Returns None when the prim, shader identifier,
    registered node, or port cannot be resolved. This lookup does not author
    or modify the prim.
    """
    if not prim:
        return None
    id_attr = prim.GetAttribute("info:id")
    shader_id = id_attr.Get() if id_attr else ""
    if not shader_id:
        return None
    node = Sdr.Registry().GetShaderNodeByIdentifier(shader_id)
    if node is None:
        return None
    port = (
        node.GetShaderInput(attr.base_name)
        if attr.is_input
        else node.GetShaderOutput(attr.base_name)
    )
    if port is None:
        return None
    return port.GetTypeAsSdfType().GetSdfType()
