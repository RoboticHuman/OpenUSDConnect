"""Validation at event admission boundaries.

Validators describe the dictionary contract that must survive FlatBuffers
encoding.  They deliberately use public Sdf APIs for USD names and paths and
avoid walking bulk geometry arrays a second time.
"""

from __future__ import annotations

import math
from numbers import Integral, Real

import numpy as np
from pxr import Sdf

from .connectable_attrs import split_qualified_attr
from .events import get as get_event_spec
from .events import register_validator
from .protocol_constants import (
    ARC_LIST_POSITIONS,
    EVENT_KIND_INFO,
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_REPLACE_SDF_LAYER_CONTENT,
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
    K_SET_SUBLAYERS,
    K_SET_VARIANT_SELECTIONS,
    K_SET_VISIBILITY,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
    POINT_INSTANCER_FIELDS,
    SDF_SPEC_KINDS,
    STAGE_METADATA_KEYS,
    TRS_FIELDS,
    LayerMode,
)


def _fail(message: str) -> None:
    raise ValueError(message)


def _is_finite_number(value) -> bool:
    return (
        not isinstance(value, (bool, np.bool_))
        and isinstance(value, (Real, np.integer, np.floating))
        and math.isfinite(float(value))
    )


def _require_finite(value, field: str) -> None:
    if not _is_finite_number(value):
        _fail(f"{field} must be a finite number")


def _require_optional_time(event: dict) -> None:
    if event.get("time") is not None:
        _require_finite(event["time"], "time")


def _sdf_path(value, field: str) -> Sdf.Path:
    if not isinstance(value, str) or not value or not Sdf.Path.IsValidPathString(value):
        _fail(f"{field} must be a valid Sdf path")
    return Sdf.Path(value)


def _require_prim_path(value, field: str = "prim", *, allow_root: bool = False) -> None:
    path = _sdf_path(value, field)
    if not path.IsAbsolutePath() or not path.IsPrimPath():
        _fail(f"{field} must be an absolute prim path")
    if path == Sdf.Path.absoluteRootPath and not allow_root:
        _fail(f"{field} must identify a prim, not the pseudo-root")
    if path.ContainsPrimVariantSelection():
        _fail(f"{field} cannot contain variant selections")


def _require_identifier(value, field: str, *, namespaced: bool = False) -> None:
    valid = (
        Sdf.Path.IsValidNamespacedIdentifier(value)
        if isinstance(value, str) and namespaced
        else Sdf.Path.IsValidIdentifier(value)
        if isinstance(value, str)
        else False
    )
    if not valid:
        kind = "namespaced identifier" if namespaced else "identifier"
        _fail(f"{field} must be a valid Sdf {kind}")


def _require_bool(event: dict, field: str) -> None:
    if not isinstance(event.get(field), bool):
        _fail(f"{field} must be boolean")


def _require_string(event: dict, field: str, *, empty: bool = True) -> str:
    value = event.get(field)
    if not isinstance(value, str) or (not empty and not value):
        qualifier = "non-empty " if not empty else ""
        _fail(f"{field} must be a {qualifier}string")
    return value


def _require_field_set(event: dict, allowed: set | frozenset | tuple, label: str) -> list[str]:
    fields = event.get("fields")
    if not isinstance(fields, list) or not fields:
        _fail(f"{label} fields must be a non-empty list")
    if len(fields) != len(set(fields)) or any(field not in allowed for field in fields):
        _fail(f"{label} fields contain duplicates or unsupported names")
    return fields


def _require_vector(value, size: int, field: str) -> None:
    if not isinstance(value, (list, tuple)) or len(value) != size:
        _fail(f"{field} must contain {size} numbers")
    if not all(_is_finite_number(component) for component in value):
        _fail(f"{field} must contain finite numbers")


def is_quat_valid(q: list[float]) -> bool:
    """Return whether *q* is a finite quaternion ``[w, x, y, z]``."""
    try:
        _require_vector(q, 4, "quaternion")
    except ValueError:
        return False
    return True


