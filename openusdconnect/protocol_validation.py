"""Dict-level validation for OpenUSDConnect protocol events."""

from __future__ import annotations

import math

from .connectable_attrs import split_qualified_attr
from .protocol_constants import (
    ARC_LIST_POSITIONS,
    EVENT_KEYS,
    K_DEACTIVATE_PRIM,
    K_ENSURE_PRIM,
    K_RENAME_PRIM,
    K_SET_CONNECTABLE_CONNECTION,
    K_SET_CONNECTABLE_INPUT,
    K_SET_GPRIM_ATTRS,
    K_SET_MATERIAL_BINDING,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_SDF_PROPERTY_FIELDS,
    K_SET_VARIANT_SELECTIONS,
    K_SET_VISIBILITY,
    K_SET_XFORM_TRS,
    TRS_FIELDS,
)


def _is_arc_list_valid(
    arcs: list | None,
    *,
    authored: bool,
    explicit: bool,
    references: bool,
) -> bool:
    """Validate a list of composition arc entries (refs or payloads)."""
    if not isinstance(arcs, list):
        return False
    if not authored and (arcs or explicit):
        return False
    for entry in arcs:
        if not isinstance(entry, dict):
            return False
        ap = entry.get("asset_path")
        pp = entry.get("prim_path")
        if ap is None and pp is None:
            return False
        if ap is not None and (not isinstance(ap, str) or not ap):
            return False
        if pp is not None and (not isinstance(pp, str) or not pp.startswith("/")):
            return False
        default_position = "explicit" if explicit else "prepended"
        position = entry.get("list_position", default_position)
        if position not in ARC_LIST_POSITIONS:
            return False
        if explicit != (position == "explicit"):
            return False
        for key, default in (("layer_offset", 0.0), ("layer_scale", 1.0)):
            value = entry.get(key, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
            if not math.isfinite(value):
                return False
        custom_data = entry.get("custom_data_fragment")
        if custom_data is not None:
            if not references or not isinstance(custom_data, str) or not custom_data:
                return False
    return True


def _is_arc_event_valid(ev: dict, key: str, *, references: bool) -> bool:
    arcs = ev.get(key)
    explicit = ev.get("list_op_explicit", False)
    if not isinstance(explicit, bool):
        return False
    authored = ev.get("list_op_authored", bool(arcs) or explicit)
    if not isinstance(authored, bool):
        return False
    return _is_arc_list_valid(
        arcs,
        authored=authored,
        explicit=explicit,
        references=references,
    )


def is_quat_valid(q: list[float]) -> bool:
    """Check that q is a 4-element list of numbers [w, x, y, z]."""
    return isinstance(q, list) and len(q) == 4 and all(isinstance(v, (int, float)) for v in q)


def is_vec3_valid(v: list[float]) -> bool:
    """Check that v is a 3-element list of numbers [x, y, z]."""
    return isinstance(v, list) and len(v) == 3 and all(isinstance(x, (int, float)) for x in v)


def clamp_fields(fields: list[str]) -> list[str]:
    """Filter fields list to only valid TRS field names."""
    return [f for f in fields if f in TRS_FIELDS]


def validate_event(ev: dict) -> bool:
    """Basic validation that an event dict has required fields."""
    k = ev.get("k")
    if k not in EVENT_KEYS:
        return False
    if "prim" not in ev:
        return False
    if k == K_ENSURE_PRIM:
        if not isinstance(ev.get("typeName"), str):
            return False
        api_schemas = ev.get("api_schemas")
        if api_schemas is not None:
            if not isinstance(api_schemas, list):
                return False
            if not all(isinstance(s, str) and s for s in api_schemas):
                return False
    if k == K_SET_XFORM_TRS:
        fields = ev.get("fields", [])
        if not isinstance(fields, list):
            return False
        for f in fields:
            if f not in TRS_FIELDS:
                return False
            if f == "t" and not is_vec3_valid(ev.get("t", [])):
                return False
            if f == "r" and not is_quat_valid(ev.get("r", [])):
                return False
            if f == "s" and not is_vec3_valid(ev.get("s", [])):
                return False
    if k == K_DEACTIVATE_PRIM and not isinstance(ev.get("active"), bool):
        return False
    if k == K_RENAME_PRIM:
        new_name = ev.get("new_name")
        if not isinstance(new_name, str) or not new_name:
            return False
    if k == K_SET_VISIBILITY and not isinstance(ev.get("visible"), bool):
        return False
    if k == K_SET_GPRIM_ATTRS:
        attrs = ev.get("attrs")
        if not isinstance(attrs, dict):
            return False
        if not all(isinstance(key, str) for key in attrs):
            return False
    if k == K_SET_SDF_PROPERTY_FIELDS:
        spec_path = ev.get("spec_path")
        fields = ev.get("fields")
        fragment = ev.get("fragment")
        removed = ev.get("removed")
        if not isinstance(spec_path, str) or not spec_path.startswith(ev["prim"] + "."):
            return False
        if not isinstance(fields, list) or not all(
            isinstance(field, str) and field for field in fields
        ):
            return False
        if not isinstance(fragment, str) or not isinstance(removed, bool):
            return False
        if not removed and (not fields or not fragment):
            return False
    if k == K_SET_REFERENCE and not _is_arc_event_valid(
        ev,
        "refs",
        references=True,
    ):
        return False
    # load_payload and unload_payload require only "prim" (already validated above)
    if k == K_SET_PAYLOAD and not _is_arc_event_valid(
        ev,
        "payloads",
        references=False,
    ):
        return False
    if k == K_SET_VARIANT_SELECTIONS:
        selections = ev.get("selections")
        if not isinstance(selections, dict):
            return False
        if not all(isinstance(key, str) for key in selections):
            return False
        if not all(isinstance(v, str) for v in selections.values()):
            return False
    if k == K_SET_MATERIAL_BINDING:
        material_path = ev.get("material_path")
        if not isinstance(material_path, str):
            return False
        if material_path and not material_path.startswith("/"):
            return False
    if k == K_SET_CONNECTABLE_INPUT:
        # info_id may be empty for container prims that carry interface
        # inputs without an info:id (NodeGraph, Material, UsdLux lights).
        # Apply skips CreateIdAttr when empty.
        info_id = ev.get("info_id")
        if not isinstance(info_id, str):
            return False
        inputs = ev.get("inputs")
        if not isinstance(inputs, dict):
            return False
        if not all(isinstance(key, str) for key in inputs):
            return False
        input_types = ev.get("input_types")
        if not isinstance(input_types, dict):
            return False
    if k == K_SET_CONNECTABLE_CONNECTION:
        connections = ev.get("connections", {})
        if not isinstance(connections, dict):
            return False
        for local_attr, conn in connections.items():
            # local_attr must be namespace-qualified (inputs:<name> or
            # outputs:<name>) so the receiver can dispatch via
            # UsdShade.ConnectableAPI.
            if not split_qualified_attr(local_attr)[0]:
                return False
            if not isinstance(conn, dict):
                return False
            if not isinstance(conn.get("source_prim"), str):
                return False
            source_attr = conn.get("source_attr")
            if not split_qualified_attr(source_attr)[0]:
                return False
        disconnections = ev.get("disconnections", [])
        if not isinstance(disconnections, list):
            return False
        for d in disconnections:
            if not split_qualified_attr(d)[0]:
                return False
    return True
