"""Mirror-stage introspection: let Claude read current scene state.

All reads target the in-memory mirror the session keeps in sync with the
server, so they reflect the composed result of the MCP's own writes and every
other connected DCC's.
"""

from __future__ import annotations

from pxr import Usd, UsdGeom, UsdShade

from openusdconnect.emitter import read_usdshade_connectable
from openusdconnect.protocol_constants import STAGE_METADATA_KEYS
from openusdconnect.xform_decompose import as_matrix, decompose_trs_from_matrix

from ._convert import to_jsonable
from .errors import ToolError

_ATTR_SAMPLE = 12  # summarize arrays longer than this in get_prim
_SCHEMA_HINT = (
    "Use a USD schema name, e.g. 'UsdGeomMesh', 'Mesh', 'UsdGeomGprim', "
    "or 'UsdGeomImageable'."
)


def _require_prim(stage: Usd.Stage, path: str) -> Usd.Prim:
    prim = stage.GetPrimAtPath(path)
    if not prim or not prim.IsValid():
        raise ToolError(f"no prim at {path!r} on the stage", code="not_found", field="path")
    return prim


def _resolve_schema_type(is_a: str):
    schema_type = Usd.SchemaRegistry.GetTypeFromName(is_a)
    if not schema_type:
        raise ToolError(
            f"unknown schema type {is_a!r}",
            code="invalid_request",
            field="is_a",
            hint=_SCHEMA_HINT,
        )
    return schema_type


def list_prims(
    stage: Usd.Stage,
    under: str = "/",
    type_name: str = "",
    is_a: str = "",
    max: int = 500,
    offset: int = 0,
    depth: int = 0,
) -> dict:
    """List prims under a path as ``[{path, type, active}]`` (depth-first).

    Filter by exact ``type_name`` and/or schema base ``is_a`` (e.g. 'UsdGeomMesh',
    'UsdGeomGprim', 'UsdGeomImageable'). ``depth`` limits descent under ``under``
    (0 = unlimited, 1 = ``under`` plus its immediate children). Page large scenes
    with ``offset``/``max``; ``next_offset`` is the cursor for the next page, or
    null once the listing is exhausted."""
    root = stage.GetPseudoRoot() if under in ("", "/") else _require_prim(stage, under)
    isa_type = _resolve_schema_type(is_a) if is_a else None
    base = root.GetPath().pathElementCount
    out: list[dict] = []
    total = 0
    it = iter(Usd.PrimRange(root))
    for prim in it:
        if prim.IsPseudoRoot():
            continue
        if depth and (prim.GetPath().pathElementCount - base) >= depth:
            it.PruneChildren()
        if type_name and prim.GetTypeName() != type_name:
            continue
        if isa_type is not None and not prim.IsA(isa_type):
            continue
        if total >= offset and len(out) < max:
            out.append(
                {
                    "path": str(prim.GetPath()),
                    "type": prim.GetTypeName() or "",
                    "active": prim.IsActive(),
                }
            )
        total += 1
    consumed = offset + len(out)
    return {
        "ok": True,
        "count": total,
        "returned": len(out),
        "offset": offset,
        "next_offset": consumed if consumed < total else None,
        "prims": out,
    }


_MAX_CHILDREN = 64  # cap the inline child list in get_prim (child_count is exact)


