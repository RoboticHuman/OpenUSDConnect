"""Utilities for resolving UsdShade connection topology."""

from __future__ import annotations

from pxr import UsdShade

from .connectable_attrs import input_attr, output_attr, split_qualified_attr
from .usd_state import usd_value_to_python


def flatten_interface_input_connections(
    shader,
    inputs: dict,
    input_types: dict,
    connections: dict,
) -> None:
    """Resolve interface forwarding for consumers without interface nodes.

    MaterialX files commonly author values on Material or NodeGraph inputs and
    connect shader inputs upward to them. OpenUSD resolves those chains to the
    value-producing attribute; this converts each interface edge into either a
    concrete shader value or a direct upstream shader-output connection.
    """
    for shader_input in shader.GetInputs():
        input_name = shader_input.GetBaseName()
        local_attr = input_attr(input_name).qualified_name
        connection = connections.get(local_attr)
        if connection is None:
            continue
        source_side, _source_name = split_qualified_attr(connection["source_attr"])
        if source_side != "input":
            continue

        producers = shader_input.GetValueProducingAttributes()
        if not producers:
            continue
        producer = producers[0]
        producer_side, producer_name = split_qualified_attr(producer.GetName())
        if producer_side == "output":
            connections[local_attr] = {
                "source_prim": str(producer.GetPrim().GetPath()),
                "source_attr": output_attr(producer_name).qualified_name,
            }
            continue

        value = usd_value_to_python(producer.Get())
        if value is None:
            continue
        inputs[input_name] = value
        input_types[input_name] = str(shader_input.GetAttr().GetTypeName())
        connections.pop(local_attr)


def resolve_nodegraph_connection(
    stage,
    source_prim_path: str,
    source_output: str,
) -> tuple[str, str]:
    """Resolve a NodeGraph output to the internal shader output it forwards.

    MaterialX commonly exposes shader networks through NodeGraph interface
    outputs. DCCs that do not have a native USD NodeGraph concept need the
    concrete shader port instead, so this follows one interface edge when the
    source prim is a UsdShade.NodeGraph. If resolution is not possible, the
    original source path and output name are returned unchanged.
    """
    prim = stage.GetPrimAtPath(source_prim_path)
    if prim and prim.IsA(UsdShade.NodeGraph):
        ng_out = UsdShade.NodeGraph(prim).GetOutput(source_output)
        if ng_out:
            srcs, _ = ng_out.GetConnectedSources()
            if srcs:
                return str(srcs[0].source.GetPath()), srcs[0].sourceName
    return source_prim_path, source_output