def is_vec3_valid(v: list[float]) -> bool:
    """Return whether *v* is a finite three-component vector."""
    try:
        _require_vector(v, 3, "vector")
    except ValueError:
        return False
    return True


def clamp_fields(fields: list[str]) -> list[str]:
    """Filter a fields list to valid TRS field names."""
    return [field for field in fields if field in TRS_FIELDS]


@register_validator(K_ENSURE_PRIM)
def _validate_ensure_prim(event: dict) -> None:
    _require_string(event, "typeName")
    api_schemas = event.get("api_schemas")
    if api_schemas is not None and (
        not isinstance(api_schemas, list)
        or not all(
            isinstance(name, str) and name and Sdf.Path.IsValidNamespacedIdentifier(name)
            for name in api_schemas
        )
    ):
        _fail("api_schemas must contain valid Sdf schema identifiers")


@register_validator(K_ENSURE_XFORM_OPS)
@register_validator(K_DELETE_PRIM)
@register_validator(K_LOAD_PAYLOAD)
@register_validator(K_UNLOAD_PAYLOAD)
def _validate_prim_only(_event: dict) -> None:
    return


@register_validator(K_SET_XFORM_TRS)
def _validate_xform(event: dict) -> None:
    fields = _require_field_set(event, TRS_FIELDS, "transform")
    expected = set(fields)
    present = {field for field in TRS_FIELDS if field in event}
    if expected != present:
        _fail("transform fields must exactly match the supplied t/r/s values")
    for field in fields:
        _require_vector(event[field], 4 if field == "r" else 3, field)
    _require_optional_time(event)


@register_validator(K_DEACTIVATE_PRIM)
def _validate_active(event: dict) -> None:
    _require_bool(event, "active")


@register_validator(K_RENAME_PRIM)
def _validate_rename(event: dict) -> None:
    _require_identifier(event.get("new_name"), "new_name")


@register_validator(K_SET_VISIBILITY)
def _validate_visibility(event: dict) -> None:
    _require_bool(event, "visible")
    _require_optional_time(event)


def _valid_attr_value(value) -> bool:
    if isinstance(value, (bool, str, Integral, np.integer)):
        return True
    if isinstance(value, (Real, np.floating)):
        return math.isfinite(float(value))
    if isinstance(value, list):
        return True  # the encoder already walks and type-checks list payloads
    if isinstance(value, np.ndarray):
        return value.ndim >= 1 and value.dtype.kind in "biufSU"
    return False


@register_validator(K_SET_GPRIM_ATTRS)
def _validate_gprim_attrs(event: dict) -> None:
    attrs = event.get("attrs")
    if not isinstance(attrs, dict):
        _fail("attrs must be a dictionary")
    for name, value in attrs.items():
        _require_identifier(name, "attribute name", namespaced=True)
        if not _valid_attr_value(value):
            _fail(f"attribute {name!r} has an unsupported value")
    primvar_meta = event.get("primvar_meta", {})
    if not isinstance(primvar_meta, dict):
        _fail("primvar_meta must be a dictionary")
    for name, metadata in primvar_meta.items():
        _require_identifier(name, "primvar name", namespaced=True)
        if not isinstance(metadata, dict):
            _fail(f"primvar metadata for {name!r} must be a dictionary")
        type_name = metadata.get("typeName")
        if not isinstance(type_name, str) or not Sdf.ValueTypeNames.Find(type_name):
            _fail(f"primvar {name!r} has an invalid Sdf type")
        interpolation = metadata.get("interpolation")
        if interpolation is not None and not isinstance(interpolation, str):
            _fail(f"primvar {name!r} interpolation must be a string")
    attr_interp = event.get("attr_interp", {})
    if not isinstance(attr_interp, dict) or not all(
        isinstance(name, str) and isinstance(value, str) for name, value in attr_interp.items()
    ):
        _fail("attr_interp must map attribute names to strings")
    _require_optional_time(event)