def get_prim(stage: Usd.Stage, path: str, fields: list | None = None) -> dict:
    """Return a prim's type, schemas, transform, attributes, and bindings.

    ``fields`` selects which optional sections to include (any of: instanceable,
    api_schemas, children, visibility, xform, attributes, variant_selections,
    has_composition_arcs, material_binding); omit for all. ``path``/``type``/
    ``active`` are always returned. Long attribute arrays are summarized and the
    child list is capped (``child_count`` stays exact)."""
    prim = _require_prim(stage, path)
    want = set(fields) if fields else None

    def keep(name: str) -> bool:
        return want is None or name in want

    info: dict = {
        "ok": True,
        "path": str(prim.GetPath()),
        "type": prim.GetTypeName() or "",
        "active": prim.IsActive(),
    }
    if keep("instanceable"):
        info["instanceable"] = prim.IsInstanceable()
    if keep("api_schemas"):
        info["api_schemas"] = list(prim.GetAppliedSchemas())
    if keep("children"):
        kids = [c.GetName() for c in prim.GetChildren()]
        info["child_count"] = len(kids)
        info["children"] = kids[:_MAX_CHILDREN]
        if len(kids) > _MAX_CHILDREN:
            info["children_truncated"] = True

    if keep("visibility"):
        img = UsdGeom.Imageable(prim)
        if img:
            vis = img.GetVisibilityAttr()
            if vis.HasAuthoredValue():
                info["visibility"] = vis.Get()

    if keep("xform"):
        xf = UsdGeom.Xformable(prim)
        if xf and xf.GetOrderedXformOps():
            matrix = as_matrix(xf.GetLocalTransformation())
            t, r, s = decompose_trs_from_matrix(matrix)
            info["xform"] = {"t": t, "r": r, "s": s}

    if keep("attributes"):
        attrs: dict = {}
        for attr in prim.GetAttributes():
            name = attr.GetName()
            if name.startswith("xformOp:") or not attr.IsAuthored() or not attr.HasValue():
                continue
            attrs[name] = to_jsonable(attr.Get(), max_items=_ATTR_SAMPLE)
        if attrs:
            info["attributes"] = attrs

    if keep("variant_selections"):
        vsets = prim.GetVariantSets()
        selections = {n: vsets.GetVariantSet(n).GetVariantSelection() for n in vsets.GetNames()}
        if selections:
            info["variant_selections"] = selections

    if keep("has_composition_arcs") and (prim.HasAuthoredReferences() or prim.HasPayload()):
        info["has_composition_arcs"] = True

    if keep("material_binding"):
        binding = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()[0]
        if binding:
            info["material_binding"] = str(binding.GetPath())

    return info


def get_prims(stage: Usd.Stage, paths: list, fields: list | None = None) -> dict:
    """Batch :func:`get_prim`: read several prims in one call (pairs with
    :func:`select_changes`). A path that does not resolve yields an error entry
    instead of aborting the batch."""
    out: list[dict] = []
    for p in paths:
        try:
            out.append(get_prim(stage, p, fields))
        except ToolError as exc:
            out.append({"path": p, **exc.to_dict()})
    return {"ok": True, "count": len(out), "prims": out}


def get_bounds(stage: Usd.Stage, path: str) -> dict:
    """World-space axis-aligned bounding box of a prim and its subtree as
    ``{min, max, center, size}``.

    Composes the full transform chain (scale, nesting, references) via BBoxCache,
    so relative placement ('put X beside / on top of / aligned with Y') needs no
    geometry fetch and no manual transform math. ``empty`` is True for a prim
    with no boundable geometry."""
    prim = _require_prim(stage, path)
    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        includedPurposes=[UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
    )
    rng = cache.ComputeWorldBound(prim).ComputeAlignedRange()
    if rng.IsEmpty():
        return {"ok": True, "path": str(prim.GetPath()), "empty": True}
    mn, mx = rng.GetMin(), rng.GetMax()
    return {
        "ok": True,
        "path": str(prim.GetPath()),
        "min": [mn[0], mn[1], mn[2]],
        "max": [mx[0], mx[1], mx[2]],
        "center": [(mn[i] + mx[i]) / 2.0 for i in range(3)],
        "size": [mx[i] - mn[i] for i in range(3)],
    }


