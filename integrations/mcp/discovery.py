"""Sdr-registry shader-node discovery.

Lets MCP clients discover valid shader node ids and their exact input/output
names and Sdf types, for UsdPreviewSurface, UsdUVTexture, and the ~785 MaterialX
``ND_*`` nodes alike, so authored ``info_id`` / ``input_types`` / connection
endpoints match the registry instead of being guessed. Also backs the
``input_types`` validation and the info_id existence warning.
"""

from __future__ import annotations

from pxr import Sdr

from ._convert import to_jsonable
from .errors import ToolError


def _registry():
    return Sdr.Registry()


def _sdf_type_str(prop) -> str:
    sdf_type = prop.GetTypeAsSdfType().GetSdfType()
    return str(sdf_type) if sdf_type else ""


def node_exists(info_id: str) -> bool:
    """True if ``info_id`` resolves to a registered shader node."""
    return bool(info_id) and _registry().GetShaderNodeByIdentifier(info_id) is not None


def resolve_input_type(info_id: str, input_name: str) -> str | None:
    """Return the Sdf type string of a node input, or None if unknown."""
    node = _registry().GetShaderNodeByIdentifier(info_id)
    if node is None:
        return None
    port = node.GetShaderInput(input_name)
    if port is None:
        return None
    return _sdf_type_str(port) or None


def list_shader_nodes(
    filter: str | None = None,
    source_type: str | None = None,
    max: int = 200,
) -> dict:
    """List registered shader node ids, optionally filtered.

    ``source_type`` narrows by render context (e.g. 'mtlx' for MaterialX,
    'glslfx' for UsdPreviewSurface family, 'USD'). ``filter`` is a
    case-insensitive substring match on the id.
    """
    reg = _registry()
    flt = (filter or "").lower()
    matched: list[dict] = []
    total = 0
    for info_id in sorted(reg.GetShaderNodeIdentifiers()):
        node = reg.GetShaderNodeByIdentifier(info_id)
        if node is None:
            continue
        st = node.GetSourceType()
        if source_type and st != source_type:
            continue
        if flt and flt not in info_id.lower():
            continue
        total += 1
        if len(matched) < max:
            matched.append({"info_id": info_id, "source_type": st, "family": node.GetFamily()})
    return {"ok": True, "count": total, "returned": len(matched), "nodes": matched}


def describe_shader_node(info_id: str) -> dict:
    """Return a node's inputs/outputs with Sdf types and input defaults."""
    node = _registry().GetShaderNodeByIdentifier(info_id)
    if node is None:
        raise ToolError(
            f"no shader node {info_id!r} in the Sdr registry",
            code="unknown_node",
            field="info_id",
            hint="Call usd_list_shader_nodes to discover valid ids.",
        )
    inputs = []
    for name in node.GetShaderInputNames():
        port = node.GetShaderInput(name)
        if port is None:
            continue
        entry = {"name": name, "type": _sdf_type_str(port)}
        default = port.GetDefaultValue()
        if default is not None:
            entry["default"] = to_jsonable(default, max_items=16)
        inputs.append(entry)
    outputs = []
    for name in node.GetShaderOutputNames():
        port = node.GetShaderOutput(name)
        if port is not None:
            outputs.append({"name": name, "type": _sdf_type_str(port)})
    return {
        "ok": True,
        "info_id": info_id,
        "source_type": node.GetSourceType(),
        "family": node.GetFamily(),
        "inputs": inputs,
        "outputs": outputs,
    }