def _validate_arc_event(event: dict, key: str, *, references: bool) -> None:
    arcs = event.get(key)
    if not isinstance(arcs, list):
        _fail(f"{key} must be a list")
    explicit = event.get("list_op_explicit", False)
    authored = event.get("list_op_authored", bool(arcs) or explicit)
    if not isinstance(explicit, bool) or not isinstance(authored, bool):
        _fail("arc list-op flags must be boolean")
    if not authored and (arcs or explicit):
        _fail("an unauthored list op cannot contain entries or be explicit")
    for entry in arcs:
        if not isinstance(entry, dict):
            _fail("arc entries must be dictionaries")
        asset_path = entry.get("asset_path")
        prim_path = entry.get("prim_path")
        if asset_path is None and prim_path is None:
            _fail("an arc entry requires an asset_path or prim_path")
        if asset_path is not None and not isinstance(asset_path, str):
            _fail("arc asset_path must be a string")
        if asset_path == "" and prim_path is None:
            _fail("an empty arc asset_path requires an internal prim_path")
        if prim_path is not None:
            _require_prim_path(prim_path, "arc prim_path")
        position = entry.get("list_position", "explicit" if explicit else "prepended")
        if position not in ARC_LIST_POSITIONS or explicit != (position == "explicit"):
            _fail("arc list position does not match the list-op mode")
        for field, default in (("layer_offset", 0.0), ("layer_scale", 1.0)):
            _require_finite(entry.get(field, default), field)
        fragment = entry.get("custom_data_fragment")
        if fragment is not None:
            if not references or not isinstance(fragment, str) or not fragment:
                _fail("only references may carry non-empty custom data")
            from .sdf_arc_state import deserialize_reference_custom_data

            deserialize_reference_custom_data(fragment)


@register_validator(K_SET_REFERENCE)
def _validate_references(event: dict) -> None:
    _validate_arc_event(event, "refs", references=True)


@register_validator(K_SET_PAYLOAD)
def _validate_payloads(event: dict) -> None:
    _validate_arc_event(event, "payloads", references=False)


@register_validator(K_SET_VARIANT_SELECTIONS)
def _validate_variants(event: dict) -> None:
    selections = event.get("selections")
    if not isinstance(selections, dict):
        _fail("selections must be a dictionary")
    for set_name, selection in selections.items():
        if not isinstance(set_name, str) or not isinstance(selection, str):
            _fail("variant set names and selections must be strings")
        if not Sdf.Path.IsValidPathString(f"/Prim{{{set_name}={selection}}}"):
            _fail(f"invalid variant selection {set_name!r}={selection!r}")


@register_validator(K_SET_MATERIAL_BINDING)
def _validate_material_binding(event: dict) -> None:
    material_path = _require_string(event, "material_path")
    if material_path:
        _require_prim_path(material_path, "material_path")
    purpose = event.get("material_purpose")
    if purpose:
        _require_identifier(purpose, "material_purpose", namespaced=True)
    elif purpose is not None and not isinstance(purpose, str):
        _fail("material_purpose must be a string")


def _valid_connectable_value(value) -> bool:
    if isinstance(value, (bool, str, Integral, np.integer)):
        return True
    if isinstance(value, (Real, np.floating)):
        return math.isfinite(float(value))
    if isinstance(value, np.ndarray):
        return value.ndim == 1 and value.dtype.kind in "biufSU"
    if not isinstance(value, list):
        return False
    return all(isinstance(item, str) for item in value) or all(
        _is_finite_number(item) for item in value
    )


@register_validator(K_SET_CONNECTABLE_INPUT)
def _validate_connectable_input(event: dict) -> None:
    _require_string(event, "info_id")
    inputs = event.get("inputs")
    if not isinstance(inputs, dict):
        _fail("inputs must be a dictionary")
    for name, value in inputs.items():
        _require_identifier(name, "input name", namespaced=True)
        if not _valid_connectable_value(value):
            _fail(f"input {name!r} has an unsupported value")
    input_types = event.get("input_types", {})
    if not isinstance(input_types, dict) or not set(input_types) <= set(inputs):
        _fail("input_types must map a subset of inputs")
    for name, type_name in input_types.items():
        if not isinstance(type_name, str) or (type_name and not Sdf.ValueTypeNames.Find(type_name)):
            _fail(f"input {name!r} has an invalid Sdf type")
    _require_optional_time(event)


