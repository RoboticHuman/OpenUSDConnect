"""Read-only USD inspection and formatting for server query APIs.

Stage readers require the caller to hold its stage lock. Tree formatting
accepts snapshots of the incremental indexes and performs no USD reads.
"""

from __future__ import annotations

from collections.abc import Mapping, Set

from pxr import Usd, UsdGeom

from ..emitter import read_payloads, read_references
from ..usd_state import read_material_binding, read_variant_selections


def _prim_xform_trs(prim) -> dict | None:
    """Composed translate/orient/scale of a prim."""
    xf = UsdGeom.Xformable(prim)
    if not xf:
        return None
    trs: dict = {}
    for op in xf.GetOrderedXformOps():
        name = op.GetAttr().GetName()
        value = op.Get()
        if value is None:
            continue
        if name == "xformOp:translate":
            trs["t"] = [round(float(x), 5) for x in value]
        elif name == "xformOp:orient":
            trs["r"] = [round(float(value.GetReal()), 5)] + [
                round(float(x), 5) for x in value.GetImaginary()
            ]
        elif name == "xformOp:scale":
            trs["s"] = [round(float(x), 5) for x in value]
    return trs or None


def _abbrev_scalar(value) -> str:
    if value is None:
        return ""
    text = str(value)
    return text if len(text) <= 120 else text[:117] + "…"


_PI_ARRAY_ATTRS = (
    "protoIndices",
    "positions",
    "orientations",
    "orientationsf",
    "scales",
    "velocities",
    "accelerations",
    "angularVelocities",
    "ids",
    "invisibleIds",
)


def _point_instancer_summary(prim) -> dict:
    """Bounded read of UsdGeomPointInstancer state for the inspector.

    Returns prototypes targets, instance count (from protoIndices length),
    which arrays are animated, and the size of the inactiveIds prim
    metadata when authored.
    """
    instancer = UsdGeom.PointInstancer(prim)
    proto_rel = instancer.GetPrototypesRel()
    targets = [t.pathString for t in proto_rel.GetTargets()] if proto_rel else []
    proto_indices = instancer.GetProtoIndicesAttr()
    if proto_indices and proto_indices.HasAuthoredValue():
        sample_value = proto_indices.Get()
        instance_count = len(sample_value) if sample_value is not None else 0
    else:
        instance_count = 0
    animated = []
    for name in _PI_ARRAY_ATTRS:
        attr = prim.GetAttribute(name)
        if attr and attr.IsAuthored() and attr.GetNumTimeSamples() > 0:
            animated.append(name)
    inactive_count = None
    if prim.HasAuthoredMetadata("inactiveIds"):
        list_op = prim.GetMetadata("inactiveIds")
        if list_op is not None:
            inactive_count = len(list_op.ApplyOperations([]))
    return {
        "prototypes": targets,
        "instanceCount": instance_count,
        "animatedArrays": animated,
        "inactiveIdCount": inactive_count,
    }


def _authored_attr_rows(prim) -> list[dict]:
    """Authored attributes as ``{name, type, value, numTimeSamples}`` rows.

    Array values are reported by type only, so inspecting a heavy prim does
    not copy its geometry buffers while the caller holds the stage lock.
    """
    rows = []
    for attr in sorted(prim.GetAuthoredAttributes(), key=lambda a: a.GetName()):
        value_type = attr.GetTypeName()
        rows.append(
            {
                "name": attr.GetName(),
                "type": str(value_type),
                "value": "[array]" if value_type.isArray else _abbrev_scalar(attr.Get()),
                "numTimeSamples": attr.GetNumTimeSamples(),
            }
        )
    return rows


def read_prim_types(stage: Usd.Stage) -> dict[str, str]:
    """Read composed prim types for snapshot comparison."""
    return {
        str(prim.GetPath()): prim.GetTypeName()
        for prim in stage.Traverse()
        if str(prim.GetPath()) != "/"
    }


def build_prim_tree(
    prims: Mapping[str, str],
    *,
    instanceable_paths: Set[str],
    point_instancer_paths: Set[str],
) -> list[dict]:
    """Build tree rows from tracked paths and instancing flags without USD reads."""
    child_counts: dict[str, int] = {}
    for path in prims:
        parent = path.rsplit("/", 1)[0] or "/"
        child_counts[parent] = child_counts.get(parent, 0) + 1

    result = []
    for path in sorted(prims):
        parent = path.rsplit("/", 1)[0] or "/"
        result.append(
            {
                "path": path,
                "typeName": prims[path],
                "parent": parent,
                "depth": path.count("/"),
                "has_children": child_counts.get(path, 0) > 0,
                "instanceable": path in instanceable_paths,
                "is_point_instancer": path in point_instancer_paths,
            }
        )
    return result


def read_prim_detail(stage: Usd.Stage, path: str) -> dict:
    """Read composed prim details without materializing geometry arrays."""
    prim = stage.GetPrimAtPath(path)
    if not prim or not prim.IsValid():
        return {"path": path, "exists": False}
    imageable = UsdGeom.Imageable(prim)
    visibility = imageable.GetVisibilityAttr().Get() if imageable else None
    prototype = prim.GetPrototype() if prim.IsInstance() else None
    detail = {
        "path": path,
        "exists": True,
        "typeName": str(prim.GetTypeName()),
        "active": prim.IsActive(),
        "visibility": str(visibility) if visibility is not None else None,
        "apiSchemas": [str(s) for s in prim.GetAppliedSchemas()],
        "xform": _prim_xform_trs(prim),
        "references": [
            asset_path or prim_path for asset_path, prim_path in read_references(stage, path)
        ],
        "payloads": [
            asset_path or prim_path for asset_path, prim_path in read_payloads(stage, path)
        ],
        "variantSelections": dict(read_variant_selections(stage, path)),
        "materialBinding": read_material_binding(stage, path) or None,
        "attributes": _authored_attr_rows(prim),
        "isInstanceable": prim.IsInstanceable(),
        "isInstance": prim.IsInstance(),
        "isInstanceProxy": prim.IsInstanceProxy(),
        "prototype": (prototype.GetPath().pathString if prototype else None),
    }
    if prim.IsA(UsdGeom.PointInstancer):
        detail["pointInstancer"] = _point_instancer_summary(prim)
    return detail


def read_transforms(stage: Usd.Stage) -> list[dict]:
    """Read composed TRS rows for Xformable prims with authored operations."""
    rows = []
    for prim in stage.Traverse():
        trs = _prim_xform_trs(prim)
        if trs:
            rows.append({"path": str(prim.GetPath()), **trs})
    return rows