def get_attributes(
    stage: Usd.Stage, path: str, names: list | None = None, max_items: int = _ATTR_SAMPLE
) -> dict:
    """Read just the attributes you need, without the rest of get_prim's payload.

    With ``names``: returns ``{name: value}`` for those attributes only (arrays
    summarized to ``max_items``; raise it for more, or to fetch a whole array).
    Without ``names``: a lightweight index of authored attributes (name, Sdf
    type, and array length) with no values, for cheap discovery."""
    prim = _require_prim(stage, path)
    if names is None:
        out: list[dict] = []
        for attr in prim.GetAttributes():
            if not attr.IsAuthored():
                continue
            type_name = attr.GetTypeName()
            entry = {"name": attr.GetName(), "type": str(type_name)}
            if type_name.isArray:
                value = attr.Get()
                entry["array"] = True
                entry["len"] = len(value) if value is not None else 0
            out.append(entry)
        return {"ok": True, "path": str(prim.GetPath()), "count": len(out), "attributes": out}
    values: dict = {}
    for name in names:
        attr = prim.GetAttribute(name)
        values[name] = (
            to_jsonable(attr.Get(), max_items=max_items) if attr and attr.HasValue() else None
        )
    return {"ok": True, "path": str(prim.GetPath()), "attributes": values}


def scene_summary(stage: Usd.Stage, under: str = "/") -> dict:
    """One-shot orientation for a large scene: total prim count, active count,
    material count, max depth, and a count-by-type histogram (descending)."""
    root = stage.GetPseudoRoot() if under in ("", "/") else _require_prim(stage, under)
    base = root.GetPath().pathElementCount
    by_type: dict = {}
    total = active = materials = max_depth = 0
    for prim in Usd.PrimRange(root):
        if prim.IsPseudoRoot():
            continue
        total += 1
        by_type[prim.GetTypeName() or "(untyped)"] = (
            by_type.get(prim.GetTypeName() or "(untyped)", 0) + 1
        )
        if prim.IsActive():
            active += 1
        if prim.IsA(UsdShade.Material):
            materials += 1
        d = prim.GetPath().pathElementCount - base
        if d > max_depth:
            max_depth = d
    return {
        "ok": True,
        "under": str(root.GetPath()),
        "total_prims": total,
        "active": active,
        "materials": materials,
        "max_depth": max_depth,
        "by_type": dict(sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def select_changes(dirty: dict, since_seq: int, max: int, last_seq: int) -> dict:
    """Build a changes-since result from a ``{prim_path: seq}`` dirty map: prims
    whose last-change sequence is greater than ``since_seq``, oldest first, capped
    at ``max``. Pure helper so the dirty-map logic is unit-testable."""
    changed = sorted(
        ((p, s) for p, s in dirty.items() if s > since_seq), key=lambda ps: ps[1]
    )
    window = changed[:max]
    return {
        "ok": True,
        "since_seq": since_seq,
        "last_seq": last_seq,
        "count": len(changed),
        "returned": len(window),
        "changes": [{"prim": p, "seq": s} for p, s in window],
    }


def describe_shader_network(stage: Usd.Stage, material_path: str) -> dict:
    """Return the ConnectableAPI topology under a Material prim."""
    mat = _require_prim(stage, material_path)
    shaders: list[dict] = []
    for prim in Usd.PrimRange(mat):
        kind, info_id, inputs, input_types, connections = read_usdshade_connectable(
            stage, prim.GetPath()
        )
        if not kind:
            continue
        shaders.append(
            {
                "path": str(prim.GetPath()),
                "container": kind,
                "info_id": info_id,
                "inputs": {k: to_jsonable(v) for k, v in inputs.items()},
                "input_types": input_types,
                "connections": connections,
            }
        )
    return {"ok": True, "material": str(mat.GetPath()), "shaders": shaders}


def get_stage_metadata(stage: Usd.Stage) -> dict:
    """Return stage-level units and timeline metadata."""
    meta = {
        "timeCodesPerSecond": stage.GetTimeCodesPerSecond(),
        "framesPerSecond": stage.GetFramesPerSecond(),
        "startTimeCode": stage.GetStartTimeCode(),
        "endTimeCode": stage.GetEndTimeCode(),
        "metersPerUnit": UsdGeom.GetStageMetersPerUnit(stage),
        "upAxis": UsdGeom.GetStageUpAxis(stage),
    }
    return {"ok": True, **{k: meta[k] for k in STAGE_METADATA_KEYS}}