def _require_connectable_attr(value, field: str) -> None:
    side, base_name = split_qualified_attr(value)
    if not side or not Sdf.Path.IsValidNamespacedIdentifier(base_name):
        _fail(f"{field} must be a qualified inputs:/outputs: attribute")


@register_validator(K_SET_CONNECTABLE_CONNECTION)
def _validate_connectable_connections(event: dict) -> None:
    connections = event.get("connections")
    if not isinstance(connections, dict):
        _fail("connections must be a dictionary")
    for local_attr, connection in connections.items():
        _require_connectable_attr(local_attr, "connection target")
        if not isinstance(connection, dict):
            _fail("connection sources must be dictionaries")
        _require_prim_path(connection.get("source_prim"), "source_prim")
        _require_connectable_attr(connection.get("source_attr"), "source_attr")
    disconnections = event.get("disconnections", [])
    if not isinstance(disconnections, list):
        _fail("disconnections must be a list")
    for local_attr in disconnections:
        _require_connectable_attr(local_attr, "disconnection target")


@register_validator(K_SET_STAGE_METADATA)
def _validate_stage_metadata(event: dict) -> None:
    present = [field for field in STAGE_METADATA_KEYS if field in event]
    if not present:
        _fail("set_stage_metadata must contain at least one metadata field")
    for field in present:
        value = event[field]
        if field == "upAxis":
            if value not in ("Y", "Z"):
                _fail("upAxis must be 'Y' or 'Z'")
            continue
        _require_finite(value, field)
        if field in ("timeCodesPerSecond", "framesPerSecond", "metersPerUnit") and value <= 0:
            _fail(f"{field} must be positive")


@register_validator(K_SET_INSTANCEABLE)
def _validate_instanceable(event: dict) -> None:
    _require_bool(event, "instanceable")


def _require_bulk_array(value, field: str, *, width: int | None, integer: bool) -> None:
    if isinstance(value, np.ndarray):
        expected_ndim = 1 if width is None else 2
        if value.ndim != expected_ndim or (width is not None and value.shape[1] != width):
            _fail(f"{field} has an invalid array shape")
        valid_kind = value.dtype.kind in ("iu" if integer else "iuf")
        if not valid_kind:
            _fail(f"{field} has an invalid array dtype")
        return
    if not isinstance(value, list):
        _fail(f"{field} must be a list or numpy array")
    # Lists are already walked by the encoder, so validate them here. Decoded
    # wire arrays use numpy and take the O(1) shape/dtype path above.
    if width is None:
        valid = all(
            not isinstance(item, bool)
            and isinstance(
                item, (Integral, np.integer) if integer else (Real, np.integer, np.floating)
            )
            and (integer or math.isfinite(float(item)))
            for item in value
        )
    else:
        valid = all(
            isinstance(row, (list, tuple))
            and len(row) == width
            and all(_is_finite_number(item) for item in row)
            for row in value
        )
    if not valid:
        _fail(f"{field} has invalid values")


@register_validator(K_SET_POINT_INSTANCER)
def _validate_point_instancer(event: dict) -> None:
    fields = _require_field_set(event, POINT_INSTANCER_FIELDS, "point-instancer")
    present = {field for field in POINT_INSTANCER_FIELDS if field in event}
    if set(fields) != present:
        _fail("point-instancer fields must exactly match supplied values")
    if event.get("time") is not None and {"prototypes", "inactive_ids"} & present:
        _fail("prototypes and inactive_ids cannot be time-sampled")
    if "prototypes" in present:
        prototypes = event["prototypes"]
        if not isinstance(prototypes, list):
            _fail("prototypes must be a list")
        for path in prototypes:
            _require_prim_path(path, "prototype path")
    for field in ("proto_indices", "ids", "invisible_ids", "inactive_ids"):
        if field in present:
            _require_bulk_array(event[field], field, width=None, integer=True)
    for field, width in (
        ("positions", 3),
        ("orientations", 4),
        ("scales", 3),
        ("velocities", 3),
        ("accelerations", 3),
        ("angular_velocities", 3),
    ):
        if field in present:
            _require_bulk_array(event[field], field, width=width, integer=False)
    _require_optional_time(event)


