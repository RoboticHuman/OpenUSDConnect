"""Utilities for resolving UsdShade connection topology."""

from __future__ import annotations

from pxr import UsdShade


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
