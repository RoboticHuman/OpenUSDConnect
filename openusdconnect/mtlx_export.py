"""Export a UsdShade material network to a MaterialX document.

Inline shader networks are the live-editable representation on the wire;
some consumers only evaluate MaterialX from a referenced ``.mtlx`` document.
This module materializes a composed network into that form: the returned XML,
written to disk and referenced at ``/MaterialX/Materials/<name>``, composes
back into the equivalent network (round-trip pinned by the unit tests).

Consumers that map generated materials back to USD prims by name require the
referencing Material prim to be named exactly like the material inside the
document; ``material_name`` sets it.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from pxr import Usd, UsdShade

from .emitter import read_usdshade_connectable

# mtlx nodedef identifiers end in the node's output type: ND_<category>_<type>.
_MTLX_TYPE_SUFFIXES = frozenset({
    "surfaceshader", "displacementshader", "volumeshader",
    "float", "color3", "color4", "vector2", "vector3", "vector4",
    "integer", "boolean", "string", "filename", "matrix33", "matrix44",
})

_USD_TO_MTLX_TYPE = {
    "float": "float", "double": "float",
    "color3f": "color3", "color3d": "color3",
    "color4f": "color4",
    "float2": "vector2", "texCoord2f": "vector2",
    "float3": "vector3", "vector3f": "vector3",
    "normal3f": "vector3", "point3f": "vector3",
    "float4": "vector4",
    "int": "integer", "bool": "boolean",
    "string": "string", "token": "string", "asset": "filename",
}


def _split_nodedef(info_id: str) -> tuple[str, str]:
    """``ND_<category>_<outputtype>`` -> (category, mtlx output type)."""
    if not info_id.startswith("ND_"):
        raise ValueError(f"not a MaterialX nodedef identifier: {info_id!r}")
    category, _, suffix = info_id[3:].rpartition("_")
    if not category or suffix not in _MTLX_TYPE_SUFFIXES:
        raise ValueError(f"cannot derive node category/type from {info_id!r}")
    return category, suffix


def _format_value(value, mtlx_type: str) -> str:
    if mtlx_type == "filename":
        return str(value).replace("\\", "/")
    if mtlx_type == "boolean":
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(repr(float(v)) for v in value)
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _surface_shader_path(mat_prim) -> str:
    """The shader prim driving the material's mtlx (or universal) surface output."""
    _kind, _id, _inputs, _types, connections = read_usdshade_connectable(
        mat_prim.GetStage(), str(mat_prim.GetPath())
    )
    for out_name in ("outputs:mtlx:surface", "outputs:surface"):
        conn = connections.get(out_name)
        if conn:
            return conn["source_prim"]
    raise ValueError(f"{mat_prim.GetPath()} has no connected surface output")


def material_to_mtlx(
    stage: Usd.Stage, material_path: str, material_name: str | None = None
) -> str:
    """Serialize the material's shader network as a MaterialX document string.

    ``material_name`` names the ``surfacematerial`` inside the document
    (default: the material prim's name) — reference the result at
    ``/MaterialX/Materials/<material_name>``.
    """
    mat_prim = stage.GetPrimAtPath(material_path)
    if not mat_prim or not mat_prim.IsValid():
        raise ValueError(f"no prim at {material_path}")
    if not mat_prim.IsA(UsdShade.Material):
        raise ValueError(f"{material_path} is not a UsdShade.Material")
    material_name = material_name or mat_prim.GetName()
    surface_path = _surface_shader_path(mat_prim)

    # Node names carry the material name so documents generated from the same
    # scene never collide: name-keyed consumers resolve generated assets
    # across every document on a stage, and shader prims are commonly named
    # identically (e.g. "Surface") in different materials.
    def _node_name(prim) -> str:
        return f"{material_name}_{prim.GetName()}"

    version = "1.38"
    root = ET.Element("materialx")
    for prim in Usd.PrimRange(mat_prim):
        if not prim.IsA(UsdShade.Shader):
            continue
        shader_path = str(prim.GetPath())
        _kind, info_id, inputs, input_types, connections = read_usdshade_connectable(
            stage, shader_path
        )
        category, out_type = _split_nodedef(info_id)
        if category == "open_pbr_surface":
            version = "1.39"

        node = ET.SubElement(root, category)
        node.set("name", _node_name(prim))
        node.set("type", out_type)
        for name, value in inputs.items():
            # Namespaced inputs (consumer-side bookkeeping like preserved
            # openpbr:* originals) are not MaterialX inputs; one in the
            # document makes name-matching consumers reject the whole file.
            if ":" in name:
                continue
            usd_type = input_types.get(name, "")
            mtlx_type = _USD_TO_MTLX_TYPE.get(usd_type)
            if mtlx_type is None:
                raise ValueError(
                    f"unmapped input type {usd_type!r} on {shader_path}.{name}"
                )
            inp = ET.SubElement(node, "input")
            inp.set("name", name)
            inp.set("type", mtlx_type)
            inp.set("value", _format_value(value, mtlx_type))
        for local_attr, conn in connections.items():
            side, _, base = local_attr.partition(":")
            if side != "inputs" or ":" in base:
                continue
            source = stage.GetPrimAtPath(conn["source_prim"])
            if not source or not source.IsA(UsdShade.Shader):
                raise ValueError(
                    f"connection source {conn['source_prim']} is not a Shader"
                )
            src_id = UsdShade.Shader(source).GetIdAttr().Get() or ""
            _src_cat, src_type = _split_nodedef(src_id)
            inp = ET.SubElement(node, "input")
            inp.set("name", base)
            inp.set("type", src_type)
            inp.set("nodename", _node_name(source))

    surface_prim = stage.GetPrimAtPath(surface_path)
    material = ET.SubElement(root, "surfacematerial")
    material.set("name", material_name)
    material.set("type", "material")
    shader_input = ET.SubElement(material, "input")
    shader_input.set("name", "surfaceshader")
    shader_input.set("type", "surfaceshader")
    shader_input.set("nodename", _node_name(surface_prim))

    root.set("version", version)
    ET.indent(root)
    return '<?xml version="1.0"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