@register_validator(K_SET_SDF_SPEC_FIELDS)
def _validate_sdf_spec_fields(event: dict) -> None:
    if event.get("spec_kind") not in SDF_SPEC_KINDS:
        _fail("spec_kind is invalid")
    if not isinstance(event.get("spec_path"), str):
        _fail("spec_path must be a string")
    if not isinstance(event.get("fields"), list):
        _fail("fields must be a list")
    if not isinstance(event.get("fragment"), str):
        _fail("fragment must be a string")
    if not isinstance(event.get("removed"), bool):
        _fail("removed must be boolean")
    from .sdf_spec_delta import validate_spec_delta

    validate_spec_delta(event)


@register_validator(K_REPLACE_SDF_LAYER_CONTENT)
def _validate_layer_replacement(event: dict) -> None:
    if event.get("prim") != "/":
        _fail("replace_sdf_layer_content must target the pseudo-root")
    from .sdf_spec_delta import validate_layer_content_replacement

    validate_layer_content_replacement(event)


@register_validator(K_SET_SUBLAYERS)
def _validate_sublayers(event: dict) -> None:
    if event.get("prim") != "/":
        _fail("set_sublayers must target the pseudo-root")
    _require_string(event, "generation", empty=False)
    revision = event.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        _fail("revision must be a non-negative integer")
    sublayers = event.get("sublayers")
    if not isinstance(sublayers, list):
        _fail("sublayers must be a list")
    from .shared_layer_graph import normalize_sublayer_entries

    normalize_sublayer_entries(sublayers)


def validate_event_or_raise(
    event: dict,
    *,
    layer_mode: LayerMode | str | None = None,
) -> None:
    """Validate one event, raising ``ValueError`` at the first contract error."""
    if not isinstance(event, dict):
        _fail("events must be dictionaries")
    kind = event.get("k")
    if not isinstance(kind, str):
        _fail("event kind must be a string")
    info = EVENT_KIND_INFO.get(kind)
    spec = get_event_spec(kind)
    if info is None or spec is None or spec.validate is None:
        _fail(f"unknown or unvalidated event kind {kind!r}")
    if layer_mode is not None and LayerMode(layer_mode) not in info.modes:
        _fail(f"event {kind!r} is unavailable in {LayerMode(layer_mode).value} mode")
    if kind not in (
        K_SET_STAGE_METADATA,
        K_SET_SDF_SPEC_FIELDS,
        K_REPLACE_SDF_LAYER_CONTENT,
        K_SET_SUBLAYERS,
    ):
        _require_prim_path(event.get("prim"))
    try:
        spec.validate(event)
    except (TypeError, ValueError):
        raise
    except Exception as exc:
        raise ValueError(f"invalid {kind} payload: {exc}") from exc


def validate_events(
    events: list[dict],
    *,
    layer_mode: LayerMode | str | None = None,
) -> None:
    """Validate a non-empty transaction before it is encoded or queued."""
    if not isinstance(events, list) or not events:
        _fail("a transaction must contain at least one event")
    for index, event in enumerate(events):
        try:
            validate_event_or_raise(event, layer_mode=layer_mode)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"event {index}: {exc}") from exc


def validate_event(event: dict) -> bool:
    """Compatibility predicate for callers that only need true/false."""
    try:
        validate_event_or_raise(event)
    except (TypeError, ValueError):
        return False
    return True


__all__ = [
    "clamp_fields",
    "is_quat_valid",
    "is_vec3_valid",
    "validate_event",
    "validate_event_or_raise",
    "validate_events",
]
