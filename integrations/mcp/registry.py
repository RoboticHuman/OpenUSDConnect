"""Declarative per-event-kind tool table: the single source of truth.

One :class:`ToolRow` per ``K_*`` event kind. Each row's ``build`` assembles a
single event dict matching the TypedDict in ``openusdconnect.events``. ``tools``
wraps public rows into MCP tools, and the generic ``usd_send_events`` accepts any
kind whose key is in this table. The table keys must stay equal to ``EVENT_KEYS``
(a consistency test enforces it), so adding a new core event kind requires adding
a row here.

To expose a new core event kind: add one row whose ``build`` returns a dict with
the matching ``"k"``. Nothing else in the MCP needs to change.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from openusdconnect.protocol_constants import (
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_SET_CONNECTABLE_CONNECTION,
    K_SET_CONNECTABLE_INPUT,
    K_SET_GPRIM_ATTRS,
    K_SET_INSTANCEABLE,
    K_SET_MATERIAL_BINDING,
    K_SET_PAYLOAD,
    K_SET_POINT_INSTANCER,
    K_SET_REFERENCE,
    K_SET_SDF_SPEC_FIELDS,
    K_SET_STAGE_METADATA,
    K_SET_VARIANT_SELECTIONS,
    K_SET_VISIBILITY,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
    POINT_INSTANCER_FIELDS,
    STAGE_METADATA_KEYS,
)


@dataclass(frozen=True)
class ToolRow:
    """One event kind available to MCP authoring."""

    kind: str
    summary: str
    build: Callable[..., dict]
    expose: bool = True


def euler_to_quat_wxyz(euler_deg: list[float], order: str = "XYZ") -> list[float]:
    """Convert per-axis euler degrees to a quaternion ``[w, x, y, z]``.

    Composes per-axis rotations in the given order under USD's row-vector
    convention (``v' = v * Rx * Ry * Rz`` for order ``"XYZ"``), matching
    ``xformOp:rotateXYZ``. ``euler_deg`` is indexed ``[rx, ry, rz]`` regardless
    of ``order``.
    """
    from pxr import Gf

    if len(euler_deg) != 3:
        raise ValueError("euler_deg must have 3 elements [rx, ry, rz]")
    idx = {"X": 0, "Y": 1, "Z": 2}
    axis = {"X": Gf.Vec3d(1, 0, 0), "Y": Gf.Vec3d(0, 1, 0), "Z": Gf.Vec3d(0, 0, 1)}
    order = order.upper()
    if set(order) - {"X", "Y", "Z"} or len(order) != 3:
        raise ValueError(f"rotate_order must be a permutation of XYZ, got {order!r}")
    m = Gf.Matrix3d(1)
    for ch in order:
        m = m * Gf.Matrix3d(Gf.Rotation(axis[ch], float(euler_deg[idx[ch]])))
    q = m.ExtractRotation().GetQuat()
    im = q.GetImaginary()
    return [q.GetReal(), im[0], im[1], im[2]]


# ---------------------------------------------------------------------------
# Per-kind builders. Each returns one event dict with the matching "k".
# ---------------------------------------------------------------------------


def _ensure_prim(prim: str, type_name: str = "", api_schemas: list[str] | None = None) -> dict:
    ev: dict = {"k": K_ENSURE_PRIM, "prim": prim, "typeName": type_name}
    if api_schemas:
        ev["api_schemas"] = list(api_schemas)
    return ev


def _ensure_xform_ops(prim: str) -> dict:
    return {"k": K_ENSURE_XFORM_OPS, "prim": prim}


def _set_xform_trs(
    prim: str,
    t: list[float] | None = None,
    r: list[float] | None = None,
    s: list[float] | None = None,
    rotate_euler: list[float] | None = None,
    rotate_order: str = "XYZ",
    time: float | None = None,
) -> dict:
    if r is None and rotate_euler is not None:
        r = euler_to_quat_wxyz(rotate_euler, rotate_order)
    fields = [name for name, val in (("t", t), ("r", r), ("s", s)) if val is not None]
    ev: dict = {"k": K_SET_XFORM_TRS, "prim": prim, "fields": fields}
    if t is not None:
        ev["t"] = [float(x) for x in t]
    if r is not None:
        ev["r"] = [float(x) for x in r]
    if s is not None:
        ev["s"] = [float(x) for x in s]
    if time is not None:
        ev["time"] = float(time)
    return ev


def _delete_prim(prim: str) -> dict:
    return {"k": K_DELETE_PRIM, "prim": prim}


def _deactivate_prim(prim: str, active: bool) -> dict:
    return {"k": K_DEACTIVATE_PRIM, "prim": prim, "active": bool(active)}


def _rename_prim(prim: str, new_name: str) -> dict:
    return {"k": K_RENAME_PRIM, "prim": prim, "new_name": new_name}


def _set_visibility(prim: str, visible: bool, time: float | None = None) -> dict:
    ev: dict = {"k": K_SET_VISIBILITY, "prim": prim, "visible": bool(visible)}
    if time is not None:
        ev["time"] = float(time)
    return ev


def _set_gprim_attrs(
    prim: str,
    attrs: dict[str, Any],
    primvar_meta: dict[str, dict] | None = None,
    attr_interp: dict[str, str] | None = None,
    time: float | None = None,
) -> dict:
    ev: dict = {"k": K_SET_GPRIM_ATTRS, "prim": prim, "attrs": attrs}
    if primvar_meta:
        ev["primvar_meta"] = primvar_meta
    if attr_interp:
        ev["attr_interp"] = attr_interp
    if time is not None:
        ev["time"] = float(time)
    return ev


def _set_reference(
    prim: str,
    refs: list[dict],
    list_op_authored: bool | None = None,
    list_op_explicit: bool = False,
) -> dict:
    ev = {"k": K_SET_REFERENCE, "prim": prim, "refs": refs}
    if list_op_authored is not None:
        ev["list_op_authored"] = list_op_authored
    if list_op_explicit:
        ev["list_op_explicit"] = True
    return ev


def _set_payload(
    prim: str,
    payloads: list[dict],
    list_op_authored: bool | None = None,
    list_op_explicit: bool = False,
) -> dict:
    ev = {"k": K_SET_PAYLOAD, "prim": prim, "payloads": payloads}
    if list_op_authored is not None:
        ev["list_op_authored"] = list_op_authored
    if list_op_explicit:
        ev["list_op_explicit"] = True
    return ev


def _load_payload(prim: str) -> dict:
    return {"k": K_LOAD_PAYLOAD, "prim": prim}


def _unload_payload(prim: str) -> dict:
    return {"k": K_UNLOAD_PAYLOAD, "prim": prim}


def _set_variant_selections(prim: str, selections: dict[str, str]) -> dict:
    return {"k": K_SET_VARIANT_SELECTIONS, "prim": prim, "selections": selections}


def _set_material_binding(prim: str, material_path: str, material_purpose: str = "") -> dict:
    ev: dict = {"k": K_SET_MATERIAL_BINDING, "prim": prim, "material_path": material_path}
    if material_purpose:
        ev["material_purpose"] = material_purpose
    return ev


def _set_connectable_input(
    prim: str,
    info_id: str,
    inputs: dict[str, Any],
    input_types: dict[str, str] | None = None,
    time: float | None = None,
) -> dict:
    ev: dict = {
        "k": K_SET_CONNECTABLE_INPUT,
        "prim": prim,
        "info_id": info_id,
        "inputs": inputs,
        "input_types": input_types or {},
    }
    if time is not None:
        ev["time"] = float(time)
    return ev


def _set_connectable_connection(
    prim: str,
    connections: dict[str, dict],
    disconnections: list[str] | None = None,
) -> dict:
    ev: dict = {
        "k": K_SET_CONNECTABLE_CONNECTION,
        "prim": prim,
        "connections": connections,
    }
    if disconnections:
        ev["disconnections"] = list(disconnections)
    return ev


def _set_stage_metadata(
    timeCodesPerSecond: float | None = None,
    framesPerSecond: float | None = None,
    startTimeCode: float | None = None,
    endTimeCode: float | None = None,
    metersPerUnit: float | None = None,
    upAxis: str | None = None,
) -> dict:
    # Stage metadata fires on the pseudo-root and carries no "prim" on the wire
    # (matches the emitter/adapter shape); validation special-cases this kind.
    ev: dict = {"k": K_SET_STAGE_METADATA}
    values = {
        "timeCodesPerSecond": timeCodesPerSecond,
        "framesPerSecond": framesPerSecond,
        "startTimeCode": startTimeCode,
        "endTimeCode": endTimeCode,
        "metersPerUnit": metersPerUnit,
        "upAxis": upAxis,
    }
    for key in STAGE_METADATA_KEYS:
        if values[key] is not None:
            ev[key] = values[key]
    return ev


def _set_instanceable(prim: str, instanceable: bool) -> dict:
    return {"k": K_SET_INSTANCEABLE, "prim": prim, "instanceable": bool(instanceable)}


def _set_point_instancer(
    prim: str,
    prototypes: list[str] | None = None,
    proto_indices: list[int] | None = None,
    positions: list[list[float]] | None = None,
    orientations: list[list[float]] | None = None,
    scales: list[list[float]] | None = None,
    velocities: list[list[float]] | None = None,
    accelerations: list[list[float]] | None = None,
    angular_velocities: list[list[float]] | None = None,
    ids: list[int] | None = None,
    invisible_ids: list[int] | None = None,
    inactive_ids: list[int] | None = None,
    time: float | None = None,
) -> dict:
    provided = {
        "prototypes": prototypes,
        "proto_indices": proto_indices,
        "positions": positions,
        "orientations": orientations,
        "scales": scales,
        "velocities": velocities,
        "accelerations": accelerations,
        "angular_velocities": angular_velocities,
        "ids": ids,
        "invisible_ids": invisible_ids,
        "inactive_ids": inactive_ids,
    }
    ev: dict = {"k": K_SET_POINT_INSTANCER, "prim": prim, "fields": []}
    for name in POINT_INSTANCER_FIELDS:
        if provided[name] is not None:
            ev["fields"].append(name)
            ev[name] = provided[name]
    if time is not None:
        ev["time"] = float(time)
    return ev


def _set_sdf_spec_fields(
    prim: str,
    spec_path: str,
    spec_kind: str,
    fields: list[str],
    fragment: str = "",
    removed: bool = False,
) -> dict:
    return {
        "k": K_SET_SDF_SPEC_FIELDS,
        "prim": prim,
        "spec_path": spec_path,
        "spec_kind": spec_kind,
        "fields": list(fields),
        "fragment": fragment,
        "removed": bool(removed),
    }


TOOL_TABLE: dict[str, ToolRow] = {
    K_ENSURE_PRIM: ToolRow(
        K_ENSURE_PRIM,
        "Idempotently define a prim of a type (e.g. Xform, Mesh, Material, "
        "Shader, Scope, SphereLight, Camera, PointInstancer). api_schemas "
        "applies API schemas (e.g. ['ShapingAPI'], ['CollectionAPI:render']).",
        _ensure_prim,
    ),
    K_ENSURE_XFORM_OPS: ToolRow(
        K_ENSURE_XFORM_OPS,
        "Establish canonical translate/orient/scale xform ops on an Xformable "
        "prim. Call before set_xform_trs on a freshly created prim.",
        _ensure_xform_ops,
    ),
    K_SET_XFORM_TRS: ToolRow(
        K_SET_XFORM_TRS,
        "Set translate (t=[x,y,z]), rotate, and/or scale (s=[x,y,z]). Rotation "
        "is a quaternion r=[w,x,y,z]; or pass rotate_euler=[rx,ry,rz] degrees "
        "(+rotate_order). Optional time selects a USD time sample.",
        _set_xform_trs,
    ),
    K_DELETE_PRIM: ToolRow(K_DELETE_PRIM, "Remove a prim from the stage.", _delete_prim),
    K_DEACTIVATE_PRIM: ToolRow(
        K_DEACTIVATE_PRIM,
        "Toggle prim activation (active=False prunes the subtree from composition).",
        _deactivate_prim,
    ),
    K_RENAME_PRIM: ToolRow(
        K_RENAME_PRIM, "Rename a prim in place (new_name is the leaf name only).", _rename_prim
    ),
    K_SET_VISIBILITY: ToolRow(
        K_SET_VISIBILITY,
        "Set UsdGeom.Imageable visibility. Optional time selects a time sample.",
        _set_visibility,
    ),
    K_SET_GPRIM_ATTRS: ToolRow(
        K_SET_GPRIM_ATTRS,
        "Set typed attributes on a prim: mesh geometry (points, faceVertexCounts, "
        "faceVertexIndices, normals, subdivisionScheme), primvars (primvars:st, "
        "primvars:displayColor), and camera params (focalLength, clippingRange, "
        "projection). primvar_meta gives {name:{typeName,interpolation}} for "
        "primvars; attr_interp gives interpolation for non-primvars.",
        _set_gprim_attrs,
    ),
    K_SET_REFERENCE: ToolRow(
        K_SET_REFERENCE,
        "Replace the current edit target's reference list op. Arc entries accept "
        "asset_path, prim_path, list_position, layer_offset, layer_scale, and "
        "reference custom_data_fragment. Omit prim_path to use the default prim.",
        _set_reference,
    ),
    K_SET_PAYLOAD: ToolRow(
        K_SET_PAYLOAD,
        "Replace the current edit target's payload list op (unloaded by default). "
        "Arc entries accept asset_path, prim_path, list_position, layer_offset, "
        "and layer_scale. Follow with load_payload to materialize.",
        _set_payload,
    ),
    K_LOAD_PAYLOAD: ToolRow(K_LOAD_PAYLOAD, "Load a prim's payload children.", _load_payload),
    K_UNLOAD_PAYLOAD: ToolRow(
        K_UNLOAD_PAYLOAD, "Unload a prim's payload children.", _unload_payload
    ),
    K_SET_VARIANT_SELECTIONS: ToolRow(
        K_SET_VARIANT_SELECTIONS,
        "Set variant selections on a prim, e.g. selections={'size':'large'}.",
        _set_variant_selections,
    ),
    K_SET_MATERIAL_BINDING: ToolRow(
        K_SET_MATERIAL_BINDING,
        "Bind a material to a prim. material_purpose '' (allPurpose), 'preview', "
        "or 'full' selects the per-purpose binding slot.",
        _set_material_binding,
    ),
    K_SET_CONNECTABLE_INPUT: ToolRow(
        K_SET_CONNECTABLE_INPUT,
        "Set input values on a UsdShade connectable (Shader/NodeGraph/Material/"
        "UsdLux light). info_id is the USD info:id (e.g. 'UsdPreviewSurface', "
        "'UsdUVTexture', 'ND_standard_surface_surfaceshader') for shaders, '' for "
        "nodegraphs/materials/lights. input_types maps input name -> Sdf type "
        "(e.g. {'diffuseColor':'color3f','roughness':'float'}); discover exact "
        "names/types with usd_describe_shader_node. To wire a network: ensure_prim "
        "the Material + Shader prims, set_connectable_input on each, then "
        "set_connectable_connection and set_material_binding.",
        _set_connectable_input,
    ),
    K_SET_CONNECTABLE_CONNECTION: ToolRow(
        K_SET_CONNECTABLE_CONNECTION,
        "Author UsdShade connection edges. connections maps a namespace-qualified "
        "local attr (e.g. 'inputs:diffuseColor', or 'outputs:surface' on a "
        "Material) to {source_prim, source_attr} where source_attr is also "
        "qualified (e.g. 'outputs:rgb'). disconnections clears local attrs.",
        _set_connectable_connection,
    ),
    K_SET_STAGE_METADATA: ToolRow(
        K_SET_STAGE_METADATA,
        "Set stage-level metadata: upAxis ('Y'/'Z'), metersPerUnit, "
        "timeCodesPerSecond, framesPerSecond, startTimeCode, endTimeCode. Only "
        "provided fields are written.",
        _set_stage_metadata,
    ),
    K_SET_INSTANCEABLE: ToolRow(
        K_SET_INSTANCEABLE,
        "Set the native scenegraph-instancing flag on a prim that has a "
        "reference/payload arc (composition rebuilds the instance per receiver).",
        _set_instanceable,
    ),
    K_SET_POINT_INSTANCER: ToolRow(
        K_SET_POINT_INSTANCER,
        "Set UsdGeomPointInstancer state: prototypes (relationship paths), "
        "proto_indices, positions [[x,y,z]...], orientations [[w,x,y,z]...], "
        "scales, velocities, ids, etc. Only provided arrays are authored. "
        "Optional time selects a time sample.",
        _set_point_instancer,
    ),
    K_SET_SDF_SPEC_FIELDS: ToolRow(
        K_SET_SDF_SPEC_FIELDS,
        "Apply a low-level Sdf spec delta. The fragment must contain the exact "
        "spec_path; fields lists the opinions to set or clear.",
        _set_sdf_spec_fields,
        expose=False,
    ),
}
