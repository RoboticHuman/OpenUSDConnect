"""Stage change detection and event building.

NoticeEmitter watches a Usd.Stage via Usd.Notice.ObjectsChanged,
tracks dirty prims, snapshots TRS transforms, and builds partial-diff
events ready to send over the network.

DCC-agnostic — works on any Usd.Stage regardless of what's authoring to it.
"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Callable, Sequence
from typing import NamedTuple

from pxr import Gf, Sdf, Tf, Usd, UsdGeom, UsdLux, UsdShade

from .asset_paths import transport_asset_identifier, value_contains_asset_path
from .connectable_attrs import (
    USDSHADE_INPUT_PREFIX,
    USDSHADE_OUTPUT_PREFIX,
    ConnectableAttr,
    input_attr,
    output_attr,
)
from .protocol_constants import (
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
    PRIMVAR_PREFIX,
    REL_MATERIAL_BINDING,
    STAGE_METADATA_KEYS,
)
from .sdf_arc_state import read_arc_state
from .sdf_spec_delta import (
    SDF_LAYER_TOPOLOGY_FIELDS,
    SDF_SPEC_KIND_ATTRIBUTE,
    SDF_SPEC_KIND_LAYER,
    SDF_SPEC_KIND_PRIM,
    SDF_SPEC_KIND_RELATIONSHIP,
    SDF_SPEC_KIND_VARIANT,
    SDF_SPEC_KIND_VARIANT_SET,
    event_prim_path,
    fragment_authored_fields,
    serialize_spec_fields,
    spec_kind_for_object,
)
from .xform_decompose import (
    as_matrix,
    decompose_trs_batch,
    decompose_trs_from_matrix,
    xform_sample_value,
)

# Notice field tokens the stage-metadata watcher cares about. Anything outside
# this set on the pseudo-root (comments, customLayerData, etc.) is ignored
# without triggering a snapshot diff.
_WATCHED_STAGE_METADATA_FIELDS = frozenset(STAGE_METADATA_KEYS)

# Wire-event TRS field → xformOp name. Used by the targeted time-sample
# invalidator so it can update the right cache slot without re-reading
# the stage.
_TRS_FIELD_TO_OP_NAME = {
    "t": "xformOp:translate",
    "r": "xformOp:orient",
    "s": "xformOp:scale",
}

# Canonical xform-op type → wire-event TRS field. Non-canonical op types
# (RotateXYZ, Transform, pivots) replicate through the decomposed sample
# path instead, which folds the whole stack via matrix decompose.
_XFORM_OP_TYPE_TO_TRS_FIELD = {
    UsdGeom.XformOp.TypeTranslate: "t",
    UsdGeom.XformOp.TypeOrient: "r",
    UsdGeom.XformOp.TypeScale: "s",
}


def _canonical_trs_field(op) -> str | None:
    """Return the wire field for an unsuffixed, non-inverted canonical op.

    Pivots and inverse pairs share canonical op TYPES with the real
    translate/orient/scale ops, but their semantics differ; emitting their
    samples through the per-op TRS slots would author the wrong transform
    on the receiver. Anything other than the bare canonical attr name
    routes through the decomposed sample path instead.
    """
    field = _XFORM_OP_TYPE_TO_TRS_FIELD.get(op.GetOpType())
    if field is None or op.IsInverseOp():
        return None
    if op.GetAttr().GetName() != _TRS_FIELD_TO_OP_NAME[field]:
        return None
    return field


LOG = logging.getLogger(__name__)

# Per-prim cache keys — use these instead of raw strings to catch typos.
_C_TRS = "trs"
_C_VISIBILITY = "visibility"
_C_REFERENCES = "references"
_C_PAYLOADS = "payloads"
_C_PAYLOAD_LOADED = "payload_loaded"
_C_VARIANT_SELECTIONS = "variant_selections"
_C_GPRIM_ATTRS = "gprim_attrs"
_C_MATERIAL_BINDING = "material_binding"
_C_CONNECTABLE = "connectable"
_C_API_SCHEMAS = "api_schemas"
_C_LOCAL_PRIM_SPECS = "local_prim_specs"
_C_LOCAL_DEFINITION_SPEC = "local_definition_spec"
_C_LOCAL_PROPERTY_STATE = "local_property_state"
_C_LOCAL_PROPERTY_NAMES = "local_property_names"
_C_LOCAL_BLOCKED_VALUE_FIELDS = "local_blocked_value_fields"
_C_CAMERA_ATTRS = "camera_attrs"
_C_INSTANCEABLE = "instanceable_flag"
_C_POINT_INSTANCER = "point_instancer"
# Per-attribute time-sample hashes: {attr_name: {time_float: hash}}
_C_TIME_SAMPLES = "time_samples"

# Cache slots owned by specialized diff paths in _build_dirty_prim_events.
# Channels must not reuse these keys; NoticeEmitter validates collisions at
# construction.
_SPECIALIZED_CACHE_KEYS = frozenset(
    {
        _C_TRS,
        _C_GPRIM_ATTRS,
        _C_API_SCHEMAS,
        _C_LOCAL_PRIM_SPECS,
        _C_LOCAL_DEFINITION_SPEC,
        _C_LOCAL_PROPERTY_STATE,
        _C_LOCAL_PROPERTY_NAMES,
        _C_LOCAL_BLOCKED_VALUE_FIELDS,
        _C_TIME_SAMPLES,
    }
)


class _LocalPrimState(NamedTuple):
    specifier: Sdf.Specifier
    type_name: str


_SDF_DECLARATION_FIELDS = frozenset({"custom", "typeName", "variability"})
_SDF_ATTRIBUTE_VALUE_FIELDS = frozenset({"default", "timeSamples"})
_EMPTY_FIELDS = frozenset()
_SDF_SPECIALIZED_FIELDS = frozenset(
    {
        "default",
        "timeSamples",
        "connectionPaths",
        "targetPaths",
    }
)
_SDF_PROPERTY_KINDS = frozenset({SDF_SPEC_KIND_ATTRIBUTE, SDF_SPEC_KIND_RELATIONSHIP})
_SDF_SPECIALIZED_PRIM_FIELDS = frozenset(
    {
        "active",
        "inactiveIds",
        "instanceable",
        "payload",
        "references",
        "variantSelection",
    }
)
_SDF_STRUCTURAL_NOTICE_FIELDS = frozenset({"primChildren", "propertyChildren", "variantChildren"})
_SDF_SPEC_TYPES = (
    Sdf.PseudoRootSpec,
    Sdf.PrimSpec,
    Sdf.AttributeSpec,
    Sdf.RelationshipSpec,
    Sdf.VariantSetSpec,
    Sdf.VariantSpec,
)
_SDF_CREATE_KIND_ORDER = {
    SDF_SPEC_KIND_LAYER: 0,
    SDF_SPEC_KIND_PRIM: 1,
    SDF_SPEC_KIND_VARIANT_SET: 2,
    SDF_SPEC_KIND_VARIANT: 3,
    SDF_SPEC_KIND_ATTRIBUTE: 4,
    SDF_SPEC_KIND_RELATIONSHIP: 4,
}
_SDF_REMOVE_KIND_ORDER = {
    SDF_SPEC_KIND_ATTRIBUTE: 0,
    SDF_SPEC_KIND_RELATIONSHIP: 0,
    SDF_SPEC_KIND_PRIM: 1,
    SDF_SPEC_KIND_VARIANT: 2,
    SDF_SPEC_KIND_VARIANT_SET: 3,
    SDF_SPEC_KIND_LAYER: 4,
}

_MATERIAL_BINDING_REL_BY_PURPOSE = {
    "": REL_MATERIAL_BINDING,
    "preview": REL_MATERIAL_BINDING + ":preview",
    "full": REL_MATERIAL_BINDING + ":full",
}

_POINT_INSTANCER_PROPERTIES_BY_FIELD = {
    "prototypes": "prototypes",
    "proto_indices": "protoIndices",
    "positions": "positions",
    "orientations": ("orientationsf", "orientations"),
    "scales": "scales",
    "velocities": "velocities",
    "accelerations": "accelerations",
    "angular_velocities": "angularVelocities",
    "ids": "ids",
    "invisible_ids": "invisibleIds",
}


def _is_transform_attr(attr_name: str) -> bool:
    """Return True for attrs that affect UsdGeomXformable transforms."""
    return UsdGeom.Xformable.IsTransformationAffectedByAttrNamed(attr_name)


# ---------------------------------------------------------------------------
# Replicated API schemas — emit-side filter
# ---------------------------------------------------------------------------
#
# Decides which applied API schemas show up in the api_schemas field of
# ensure_prim events. Default ships with UsdLux schemas that any DCC needs
# in the viewport. DCC integrations register their own at import time;
# tests pass an explicit set via the NoticeEmitter constructor.

DEFAULT_REPLICATED_API_SCHEMAS = frozenset({
    "ShapingAPI", "ShadowAPI",          # UsdLux user-applied
    "MeshLightAPI", "VolumeLightAPI",   # UsdLux user-applied (light on Mesh/Volume)
    # UsdHydra: marks a GenerativeProcedural prim for Hydra evaluation; without
    # it a replicated procedural loses its imaging type and never resolves.
    "HydraGenerativeProceduralAPI",
    # NOTE: LightAPI is built-in for typed UsdLux lights — replicating it
    # would add a redundant authored opinion. Excluded by design.
    # NOTE: MaterialBindingAPI is handled via K_SET_MATERIAL_BINDING.
    # NOTE: MotionAPI (motion-blur sampling) is render-time, not viewport —
    # users who need it call register_replicated_api_schema("MotionAPI").
})

_REPLICATED_API_SCHEMAS: set[str] = set(DEFAULT_REPLICATED_API_SCHEMAS)


def _validate_replicated_schema_name(name: str) -> None:
    """Hard-reject ':instance' wire form (whitelisting the bare name matches
    all instances automatically). Soft-reject unregistered names so
    plugin-loaded-later schemas still work; typos get a clear warning.
    """
    if ":" in name:
        raise ValueError(
            f"register_replicated_api_schema expects a bare schema name; "
            f"got {name!r}. Whitelist 'CollectionAPI' to replicate all instances."
        )
    if not Usd.SchemaRegistry.IsAppliedAPISchema(name):
        LOG.warning(
            "Schema %r is not currently a registered applied API schema. "
            "Adding to whitelist anyway (plugin may load later); if it never "
            "resolves, the whitelist entry has no effect.",
            name,
        )


def register_replicated_api_schema(name: str) -> None:
    """Add an API schema to the global replicate-list for new NoticeEmitters.

    DCC integrations call at import time to add their own schemas. Existing
    NoticeEmitters that snapshotted the global already are unaffected — only
    NoticeEmitters constructed AFTER the call pick up the addition.

    Validated against Usd.SchemaRegistry.IsAppliedAPISchema(name).
    """
    _validate_replicated_schema_name(name)
    _REPLICATED_API_SCHEMAS.add(name)


def unregister_replicated_api_schema(name: str) -> None:
    """Remove an API schema from the global replicate-list."""
    _REPLICATED_API_SCHEMAS.discard(name)


def _make_attr_filter(channels):
    """Build a gprim-attr filter from a channel set.

    The returned callable returns True for attrs the gprim scan should track.
    Attrs owned by specialized paths or by any channel's watched_* declarations
    are filtered out, so adding a channel automatically excludes its attrs
    from generic gprim emission.
    """
    skip_attrs = set()
    skip_prefixes = []
    for ch in channels:
        skip_attrs.update(ch.filter_attrs if ch.filter_attrs is not None else ch.watched_attrs)
        skip_prefixes.extend(ch.watched_prefixes)
    skip_attrs_fs = frozenset(skip_attrs)
    skip_prefixes_t = tuple(skip_prefixes)

    def _filter(attr_name: str) -> bool:
        if _is_transform_attr(attr_name):
            return False
        if attr_name in skip_attrs_fs:
            return False
        return not attr_name.startswith(skip_prefixes_t)

    return _filter


def _values_equal(a, b) -> bool:
    """Compare two attribute values, handling numpy arrays."""
    import numpy as np

    if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
        try:
            return np.array_equal(a, b)
        except (TypeError, ValueError):
            return False
    return a == b


def _usd_value_to_python(
    val,
    asset_path_transform: Callable[[str], str] | None = None,
):
    """Convert a USD attribute value to a codec-friendly Python type.

    Handles scalars, GfVec types, and VtArrays (including arrays of vectors).
    VtArrays are converted to numpy arrays (zero-copy when possible) so the
    codec can use CreateNumpyVector for bulk encoding.
    Returns None for unsupported types so the caller can skip them.
    """
    import numpy as np

    if val is None:
        return None
    # Simple scalars
    if isinstance(val, (int, float, bool, str)):
        return val
    if isinstance(val, Sdf.AssetPath):
        if asset_path_transform is not None:
            identifier = val.evaluatedPath or val.authoredPath or val.path
            return asset_path_transform(identifier)
        return val.resolvedPath or val.path
    # GfVec types → list of floats (small, not worth numpy overhead)
    for vec_type in (Gf.Vec2d, Gf.Vec2f, Gf.Vec3d, Gf.Vec3f, Gf.Vec4d, Gf.Vec4f):
        if isinstance(val, vec_type):
            return [float(v) for v in val]
    # Quaternion → [w, x, y, z]; matches the wire format for xformOp:orient.
    for quat_type in (Gf.Quatf, Gf.Quatd, Gf.Quath):
        if isinstance(val, quat_type):
            im = val.GetImaginary()
            return [float(val.GetReal()), float(im[0]), float(im[1]), float(im[2])]
    # GfMatrix types → row-major flat list; the wire reuses FloatArray and
    # the receiver reconstructs from the type_name (matrix4d/matrix4f/etc.).
    for matrix_type in (
        Gf.Matrix2d,
        Gf.Matrix2f,
        Gf.Matrix3d,
        Gf.Matrix3f,
        Gf.Matrix4d,
        Gf.Matrix4f,
    ):
        if isinstance(val, matrix_type):
            return [float(c) for row in val for c in row]
    # VtArray types (Vec3fArray, IntArray, FloatArray, etc.)
    # Detected by type name ending in "Array" — no shared base class in pxr.
    # Convert to numpy directly — pxr VtArrays support the buffer protocol.
    type_name = type(val).__name__
    if isinstance(val, Sdf.AssetPathArray):
        return [
            _usd_value_to_python(elem, asset_path_transform=asset_path_transform) for elem in val
        ]
    if type_name.endswith("Array"):
        try:
            return np.array(val)
        except (TypeError, ValueError):
            # Fallback for exotic array types — iterate element-by-element
            result = []
            for elem in val:
                converted = _usd_value_to_python(
                    elem,
                    asset_path_transform=asset_path_transform,
                )
                if converted is None:
                    return None
                result.append(converted)
            return result
    # Pxr value types that have a Python numeric equivalent
    if type_name in ("Half",):
        return float(val)
    # Numeric coercion only — never fall through to str() which would produce
    # unrecoverable representations like "Vt.Vec3fArray(...)"
    for coerce in (float, int):
        try:
            return coerce(val)
        except (TypeError, ValueError):
            continue
    return None


def _usd_value_to_transport_python(
    stage: Usd.Stage,
    source_layer: Sdf.Layer,
    value,
):
    if not value_contains_asset_path(value):
        return _usd_value_to_python(value)
    expression_variables = stage.GetMetadata("expressionVariables")
    resolver_context = stage.GetPathResolverContext()
    return _usd_value_to_python(
        value,
        asset_path_transform=lambda identifier: transport_asset_identifier(
            source_layer,
            identifier,
            expression_variables=expression_variables,
            resolver_context=resolver_context,
        ),
    )


def _attribute_default_source_layer(attr: Usd.Attribute) -> Sdf.Layer | None:
    for spec in attr.GetPropertyStack():
        if isinstance(spec, Sdf.AttributeSpec) and spec.HasDefaultValue():
            return spec.layer
    return None


# PrimResyncType enum for classifying resync notices. Not available in all
# USD builds; some embedded pxr distributions lag the open-source release.
try:
    _PrimResyncType = Usd.Notice.ObjectsChanged.PrimResyncType
except AttributeError:
    _PrimResyncType = None


def near_list(a: list[float] | None, b: list[float] | None, eps: float) -> bool:
    """Check if two float lists are element-wise within epsilon."""
    if a is None or b is None or len(a) != len(b):
        return False
    return all(abs(float(x) - float(y)) <= eps for x, y in zip(a, b, strict=True))


def _prim_path_from_notice_path(path_str: str) -> str | None:
    """Convert a USD notice path to a prim path.

    Property paths like '/World/Sphere.xformOp:translate' become '/World/Sphere'.
    Prim paths pass through unchanged.
    """
    if not path_str.startswith("/"):
        return None
    if "." in path_str:
        return path_str.split(".", 1)[0]
    return path_str


def _value_hash(val) -> int:
    """Stable per-sample fingerprint. Caller must pass values already converted
    via ``_usd_value_to_python`` — exotic pxr types aren't supported here.
    """
    import numpy as np

    if isinstance(val, np.ndarray):
        return hash((val.shape, val.dtype.str, val.tobytes()))
    if isinstance(val, list):
        return hash(tuple(_value_hash(v) for v in val))
    if isinstance(val, dict):
        return hash(tuple(sorted((k, _value_hash(v)) for k, v in val.items())))
    return hash(val)


def _diff_time_samples(attr, cached: dict[float, int] | None, layer=None, convert=None):
    """Return ``(new_cache, dirty)`` for an attribute's time-sample table.

    ``cached`` is a previous ``{time: value_hash}`` snapshot, or ``None``
    on first encounter (every authored sample is reported dirty).
    ``dirty`` lists ``(time, python_value)`` pairs that were added or
    whose hashed value changed. ``convert`` overrides the value converter
    (default ``_usd_value_to_python``).

    When ``layer`` is given, the times and values come from that layer
    directly via ``Sdf.Layer.ListTimeSamplesForPath`` / ``QueryTimeSample``
    — only samples this layer authored, ignoring opinions on other layers.
    Required for per-client-layer setups where the composed view shadows
    weaker clients' samples and would otherwise leak the stronger
    client's keyframes back to its peers' emitters.
    """
    if not attr or not attr.IsValid():
        return {}, []
    if layer is not None:
        path = attr.GetPath()
        times = sorted(layer.ListTimeSamplesForPath(path))
    else:
        times = attr.GetTimeSamples()
    if not times:
        return {}, []
    if convert is None:
        convert = _usd_value_to_python
    new_cache: dict[float, int] = {}
    dirty: list[tuple[float, object]] = []
    is_first = cached is None
    cached = cached or {}
    for t in times:
        if layer is not None:
            val = convert(layer.QueryTimeSample(path, t))
        else:
            val = convert(attr.Get(Usd.TimeCode(t)))
        if val is None:
            continue
        h = _value_hash(val)
        new_cache[t] = h
        if is_first or cached.get(t) != h:
            dirty.append((t, val))
    return new_cache, dirty


def _has_layer_samples(layer, attr) -> bool:
    """True if *attr* has time samples authored on *layer*.

    Exact check, deliberately not ``Usd.Attribute.ValueMightBeTimeVarying``:
    that is certain-False for a single-sample attr (one sample cannot
    "vary"), yet such an attr has no default opinion and resolves to the
    held sample at every numeric time — it must still emit. The layer
    query is also ~6x cheaper and layer-scoped, matching what
    ``_diff_time_samples`` reads.
    """
    return layer.GetNumTimeSamplesForPath(attr.GetPath()) > 0


def read_stage_metadata(stage: Usd.Stage) -> dict:
    """Snapshot stage-level units + timeline metadata, returning only
    authored opinions (empty dict for a stage with no authored metadata).
    """
    out: dict = {}
    if stage.HasAuthoredMetadata("timeCodesPerSecond"):
        out["timeCodesPerSecond"] = stage.GetTimeCodesPerSecond()
    if stage.HasAuthoredMetadata("framesPerSecond"):
        out["framesPerSecond"] = stage.GetFramesPerSecond()
    if stage.HasAuthoredTimeCodeRange():
        out["startTimeCode"] = stage.GetStartTimeCode()
        out["endTimeCode"] = stage.GetEndTimeCode()
    if stage.HasAuthoredMetadata("metersPerUnit"):
        out["metersPerUnit"] = UsdGeom.GetStageMetersPerUnit(stage)
    if stage.HasAuthoredMetadata("upAxis"):
        out["upAxis"] = str(UsdGeom.GetStageUpAxis(stage))
    return out


def _read_composition_arcs(stage, prim_path, arc_attr):
    """Read composition arcs authored on this stage's own layers.

    Returns a list of (asset_path, prim_path_str) tuples, or empty list.
    Only considers the root and session layers — ignores arcs that come
    from composed-in layers (e.g. internal refs inside referenced assets).

    Args:
        arc_attr: Spec attribute name — "referenceList" or "payloadList".
    """
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return []
    own_layers = {stage.GetRootLayer().identifier, stage.GetSessionLayer().identifier}
    result = []
    for spec in prim.GetPrimStack():
        if spec.layer.identifier not in own_layers:
            continue
        arc_list = getattr(spec, arc_attr)
        for item in (
            *arc_list.prependedItems,
            *arc_list.explicitItems,
            *arc_list.appendedItems,
        ):
            # Anchor authored-relative asset paths to the layer that holds
            # them: receivers compose onto stages with different (often
            # in-memory) anchors. Internal references (empty path) and
            # already-absolute paths pass through unchanged.
            asset_path = transport_asset_identifier(
                spec.layer,
                item.assetPath,
                expression_variables=stage.GetMetadata("expressionVariables"),
                resolver_context=stage.GetPathResolverContext(),
            )
            result.append((asset_path, str(item.primPath)))
    return result


def _edit_target_prim_specs(stage: Usd.Stage, prim_path: str) -> list[Sdf.PrimSpec]:
    """Return active PrimSpecs owned by the edit target at a scene path.

    Variant-authored specs have paths such as ``/World{look=red}Mesh``.
    Stripping variant selections lets those count as local opinions on
    ``/World/Mesh`` while excluding specs reached through references,
    inherits, or specializes whose namespace path differs.
    """
    edit_target = stage.GetEditTarget()
    spec_path = edit_target.MapToSpecPath(Sdf.Path(prim_path))
    if spec_path.isEmpty:
        return []
    namespace_path = spec_path.StripAllVariantSelections()
    layer = edit_target.GetLayer()
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return []
    return [
        spec
        for spec in prim.GetPrimStack()
        if spec.layer == layer and spec.path.StripAllVariantSelections() == namespace_path
    ]


def _edit_target_prim_spec(stage: Usd.Stage, prim_path: str):
    """Return the strongest active edit-target PrimSpec for a scene path."""
    specs = _edit_target_prim_specs(stage, prim_path)
    return specs[0] if specs else None


def _local_prim_state(spec: Sdf.PrimSpec | None) -> _LocalPrimState | None:
    if not spec:
        return None
    return _LocalPrimState(
        specifier=spec.specifier,
        type_name=str(spec.typeName),
    )


def _sdf_path_is_under(path: Sdf.Path, root: Sdf.Path) -> bool:
    if path == root:
        return True
    if root == Sdf.Path.absoluteRootPath:
        return True
    return path.HasPrefix(root)


def _sdf_event_sort_key(event: dict) -> tuple[int, int, int, str]:
    path = Sdf.Path(event["spec_path"])
    depth = len(path.GetPrefixes())
    if event.get("removed", False):
        return (
            0,
            -depth,
            _SDF_REMOVE_KIND_ORDER[event["spec_kind"]],
            event["spec_path"],
        )
    return (
        1,
        depth,
        _SDF_CREATE_KIND_ORDER[event["spec_kind"]],
        event["spec_path"],
    )


def _read_edit_target_arc_state(stage, prim_path, arc_attr):
    """Read one exact list-op opinion from the current edit target."""
    edit_target = stage.GetEditTarget()
    spec_path = edit_target.MapToSpecPath(Sdf.Path(prim_path))
    if spec_path.isEmpty:
        return {
            "entries": [],
            "list_op_authored": False,
            "list_op_explicit": False,
        }
    return read_arc_state(
        edit_target.GetLayer(),
        spec_path,
        arc_attr,
        absolute_asset_paths=True,
        source_stage=stage,
    )


def _read_edit_target_variant_selections(stage, prim_path):
    """Read variant selections authored by the current edit target only."""
    result = {}
    for spec in _edit_target_prim_specs(stage, prim_path):
        if not spec.HasInfo("variantSelection"):
            continue
        for name, value in spec.variantSelections.items():
            result.setdefault(str(name), str(value))
    return result


def read_references(stage, prim_path):
    """Read reference arcs authored on this stage's own layers."""
    return _read_composition_arcs(stage, prim_path, "referenceList")


def read_payloads(stage, prim_path):
    """Read payload arcs authored on this stage's own layers."""
    return _read_composition_arcs(stage, prim_path, "payloadList")


def read_variant_selections(stage, prim_path):
    """Read variant selections on a prim.

    Returns a dict mapping variant set name -> selected variant name,
    or empty dict if no variant sets or no selections.
    """
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return {}
    vsets = prim.GetVariantSets()
    result = {}
    for name in vsets.GetNames():
        sel = vsets.GetVariantSelection(name)
        if sel:
            result[name] = sel
    return result


_MATERIAL_BINDING_PURPOSE_RELS = (
    ("", REL_MATERIAL_BINDING),
    ("preview", REL_MATERIAL_BINDING + ":preview"),
    ("full", REL_MATERIAL_BINDING + ":full"),
)


def read_material_binding(stage, prim_path):
    """Read all authored material binding targets keyed by purpose.

    Returns a dict ``{purpose: target_path}`` over the three USD purposes
    (allPurpose as ``""``, ``"preview"``, ``"full"``). Unauthored slots
    are absent from the dict; an authored-but-empty binding maps to ``""``.
    """
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return {}
    result: dict[str, str] = {}
    for purpose, rel_name in _MATERIAL_BINDING_PURPOSE_RELS:
        rel = prim.GetRelationship(rel_name)
        if not rel or not rel.IsValid() or not rel.IsAuthored():
            continue
        targets = rel.GetTargets()
        result[purpose] = str(targets[0]) if targets else ""
    return result


def _attr_event_metadata(prim, attr_name: str, attr) -> tuple[dict, dict]:
    """Return ``(primvar_meta, attr_interp)`` entries for one attribute.

    Each dict either holds one ``{attr_name: ...}`` entry or is empty.
    Callers merge per-attr results into the wire event's bundled
    ``primvar_meta`` / ``attr_interp`` dicts.
    """
    primvar_meta: dict = {}
    attr_interp: dict = {}
    if attr_name.startswith(PRIMVAR_PREFIX):
        pv = UsdGeom.PrimvarsAPI(prim).GetPrimvar(attr_name[len(PRIMVAR_PREFIX) :])
        if pv:
            meta: dict = {"typeName": str(attr.GetTypeName())}
            if pv.HasAuthoredInterpolation():
                meta["interpolation"] = str(pv.GetInterpolation())
            primvar_meta[attr_name] = meta
    else:
        interp = attr.GetMetadata("interpolation")
        if interp:
            attr_interp[attr_name] = str(interp)
    return primvar_meta, attr_interp


def _connectable_kind(prim) -> str:
    """Return the wire-protocol ``container_kind`` label for ``prim``.

    Three buckets, checked in priority order:

      - ``"shader"`` — prim ``IsA`` ``UsdShade.Shader`` (typed schema).
      - ``"nodegraph"`` — prim ``IsA`` ``UsdShade.NodeGraph``. ``UsdShade.Material``
        derives from ``NodeGraph``, so Materials land here too.
      - ``"light"`` — prim ``HasAPI`` ``UsdLux.LightAPI``. Typed UsdLux lights
        (``SphereLight``, ``RectLight``, ``DomeLight``, …) carry it built-in;
        ``MeshLightAPI`` / ``VolumeLightAPI`` pull a ``Mesh``/``Volume`` prim
        into this bucket too.

    Returns ``""`` for prims that don't match any of the three checks.
    Other USD prims can still expose ``UsdShade.ConnectableAPI`` (anything
    with a registered ``UsdShadeConnectableAPIBehavior``) — those aren't
    replicated and intentionally return ``""``.
    """
    if not prim or not prim.IsValid():
        return ""
    if prim.IsA(UsdShade.Shader):
        return "shader"
    if prim.IsA(UsdShade.NodeGraph):
        return "nodegraph"
    if prim.HasAPI(UsdLux.LightAPI):
        return "light"
    return ""


def _connected_source_attr(src) -> ConnectableAttr:
    """Return the protocol attribute reference for a UsdShade connection source.

    Uses `sourceType` to choose `inputs:` vs `outputs:` so we cover the
    NodeGraph interface-forwarding case where the source is itself an
    input.
    """
    if src.sourceType == UsdShade.AttributeType.Output:
        return output_attr(src.sourceName)
    return input_attr(src.sourceName)


def read_usdshade_connectable(stage, prim_path):
    """Read interface data from a UsdShade.ConnectableAPI-bearing prim.

    Polymorphic over Shader, NodeGraph, Material (Material inherits
    NodeGraph in UsdShade), and UsdLux lights (LightAPI is a UsdShade
    connectable container).  Reads authored input values, their USD
    types, and any authored connections — on inputs AND outputs.

    Returns (container_kind, info_id, inputs, input_types, connections):
      - container_kind: "" if the prim doesn't bear an interface, otherwise
        "shader", "nodegraph" (covers Material), or "light".
      - info_id: info:id for Shader prims, "" for NodeGraph/Material/Light
        which carry no info:id by design.
      - inputs/input_types: keyed by the input's base name (no namespace
        prefix), since these are direct values, not connection edges.
      - connections: keyed by namespace-qualified local attribute name
        ("inputs:foo" or "outputs:bar") and valued by
        {"source_prim", "source_attr"} where source_attr is similarly
        qualified.  Mirrors USD's .connect authoring shape.  Inputs that
        have a connection are excluded from `inputs` since their value
        comes from the source, not direct authoring.

    Callers gate on `container_kind` to distinguish "no interface here"
    from "interface present but nothing authored yet".
    """
    prim = stage.GetPrimAtPath(prim_path)
    container_kind = _connectable_kind(prim)
    if not container_kind:
        return "", "", {}, {}, {}

    if container_kind == "shader":
        shader = UsdShade.Shader(prim)
        info_id = shader.GetIdAttr().Get() or ""
        if not info_id:
            return "", "", {}, {}, {}
        connectable = shader
    else:
        # NodeGraph (covers Material) and Light containers share
        # ConnectableAPI for input/output enumeration.
        info_id = ""
        connectable = UsdShade.ConnectableAPI(prim)

    inputs = {}
    input_types = {}
    connections = {}

    for inp in connectable.GetInputs():
        if not inp.GetAttr().IsAuthored():
            continue
        name = inp.GetBaseName()
        sources, _ = inp.GetConnectedSources()
        if sources:
            connections[input_attr(name).qualified_name] = {
                "source_prim": str(sources[0].source.GetPath()),
                "source_attr": _connected_source_attr(sources[0]).qualified_name,
            }
            continue
        val = _usd_value_to_python(inp.Get())
        if val is not None:
            inputs[name] = val
            input_types[name] = str(inp.GetAttr().GetTypeName())

    # Output-side authored connections: NodeGraph/Material output ports
    # that bubble internal shader values up to consumers outside.  Shaders
    # generally don't author connections on their outputs, but the check
    # is uniform so we read them either way.
    for outp in connectable.GetOutputs():
        if not outp.GetAttr().HasAuthoredConnections():
            continue
        sources, _ = outp.GetConnectedSources()
        if not sources:
            continue
        connections[output_attr(outp.GetBaseName()).qualified_name] = {
            "source_prim": str(sources[0].source.GetPath()),
            "source_attr": _connected_source_attr(sources[0]).qualified_name,
        }

    return container_kind, info_id, inputs, input_types, connections


def _connection_from_local_spec(stage: Usd.Stage, spec: Sdf.AttributeSpec):
    list_op = spec.GetInfo("connectionPaths")
    paths = list(list_op.ApplyOperations([]) or ())
    if not paths:
        return None
    if not list_op.isExplicit or len(paths) > 1:
        LOG.debug(
            "Projecting the first source of UsdShade connection %s through the "
            "specialized event; its Sdf field delta carries the complete list op",
            spec.path,
        )

    path = paths[0]
    if not path.IsAbsolutePath():
        path = path.MakeAbsolutePath(spec.path.GetPrimPath())
    path = stage.GetEditTarget().GetMapFunction().MapSourceToTarget(path)
    if path.isEmpty:
        LOG.debug(
            "UsdShade connection %s has no specialized stage-path projection",
            spec.path,
        )
        return None
    path = path.StripAllVariantSelections()
    source_attr = ConnectableAttr.from_qualified_name(str(path.name))
    if source_attr is None:
        LOG.debug(
            "UsdShade connection source %s has no specialized representation",
            path,
        )
        return None
    return {
        "source_prim": str(path.GetPrimPath()),
        "source_attr": source_attr.qualified_name,
    }


def _connection_spec_needs_sdf(spec: Sdf.AttributeSpec) -> bool:
    list_op = spec.GetInfo("connectionPaths")
    paths = list(list_op.ApplyOperations([]) or ())
    if not list_op.isExplicit or len(paths) > 1:
        return True
    return bool(paths and ConnectableAttr.from_qualified_name(str(paths[0].name)) is None)


def _property_spec_is_untyped_over(spec: Sdf.PropertySpec) -> bool:
    owner = spec.owner
    if isinstance(owner, Sdf.VariantSpec):
        owner = owner.primSpec
    return owner.specifier == Sdf.SpecifierOver and not owner.typeName


def _read_edit_target_usdshade_connectable(
    stage: Usd.Stage,
    prim_path: str,
    property_sources: dict[str, dict[str, Sdf.PropertySpec]],
):
    """Read UsdShade opinions owned by the current edit-target layer."""
    prim = stage.GetPrimAtPath(prim_path)
    container_kind = _connectable_kind(prim)
    if not container_kind:
        return "", "", {}, {}, {}

    if container_kind == "shader":
        shader = UsdShade.Shader(prim)
        composed_info_id = shader.GetIdAttr().Get() or ""
        info_id = ""
        local_id = property_sources.get("info:id", {}).get("default")
        if local_id is not None and local_id.HasDefaultValue():
            value = local_id.default
            if not isinstance(value, Sdf.ValueBlock):
                info_id = str(value)
        if not composed_info_id and not info_id:
            return "", "", {}, {}, {}
    else:
        info_id = ""

    inputs: dict = {}
    input_types: dict = {}
    connections: dict = {}
    for qualified_name, field_sources in property_sources.items():
        port = ConnectableAttr.from_qualified_name(qualified_name)
        if port is None:
            continue

        connection_source = field_sources.get("connectionPaths")
        if isinstance(connection_source, Sdf.AttributeSpec):
            connections[qualified_name] = _connection_from_local_spec(
                stage,
                connection_source,
            )

        if not port.is_input:
            continue
        value_source = field_sources.get("default")
        if not isinstance(value_source, Sdf.AttributeSpec) or not value_source.HasDefaultValue():
            continue
        value = value_source.default
        if isinstance(value, Sdf.ValueBlock):
            continue
        converted = _usd_value_to_transport_python(
            stage,
            value_source.layer,
            value,
        )
        if converted is not None:
            inputs[port.base_name] = converted
            input_types[port.base_name] = str(value_source.typeName)

    return container_kind, info_id, inputs, input_types, connections


# ---------------------------------------------------------------------------
# PrimChannel - snapshot/diff/emit pipeline for one prim state slice
# ---------------------------------------------------------------------------
#
# Use a channel for prim state that is cheap or bounded enough to read as a
# whole when its dirty gate fires. Keep specialized paths for state that uses
# precomputed snapshots (TRS) or must be dirty-attr selective because
# full reads can be expensive (generic gprim attrs, including mesh arrays).
# The ensure_prim/api_schemas handshake is also specialized because it owns
# first-encounter structure, not a normal value diff.


class PrimChannel:
    """One slice of per-prim state with a uniform read/diff/emit lifecycle.

    Subclasses must set ``cache_key`` and implement ``read`` + ``to_event``.
    Declare ``watched_attrs`` / ``watched_prefixes`` when USD can tell us
    exactly which properties changed; otherwise the channel reads on every
    dirty cycle. Override ``diff`` for partial-event behavior.
    """

    cache_key: str = ""

    # Cache-miss baseline for the default diff. Empty containers prevent
    # spurious first-encounter events for channels with no authored state.
    cache_default = None

    # Attr-level gates for channels whose state changes through named USD
    # attributes or relationships.
    watched_attrs: tuple[str, ...] = ()

    # Prefix gates for property namespaces such as inputs:* and outputs:*.
    watched_prefixes: tuple[str, ...] = ()

    # Attrs the generic gprim scan skips globally (None = watched_attrs).
    # Channels whose watched names also exist on unrelated schemas (e.g.
    # velocities on both PointInstancer and UsdGeomPoints) declare () and
    # rely on the build cycle's per-prim owned-attr exclusion instead.
    filter_attrs: tuple[str, ...] | None = None

    # Composition arcs and load state arrive as resync notices, not ordinary
    # info-only attr changes. Such channels can skip pure attr-only cycles.
    reads_on_resync_only: bool = False

    # Only channels that need exact edit-target property specs pay for the
    # local-source-aware read path.
    uses_local_property_sources: bool = False

    def applies_to(self, prim) -> bool:
        """Does this channel apply to this prim at all? Cheap predicate."""
        return bool(prim and prim.IsValid())

    def needs_read(self, dirty_attrs: set[str] | None) -> bool:
        """Should this channel actually read on this cycle?

        ``dirty_attrs`` is the set of attr names the USD notice handler
        recorded as changed on this prim, or ``None`` when a full-state read
        is required (first encounter / resync / manual dirty).

        Channels with no gate default to always-read, which is slower but
        safe for integrations that do not know their USD notice pattern yet.
        """
        if not dirty_attrs:
            return True
        if self.reads_on_resync_only:
            return False
        if not self.watched_attrs and not self.watched_prefixes:
            return True
        for a in dirty_attrs:
            if a in self.watched_attrs:
                return True
            if self.watched_prefixes and a.startswith(self.watched_prefixes):
                return True
        return False

    def read(self, stage, prim_path):
        """Return current state, or ``None`` to skip the cache write."""
        raise NotImplementedError

    def read_scoped(self, stage, prim_path, dirty_attrs):
        """Read only the state slice named by ``dirty_attrs``.

        Returns a partial dict that is diffed key-wise and merged into the
        cache, or ``None`` to fall back to a full ``read``. Worth
        implementing for channels whose full state is expensive to read
        (bulk arrays) and whose dirty gate names exact attrs.
        """
        return None

    def diff(self, current, cached):
        """Return the diff to emit (any truthy value), or ``None`` if unchanged.

        The default compares full current state against ``cache_default`` on
        cache miss.
        """
        if cached is None:
            cached = self.cache_default
        return current if current != cached else None

    def to_event(self, prim_path, diff):
        """Build the wire event(s) for the diff.

        Return a ``dict`` for one event, ``list[dict]`` for multiple
        (channels that produce more than one event kind from a single
        read), or ``None`` to suppress.
        """
        raise NotImplementedError


class VariantSelectionsChannel(PrimChannel):
    cache_key = _C_VARIANT_SELECTIONS
    cache_default = {}
    reads_on_resync_only = True

    def read(self, stage, prim_path):
        return _read_edit_target_variant_selections(stage, prim_path)

    def diff(self, current, cached):
        cached = cached or {}
        changed = {
            name: current.get(name, "")
            for name in set(current) | set(cached)
            if current.get(name, "") != cached.get(name, "")
        }
        return changed or None

    def to_event(self, prim_path, diff):
        return {"k": K_SET_VARIANT_SELECTIONS, "prim": prim_path, "selections": diff}


class ReferencesChannel(PrimChannel):
    cache_key = _C_REFERENCES
    cache_default = {
        "entries": [],
        "list_op_authored": False,
        "list_op_explicit": False,
    }
    reads_on_resync_only = True

    def read(self, stage, prim_path):
        return _read_edit_target_arc_state(stage, prim_path, "referenceList")

    def to_event(self, prim_path, diff):
        return {
            "k": K_SET_REFERENCE,
            "prim": prim_path,
            "refs": diff["entries"],
            "list_op_authored": diff["list_op_authored"],
            "list_op_explicit": diff["list_op_explicit"],
        }


class PayloadsChannel(PrimChannel):
    cache_key = _C_PAYLOADS
    cache_default = {
        "entries": [],
        "list_op_authored": False,
        "list_op_explicit": False,
    }
    reads_on_resync_only = True

    def read(self, stage, prim_path):
        return _read_edit_target_arc_state(stage, prim_path, "payloadList")

    def to_event(self, prim_path, diff):
        return {
            "k": K_SET_PAYLOAD,
            "prim": prim_path,
            "payloads": diff["entries"],
            "list_op_authored": diff["list_op_authored"],
            "list_op_explicit": diff["list_op_explicit"],
        }


class MaterialBindingChannel(PrimChannel):
    """``material:binding`` is a relationship property, not a composition arc.

    Rebinds and clears arrive as info-only notices on the relationship name,
    so this channel watches the property directly. The cache holds a
    ``{purpose: target_path}`` dict; the diff emits one event per purpose
    whose target changed (added, removed, or rebound).
    """

    cache_key = _C_MATERIAL_BINDING
    cache_default: dict[str, str] = {}
    watched_prefixes = ("material:binding",)

    def read(self, stage, prim_path):
        return read_material_binding(stage, prim_path)

    def diff(self, current, cached):
        if cached is None:
            cached = self.cache_default
        changed: dict[str, str] = {}
        for purpose in set(current) | set(cached):
            if current.get(purpose, "") != cached.get(purpose, ""):
                changed[purpose] = current.get(purpose, "")
        return changed or None

    def to_event(self, prim_path, diff):
        events = []
        for purpose, target in diff.items():
            ev = {
                "k": K_SET_MATERIAL_BINDING,
                "prim": prim_path,
                "material_path": target,
            }
            if purpose:
                ev["material_purpose"] = purpose
            events.append(ev)
        return events


class ConnectableChannel(PrimChannel):
    """UsdShade.ConnectableAPI inputs + connections in one channel.

    Reads the connectable interface once per cycle and fans out into
    both wire events (``set_connectable_input``, ``set_connectable_connection``)
    so the expensive UsdShade traversal is not repeated.
    """

    cache_key = _C_CONNECTABLE
    watched_attrs = ("info:id",)
    watched_prefixes = (USDSHADE_INPUT_PREFIX, USDSHADE_OUTPUT_PREFIX)
    uses_local_property_sources = True

    def read(self, stage, prim_path):
        kind, info_id, inputs, types, conns = read_usdshade_connectable(stage, prim_path)
        if not kind:
            return None
        return {
            "info_id": info_id,
            "inputs": inputs,
            "types": types,
            "connections": conns,
        }

    def read_local(self, stage, prim_path, property_sources):
        kind, info_id, inputs, types, conns = _read_edit_target_usdshade_connectable(
            stage,
            prim_path,
            property_sources,
        )
        if not kind:
            return None
        return {
            "info_id": info_id,
            "inputs": inputs,
            "types": types,
            "connections": conns,
        }

    def diff(self, current, cached):
        cached = cached or {}
        last_inputs = cached.get("inputs", {})
        last_conns = cached.get("connections", {})

        changed_inputs = {
            n: v for n, v in current["inputs"].items() if not _values_equal(v, last_inputs.get(n))
        }
        info_id = current["info_id"]
        # Only shaders carry a meaningful info:id. Lights and node graphs
        # use the same connectable path with an empty id.
        info_id_changed = bool(info_id) and info_id != cached.get("info_id")

        changed_conns = {
            name: value
            for name, value in current["connections"].items()
            if name not in last_conns or value != last_conns[name]
        }
        new_conns = {name: value for name, value in changed_conns.items() if value is not None}
        removed_conns = [name for name in last_conns if name not in current["connections"]]
        removed_conns.extend(name for name, value in changed_conns.items() if value is None)

        inputs_emit = changed_inputs or info_id_changed
        conns_emit = new_conns or removed_conns
        if not inputs_emit and not conns_emit:
            return None
        return {
            "info_id": info_id,
            "inputs": changed_inputs if inputs_emit else None,
            "input_types": (
                {n: current["types"][n] for n in changed_inputs} if inputs_emit else None
            ),
            "new_conns": new_conns if conns_emit else None,
            "removed_conns": removed_conns if conns_emit else None,
        }

    def to_event(self, prim_path, diff):
        events: list[dict] = []
        if diff["inputs"] is not None:
            events.append(
                {
                    "k": K_SET_CONNECTABLE_INPUT,
                    "prim": prim_path,
                    "info_id": diff["info_id"],
                    "inputs": diff["inputs"],
                    "input_types": diff["input_types"],
                }
            )
        if diff["new_conns"] is not None or diff["removed_conns"]:
            ev: dict = {
                "k": K_SET_CONNECTABLE_CONNECTION,
                "prim": prim_path,
                "connections": diff["new_conns"] or {},
            }
            if diff["removed_conns"]:
                ev["disconnections"] = diff["removed_conns"]
            events.append(ev)
        return events


class VisibilityChannel(PrimChannel):
    cache_key = _C_VISIBILITY
    watched_attrs = ("visibility",)

    def read(self, stage, prim_path):
        prim = stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid():
            return None
        vis_attr = UsdGeom.Imageable(prim).GetVisibilityAttr()
        if not vis_attr or not vis_attr.IsValid() or not vis_attr.IsAuthored():
            return None
        return vis_attr.Get() or "inherited"

    def to_event(self, prim_path, diff):
        return {"k": K_SET_VISIBILITY, "prim": prim_path, "visible": diff != "invisible"}


class PayloadLoadStateChannel(PrimChannel):
    """Payload load/unload toggle. Emits ``load_payload`` or ``unload_payload``
    depending on ``IsLoaded()``.

    USD reports load/unload and payload arc edits as resyncs on the prim.
    """

    cache_key = _C_PAYLOAD_LOADED
    reads_on_resync_only = True

    def read(self, stage, prim_path):
        prim = stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid() or not prim.HasAuthoredPayloads():
            return None
        return prim.IsLoaded()

    def to_event(self, prim_path, diff):
        return {
            "k": K_LOAD_PAYLOAD if diff else K_UNLOAD_PAYLOAD,
            "prim": prim_path,
        }


# ---------------------------------------------------------------------------
# Typed-schema channels
# ---------------------------------------------------------------------------
#
# Channels that adopt a typed USD schema emit every authored attr of that
# schema through the generic set_gprim_attrs wire event. _typed_schema_attrs()
# drops attrs already owned by a peer channel (visibility, info:id, inputs:*,
# material:binding, ...) so the same property never emits twice from one prim.

_TYPED_SCHEMA_PEER_CHANNELS: tuple[type[PrimChannel], ...] = (
    VariantSelectionsChannel,
    ReferencesChannel,
    PayloadsChannel,
    PayloadLoadStateChannel,
    MaterialBindingChannel,
    ConnectableChannel,
    VisibilityChannel,
)


def _typed_schema_attrs(schema_cls) -> frozenset[str]:
    """Return direct typed-schema attrs that no peer channel already handles."""
    skip_attrs: set[str] = set()
    skip_prefixes: list[str] = []
    for cls in _TYPED_SCHEMA_PEER_CHANNELS:
        skip_attrs.update(cls.watched_attrs)
        skip_prefixes.extend(cls.watched_prefixes)
    skip_prefixes_t = tuple(skip_prefixes)
    return frozenset(
        n
        for n in schema_cls.GetSchemaAttributeNames(False)
        if not _is_transform_attr(n) and n not in skip_attrs and not n.startswith(skip_prefixes_t)
    )


_CAMERA_ATTR_NAMES = _typed_schema_attrs(UsdGeom.Camera)


def read_camera_attrs(stage, prim_path):
    """Read authored UsdGeomCamera attributes from a prim.

    Returns ``{name: python_value}`` for every authored camera attr, or
    ``None`` if the prim is not a ``UsdGeom.Camera``.
    """
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid() or not prim.IsA(UsdGeom.Camera):
        return None
    attrs = {}
    for name in _CAMERA_ATTR_NAMES:
        attr = prim.GetAttribute(name)
        if attr and attr.IsValid() and attr.IsAuthored():
            val = _usd_value_to_python(attr.Get())
            if val is not None:
                attrs[name] = val
    return attrs


class CameraAttrsChannel(PrimChannel):
    """UsdGeomCamera typed-schema attribute replication.

    Camera attrs use the generic ``set_gprim_attrs`` wire event so receivers
    do not need a camera-specific event kind.
    """

    cache_key = _C_CAMERA_ATTRS
    watched_attrs = tuple(_CAMERA_ATTR_NAMES)

    def applies_to(self, prim):
        return bool(prim and prim.IsValid() and prim.IsA(UsdGeom.Camera))

    def read(self, stage, prim_path):
        return read_camera_attrs(stage, prim_path) or {}

    def diff(self, current, cached):
        cached = cached or {}
        changed = {n: v for n, v in current.items() if not _values_equal(v, cached.get(n))}
        return changed if changed else None

    def to_event(self, prim_path, diff):
        return {"k": K_SET_GPRIM_ATTRS, "prim": prim_path, "attrs": diff}


class InstanceableChannel(PrimChannel):
    """Native scenegraph-instancing flag replication.

    Emits the authored ``instanceable`` bit only; prototype paths are
    implementation-defined and never cross the wire. Flag edits arrive
    as resync notices on the instance prim.
    """

    cache_key = _C_INSTANCEABLE
    cache_default = False
    reads_on_resync_only = True

    def read(self, stage, prim_path):
        prim = stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid() or not prim.HasAuthoredInstanceable():
            return None
        return prim.IsInstanceable()

    def to_event(self, prim_path, diff):
        return {"k": K_SET_INSTANCEABLE, "prim": prim_path, "instanceable": diff}


# UsdGeomPointInstancer attr name -> wire field name. orientationsf is read
# in preference to orientations when authored; both map to the same wire
# field (float32 wxyz rows).
_PI_USD_TO_WIRE = {
    "protoIndices": "proto_indices",
    "positions": "positions",
    "orientations": "orientations",
    "orientationsf": "orientations",
    "scales": "scales",
    "velocities": "velocities",
    "accelerations": "accelerations",
    "angularVelocities": "angular_velocities",
    "ids": "ids",
    "invisibleIds": "invisible_ids",
}

_PI_QUAT_ATTRS = frozenset({"orientations", "orientationsf"})


def _quat_array_to_wire(value):
    """VtQuat*Array (numpy xyzw rows) to float32 wxyz rows."""
    import numpy as np

    return np.asarray(value)[:, [3, 0, 1, 2]].astype(np.float32, copy=False)


def read_point_instancer(stage, prim_path, only=None):
    """Read prototypes rel targets + default-time arrays from a PointInstancer.

    Returns ``{wire_field: value}`` with orientations as float32 wxyz rows,
    or ``None`` if the prim is not a ``UsdGeom.PointInstancer``. Arrays
    authored only as time samples have no default opinion and are omitted;
    the per-time sample path carries those. ``only`` restricts the read to
    a set of USD property names (plus ``"prototypes"``) so a single-array
    edit does not pay for re-reading every other bulk array.
    """
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid() or not prim.IsA(UsdGeom.PointInstancer):
        return None
    pi = UsdGeom.PointInstancer(prim)
    state: dict = {}
    if only is None or "prototypes" in only:
        targets = pi.GetPrototypesRel().GetTargets()
        if targets:
            state["prototypes"] = [str(t) for t in targets]
    if (only is None or "inactiveIds" in only) and prim.HasAuthoredMetadata("inactiveIds"):
        list_op = prim.GetMetadata("inactiveIds")
        state["inactive_ids"] = [int(i) for i in list_op.ApplyOperations([])]

    orient_f = pi.GetOrientationsfAttr()
    quat_attr = "orientationsf" if orient_f and orient_f.IsAuthored() else "orientations"
    if only is not None and only & _PI_QUAT_ATTRS:
        # A dirty mark on either quat attr reads the resolved one.
        only = set(only) | {quat_attr}
    for usd_name, wire_name in _PI_USD_TO_WIRE.items():
        if usd_name in _PI_QUAT_ATTRS and usd_name != quat_attr:
            continue
        if only is not None and usd_name not in only:
            continue
        attr = prim.GetAttribute(usd_name)
        if not attr or not attr.IsValid() or not attr.IsAuthored():
            continue
        val = attr.Get()
        if val is None:
            continue
        if usd_name in _PI_QUAT_ATTRS:
            wire = _quat_array_to_wire(val)
        else:
            wire = _usd_value_to_python(val)
        if wire is not None:
            state[wire_name] = wire
    return state


class PointInstancerChannel(PrimChannel):
    """UsdGeomPointInstancer prototypes + per-instance array replication."""

    cache_key = _C_POINT_INSTANCER
    watched_attrs = tuple(_PI_USD_TO_WIRE) + ("prototypes", "inactiveIds")
    # velocities/accelerations/ids also exist on UsdGeomPoints; per-prim
    # exclusion keeps them flowing generically for non-PointInstancer prims.
    filter_attrs = ()

    def applies_to(self, prim):
        return bool(prim and prim.IsValid() and prim.IsA(UsdGeom.PointInstancer))

    def read(self, stage, prim_path):
        return read_point_instancer(stage, prim_path) or {}

    def read_scoped(self, stage, prim_path, dirty_attrs):
        return read_point_instancer(stage, prim_path, only=dirty_attrs) or {}

    def diff(self, current, cached):
        cached = cached or {}
        changed = {n: v for n, v in current.items() if not _values_equal(v, cached.get(n))}
        return changed if changed else None

    def to_event(self, prim_path, diff):
        return {
            "k": K_SET_POINT_INSTANCER,
            "prim": prim_path,
            "fields": list(diff),
            **diff,
        }


# Framework-owned channels. Receive-side ordering still sorts events before
# apply, but this order keeps raw emitter logs stable and readable.
_BUILTIN_PRIM_CHANNELS: tuple[PrimChannel, ...] = (
    VariantSelectionsChannel(),
    ReferencesChannel(),
    PayloadsChannel(),
    PayloadLoadStateChannel(),
    MaterialBindingChannel(),
    ConnectableChannel(),
    VisibilityChannel(),
    CameraAttrsChannel(),
    InstanceableChannel(),
    PointInstancerChannel(),
)


def _emit_channel_events(channel, prim_path, current, pc, events_out, partial=False):
    """Run the channel's diff, append any events, refresh the cache.

    ``partial`` marks a scoped read: ``current`` covers only the dirty
    slice, so it merges into the cached state instead of replacing it.
    """
    cached = pc.get(channel.cache_key)
    d = channel.diff(current, cached)
    if d is not None:
        ev = channel.to_event(prim_path, d)
        if ev is not None:
            if isinstance(ev, list):
                events_out.extend(ev)
            else:
                events_out.append(ev)
    if partial:
        merged = dict(cached or {})
        merged.update(current)
        pc[channel.cache_key] = merged
    else:
        pc[channel.cache_key] = current


# ---------------------------------------------------------------------------
# Cache invalidation — for receivers applying remote events
# ---------------------------------------------------------------------------
#
# After a remote event is applied to the stage, the per-prim diff cache
# reflects pre-mutation state.  Without invalidation, the next emit cycle
# would compare current stage state to the stale cache and re-emit the
# change the server already knows about (a feedback loop).
#
# Each entry maps an event kind to a callable that re-syncs the affected
# channel from the emitter's stage.  Only stage-affecting kinds need
# entries; kinds that mutate DCC objects directly (TRS, visibility,
# gprim attrs) are absorbed by the depsgraph's normal write-back path,
# and ``suppressed()`` keeps them from echoing.


def _invalidate_ensure_prim(emitter, prim_path, _ev):
    emitter._know_prim(prim_path)
    pc = emitter._prim_cache.setdefault(prim_path, {})
    for key in (
        _C_LOCAL_PRIM_SPECS,
        _C_LOCAL_DEFINITION_SPEC,
        _C_LOCAL_PROPERTY_STATE,
        _C_LOCAL_PROPERTY_NAMES,
        _C_LOCAL_BLOCKED_VALUE_FIELDS,
    ):
        pc.pop(key, None)
    specs = emitter._local_prim_specs(prim_path)
    ownership_spec = emitter._local_definition_spec(specs) or (specs[0] if specs else None)
    state = _local_prim_state(ownership_spec)
    if state:
        emitter._local_prim_states[prim_path] = state


def _invalidate_delete_prim(emitter, prim_path, _ev):
    emitter._purge_subtree(prim_path)


def _invalidate_deactivate_prim(emitter, prim_path, ev):
    # Active=False removes the subtree from the composed view; on the next
    # reactivation the child set re-composes, so child caches become stale.
    if not ev.get("active", True):
        emitter._purge_subtree(prim_path)


def _invalidate_rename_prim(emitter, prim_path, ev):
    new_name = ev.get("new_name", "")
    if not new_name:
        return
    parent = prim_path.rsplit("/", 1)[0]
    new_path = f"{parent}/{new_name}" if parent else f"/{new_name}"
    emitter._migrate_caches(prim_path, new_path)


def _invalidate_set_reference(emitter, prim_path, _ev):
    pc = emitter._prim_cache.setdefault(prim_path, {})
    pc[_C_REFERENCES] = _read_edit_target_arc_state(
        emitter.stage,
        prim_path,
        "referenceList",
    )
    pc[_C_VARIANT_SELECTIONS] = _read_edit_target_variant_selections(
        emitter.stage,
        prim_path,
    )
    # Composed children may carry their own variant selections — capture
    # them so subsequent diffs don't fire on imported state.
    prim = emitter.stage.GetPrimAtPath(prim_path)
    if prim and prim.IsValid():
        for child in Usd.PrimRange(prim):
            cp = str(child.GetPath())
            if cp == prim_path:
                continue
            cvs = _read_edit_target_variant_selections(emitter.stage, cp)
            if cvs:
                emitter._prim_cache.setdefault(cp, {})[_C_VARIANT_SELECTIONS] = cvs


def _invalidate_set_payload(emitter, prim_path, _ev):
    emitter._prim_cache.setdefault(prim_path, {})[_C_PAYLOADS] = _read_edit_target_arc_state(
        emitter.stage,
        prim_path,
        "payloadList",
    )


def _invalidate_load_payload(emitter, prim_path, _ev):
    prim = emitter.stage.GetPrimAtPath(prim_path)
    if prim and prim.IsValid():
        emitter._prim_cache.setdefault(prim_path, {})[_C_PAYLOAD_LOADED] = prim.IsLoaded()


def _invalidate_unload_payload(emitter, prim_path, _ev):
    # Children have left the composed stage — drop their caches so they're
    # rediscovered on the next load_payload.
    emitter._purge_subtree(prim_path, include_root=False)
    prim = emitter.stage.GetPrimAtPath(prim_path)
    if prim and prim.IsValid():
        emitter._prim_cache.setdefault(prim_path, {})[_C_PAYLOAD_LOADED] = prim.IsLoaded()


def _invalidate_set_variant_selections(emitter, prim_path, _ev):
    emitter._prim_cache.setdefault(prim_path, {})[_C_VARIANT_SELECTIONS] = (
        _read_edit_target_variant_selections(emitter.stage, prim_path)
    )
    # Variant change rewrites the child set — purge caches under this prim
    # so they're rebuilt from the new composition.
    emitter._purge_subtree(prim_path, include_root=False)


def _invalidate_set_material_binding(emitter, prim_path, _ev):
    emitter._prim_cache.setdefault(prim_path, {})[_C_MATERIAL_BINDING] = read_material_binding(
        emitter.stage,
        prim_path,
    )


def _resync_connectable_cache(emitter, prim_path):
    """Re-read the full connectable interface into the combined cache slot.
    Used by both connectable input and connection invalidators since they
    share one read and one cache entry now.
    """
    _fields, property_sources = emitter._local_property_state(prim_path)
    kind, info_id, inputs, types, connections = _read_edit_target_usdshade_connectable(
        emitter.stage,
        prim_path,
        property_sources,
    )
    if kind:
        emitter._prim_cache.setdefault(prim_path, {})[_C_CONNECTABLE] = {
            "info_id": info_id,
            "inputs": inputs,
            "types": types,
            "connections": connections,
        }


def _set_time_sample_cache(emitter, prim_path: str, cache_key: str, time: float, value):
    """Write a single ``(time, value)`` entry into ``_C_TIME_SAMPLES``.

    Cheap regardless of how many other samples the attribute has — we
    fingerprint just the value the event carries instead of re-reading
    every authored sample from the composed stage.
    """
    if value is None:
        return
    ts = emitter._prim_cache.setdefault(prim_path, {}).setdefault(_C_TIME_SAMPLES, {})
    ts.setdefault(cache_key, {})[time] = _value_hash(value)


def _invalidate_set_connectable_input(emitter, prim_path, ev):
    _resync_connectable_cache(emitter, prim_path)
    if ev.get("time") is not None:
        time = float(ev["time"])
        for name, value in ev.get("inputs", {}).items():
            _set_time_sample_cache(emitter, prim_path, "inputs:" + name, time, value)


def _invalidate_set_gprim_attrs(emitter, prim_path, ev):
    cam_attrs = read_camera_attrs(emitter.stage, prim_path)
    if cam_attrs is not None:
        emitter._prim_cache.setdefault(prim_path, {})[_C_CAMERA_ATTRS] = cam_attrs

    if ev.get("time") is not None:
        time = float(ev["time"])
        for name, value in ev.get("attrs", {}).items():
            _set_time_sample_cache(emitter, prim_path, name, time, value)
        return

    # Default-time path: refresh just the attrs this event mutated so a
    # local edit back to the server's value doesn't re-emit. Bounded by
    # the size of ev["attrs"]; no full prim scan.
    last_attrs = emitter._prim_cache.setdefault(prim_path, {}).setdefault(_C_GPRIM_ATTRS, {})
    last_attrs.update(ev.get("attrs", {}))


def _invalidate_set_connectable_connection(emitter, prim_path, _ev):
    _resync_connectable_cache(emitter, prim_path)


def _invalidate_set_xform_trs(emitter, prim_path, ev):
    if ev.get("time") is not None:
        time = float(ev["time"])
        for field, op_name in _TRS_FIELD_TO_OP_NAME.items():
            if field in ev:
                _set_time_sample_cache(emitter, prim_path, op_name, time, ev[field])
        return

    # Refresh the default-time TRS cache so a subsequent user edit
    # against a stale "last-sent" value still produces a correct diff.
    snap = emitter.snapshot_prim(prim_path)
    if snap is not None:
        emitter._prim_cache.setdefault(prim_path, {})[_C_TRS] = snap


def _invalidate_set_visibility(emitter, prim_path, ev):
    if ev.get("time") is not None:
        # Cache stores the USD string form ("inherited"/"invisible") via the
        # build path, so hash the matching string from the event's bool.
        vis_str = "inherited" if ev.get("visible", True) else "invisible"
        _set_time_sample_cache(
            emitter,
            prim_path,
            "visibility",
            float(ev["time"]),
            vis_str,
        )
        return

    prim = emitter.stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return
    vis_attr = UsdGeom.Imageable(prim).GetVisibilityAttr()
    if vis_attr and vis_attr.IsValid() and vis_attr.IsAuthored():
        value = vis_attr.Get() or "inherited"
        emitter._prim_cache.setdefault(prim_path, {})[_C_VISIBILITY] = value


def _invalidate_set_instanceable(emitter, prim_path, ev):
    emitter._prim_cache.setdefault(prim_path, {})[_C_INSTANCEABLE] = bool(ev["instanceable"])


# Wire field -> USD attr name the applier authors. The sample cache is keyed
# by USD attr names, so orientations maps to orientationsf here.
_PI_WIRE_TO_USD = {
    "proto_indices": "protoIndices",
    "positions": "positions",
    "orientations": "orientationsf",
    "scales": "scales",
    "velocities": "velocities",
    "accelerations": "accelerations",
    "angular_velocities": "angularVelocities",
    "ids": "ids",
    "invisible_ids": "invisibleIds",
}


def _invalidate_set_point_instancer(emitter, prim_path, ev):
    """Write the event's own values into the cache.

    The applied state equals the event payload, so caching it directly is
    exact and avoids re-reading every per-instance array from the stage.
    """
    import numpy as np

    fields = ev.get("fields", [])
    if ev.get("time") is not None:
        time = float(ev["time"])
        for f in fields:
            if f in ("prototypes", "inactive_ids"):
                continue
            value = ev[f]
            if f == "orientations":
                # The sample diff hashes the layer's xyzw quat layout.
                value = np.asarray(value, dtype=np.float32).reshape(-1, 4)[:, [1, 2, 3, 0]]
            _set_time_sample_cache(emitter, prim_path, _PI_WIRE_TO_USD[f], time, value)
        return
    state = emitter._prim_cache.setdefault(prim_path, {}).setdefault(_C_POINT_INSTANCER, {})
    for f in fields:
        state[f] = ev[f]


def _invalidate_set_stage_metadata(emitter, _prim_path, _ev):
    # Refresh the cached snapshot so the next dirty cycle doesn't re-emit
    # a value the server already knows about. The dirty flag is owned by
    # _build_stage_metadata_events.
    emitter._stage_metadata_cache = read_stage_metadata(emitter.stage)


def _invalidate_set_sdf_spec_fields(emitter, _prim_path, ev):
    spec_path = ev.get("spec_path", "")
    spec_kind = ev.get("spec_kind", "")
    if not spec_path or not spec_kind:
        return
    path = Sdf.Path(spec_path)
    key = (spec_kind, spec_path)
    prim_path = event_prim_path(path, spec_kind)
    pc = emitter._prim_cache.get(prim_path)
    if pc:
        for cache_key in (
            _C_LOCAL_PRIM_SPECS,
            _C_LOCAL_PROPERTY_STATE,
            _C_LOCAL_PROPERTY_NAMES,
            _C_LOCAL_BLOCKED_VALUE_FIELDS,
        ):
            pc.pop(cache_key, None)
    emitter._dirty_sdf_specs.pop(key, None)
    if ev.get("removed", False):
        emitter._sdf_spec_fields.pop(key, None)
        if spec_kind in _SDF_PROPERTY_KINDS:
            emitter._local_property_spec_fields.pop(
                str(path.StripAllVariantSelections()),
                None,
            )
        return
    authored = fragment_authored_fields(
        ev.get("fragment", ""),
        spec_path,
        ev["spec_kind"],
    )
    tracked = set(emitter._sdf_spec_fields.get(key, ()))
    for field in ev.get("fields", ()):
        if field in authored:
            tracked.add(field)
        else:
            tracked.discard(field)
    if tracked:
        emitter._sdf_spec_fields[key] = tracked
    else:
        emitter._sdf_spec_fields.pop(key, None)
    if spec_kind in _SDF_PROPERTY_KINDS:
        spec = emitter.stage.GetEditTarget().GetLayer().GetObjectAtPath(path)
        if isinstance(spec, (Sdf.AttributeSpec, Sdf.RelationshipSpec)):
            emitter._local_property_spec_fields[str(path.StripAllVariantSelections())] = {
                str(field) for field in spec.ListInfoKeys()
            }


_INVALIDATE_DISPATCH = {
    K_ENSURE_PRIM: _invalidate_ensure_prim,
    K_DELETE_PRIM: _invalidate_delete_prim,
    K_DEACTIVATE_PRIM: _invalidate_deactivate_prim,
    K_RENAME_PRIM: _invalidate_rename_prim,
    K_SET_REFERENCE: _invalidate_set_reference,
    K_SET_PAYLOAD: _invalidate_set_payload,
    K_LOAD_PAYLOAD: _invalidate_load_payload,
    K_UNLOAD_PAYLOAD: _invalidate_unload_payload,
    K_SET_VARIANT_SELECTIONS: _invalidate_set_variant_selections,
    K_SET_MATERIAL_BINDING: _invalidate_set_material_binding,
    K_SET_CONNECTABLE_INPUT: _invalidate_set_connectable_input,
    K_SET_CONNECTABLE_CONNECTION: _invalidate_set_connectable_connection,
    K_SET_GPRIM_ATTRS: _invalidate_set_gprim_attrs,
    K_SET_XFORM_TRS: _invalidate_set_xform_trs,
    K_SET_VISIBILITY: _invalidate_set_visibility,
    K_SET_STAGE_METADATA: _invalidate_set_stage_metadata,
    K_SET_INSTANCEABLE: _invalidate_set_instanceable,
    K_SET_POINT_INSTANCER: _invalidate_set_point_instancer,
    K_SET_SDF_SPEC_FIELDS: _invalidate_set_sdf_spec_fields,
}


class _SuppressScope:
    """Context manager for NoticeEmitter.suppressed().

    Calls suppress() on enter and unsuppress() on exit.
    __exit__ returns False -- exceptions propagate, never swallowed.
    """

    __slots__ = ("_emitter",)

    def __init__(self, emitter: NoticeEmitter):
        self._emitter = emitter

    def __enter__(self):
        self._emitter.suppress()
        return self._emitter

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._emitter.unsuppress()
        return False


class NoticeEmitter:
    """Watches a Usd.Stage for changes and builds idempotent transform events.

    Detects creation, deletion, deactivation, and renames via
    ``notice.GetPrimResyncType()`` on resync paths. Supports a reentrant
    suppress counter for feedback-loop prevention.

    Usage:
        emitter = NoticeEmitter(stage)
        # ... something authors to stage ...
        events = emitter.build_events_for_dirty()
        # events is a list of event dicts ready to wrap in a txn
    """

    def __init__(
        self,
        stage: Usd.Stage,
        attr_filter=None,
        *,
        replicated_api_schemas: set[str] | None = None,
        extra_channels: Sequence[PrimChannel] | None = None,
    ):
        """
        Args:
            stage: The Usd.Stage to watch.
            attr_filter: Optional callable(attr_name: str) -> bool.
                Controls which attributes are tracked for gprim attr diffing.
                Return True to track, False to skip. By default, attrs owned
                by specialized paths or channels are excluded; primvars and
                other generic attrs are tracked.
            replicated_api_schemas: Optional explicit override of the API
                schema names to replicate via the ensure_prim ``api_schemas``
                field. Each name must be a bare schema name (no
                ``":instance"``). If None, snapshots the module-level
                ``_REPLICATED_API_SCHEMAS`` at construction (default behavior
                — DCC integrations register their schemas at import time,
                then any later-constructed emitter picks them up).
            extra_channels: Optional additional ``PrimChannel`` instances to
                run alongside the built-in set. The framework-owned channels
                (variants, refs, payloads, material binding, connectable
                inputs/connections, visibility, camera attrs) are always
                active and run first; ``extra_channels`` are appended in
                order. Use this to replicate custom typed schemas without
                losing core USD coverage. Each channel's ``cache_key`` must
                be unique across the full set.
        """
        self.stage = stage
        if replicated_api_schemas is not None:
            for n in replicated_api_schemas:
                _validate_replicated_schema_name(n)
            self._replicated_apis: frozenset[str] = frozenset(replicated_api_schemas)
        else:
            self._replicated_apis = frozenset(_REPLICATED_API_SCHEMAS)
        self.dirty: set[str] = set()
        self._known_prims: set[str] = set()
        # Sorted mirror of _known_prims for O(log N) subtree range queries.
        # Mutate both only through _know_prim / _forget_prim.
        self._known_index: list[str] = []
        self._deleted_prims: set[str] = set()
        self._deactivated_prims: set[str] = set()
        self._removed_local_definition_prims: set[str] = set()
        self._renamed_prims: list[tuple[str, str]] = []  # (old_path, new_path)
        self._suppress_depth: int = 0
        self.listener = Tf.Notice.Register(Usd.Notice.ObjectsChanged, self._on_changed, stage)
        self._prim_cache: dict[str, dict] = {}
        # Unfiltered info-only attr names. Channels use this for read gating;
        # the gprim attr scan applies _attr_filter later.
        self._dirty_attrs: dict[str, set[str]] = {}
        # Subset of _dirty_attrs whose time-sample tables changed (classified
        # in _on_changed). Gates the sample diff path so a default-time edit
        # on a keyframed attr doesn't re-read every authored sample.
        self._sample_dirty_attrs: dict[str, set[str]] = {}
        self._notice_resynced_prims: set[str] = set()
        # Exact authored Sdf specs pending transport. Keys retain variant
        # selections, so inactive variant definitions never collapse into
        # the composed scene namespace.
        self._dirty_sdf_specs: dict[tuple[str, str], set[str] | None] = {}
        self._dirty_sdf_subtrees: dict[str, bool] = {}
        self._sdf_spec_fields: dict[tuple[str, str], set[str]] = {}
        self._full_sdf_spec_scan = False
        self._pending_edit_target: Usd.EditTarget | None = None
        self._edit_target_conflict = False
        # Specialized channels transport their own values, but their local
        # field ownership can still change when an opinion is set or cleared.
        # Keep those field deltas separate from fragment serialization so a
        # transform drag does not enter the fragment serializer.
        self._dirty_local_property_fields: dict[
            str,
            dict[str, set[str] | None],
        ] = {}
        # All fields authored on the current edit target, including fields
        # transported by specialized events. This is separate from the Sdf
        # fragment cache so clearing a fast-path value can remove the local
        # opinion instead of replaying the newly exposed weaker value.
        self._local_property_spec_fields: dict[str, set[str]] = {}
        # Last observed edit-target ownership. Namespace resyncs use it to
        # distinguish a removed local definition from a composed descendant
        # that remains visible through a reference or weaker layer.
        self._local_prim_states: dict[str, _LocalPrimState] = {}
        # Stage-level metadata replication state.
        self._stage_metadata_dirty: bool = False
        self._stage_metadata_cache: dict = read_stage_metadata(stage)
        extras = tuple(extra_channels) if extra_channels else ()
        for ch in extras:
            if not isinstance(ch, PrimChannel):
                raise TypeError(
                    f"extra_channels must contain PrimChannel instances; got {type(ch).__name__}"
                )
        # Silent cache-key collisions would make channels overwrite each
        # other's snapshots, so reject them before the emitter can run.
        seen_keys: set[str] = set(_SPECIALIZED_CACHE_KEYS)
        for ch in (*_BUILTIN_PRIM_CHANNELS, *extras):
            if ch.cache_key in seen_keys:
                raise ValueError(
                    f"Duplicate PrimChannel cache_key {ch.cache_key!r}; "
                    f"each channel must own a unique per-prim cache slot "
                    f"(reserved specialized slots: {sorted(_SPECIALIZED_CACHE_KEYS)})."
                )
            seen_keys.add(ch.cache_key)
            # These modes describe different USD notice patterns; using both
            # would make channel reads ambiguous.
            if ch.reads_on_resync_only and (ch.watched_attrs or ch.watched_prefixes):
                raise ValueError(
                    f"{type(ch).__name__} declares both reads_on_resync_only "
                    f"and watched_attrs/watched_prefixes; pick one — resync-only "
                    f"for composition arcs, watched names for attributes/rels."
                )
        self._channels: tuple[PrimChannel, ...] = _BUILTIN_PRIM_CHANNELS + extras
        # User-provided attr_filter wins; otherwise derive it from the active
        # channel set.
        self._attr_filter = attr_filter or _make_attr_filter(self._channels)

    def _local_prim_spec(self, prim_path: str):
        return _edit_target_prim_spec(self.stage, prim_path)

    def _local_prim_specs(self, prim_path: str) -> list[Sdf.PrimSpec]:
        return _edit_target_prim_specs(self.stage, prim_path)

    @staticmethod
    def _local_definition_spec(specs) -> Sdf.PrimSpec | None:
        for spec in specs or ():
            if spec.specifier != Sdf.SpecifierOver and not spec.path.ContainsPrimVariantSelection():
                return spec
        return None

    def _local_api_schemas(self, specs) -> set[str]:
        result = set()
        for spec in specs or ():
            if not spec.HasInfo("apiSchemas"):
                continue
            list_op = spec.GetInfo("apiSchemas")
            for name in list_op.ApplyOperations([]) or ():
                name = str(name)
                if name.split(":", 1)[0] in self._replicated_apis:
                    result.add(name)
        return result

    def _local_property_state(
        self,
        prim_path: str,
        specs=None,
    ) -> tuple[dict[str, set[str]], dict[str, dict[str, Sdf.PropertySpec]]]:
        specs = specs if specs is not None else self._local_prim_specs(prim_path)
        fields_by_name: dict[str, set[str]] = {}
        sources_by_name: dict[str, dict[str, Sdf.PropertySpec]] = {}
        for spec in specs:
            for prop in spec.properties:
                name = str(prop.name)
                fields = fields_by_name.setdefault(name, set())
                sources = sources_by_name.setdefault(name, {})
                for key in prop.ListInfoKeys():
                    field = str(key)
                    fields.add(field)
                    # GetPrimStack is strongest-to-weakest. Keep the first
                    # source for each field so direct and variant opinions
                    # retain USD's field-level precedence.
                    sources.setdefault(field, prop)
        return fields_by_name, sources_by_name

    @staticmethod
    def _refresh_local_property_state(
        specs: list[Sdf.PrimSpec],
        state: tuple[
            dict[str, set[str]],
            dict[str, dict[str, Sdf.PropertySpec]],
        ],
        names: set[str],
    ) -> None:
        """Refresh selected property ownership entries in a cached state."""
        fields_by_name, sources_by_name = state
        for name in names:
            fields_by_name.pop(name, None)
            sources_by_name.pop(name, None)
            for prim_spec in specs:
                prop = prim_spec.layer.GetPropertyAtPath(prim_spec.path.AppendProperty(name))
                if not prop:
                    continue
                fields = fields_by_name.setdefault(name, set())
                sources = sources_by_name.setdefault(name, {})
                for key in prop.ListInfoKeys():
                    field = str(key)
                    fields.add(field)
                    sources.setdefault(field, prop)

    @staticmethod
    def _refresh_local_property_fields(
        specs: list[Sdf.PrimSpec],
        state: tuple[
            dict[str, set[str]],
            dict[str, dict[str, Sdf.PropertySpec]],
        ],
        changes: dict[str, set[str] | None],
    ) -> None:
        """Apply field-level ownership changes to a cached property state.

        USD reports the same info-only field token for setting, updating, and
        clearing a value. Query only those fields in the current edit target
        to distinguish the three cases without rescanning every property.
        """
        full_refresh = {name for name, changed_fields in changes.items() if changed_fields is None}
        if full_refresh:
            NoticeEmitter._refresh_local_property_state(specs, state, full_refresh)

        fields_by_name, sources_by_name = state
        for name, changed_fields in changes.items():
            if changed_fields is None:
                continue
            fields = fields_by_name.setdefault(name, set())
            sources = sources_by_name.setdefault(name, {})
            unresolved = set(changed_fields)
            # The common update case already has the strongest source cached.
            # Reuse that live Sdf spec directly; only a newly authored or
            # cleared field needs the edit-target stack walk below.
            for field in tuple(unresolved):
                source = sources.get(field)
                if source and source.HasInfo(field):
                    fields.add(field)
                    unresolved.remove(field)
            if not unresolved:
                continue
            for prim_spec in specs:
                prop = prim_spec.layer.GetPropertyAtPath(prim_spec.path.AppendProperty(name))
                if not prop:
                    continue
                for field in tuple(unresolved):
                    if prop.HasInfo(field):
                        fields.add(field)
                        sources[field] = prop
                        unresolved.remove(field)
                if not unresolved:
                    break
            for field in unresolved:
                fields.discard(field)
                sources.pop(field, None)
            if not fields:
                fields_by_name.pop(name, None)
                sources_by_name.pop(name, None)

    @staticmethod
    def _property_authors_value(
        properties: dict[str, set[str]],
        blocked_values: dict[str, set[str]],
        name: str,
        time: float | None,
    ) -> bool:
        field = "timeSamples" if time is not None else "default"
        return field in properties.get(name, ()) and field not in blocked_values.get(
            name,
            (),
        )

    @staticmethod
    def _value_block_fields(
        field_sources: dict[str, Sdf.PropertySpec],
        fields: set[str],
    ) -> set[str]:
        """Return value fields whose local opinion contains an Sdf block."""
        blocked = set()
        if "default" in fields:
            source = field_sources.get("default")
            if source and isinstance(source.GetInfo("default"), Sdf.ValueBlock):
                blocked.add("default")
        if "timeSamples" in fields:
            source = field_sources.get("timeSamples")
            samples = source.GetInfo("timeSamples") if source else None
            if samples and any(isinstance(value, Sdf.ValueBlock) for value in samples.values()):
                blocked.add("timeSamples")
        return blocked

    @staticmethod
    def _refresh_blocked_value_fields(
        blocked_by_name: dict[str, set[str]],
        property_sources: dict[str, dict[str, Sdf.PropertySpec]],
        names,
    ) -> None:
        for name in names:
            sources = property_sources.get(name, {})
            blocked = NoticeEmitter._value_block_fields(
                sources,
                set(sources) & _SDF_ATTRIBUTE_VALUE_FIELDS,
            )
            if blocked:
                blocked_by_name[name] = blocked
            else:
                blocked_by_name.pop(name, None)

    @staticmethod
    def _update_blocked_value_fields(
        blocked_by_name: dict[str, set[str]],
        name: str,
        changed_fields: set[str],
        blocked_fields: set[str],
    ) -> None:
        if not blocked_fields and name not in blocked_by_name:
            return
        current = set(blocked_by_name.get(name, ()))
        current.difference_update(changed_fields)
        current.update(blocked_fields)
        if current:
            blocked_by_name[name] = current
        else:
            blocked_by_name.pop(name, None)

    @staticmethod
    def _local_xform_event_fields(
        properties: dict[str, set[str]],
        blocked_values: dict[str, set[str]],
        event_fields: list[str],
        time: float | None,
    ) -> list[str]:
        value_field = "timeSamples" if time is not None else "default"
        result = [
            field
            for field in event_fields
            if value_field in properties.get(_TRS_FIELD_TO_OP_NAME[field], ())
            and value_field not in blocked_values.get(_TRS_FIELD_TO_OP_NAME[field], ())
        ]
        if len(result) == len(event_fields):
            return result

        for name, fields in properties.items():
            if (
                name in ("xformOp:translate", "xformOp:orient", "xformOp:scale")
                or not name.startswith("xformOp:")
                or value_field not in fields
            ):
                continue
            if value_field in blocked_values.get(name, ()):
                continue
            # Pivots, Euler ops, and matrix ops are transported through the
            # decomposed TRS representation, so any local value can affect all
            # three output components.
            return list(event_fields)
        return result

    def _filter_events_to_local_opinions(
        self,
        events: list[dict],
        local_specs,
        properties: dict[str, set[str]],
        blocked_values: dict[str, set[str]],
    ) -> list[dict]:
        """Keep only fields authored by the current edit target.

        Value channels may read the composed stage for efficient diffs, while
        composition-arc channels read the current edit target directly.
        Filtering prevents either path from turning weaker composed values
        into stronger local opinions on the receiver.
        """
        if len(events) == 1 and events[0]["k"] == K_SET_XFORM_TRS:
            event = events[0]
            fields = self._local_xform_event_fields(
                properties,
                blocked_values,
                event.get("fields", []),
                event.get("time"),
            )
            if not fields:
                return []
            if fields == event.get("fields", []):
                return events
            kept = dict(event)
            kept["fields"] = fields
            for field in ("t", "r", "s"):
                if field not in fields:
                    kept.pop(field, None)
            return [kept]

        filtered: list[dict] = []
        for event in events:
            kind = event["k"]
            time = event.get("time")

            if kind == K_SET_SDF_SPEC_FIELDS:
                filtered.append(event)
                continue

            if kind == K_ENSURE_XFORM_OPS:
                if "xformOpOrder" in properties or any(
                    name.startswith("xformOp:") for name in properties
                ):
                    filtered.append(event)
                continue

            if kind == K_SET_XFORM_TRS:
                fields = self._local_xform_event_fields(
                    properties,
                    blocked_values,
                    event.get("fields", []),
                    time,
                )
                if fields:
                    kept = dict(event)
                    kept["fields"] = fields
                    for field in ("t", "r", "s"):
                        if field not in fields:
                            kept.pop(field, None)
                    filtered.append(kept)
                continue

            if kind == K_SET_GPRIM_ATTRS:
                attrs = {
                    name: value
                    for name, value in event.get("attrs", {}).items()
                    if self._property_authors_value(
                        properties,
                        blocked_values,
                        name,
                        time,
                    )
                }
                if attrs:
                    kept = dict(event)
                    kept["attrs"] = attrs
                    for metadata_key in ("primvar_meta", "attr_interp"):
                        if metadata_key in kept:
                            kept[metadata_key] = {
                                name: value
                                for name, value in kept[metadata_key].items()
                                if name in attrs
                            }
                            if not kept[metadata_key]:
                                kept.pop(metadata_key)
                    filtered.append(kept)
                continue

            if kind == K_SET_VISIBILITY:
                if self._property_authors_value(
                    properties,
                    blocked_values,
                    "visibility",
                    time,
                ):
                    filtered.append(event)
                continue

            if kind == K_SET_MATERIAL_BINDING:
                rel_name = _MATERIAL_BINDING_REL_BY_PURPOSE.get(event.get("material_purpose", ""))
                if rel_name and "targetPaths" in properties.get(rel_name, ()):
                    filtered.append(event)
                continue

            if kind == K_SET_CONNECTABLE_INPUT:
                inputs = {
                    name: value
                    for name, value in event.get("inputs", {}).items()
                    if self._property_authors_value(
                        properties,
                        blocked_values,
                        f"inputs:{name}",
                        time,
                    )
                }
                local_id = self._property_authors_value(
                    properties,
                    blocked_values,
                    "info:id",
                    time,
                )
                if inputs or local_id:
                    kept = dict(event)
                    kept["inputs"] = inputs
                    kept["input_types"] = {
                        name: value
                        for name, value in event.get("input_types", {}).items()
                        if name in inputs
                    }
                    filtered.append(kept)
                continue

            if kind == K_SET_CONNECTABLE_CONNECTION:
                connections = {
                    name: value
                    for name, value in event.get("connections", {}).items()
                    if "connectionPaths" in properties.get(name, ())
                }
                disconnections = [
                    name
                    for name in event.get("disconnections", ())
                    if "connectionPaths" in properties.get(name, ())
                ]
                if connections or disconnections:
                    kept = dict(event)
                    kept["connections"] = connections
                    if disconnections:
                        kept["disconnections"] = disconnections
                    else:
                        kept.pop("disconnections", None)
                    filtered.append(kept)
                continue

            if kind == K_SET_POINT_INSTANCER:
                fields = []
                kept = dict(event)
                for field in event.get("fields", ()):
                    if field == "inactive_ids":
                        authored = any(spec.HasInfo("inactiveIds") for spec in local_specs)
                    else:
                        prop_names = _POINT_INSTANCER_PROPERTIES_BY_FIELD.get(field)
                        if isinstance(prop_names, str):
                            prop_names = (prop_names,)
                        prop_names = prop_names or ()
                        if field == "prototypes":
                            authored = any(
                                "targetPaths" in properties.get(prop_name, ())
                                for prop_name in prop_names
                            )
                        else:
                            authored = any(
                                self._property_authors_value(
                                    properties,
                                    blocked_values,
                                    prop_name,
                                    time,
                                )
                                for prop_name in prop_names
                            )
                    if authored:
                        fields.append(field)
                    else:
                        kept.pop(field, None)
                if fields:
                    kept["fields"] = fields
                    filtered.append(kept)
                continue

            if kind == K_SET_REFERENCE:
                # The channel itself reads only edit-target arcs. If it
                # produced an empty event, that is a required clear after a
                # previously authored local list was removed.
                filtered.append(event)
                continue
            if kind == K_SET_PAYLOAD:
                filtered.append(event)
                continue
            if kind in (K_LOAD_PAYLOAD, K_UNLOAD_PAYLOAD):
                if any(spec.HasInfo("payload") for spec in local_specs):
                    filtered.append(event)
                continue
            if kind == K_SET_VARIANT_SELECTIONS:
                filtered.append(event)
                continue
            if kind == K_SET_INSTANCEABLE:
                if any(spec.HasInfo("instanceable") for spec in local_specs):
                    filtered.append(event)
                continue

            filtered.append(event)
        return filtered

    def cleanup(self):
        """Deregister notice listener and clear all caches.

        Call this before discarding the emitter (e.g., on DCC addon
        unregister/reload) to prevent stale callbacks from firing.
        """
        if self.listener:
            self.listener.Revoke()
            self.listener = None
        self._prim_cache.clear()
        self._known_prims.clear()
        self._known_index.clear()
        self._dirty_attrs.clear()
        self._sample_dirty_attrs.clear()
        self._notice_resynced_prims.clear()
        self._dirty_sdf_specs.clear()
        self._dirty_sdf_subtrees.clear()
        self._sdf_spec_fields.clear()
        self._dirty_local_property_fields.clear()
        self._local_property_spec_fields.clear()
        self._local_prim_states.clear()
        self.dirty.clear()
        self._deleted_prims.clear()
        self._deactivated_prims.clear()
        self._removed_local_definition_prims.clear()
        self._renamed_prims.clear()
        self._full_sdf_spec_scan = False
        self._pending_edit_target = None
        self._edit_target_conflict = False
        self._suppress_depth = 0

    def seed_prim_cache(self, stage: Usd.Stage, prim_path: str):
        """Seed the per-prim diff cache for a prim and its composed children.

        Snapshots the current state of every applicable channel (plus the
        specialized gprim-attrs and api_schemas slots) into the cache, so the
        next emit cycle diffs against authored state instead of treating
        everything as a first-encounter delta.

        Does NOT add to ``_known_prims`` — the emitter should still send
        structural events (ensure_prim, ensure_xform_ops) on first
        encounter so the server can create xform ops on payload prims.
        """
        prim = stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid():
            return
        for child in Usd.PrimRange(prim):
            cp = str(child.GetPath())
            pc = self._prim_cache.setdefault(cp, {})
            _fields, property_sources = self._local_property_state(cp)
            for channel in self._channels:
                if not channel.applies_to(child):
                    continue
                if channel.uses_local_property_sources:
                    current = channel.read_local(stage, cp, property_sources)
                else:
                    current = channel.read(stage, cp)
                if current is None:
                    continue
                pc[channel.cache_key] = current
            # Generic gprim attrs use a specialized path so info-only notices
            # can read only named dirty attrs, not every heavy mesh array.
            gprim_snapshot = {}
            for attr in child.GetAttributes():
                name = attr.GetName()
                if attr.IsAuthored() and self._attr_filter(name):
                    value_source = property_sources.get(name, {}).get("default")
                    if (
                        isinstance(value_source, Sdf.AttributeSpec)
                        and value_source.HasDefaultValue()
                    ):
                        value = value_source.default
                        source_layer = value_source.layer
                    else:
                        value = attr.Get()
                        source_layer = self.stage.GetEditTarget().GetLayer()
                        if value_contains_asset_path(value):
                            source_layer = _attribute_default_source_layer(attr)
                            if source_layer is None:
                                continue
                    val = _usd_value_to_transport_python(
                        self.stage,
                        source_layer,
                        value,
                    )
                    if val is not None:
                        gprim_snapshot[name] = val
            if gprim_snapshot:
                pc[_C_GPRIM_ATTRS] = gprim_snapshot
            # Seed the api_schemas snapshot so a later diff cycle doesn't
            # spuriously re-emit ensure_prim on first encounter.
            pc[_C_API_SCHEMAS] = self._local_api_schemas(self._local_prim_specs(cp))
            # Seed time-sample hashes so first emit cycle doesn't replay every
            # authored sample on an already-keyframed prim.
            ts_seed: dict = {}
            for attr in child.GetAttributes():
                if not attr.IsAuthored():
                    continue
                name = attr.GetName()
                times = attr.GetTimeSamples()
                if not times:
                    continue
                ts_seed[name] = {
                    t: _value_hash(xform_sample_value(attr.Get(Usd.TimeCode(t)))) for t in times
                }
            if ts_seed:
                pc[_C_TIME_SAMPLES] = ts_seed

    def suppress(self):
        """Suppress notice collection (feedback guard).

        Reentrant: each call increments the suppress depth.
        Must be paired with a matching unsuppress() call.
        """
        self._suppress_depth += 1

    def unsuppress(self):
        """Resume notice collection.

        Decrements the suppress depth. Notices are only collected
        again when depth reaches zero.
        """
        assert self._suppress_depth > 0, "unsuppress() called without matching suppress()"
        self._suppress_depth -= 1

    def suppressed(self):
        """Return a context manager that suppresses notices for the block.

        Usage::

            with emitter.suppressed():
                apply_events(stage, events)
            # notices automatically resume here

        Reentrant -- nests correctly with other suppress/unsuppress calls.
        Exceptions are NOT swallowed: __exit__ returns False.
        """
        return _SuppressScope(self)

    def clear_all(self):
        """Flush all dirty/deleted/renamed sets without building events."""
        self.dirty.clear()
        self._deleted_prims.clear()
        self._deactivated_prims.clear()
        self._removed_local_definition_prims.clear()
        self._renamed_prims.clear()
        self._dirty_attrs.clear()
        self._sample_dirty_attrs.clear()
        self._notice_resynced_prims.clear()
        self._dirty_sdf_specs.clear()
        self._dirty_sdf_subtrees.clear()
        self._full_sdf_spec_scan = False
        self._pending_edit_target = None
        self._edit_target_conflict = False
        self._dirty_local_property_fields.clear()

    def _record_edit_target(self) -> Usd.EditTarget:
        edit_target = self.stage.GetEditTarget()
        if self._pending_edit_target is None:
            self._pending_edit_target = edit_target
        elif self._pending_edit_target != edit_target:
            if self._pending_edit_target.GetLayer() == edit_target.GetLayer():
                self._pending_edit_target = Usd.EditTarget(edit_target.GetLayer())
            else:
                self._edit_target_conflict = True
        return edit_target

    def _mark_exact_sdf_spec(
        self,
        spec_path: str | Sdf.Path,
        spec_kind: str,
        fields: set[str] | None,
    ) -> None:
        key = (spec_kind, str(spec_path))
        if key in self._dirty_sdf_specs:
            current = self._dirty_sdf_specs[key]
            if current is None or fields is None:
                self._dirty_sdf_specs[key] = None
            else:
                current.update(fields)
            return
        self._dirty_sdf_specs[key] = None if fields is None else set(fields)

    def _mark_sdf_subtree(
        self,
        spec_path: str | Sdf.Path,
        *,
        resync: bool = True,
    ) -> None:
        path = str(spec_path)
        self._dirty_sdf_subtrees[path] = self._dirty_sdf_subtrees.get(path, False) or resync

    def _mark_sdf_property_spec(
        self,
        spec_path: str,
        fields: set[str] | None,
        *,
        source_path: Sdf.Path | None = None,
        source_layer: Sdf.Layer | None = None,
    ) -> None:
        """Map a scene property path to its exact current edit-target spec."""
        event_path = Sdf.Path(spec_path)
        edit_target = self._pending_edit_target or self.stage.GetEditTarget()
        if source_path is None:
            source_path = edit_target.MapToSpecPath(event_path)
        layer = source_layer or edit_target.GetLayer()
        spec = layer.GetObjectAtPath(source_path) if not source_path.isEmpty else None
        if isinstance(spec, Sdf.AttributeSpec):
            self._mark_exact_sdf_spec(source_path, SDF_SPEC_KIND_ATTRIBUTE, fields)
            return
        if isinstance(spec, Sdf.RelationshipSpec):
            self._mark_exact_sdf_spec(source_path, SDF_SPEC_KIND_RELATIONSHIP, fields)
            return

        if not source_path.isEmpty:
            exact_path = str(source_path)
            for kind in _SDF_PROPERTY_KINDS:
                if (kind, exact_path) in self._sdf_spec_fields:
                    self._mark_exact_sdf_spec(source_path, kind, fields)
                    return

        namespace_path = event_path.StripAllVariantSelections()
        for kind, path in tuple(self._sdf_spec_fields):
            if kind not in _SDF_PROPERTY_KINDS:
                continue
            if Sdf.Path(path).StripAllVariantSelections() == namespace_path:
                self._mark_exact_sdf_spec(path, kind, fields)
                return

        prop = self.stage.GetPropertyAtPath(event_path)
        if not prop or not prop.IsValid() or source_path.isEmpty:
            return
        if isinstance(prop, Usd.Attribute):
            kind = SDF_SPEC_KIND_ATTRIBUTE
        elif isinstance(prop, Usd.Relationship):
            kind = SDF_SPEC_KIND_RELATIONSHIP
        else:
            return
        removed_fields = fields
        if removed_fields is None:
            removed_fields = set(self._local_property_spec_fields.get(str(namespace_path), ()))
        self._mark_exact_sdf_spec(source_path, kind, removed_fields)

    def _mark_local_property_fields(
        self,
        spec_path: str,
        fields: set[str] | None,
    ) -> None:
        """Record specialized field-presence changes for ownership filtering."""
        path = Sdf.Path(spec_path)
        prim_path = str(path.GetPrimPath())
        name = str(path.name)
        by_name = self._dirty_local_property_fields.setdefault(prim_path, {})
        if name in by_name:
            current = by_name[name]
            if current is None or fields is None:
                by_name[name] = None
            else:
                current.update(fields)
            return
        by_name[name] = None if fields is None else set(fields)

    def _refresh_cached_local_property_fields(
        self,
        prim_path: str,
        name: str,
        spec_path: str,
        fields: set[str],
        *,
        source_path: Sdf.Path | None = None,
        source_layer: Sdf.Layer | None = None,
    ) -> bool:
        """Update cached specialized ownership directly when it is safe.

        Returns False when the prim needs a later full refresh. The common
        value-update path avoids allocating pending maps; only a field clear
        stays pending so the Sdf clear event can compare old and new state.
        """
        if prim_path in self._notice_resynced_prims:
            return False
        pc = self._prim_cache.get(prim_path)
        if not pc:
            return False
        local_specs = pc.get(_C_LOCAL_PRIM_SPECS)
        property_state = pc.get(_C_LOCAL_PROPERTY_STATE)
        if local_specs is None or property_state is None:
            return False

        previous = property_state[0].get(name, set())
        sources = property_state[1].get(name, {})
        if fields == {"default"}:
            source = sources.get("default")
            if "default" in previous and source and source.HasDefaultValue():
                if isinstance(source.default, Sdf.ValueBlock):
                    blocked_by_name = pc[_C_LOCAL_BLOCKED_VALUE_FIELDS]
                    blocked_by_name.setdefault(name, set()).add("default")
                    self._mark_sdf_property_spec(
                        spec_path,
                        fields,
                        source_path=source_path,
                        source_layer=source_layer,
                    )
                else:
                    blocked_by_name = pc[_C_LOCAL_BLOCKED_VALUE_FIELDS]
                    if blocked_by_name:
                        blocked = blocked_by_name.get(name)
                        if blocked and "default" in blocked:
                            blocked.discard("default")
                            if not blocked:
                                blocked_by_name.pop(name, None)
                return True

        unchanged = True
        for field in fields:
            source = sources.get(field)
            if field not in previous or not source or not source.HasInfo(field):
                unchanged = False
                break
        if unchanged:
            blocked = self._value_block_fields(sources, fields)
            blocked_by_name = pc.setdefault(_C_LOCAL_BLOCKED_VALUE_FIELDS, {})
            self._update_blocked_value_fields(
                blocked_by_name,
                name,
                fields,
                blocked,
            )
            if blocked:
                self._mark_sdf_property_spec(
                    spec_path,
                    blocked,
                    source_path=source_path,
                    source_layer=source_layer,
                )
            return True

        previous = set(previous)
        self._refresh_local_property_fields(
            local_specs,
            property_state,
            {name: fields},
        )
        current = set(property_state[0].get(name, ()))
        sources = property_state[1].get(name, {})
        blocked = self._value_block_fields(sources, fields)
        blocked_by_name = pc.setdefault(_C_LOCAL_BLOCKED_VALUE_FIELDS, {})
        self._update_blocked_value_fields(
            blocked_by_name,
            name,
            fields,
            blocked,
        )
        if blocked:
            self._mark_sdf_property_spec(
                spec_path,
                blocked,
                source_path=source_path,
                source_layer=source_layer,
            )
        cleared = (fields & previous) - current
        if cleared:
            self._mark_sdf_property_spec(
                spec_path,
                cleared,
                source_path=source_path,
                source_layer=source_layer,
            )
            self._mark_local_property_fields(spec_path, fields)
            return True

        if current:
            self._local_property_spec_fields[spec_path] = current
        else:
            self._local_property_spec_fields.pop(spec_path, None)
        return True

    def _channel_owns_property(self, prim: Usd.Prim, name: str) -> bool:
        for channel in self._channels:
            if not channel.applies_to(prim):
                continue
            if name in channel.watched_attrs:
                return True
            if channel.watched_prefixes and name.startswith(channel.watched_prefixes):
                return True
        return False

    def _sdf_owns_attribute_value(self, prim: Usd.Prim, name: str) -> bool:
        """Whether an attribute value lacks a faithful specialized path."""
        if name.startswith(PRIMVAR_PREFIX) or not self._attr_filter(name):
            return False
        attr = prim.GetAttribute(name)
        definition = prim.GetPrimDefinition()
        needs_sdf_value = (
            attr
            and attr.IsValid()
            and (attr.IsCustom() or not definition or not definition.GetSchemaPropertySpec(name))
        )
        if not needs_sdf_value:
            return False

        event_path = prim.GetPath().AppendProperty(name)
        edit_target = self.stage.GetEditTarget()
        source_path = edit_target.MapToSpecPath(event_path)
        if source_path.isEmpty:
            return False
        namespace_path = source_path.StripAllVariantSelections()
        layer = edit_target.GetLayer()
        return any(
            spec.layer == layer and spec.path.StripAllVariantSelections() == namespace_path
            for spec in attr.GetPropertyStack()
        )

    def _sdf_fields_for_spec(
        self,
        prim: Usd.Prim,
        spec,
        fields: set[str],
        field_sources: dict[str, Sdf.PropertySpec],
    ) -> set[str]:
        name = str(spec.name)
        blocked_values = self._value_block_fields(field_sources, fields)
        if isinstance(spec, Sdf.AttributeSpec):
            if spec.custom:
                return set(fields)
            if self._channel_owns_property(prim, name):
                result = blocked_values | (
                    set(fields)
                    - _SDF_DECLARATION_FIELDS
                    - _SDF_ATTRIBUTE_VALUE_FIELDS
                    - {"connectionPaths"}
                )
                # A bare UsdShade input/output has no value or connection for
                # the specialized channel to emit. Preserve its declaration so
                # the port itself is not lost.
                if (
                    not result
                    and name.startswith((USDSHADE_INPUT_PREFIX, USDSHADE_OUTPUT_PREFIX))
                    and not (set(fields) & (_SDF_ATTRIBUTE_VALUE_FIELDS | {"connectionPaths"}))
                ):
                    result.update(set(fields) & _SDF_DECLARATION_FIELDS)
                connection_source = field_sources.get("connectionPaths")
                # An untyped over can arrive before its weaker defining prim.
                # Its exact field preserves the edge without inventing port types.
                if (
                    "connectionPaths" in fields
                    and isinstance(connection_source, Sdf.AttributeSpec)
                    and (
                        _connection_spec_needs_sdf(connection_source)
                        or _property_spec_is_untyped_over(connection_source)
                    )
                ):
                    result.add("connectionPaths")
                return result
            if self._sdf_owns_attribute_value(prim, name):
                return set(fields)
            result = set(fields) - _SDF_DECLARATION_FIELDS - _SDF_ATTRIBUTE_VALUE_FIELDS
            result.update(blocked_values)
            return result
        if isinstance(spec, Sdf.RelationshipSpec):
            if spec.custom:
                return set(fields)
            if self._channel_owns_property(prim, name):
                return set(fields) - _SDF_DECLARATION_FIELDS - {"targetPaths"}
            return set(fields)
        return set()

    def _collect_sdf_specs(
        self,
        layer: Sdf.Layer,
        roots: set[str],
        *,
        full_scan: bool,
    ) -> dict[tuple[str, str], object]:
        specs: dict[tuple[str, str], object] = {}

        def _add(path: Sdf.Path) -> None:
            spec = (
                layer.pseudoRoot
                if path == Sdf.Path.absoluteRootPath
                else layer.GetObjectAtPath(path)
            )
            if isinstance(spec, _SDF_SPEC_TYPES):
                specs[(spec_kind_for_object(spec), str(path))] = spec

        def _visit(path: Sdf.Path) -> None:
            _add(path)

        scan_roots = {"/"} if full_scan else roots
        for value in scan_roots:
            root = Sdf.Path(value)
            _add(root)
            if not root.IsPropertyPath():
                layer.Traverse(root, _visit)

        for _kind, value in self._dirty_sdf_specs:
            path = Sdf.Path(value)
            spec = layer.GetObjectAtPath(path)
            if isinstance(spec, _SDF_SPEC_TYPES):
                specs[(spec_kind_for_object(spec), value)] = spec
        return specs

    def _generic_sdf_fields(
        self,
        spec,
        spec_path: Sdf.Path,
        spec_kind: str,
    ) -> set[str]:
        fields = {str(key) for key in spec.ListInfoKeys()}
        if spec_kind == SDF_SPEC_KIND_LAYER:
            fields.difference_update(_WATCHED_STAGE_METADATA_FIELDS)
            fields.difference_update(SDF_LAYER_TOPOLOGY_FIELDS)
            if "customLayerData" in fields:
                data = dict(spec.GetInfo("customLayerData"))
                if not (set(data) - {"openusdconnect"}):
                    fields.discard("customLayerData")
            return fields

        if spec_kind == SDF_SPEC_KIND_PRIM:
            direct_path = not spec_path.ContainsPrimVariantSelection()
            if direct_path:
                fields.difference_update(_SDF_SPECIALIZED_PRIM_FIELDS)
            represented_by_ensure = direct_path and (
                spec.specifier == Sdf.SpecifierDef
                or (spec.specifier == Sdf.SpecifierOver and not spec.typeName)
            )
            if represented_by_ensure:
                fields.difference_update({"specifier", "typeName"})
            return fields

        if spec_kind not in _SDF_PROPERTY_KINDS:
            return fields

        scene_path = spec_path.StripAllVariantSelections()
        prop = self.stage.GetPropertyAtPath(scene_path)
        active = bool(
            prop
            and prop.IsValid()
            and any(
                item.layer == spec.layer and item.path == spec.path
                for item in prop.GetPropertyStack()
            )
        )
        if not active:
            return fields
        prim = self.stage.GetPrimAtPath(scene_path.GetPrimPath())
        if not prim or not prim.IsValid():
            return fields
        sources = {field: spec for field in fields}
        return self._sdf_fields_for_spec(prim, spec, fields, sources)

    @staticmethod
    def _sdf_spec_requires_identity(
        spec,
        spec_path: Sdf.Path,
        spec_kind: str,
    ) -> bool:
        if spec_kind in (SDF_SPEC_KIND_VARIANT, SDF_SPEC_KIND_VARIANT_SET):
            return True
        if spec_kind != SDF_SPEC_KIND_PRIM:
            return False
        if spec_path.ContainsPrimVariantSelection():
            return True
        return spec.specifier == Sdf.SpecifierClass or (
            spec.specifier == Sdf.SpecifierOver and bool(spec.typeName)
        )

    def _build_sdf_spec_events(
        self,
        layer: Sdf.Layer,
        *,
        full_scan: bool,
    ) -> list[dict]:
        root_modes = dict(self._dirty_sdf_subtrees)
        roots = set(root_modes)
        specs = self._collect_sdf_specs(layer, roots, full_scan=full_scan)
        dirty = dict(self._dirty_sdf_specs)
        self._dirty_sdf_specs.clear()
        self._dirty_sdf_subtrees.clear()

        root_paths = [(Sdf.Path(value), resync) for value, resync in root_modes.items()]

        def _scan_mode(path: Sdf.Path) -> int:
            if full_scan:
                return 2
            mode = 0
            for root, resync in root_paths:
                if _sdf_path_is_under(path, root):
                    mode = max(mode, 2 if resync else 1)
            return mode

        candidates = set(dirty) | set(specs)
        candidates.update(key for key in self._sdf_spec_fields if _scan_mode(Sdf.Path(key[1])))

        events: list[dict] = []
        expression_variables = self.stage.GetMetadata("expressionVariables")
        resolver_context = self.stage.GetPathResolverContext()
        for spec_kind, path_string in candidates:
            key = (spec_kind, path_string)
            path = Sdf.Path(path_string)
            spec = specs.get(key)
            if spec is None:
                current = (
                    layer.pseudoRoot
                    if spec_kind == SDF_SPEC_KIND_LAYER
                    else layer.GetObjectAtPath(path)
                )
                if (
                    isinstance(current, _SDF_SPEC_TYPES)
                    and spec_kind_for_object(current) == spec_kind
                ):
                    spec = current

            previous_exists = key in self._sdf_spec_fields
            previous_fields = set(self._sdf_spec_fields.get(key, ()))
            if spec is None:
                if previous_exists or (key in dirty and spec_kind in _SDF_PROPERTY_KINDS):
                    removed_fields = set(previous_fields)
                    changed = dirty.get(key)
                    if changed:
                        removed_fields.update(changed)
                    events.append(
                        {
                            "k": K_SET_SDF_SPEC_FIELDS,
                            "prim": event_prim_path(path, spec_kind),
                            "spec_path": path_string,
                            "spec_kind": spec_kind,
                            "fields": sorted(removed_fields),
                            "fragment": "",
                            "removed": True,
                        }
                    )
                    self._sdf_spec_fields.pop(key, None)
                continue

            available_fields = self._generic_sdf_fields(spec, path, spec_kind)
            scan_mode = _scan_mode(path)
            changed_fields = dirty.get(key)
            if (
                scan_mode == 2
                or (key in dirty and changed_fields is None)
                or (scan_mode and not previous_exists)
            ):
                selected_fields = available_fields | previous_fields
            elif changed_fields is None:
                selected_fields = available_fields ^ previous_fields
            else:
                changed_fields = set(changed_fields)
                authored_fields = {str(field) for field in spec.ListInfoKeys()}
                selected_fields = changed_fields & (available_fields | previous_fields)
                selected_fields.update(changed_fields - authored_fields)

            requires_identity = self._sdf_spec_requires_identity(
                spec,
                path,
                spec_kind,
            )
            if selected_fields or (requires_identity and not previous_exists):
                fields = sorted(selected_fields)
                events.append(
                    {
                        "k": K_SET_SDF_SPEC_FIELDS,
                        "prim": event_prim_path(path, spec_kind),
                        "spec_path": path_string,
                        "spec_kind": spec_kind,
                        "fields": fields,
                        "fragment": serialize_spec_fields(
                            layer,
                            path,
                            spec_kind,
                            fields,
                            expression_variables=expression_variables,
                            resolver_context=resolver_context,
                        ),
                        "removed": False,
                    }
                )

            tracked = (previous_fields | selected_fields) & available_fields
            if tracked or requires_identity:
                self._sdf_spec_fields[key] = tracked
            else:
                self._sdf_spec_fields.pop(key, None)

        events.sort(key=_sdf_event_sort_key)
        return events

    def _classify_resync(self, notice, prim_path: str) -> str | None:
        """Classify a resync path into an action.

        Returns "rename", "delete", "deactivate", "remove_local_definition",
        "dirty", or None (skip). For renames, also appends to
        self._renamed_prims as a side effect.
        """
        if _PrimResyncType is not None:
            sdf_path = Sdf.Path(prim_path)
            resync_info = notice.GetPrimResyncType(sdf_path)
            resync_type = resync_info[0]
            associated_path = str(resync_info[1]) if len(resync_info) > 1 else ""

            if resync_type == _PrimResyncType.Delete:
                return "delete"
            if resync_type == _PrimResyncType.RenameSource:
                if associated_path and associated_path != ".":
                    self._renamed_prims.append((prim_path, associated_path))
                return "rename"
            if resync_type == _PrimResyncType.RenameDestination:
                return None

        # Fallback (or "Other" resync type with PrimResyncType available)
        prim = self.stage.GetPrimAtPath(prim_path)
        if prim and prim.IsValid():
            previous = self._local_prim_states.get(prim_path)
            current_definition = self._local_definition_spec(self._local_prim_specs(prim_path))
            if previous and previous.specifier != Sdf.SpecifierOver and current_definition is None:
                return "remove_local_definition"
            if not prim.IsActive() and prim_path in self._known_prims:
                return "deactivate"
            return "dirty"
        if prim_path in self._known_prims:
            return "delete"
        return None

    def _on_changed(self, notice, stage):
        if self._suppress_depth > 0:
            return

        resynced_paths = list(notice.GetResyncedPaths())
        changed_paths = list(notice.GetChangedInfoOnlyPaths())
        if not resynced_paths and not changed_paths:
            return
        edit_target = self._record_edit_target()
        edit_layer = edit_target.GetLayer()

        for p in resynced_paths:
            source_path = edit_target.MapToSpecPath(p)
            changed_fields = {str(field) for field in notice.GetChangedFields(p)}
            if p.IsPropertyPath():
                spec = edit_layer.GetObjectAtPath(source_path) if not source_path.isEmpty else None
                if isinstance(spec, Sdf.AttributeSpec):
                    self._mark_exact_sdf_spec(
                        source_path,
                        SDF_SPEC_KIND_ATTRIBUTE,
                        None,
                    )
                elif isinstance(spec, Sdf.RelationshipSpec):
                    self._mark_exact_sdf_spec(
                        source_path,
                        SDF_SPEC_KIND_RELATIONSHIP,
                        None,
                    )
                else:
                    self._mark_sdf_property_spec(
                        str(p),
                        None,
                        source_path=source_path,
                        source_layer=edit_layer,
                    )
            elif not source_path.isEmpty:
                spec = edit_layer.GetObjectAtPath(source_path)
                self._mark_sdf_subtree(
                    source_path,
                    resync=not changed_fields and bool(spec),
                )
                authored_fields = changed_fields - _SDF_STRUCTURAL_NOTICE_FIELDS
                if authored_fields and isinstance(spec, (Sdf.PrimSpec, Sdf.VariantSpec)):
                    self._mark_exact_sdf_spec(
                        source_path,
                        spec_kind_for_object(spec),
                        authored_fields,
                    )
            prim_path = _prim_path_from_notice_path(str(p))
            # Prototype prims are stage-local composition artifacts with
            # unstable names; toggling instanceable resyncs them alongside
            # the instance prim. Their paths must never reach the wire.
            if not prim_path or prim_path.startswith("/__Prototype"):
                continue
            action = self._classify_resync(notice, prim_path)
            if action == "delete":
                self._deleted_prims.add(prim_path)
            elif action == "deactivate":
                self._deactivated_prims.add(prim_path)
            elif action == "remove_local_definition":
                self._removed_local_definition_prims.add(prim_path)
                self._notice_resynced_prims.add(prim_path)
            elif action == "dirty":
                self.dirty.add(prim_path)
                self._notice_resynced_prims.add(prim_path)

        for p in changed_paths:
            path_str = str(p)
            # Stage-level metadata fires on the pseudo-root path. Inspect
            # the field tokens directly so unrelated edits (comments,
            # customLayerData) don't trigger a full snapshot diff.
            if path_str == "/":
                fields = {str(f) for f in notice.GetChangedFields(p)}
                if fields & _WATCHED_STAGE_METADATA_FIELDS:
                    self._stage_metadata_dirty = True
                generic_fields = fields - _WATCHED_STAGE_METADATA_FIELDS
                if generic_fields:
                    self._mark_exact_sdf_spec(
                        Sdf.Path.absoluteRootPath,
                        SDF_SPEC_KIND_LAYER,
                        generic_fields,
                    )
                continue
            prim_path = _prim_path_from_notice_path(path_str)
            if prim_path and not prim_path.startswith("/__Prototype"):
                self.dirty.add(prim_path)
                source_path = edit_target.MapToSpecPath(p)
                # Keep this unfiltered: channel gating needs names that the
                # generic gprim attr path will later ignore.
                if "." in path_str:
                    attr_name = path_str.split(".", 1)[1]
                    self._dirty_attrs.setdefault(prim_path, set()).add(attr_name)
                    # Time-sample edits set a bit-flag on the Sdf change
                    # entry rather than a per-path infoChanged field
                    # (pxr/usd/sdf/changeList.h). USD builds that don't
                    # translate the flag report an empty GetChangedFields
                    # for them; newer USD appends SdfFieldKeys->TimeSamples
                    # explicitly (pxr/usd/usd/notice.cpp). Accept both
                    # signatures when classifying sample-dirty attrs.
                    fields = notice.GetChangedFields(p)
                    sdf_fields = {str(field) for field in fields}
                    if not sdf_fields:
                        sdf_fields.add("timeSamples")
                    if sdf_fields <= _SDF_SPECIALIZED_FIELDS and not self._attr_filter(attr_name):
                        if not self._refresh_cached_local_property_fields(
                            prim_path,
                            attr_name,
                            path_str,
                            sdf_fields,
                            source_path=source_path,
                            source_layer=edit_layer,
                        ):
                            self._mark_local_property_fields(path_str, sdf_fields)
                    else:
                        spec = (
                            edit_layer.GetObjectAtPath(source_path)
                            if not source_path.isEmpty
                            else None
                        )
                        if isinstance(spec, Sdf.AttributeSpec):
                            kind = SDF_SPEC_KIND_ATTRIBUTE
                        elif isinstance(spec, Sdf.RelationshipSpec):
                            kind = SDF_SPEC_KIND_RELATIONSHIP
                        else:
                            kind = None
                        if kind is None:
                            self._mark_sdf_property_spec(
                                path_str,
                                sdf_fields,
                                source_path=source_path,
                                source_layer=edit_layer,
                            )
                        else:
                            self._mark_exact_sdf_spec(
                                source_path,
                                kind,
                                sdf_fields,
                            )
                    if not fields or any(str(f) == "timeSamples" for f in fields):
                        self._sample_dirty_attrs.setdefault(prim_path, set()).add(attr_name)
                else:
                    prim_fields = {str(field) for field in notice.GetChangedFields(p)}
                    spec = (
                        edit_layer.GetObjectAtPath(source_path) if not source_path.isEmpty else None
                    )
                    if not source_path.isEmpty and (
                        not prim_fields or bool(prim_fields & _SDF_STRUCTURAL_NOTICE_FIELDS)
                    ):
                        self._mark_sdf_subtree(source_path, resync=False)
                    authored_fields = prim_fields - _SDF_STRUCTURAL_NOTICE_FIELDS
                    if authored_fields and isinstance(spec, (Sdf.PrimSpec, Sdf.VariantSpec)):
                        self._mark_exact_sdf_spec(
                            source_path,
                            spec_kind_for_object(spec),
                            authored_fields,
                        )
                    if "inactiveIds" in prim_fields:
                        # Prim metadata, but transported by PointInstancer.
                        self._dirty_attrs.setdefault(prim_path, set()).add("inactiveIds")

    def mark_dirty(self, prim_path: str):
        """Manually mark a prim as dirty (useful for DCC integrations)."""
        self.dirty.add(prim_path)

    def invalidate_for_event(self, ev: dict) -> None:
        """Sync internal diff caches with a remotely-applied event.

        After a receiver applies a network event to the stage, the diff
        cache reflects pre-mutation state.  Pass each applied event
        through here so the next ``build_events_for_dirty()`` doesn't
        re-emit a change the server already knows about.

        Idempotent.  Safe inside or outside a ``suppressed()`` block.
        Unknown event kinds are no-ops.
        """
        k = ev.get("k")
        if not k:
            return
        fn = _INVALIDATE_DISPATCH.get(k)
        if fn is None:
            return
        # Stage-level events (no ``prim``) still dispatch — handlers ignore
        # the empty path arg.
        fn(self, ev.get("prim", ""), ev)

    def invalidate_for_events(self, events: list[dict]) -> None:
        """Batch version of :meth:`invalidate_for_event`."""
        for ev in events:
            self.invalidate_for_event(ev)

    def snapshot_events(self, eps_trs: float = 1e-9) -> list[dict]:
        """Build events for every prim on the stage as if newly authored.

        Marks every prim under the pseudo-root dirty and runs the normal
        build-events pipeline. Equivalent to walking ``Usd.PrimRange`` and
        calling ``mark_dirty`` on each path, then ``build_events_for_dirty``.

        Useful for initial-sync scenarios — a DCC plugin coming online with
        a populated stage, a replay harness reproducing a captured scene,
        or tests that need a full event stream for an authored stage.
        """
        self._record_edit_target()
        self._full_sdf_spec_scan = True
        for prim in Usd.PrimRange(self.stage.GetPseudoRoot()):
            path = str(prim.GetPath())
            if path != "/":
                self.mark_dirty(path)
        return self.build_events_for_dirty(eps_trs=eps_trs)

    def snapshot_prim(self, prim_path: str) -> dict | None:
        """Snapshot the current local transform of a prim as TRS."""
        prim = self.stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid():
            return None

        xf = UsdGeom.Xformable(prim)
        local_ret = xf.GetLocalTransformation(Usd.TimeCode.Default())
        local_m = as_matrix(local_ret)

        t, r, s = decompose_trs_from_matrix(local_m)

        return {
            "t": t,
            "r": r,
            "s": s,
        }

    def _know_prim(self, prim_path: str) -> None:
        if prim_path not in self._known_prims:
            self._known_prims.add(prim_path)
            bisect.insort(self._known_index, prim_path)

    def _forget_prim(self, prim_path: str) -> None:
        if prim_path in self._known_prims:
            self._known_prims.discard(prim_path)
            del self._known_index[bisect.bisect_left(self._known_index, prim_path)]

    def _purge_sdf_paths(
        self,
        prim_path: str,
        *,
        include_root: bool = True,
        include_descendants: bool,
        preserve_sdf: bool = False,
    ) -> None:
        prefix = prim_path + "/"
        for cache in (self._sdf_spec_fields, self._dirty_sdf_specs):
            for key in tuple(cache):
                kind, spec_path = key
                owner = event_prim_path(spec_path, kind)
                if (include_root and owner == prim_path) or (
                    include_descendants and owner.startswith(prefix)
                ):
                    if preserve_sdf:
                        continue
                    cache.pop(key, None)
        for spec_path in tuple(self._dirty_sdf_subtrees):
            owner = str(Sdf.Path(spec_path).GetPrimPath().StripAllVariantSelections())
            if (include_root and owner == prim_path) or (
                include_descendants and owner.startswith(prefix)
            ):
                if preserve_sdf:
                    continue
                self._dirty_sdf_subtrees.pop(spec_path, None)
        for spec_path in tuple(self._local_property_spec_fields):
            owner = str(Sdf.Path(spec_path).GetPrimPath())
            if (include_root and owner == prim_path) or (
                include_descendants and owner.startswith(prefix)
            ):
                self._local_property_spec_fields.pop(spec_path, None)
        for owner in tuple(self._dirty_local_property_fields):
            if (include_root and owner == prim_path) or (
                include_descendants and owner.startswith(prefix)
            ):
                self._dirty_local_property_fields.pop(owner, None)

    def _purge_subtree(
        self,
        prim_path: str,
        *,
        include_root: bool = True,
        preserve_sdf: bool = False,
    ) -> None:
        """Purge caches for every known descendant of prim_path, plus the
        prim itself unless include_root is False.

        Bisect range on the sorted index (prim paths are ASCII so
        prefix + U+FFFF is a tight exclusive upper bound) with one bulk
        slice deletion, instead of an O(N) startswith scan per event and
        an O(N) index memmove per purged prim.
        """
        prefix = prim_path + "/"
        lo = bisect.bisect_left(self._known_index, prefix)
        hi = bisect.bisect_left(self._known_index, prefix + "\uffff")
        descendants = self._known_index[lo:hi]
        del self._known_index[lo:hi]
        self._known_prims.difference_update(descendants)
        for path in descendants:
            self._local_prim_states.pop(path, None)
        self._purge_sdf_paths(
            prim_path,
            include_root=include_root,
            include_descendants=True,
            preserve_sdf=preserve_sdf,
        )
        for p in descendants:
            self._prim_cache.pop(p, None)
            self._dirty_attrs.pop(p, None)
            self._sample_dirty_attrs.pop(p, None)
            self.dirty.discard(p)
        if include_root:
            self._purge_caches(prim_path, preserve_sdf=preserve_sdf)

    def _migrate_caches(self, old_path: str, new_path: str):
        """Migrate all per-prim caches from old_path to new_path."""
        if old_path in self._known_prims:
            self._forget_prim(old_path)
            self._know_prim(new_path)
        if old_path in self._prim_cache:
            self._prim_cache[new_path] = self._prim_cache.pop(old_path)
        old_prefix = old_path + "/"
        for cache in (self._sdf_spec_fields, self._dirty_sdf_specs):
            moved = []
            for kind, spec_path in tuple(cache):
                owner = event_prim_path(spec_path, kind)
                if owner == old_path or owner.startswith(old_prefix):
                    suffix = spec_path[len(old_path) :]
                    moved.append(((kind, spec_path), (kind, new_path + suffix)))
            for source, target in moved:
                cache[target] = cache.pop(source)
        moved_subtrees = []
        for spec_path, resync in self._dirty_sdf_subtrees.items():
            owner = str(Sdf.Path(spec_path).GetPrimPath().StripAllVariantSelections())
            if owner == old_path or owner.startswith(old_prefix):
                moved_subtrees.append(
                    (
                        spec_path,
                        new_path + spec_path[len(old_path) :],
                        resync,
                    )
                )
        for source, target, resync in moved_subtrees:
            self._dirty_sdf_subtrees.pop(source, None)
            self._dirty_sdf_subtrees[target] = resync
        moved = []
        for spec_path in tuple(self._local_property_spec_fields):
            owner = str(Sdf.Path(spec_path).GetPrimPath())
            if owner == old_path or owner.startswith(old_prefix):
                suffix = spec_path[len(old_path) :]
                moved.append((spec_path, new_path + suffix))
        for source, target in moved:
            self._local_property_spec_fields[target] = self._local_property_spec_fields.pop(source)
        moved_local_fields = []
        for owner in tuple(self._dirty_local_property_fields):
            if owner == old_path or owner.startswith(old_prefix):
                suffix = owner[len(old_path) :]
                moved_local_fields.append((owner, new_path + suffix))
        for source, target in moved_local_fields:
            self._dirty_local_property_fields[target] = self._dirty_local_property_fields.pop(
                source
            )
        if old_path in self._local_prim_states:
            self._local_prim_states.pop(old_path)
            specs = self._local_prim_specs(new_path)
            ownership_spec = self._local_definition_spec(specs) or (specs[0] if specs else None)
            state = _local_prim_state(ownership_spec)
            if state:
                self._local_prim_states[new_path] = state

    def _purge_caches(self, prim_path: str, *, preserve_sdf: bool = False):
        """Remove all per-prim caches for a deactivated/deleted prim."""
        self._forget_prim(prim_path)
        self._prim_cache.pop(prim_path, None)
        self._dirty_attrs.pop(prim_path, None)
        self._sample_dirty_attrs.pop(prim_path, None)
        self._purge_sdf_paths(
            prim_path,
            include_descendants=False,
            preserve_sdf=preserve_sdf,
        )
        self._local_prim_states.pop(prim_path, None)
        self.dirty.discard(prim_path)

    def _build_rename_events(self) -> list[dict]:
        """Build rename events and migrate caches."""
        events: list[dict] = []
        renamed_now = list(self._renamed_prims)
        self._renamed_prims.clear()
        for old_path, new_path in renamed_now:
            previous = self._local_prim_states.get(old_path)
            if previous is None or previous.specifier == Sdf.SpecifierOver:
                self._purge_caches(old_path)
                continue
            new_name = new_path.rsplit("/", 1)[-1]
            events.append({"k": K_RENAME_PRIM, "prim": old_path, "new_name": new_name})
            self._migrate_caches(old_path, new_path)
            if old_path in self.dirty:
                self.dirty.discard(old_path)
                self.dirty.add(new_path)
        return events

    def _build_local_definition_removal_events(self) -> list[dict]:
        """Remove stale receiver defs when a weaker composed prim is revealed."""
        events: list[dict] = []
        removed_now = set(self._removed_local_definition_prims)
        self._removed_local_definition_prims.clear()
        for prim_path in removed_now:
            events.append({"k": K_DELETE_PRIM, "prim": prim_path})
            self._purge_subtree(prim_path, preserve_sdf=True)
            if self._local_prim_specs(prim_path):
                self.dirty.add(prim_path)
                self._notice_resynced_prims.add(prim_path)
        return events

    def _build_deactivation_events(self) -> list[dict]:
        """Build deactivation events for deactivated and deleted prims."""
        events: list[dict] = []
        deactivated_now = self._deactivated_prims | self._deleted_prims
        self._deactivated_prims.clear()
        self._deleted_prims.clear()
        for prim_path in deactivated_now:
            previous = self._local_prim_states.get(prim_path)
            current = self._local_prim_spec(prim_path)
            authors_active = bool(current and current.HasInfo("active"))
            if authors_active or (previous is not None and previous.specifier != Sdf.SpecifierOver):
                events.append({"k": K_DEACTIVATE_PRIM, "prim": prim_path, "active": False})
            self._purge_caches(prim_path, preserve_sdf=True)
        return events

    def _build_dirty_prim_events(
        self,
        prim_path: str,
        prim,
        eps_trs: float,
    ) -> list[dict]:
        """Build events for a single dirty prim. ``prim`` must be valid."""
        events: list[dict] = []
        pc = self._prim_cache.setdefault(prim_path, {})
        first_encounter = prim_path not in self._known_prims
        is_resync = prim_path in self._notice_resynced_prims
        local_specs = pc.get(_C_LOCAL_PRIM_SPECS)
        property_state = pc.get(_C_LOCAL_PROPERTY_STATE)
        blocked_values = pc.get(_C_LOCAL_BLOCKED_VALUE_FIELDS)
        local_field_changes = self._dirty_local_property_fields.pop(prim_path, None)
        dirty_property_names: set[str] | None = None
        refresh_local_state = (
            first_encounter or is_resync or local_specs is None or property_state is None
        )
        if refresh_local_state:
            local_specs = self._local_prim_specs(prim_path)
            property_state = self._local_property_state(prim_path, local_specs)
            pc[_C_LOCAL_PRIM_SPECS] = local_specs
            pc[_C_LOCAL_DEFINITION_SPEC] = self._local_definition_spec(local_specs)
            pc[_C_LOCAL_PROPERTY_STATE] = property_state
            blocked_values = {}
            self._refresh_blocked_value_fields(
                blocked_values,
                property_state[1],
                property_state[0],
            )
            pc[_C_LOCAL_BLOCKED_VALUE_FIELDS] = blocked_values
        else:
            if blocked_values is None:
                blocked_values = {}
                self._refresh_blocked_value_fields(
                    blocked_values,
                    property_state[1],
                    property_state[0],
                )
                pc[_C_LOCAL_BLOCKED_VALUE_FIELDS] = blocked_values
            dirty_paths = {
                path
                for kind, path in self._dirty_sdf_specs
                if kind in _SDF_PROPERTY_KINDS and event_prim_path(path, kind) == prim_path
            }
            if dirty_paths:
                dirty_property_names = {str(Sdf.Path(path).name) for path in dirty_paths}
            # Specialized properties do not enter the Sdf field-delta index,
            # but a notice for a newly created property still has to update
            # the cached edit-target ownership before filtering its event.
            # Existing properties stay on the cached hot path.
            for name in self._dirty_attrs.get(prim_path, ()):
                if name in property_state[0]:
                    continue
                if dirty_property_names is None:
                    dirty_property_names = set()
                dirty_property_names.add(name)
            if dirty_property_names:
                self._refresh_local_property_state(
                    local_specs,
                    property_state,
                    dirty_property_names,
                )
            if local_field_changes:
                self._refresh_local_property_fields(
                    local_specs,
                    property_state,
                    local_field_changes,
                )
            if dirty_property_names or local_field_changes:
                refreshed_names = set(dirty_property_names or ())
                if local_field_changes:
                    refreshed_names.update(local_field_changes)
                self._refresh_blocked_value_fields(
                    blocked_values,
                    property_state[1],
                    refreshed_names,
                )
        local_spec = local_specs[0] if local_specs else None
        local_definition_spec = pc.get(_C_LOCAL_DEFINITION_SPEC)
        previous_prim_state = self._local_prim_states.get(prim_path)
        local_properties, local_property_sources = property_state
        if local_field_changes:
            for name, changed_fields in local_field_changes.items():
                event_path = str(Sdf.Path(prim_path).AppendProperty(name))
                previous_fields = self._local_property_spec_fields.get(
                    event_path,
                    _EMPTY_FIELDS,
                )
                current_fields = local_properties.get(name, _EMPTY_FIELDS)
                candidates = previous_fields if changed_fields is None else changed_fields
                cleared_fields = (set(candidates) & previous_fields) - current_fields
                if cleared_fields:
                    # Fast-path events carry present values. A removed opinion
                    # needs the generic Sdf clear so the receiver exposes its
                    # weaker composed value instead of retaining stale data.
                    self._mark_sdf_property_spec(event_path, cleared_fields)
        if refresh_local_state:
            local_api_schemas = self._local_api_schemas(local_specs)
        else:
            local_api_schemas = pc.get(_C_API_SCHEMAS, _EMPTY_FIELDS)

        # Structural events on first encounter
        if first_encounter:
            local_definition = local_definition_spec is not None
            # An over normally needs no structural event: its defining prim
            # already composes through a reference, payload, or weaker layer.
            # Applied API schemas are prim metadata, so an over carrying one
            # still uses ensure_prim as the existing additive API handshake.
            if local_definition or local_api_schemas:
                type_name = str(local_definition_spec.typeName) if local_definition else ""
                events.append(
                    {
                        "k": K_ENSURE_PRIM,
                        "prim": prim_path,
                        "typeName": type_name,
                        "api_schemas": list(local_api_schemas),
                    }
                )
            pc[_C_API_SCHEMAS] = local_api_schemas
            if "xformOpOrder" in local_properties or any(
                name.startswith("xformOp:") for name in local_properties
            ):
                events.append({"k": K_ENSURE_XFORM_OPS, "prim": prim_path})
            self._know_prim(prim_path)
        else:
            current_type_name = (
                str(local_definition_spec.typeName) if local_definition_spec is not None else ""
            )
            definition_type_changed = (
                local_definition_spec is not None
                and previous_prim_state is not None
                and current_type_name != previous_prim_state.type_name
            )
            if definition_type_changed:
                events.append(
                    {
                        "k": K_ENSURE_PRIM,
                        "prim": prim_path,
                        "typeName": current_type_name,
                        "api_schemas": list(local_api_schemas),
                    }
                )
            # Re-emit ensure_prim when the applied api_schemas change (e.g.
            # ShapingAPI applied to an existing SphereLight to make it a spot).
            last_apis = pc.get(_C_API_SCHEMAS)
            current_apis = local_api_schemas
            if (
                not definition_type_changed
                and last_apis is not None
                and current_apis != last_apis
                and current_apis
            ):
                local_definition = local_definition_spec is not None
                type_name = str(local_definition_spec.typeName) if local_definition else ""
                events.append(
                    {
                        "k": K_ENSURE_PRIM,
                        "prim": prim_path,
                        "typeName": type_name,
                        "api_schemas": list(current_apis),
                    }
                )
            pc[_C_API_SCHEMAS] = current_apis

        # First encounter and resync both require a full channel read. The
        # dirty attr that woke the prim might be unrelated to already-authored
        # channel state, and resyncs do not provide reliable per-attr detail.
        # The gprim attr block below consumes the resync marker.
        dirty_attrs = self._dirty_attrs.get(prim_path)
        if first_encounter or prim_path in self._notice_resynced_prims:
            dirty_attrs = None
        # Attrs owned by a channel that applies to this prim. The global
        # _attr_filter only excludes filter_attrs; the gprim scan and
        # sample paths below additionally skip this per-prim set, which
        # covers channels whose names collide with other schemas.
        owned_attrs: set[str] = set()
        for channel in self._channels:
            if not channel.applies_to(prim):
                continue
            owned_attrs.update(channel.watched_attrs)
            if not channel.needs_read(dirty_attrs):
                continue
            partial = False
            current = None
            if dirty_attrs:
                current = channel.read_scoped(self.stage, prim_path, dirty_attrs)
                partial = current is not None
            if current is None:
                if channel.uses_local_property_sources:
                    current = channel.read_local(
                        self.stage,
                        prim_path,
                        local_property_sources,
                    )
                else:
                    current = channel.read(self.stage, prim_path)
            if current is None:
                continue
            _emit_channel_events(channel, prim_path, current, pc, events, partial)

        if local_field_changes:
            for name in local_field_changes:
                event_path = str(Sdf.Path(prim_path).AppendProperty(name))
                current_fields = local_properties.get(name)
                if current_fields:
                    self._local_property_spec_fields[event_path] = set(current_fields)
                else:
                    self._local_property_spec_fields.pop(event_path, None)
        if refresh_local_state or local_field_changes or dirty_property_names:
            pc[_C_LOCAL_PROPERTY_NAMES] = set(local_properties)

        # TRS keeps a specialized path (not a PrimChannel): the diff needs a
        # matrix decompose snapshot, which is only worth computing for prims
        # with authored xform ops. Materials/Shaders/Scopes skip it.
        xf = UsdGeom.Xformable(prim)
        has_xform = xf and xf.GetXformOpOrderAttr().IsAuthored()

        if has_xform:
            snap = self.snapshot_prim(prim_path)
            last_trs = pc.get(_C_TRS, {})
            fields = []
            payload = {"k": K_SET_XFORM_TRS, "prim": prim_path, "fields": fields}

            if not near_list(snap["t"], last_trs.get("t"), eps_trs):
                fields.append("t")
                payload["t"] = snap["t"]
            if not near_list(snap["r"], last_trs.get("r"), eps_trs):
                fields.append("r")
                payload["r"] = snap["r"]
            if not near_list(snap["s"], last_trs.get("s"), eps_trs):
                fields.append("s")
                payload["s"] = snap["s"]

            if fields:
                events.append(payload)
                pc[_C_TRS] = {"t": snap["t"], "r": snap["r"], "s": snap["s"]}

        # Gprim attribute diff + time-sample emission share the dirty-attr
        # bookkeeping. had_notice_detail records whether the names came from
        # real per-attr notices (reliable sample classification) or from the
        # full-scan expansion below (no per-attr detail available).
        dirty_attr_names = self._dirty_attrs.pop(prim_path, set())
        sample_dirty = self._sample_dirty_attrs.pop(prim_path, set())
        had_notice_detail = bool(dirty_attr_names)
        self._notice_resynced_prims.discard(prim_path)
        last_attrs = pc.get(_C_GPRIM_ATTRS, {})

        # Full attr scan: needed on first encounter (cache empty) or
        # after a resync notice (variant switch, structural change).
        # Skipped for plain info-only changes to avoid reading thousands
        # of mesh vertices every frame.
        if not dirty_attr_names and (not last_attrs or is_resync):
            for attr in prim.GetAttributes():
                name = attr.GetName()
                if (
                    attr.IsAuthored()
                    and self._attr_filter(name)
                    and name not in owned_attrs
                    and not self._sdf_owns_attribute_value(prim, name)
                ):
                    dirty_attr_names.add(name)

        changed_attrs = {}
        primvar_meta: dict = {}
        attr_interp: dict = {}
        for attr_name in dirty_attr_names:
            # _dirty_attrs is unfiltered for channel gating; filter again
            # here before emitting generic gprim attrs.
            if not self._attr_filter(attr_name) or attr_name in owned_attrs:
                continue
            if self._sdf_owns_attribute_value(prim, attr_name):
                continue
            attr = prim.GetAttribute(attr_name)
            if not attr or not attr.IsValid():
                continue
            value_source = local_property_sources.get(attr_name, {}).get("default")
            if isinstance(value_source, Sdf.AttributeSpec) and value_source.HasDefaultValue():
                value = value_source.default
                source_layer = value_source.layer
            else:
                value = attr.Get()
                source_layer = self.stage.GetEditTarget().GetLayer()
                if value_contains_asset_path(value):
                    source_layer = _attribute_default_source_layer(attr)
                    if source_layer is None:
                        continue
            val = _usd_value_to_transport_python(
                self.stage,
                source_layer,
                value,
            )
            if val is None:
                continue
            if not _values_equal(val, last_attrs.get(attr_name)):
                changed_attrs[attr_name] = val
                pvm, ai = _attr_event_metadata(prim, attr_name, attr)
                primvar_meta.update(pvm)
                attr_interp.update(ai)

        if changed_attrs:
            ev = {
                "k": K_SET_GPRIM_ATTRS,
                "prim": prim_path,
                "attrs": changed_attrs,
            }
            if primvar_meta:
                ev["primvar_meta"] = primvar_meta
            if attr_interp:
                ev["attr_interp"] = attr_interp
            events.append(ev)
            pc.setdefault(_C_GPRIM_ATTRS, {}).update(changed_attrs)

        # With per-attr notice detail, only attrs classified sample-dirty
        # need their sample tables re-read; a default-time drag on a
        # keyframed prim then skips sample diffing entirely. Without
        # detail (first encounter, resync, manual dirty), scan everything:
        # the gprim expansion above is filter-scoped and would hide
        # transform ops and connectable inputs from the sample paths.
        sample_attrs = sample_dirty if had_notice_detail else None
        events.extend(
            self._build_time_sample_events(prim_path, prim, pc, sample_attrs, owned_attrs),
        )

        ownership_spec = local_definition_spec or local_spec
        if refresh_local_state or previous_prim_state is None:
            ownership_state = _local_prim_state(ownership_spec)
            if ownership_state:
                self._local_prim_states[prim_path] = ownership_state
            else:
                self._local_prim_states.pop(prim_path, None)
        return self._filter_events_to_local_opinions(
            events,
            local_specs,
            local_properties,
            blocked_values,
        )

    def _build_time_sample_events(
        self,
        prim_path: str,
        prim,
        pc: dict,
        sample_attrs: set[str] | None,
        owned_attrs: set[str] = frozenset(),
    ) -> list[dict]:
        """One event per ``(attr, time)`` for the time-sampleable attrs on a
        prim: xformOps, visibility, watched gprim attrs, and UsdShade
        input attrs.

        ``sample_attrs`` restricts re-reads to attrs whose sample tables
        changed this cycle, classified in ``_on_changed`` from the
        time-sample signature of ``GetChangedFields`` (empty list or an
        explicit ``timeSamples`` field, depending on USD version).
        ``None`` means full-scan (first encounter, resync, or no per-attr
        notice detail); an empty set skips sample diffing entirely.
        """
        if not prim or not prim.IsValid():
            return []
        full_scan = sample_attrs is None
        if not full_scan and not sample_attrs:
            return []
        dirty_attr_names = sample_attrs or set()
        events: list[dict] = []
        ts_cache: dict = pc.setdefault(_C_TIME_SAMPLES, {})
        # Per-client / stacked-layer setups: emit only samples authored on
        # the stage's current edit target. The composed view would let a
        # stronger client's samples leak into a weaker client's emit cycle
        # (USD's "strongest layer with samples wins the time domain" rule
        # also shadows the weaker layer's own samples — both fail modes
        # disappear once we read from the layer directly).
        edit_layer = self.stage.GetEditTarget().GetLayer()

        events.extend(
            self._xform_op_sample_events(
                prim_path,
                prim,
                ts_cache,
                dirty_attr_names,
                full_scan,
                edit_layer,
            )
        )
        events.extend(
            self._visibility_sample_events(
                prim_path,
                prim,
                ts_cache,
                dirty_attr_names,
                full_scan,
                edit_layer,
            )
        )
        events.extend(
            self._gprim_attr_sample_events(
                prim_path,
                prim,
                ts_cache,
                dirty_attr_names,
                full_scan,
                edit_layer,
                owned_attrs,
            )
        )
        events.extend(
            self._connectable_input_sample_events(
                prim_path,
                prim,
                ts_cache,
                dirty_attr_names,
                full_scan,
                edit_layer,
            )
        )
        events.extend(
            self._point_instancer_sample_events(
                prim_path,
                prim,
                ts_cache,
                dirty_attr_names,
                full_scan,
                edit_layer,
            )
        )
        return events

    def _xform_op_sample_events(
        self,
        prim_path,
        prim,
        ts_cache,
        dirty_attr_names,
        full_scan,
        layer,
    ) -> list[dict]:
        xf = UsdGeom.Xformable(prim)
        if not xf or not xf.GetXformOpOrderAttr().IsAuthored():
            return []
        ops = list(xf.GetOrderedXformOps())
        fields = [_canonical_trs_field(op) for op in ops]
        # A sampled non-canonical op (matrix transform, euler, pivot,
        # inverse) cannot ride a per-op TRS field; the whole stack folds
        # through the matrix decompose the default-time path uses.
        # Canonical-only stacks short-circuit before any layer query.
        for op, field in zip(ops, fields, strict=True):
            if field is None and _has_layer_samples(layer, op.GetAttr()):
                return self._decomposed_xform_sample_events(
                    prim_path,
                    xf,
                    ops,
                    ts_cache,
                    dirty_attr_names,
                    full_scan,
                    layer,
                )

        events: list[dict] = []
        for op, field in zip(ops, fields, strict=True):
            if field is None:
                continue
            attr = op.GetAttr()
            op_name = attr.GetName()
            if not full_scan and op_name not in dirty_attr_names:
                continue
            if not _has_layer_samples(layer, attr):
                continue
            new_cache, dirty = _diff_time_samples(attr, ts_cache.get(op_name), layer)
            for t, val in dirty:
                events.append(
                    {
                        "k": K_SET_XFORM_TRS,
                        "prim": prim_path,
                        "fields": [field],
                        field: val,
                        "time": float(t),
                    }
                )
            ts_cache[op_name] = new_cache
        return events

    def _decomposed_xform_sample_events(
        self,
        prim_path,
        xf,
        ops,
        ts_cache,
        dirty_attr_names,
        full_scan,
        layer,
    ) -> list[dict]:
        """One full TRS event per dirty sample time, via matrix decompose.

        The composed local transformation folds every op in the stack at
        that time regardless of op type. Decompose cannot represent shear;
        the default-time path shares that limit. Reads compose across the
        whole layer stack, unlike the per-op path's edit-layer scoping.
        """
        import numpy as np

        dirty_times: set[float] = set()
        sampled_indices: list[int] = []
        matrix_dirty: dict[str, dict[float, object]] = {}
        for i, op in enumerate(ops):
            attr = op.GetAttr()
            if not _has_layer_samples(layer, attr):
                continue
            sampled_indices.append(i)
            name = attr.GetName()
            if not full_scan and name not in dirty_attr_names:
                continue
            new_cache, dirty = _diff_time_samples(
                attr,
                ts_cache.get(name),
                layer,
                convert=xform_sample_value,
            )
            dirty_times.update(float(t) for t, _val in dirty)
            if op.GetOpType() == UsdGeom.XformOp.TypeTransform:
                matrix_dirty[name] = {float(t): val for t, val in dirty}
            ts_cache[name] = new_cache
        if not dirty_times:
            return []

        times = sorted(dirty_times)
        single = sampled_indices[0] if len(sampled_indices) == 1 else None
        sampled_vals = (
            matrix_dirty.get(ops[single].GetAttr().GetName())
            if single is not None and not ops[single].IsInverseOp()
            else None
        )
        if sampled_vals is not None:
            # local = ops[n-1] * ... * ops[0] (row-vector convention), so a
            # single sampled matrix op sandwiches between two static
            # products computed once, skipping per-time USD resolution.
            default_tc = Usd.TimeCode.Default()
            pre = Gf.Matrix4d(1.0)
            for op in ops[:single]:
                pre = op.GetOpTransform(default_tc) * pre
            post = Gf.Matrix4d(1.0)
            for op in ops[single + 1 :]:
                post = op.GetOpTransform(default_tc) * post
            # With a single sampled op, every dirty time came from its own
            # diff, so the lookup cannot miss; a KeyError here means a new
            # dirty_times source broke that invariant.
            stack = np.stack([sampled_vals[t] for t in times])
            locals_np = np.array(post) @ stack @ np.array(pre)
        else:
            locals_np = np.stack(
                [
                    np.array(as_matrix(xf.GetLocalTransformation(ops, Usd.TimeCode(t))))
                    for t in times
                ]
            )

        translates, rotates, scales = decompose_trs_batch(locals_np)
        return [
            {
                "k": K_SET_XFORM_TRS,
                "prim": prim_path,
                "fields": ["t", "r", "s"],
                "t": translates[i].tolist(),
                "r": rotates[i].tolist(),
                "s": scales[i].tolist(),
                "time": t,
            }
            for i, t in enumerate(times)
        ]

    def _visibility_sample_events(
        self,
        prim_path,
        prim,
        ts_cache,
        dirty_attr_names,
        full_scan,
        layer,
    ) -> list[dict]:
        if not full_scan and "visibility" not in dirty_attr_names:
            return []
        imageable = UsdGeom.Imageable(prim)
        vis_attr = imageable.GetVisibilityAttr() if imageable else None
        if not vis_attr or not vis_attr.IsValid() or not _has_layer_samples(layer, vis_attr):
            return []
        new_cache, dirty = _diff_time_samples(vis_attr, ts_cache.get("visibility"), layer)
        events = [
            {
                "k": K_SET_VISIBILITY,
                "prim": prim_path,
                "visible": val != "invisible",
                "time": float(t),
            }
            for t, val in dirty
        ]
        ts_cache["visibility"] = new_cache
        return events

    def _gprim_attr_sample_events(
        self,
        prim_path,
        prim,
        ts_cache,
        dirty_attr_names,
        full_scan,
        layer,
        owned_attrs: set[str] = frozenset(),
    ) -> list[dict]:
        if full_scan:
            attr_names = [
                a.GetName()
                for a in prim.GetAttributes()
                if a.IsAuthored()
                and self._attr_filter(a.GetName())
                and a.GetName() not in owned_attrs
                and not self._sdf_owns_attribute_value(prim, a.GetName())
            ]
        else:
            attr_names = [
                n
                for n in dirty_attr_names
                if self._attr_filter(n)
                and n not in owned_attrs
                and not self._sdf_owns_attribute_value(prim, n)
            ]
        events: list[dict] = []

        def _convert(value):
            return _usd_value_to_transport_python(self.stage, layer, value)

        for name in attr_names:
            attr = prim.GetAttribute(name)
            if not attr or not attr.IsValid() or not _has_layer_samples(layer, attr):
                continue
            new_cache, dirty = _diff_time_samples(
                attr,
                ts_cache.get(name),
                layer,
                convert=_convert,
            )
            if dirty:
                primvar_meta, attr_interp = _attr_event_metadata(prim, name, attr)
                for t, val in dirty:
                    ev_out: dict = {
                        "k": K_SET_GPRIM_ATTRS,
                        "prim": prim_path,
                        "attrs": {name: val},
                        "time": float(t),
                    }
                    if primvar_meta:
                        ev_out["primvar_meta"] = primvar_meta
                    if attr_interp:
                        ev_out["attr_interp"] = attr_interp
                    events.append(ev_out)
            ts_cache[name] = new_cache
        return events

    def _connectable_input_sample_events(
        self,
        prim_path,
        prim,
        ts_cache,
        dirty_attr_names,
        full_scan,
        layer,
    ) -> list[dict]:
        kind = _connectable_kind(prim)
        if not kind:
            return []
        info_id = UsdShade.Shader(prim).GetIdAttr().Get() or "" if kind == "shader" else ""

        def _convert(value):
            return _usd_value_to_transport_python(self.stage, layer, value)

        events: list[dict] = []
        for inp in UsdShade.ConnectableAPI(prim).GetInputs():
            attr = inp.GetAttr()
            if not attr.IsAuthored() or inp.HasConnectedSource():
                continue
            if not _has_layer_samples(layer, attr):
                continue
            name = inp.GetBaseName()
            cache_key = "inputs:" + name
            if not full_scan and cache_key not in dirty_attr_names:
                continue
            type_name = str(attr.GetTypeName())
            new_cache, dirty = _diff_time_samples(
                attr,
                ts_cache.get(cache_key),
                layer,
                convert=_convert,
            )
            for t, val in dirty:
                events.append(
                    {
                        "k": K_SET_CONNECTABLE_INPUT,
                        "prim": prim_path,
                        "info_id": info_id,
                        "inputs": {name: val},
                        "input_types": {name: type_name},
                        "time": float(t),
                    }
                )
            ts_cache[cache_key] = new_cache
        return events

    def _point_instancer_sample_events(
        self,
        prim_path,
        prim,
        ts_cache,
        dirty_attr_names,
        full_scan,
        layer,
    ) -> list[dict]:
        if not prim.IsA(UsdGeom.PointInstancer):
            return []
        by_time: dict[float, dict] = {}
        for usd_name, wire_name in _PI_USD_TO_WIRE.items():
            if not full_scan and usd_name not in dirty_attr_names:
                continue
            attr = prim.GetAttribute(usd_name)
            if not attr or not attr.IsValid() or not _has_layer_samples(layer, attr):
                continue
            new_cache, dirty = _diff_time_samples(attr, ts_cache.get(usd_name), layer)
            for t, val in dirty:
                if usd_name in _PI_QUAT_ATTRS:
                    val = _quat_array_to_wire(val)
                    if val is None:
                        continue
                by_time.setdefault(float(t), {})[wire_name] = val
            ts_cache[usd_name] = new_cache
        # The prototypes rel is uniform and never rides timed events.
        return [
            {
                "k": K_SET_POINT_INSTANCER,
                "prim": prim_path,
                "fields": list(fields),
                **fields,
                "time": t,
            }
            for t, fields in sorted(by_time.items())
        ]

    def _build_stage_metadata_events(self) -> list[dict]:
        """Emit a SetStageMetadata event when the stage's units/timeline change."""
        if not self._stage_metadata_dirty:
            return []
        self._stage_metadata_dirty = False
        current = read_stage_metadata(self.stage)
        changed = {
            key: val for key, val in current.items() if self._stage_metadata_cache.get(key) != val
        }
        self._stage_metadata_cache = current
        if not changed:
            return []
        return [{"k": K_SET_STAGE_METADATA, **changed}]

    def _build_events_for_dirty_current_target(
        self,
        eps_trs: float,
    ) -> list[dict]:
        """Build events for all dirty prims, diffing against last-sent state.

        Returns a list of event dicts (ensure_prim, ensure_xform_ops, set_xform_trs,
        rename_prim, deactivate_prim) ready to wrap in a transaction.

        Processing order: stage metadata first, then renames, then deactivations/
        deletions, then per-prim work.
        """
        events: list[dict] = []

        events.extend(self._build_stage_metadata_events())
        events.extend(self._build_rename_events())
        events.extend(self._build_local_definition_removal_events())
        events.extend(self._build_deactivation_events())

        # A composition resync (variant flip, reference/payload swap) on a
        # parent path invalidates the whole subtree but Sdf reports only the
        # parent. Walk the now-composed subtree so descendants re-diff
        # (binding rels that moved) or first-encounter-emit (content that
        # was hidden by the prior variant selection).
        for prim_path in list(self._notice_resynced_prims):
            prim = self.stage.GetPrimAtPath(prim_path)
            if not (prim and prim.IsValid()):
                continue
            for desc in Usd.PrimRange(prim):
                if desc.GetPath() == prim.GetPath() or desc.IsInPrototype():
                    continue
                desc_path = str(desc.GetPath())
                if desc_path.startswith("/__Prototype"):
                    continue
                self.dirty.add(desc_path)
                self._notice_resynced_prims.add(desc_path)

        # Dirty prims (creation + TRS changes)
        # Sort by path depth so parents are emitted before children.
        dirty_now = sorted(self.dirty, key=lambda p: p.count("/"))
        self.dirty.clear()

        for prim_path in dirty_now:
            prim = self.stage.GetPrimAtPath(prim_path)
            if prim and prim.IsValid() and prim.IsInPrototype():
                self._dirty_attrs.pop(prim_path, None)
                self._sample_dirty_attrs.pop(prim_path, None)
                self._notice_resynced_prims.discard(prim_path)
                continue
            if not prim or not prim.IsValid():
                # Prim vanished between notice and build; drop its pending
                # per-attr state so the dicts don't accumulate dead paths.
                self._dirty_attrs.pop(prim_path, None)
                self._sample_dirty_attrs.pop(prim_path, None)
                self._notice_resynced_prims.discard(prim_path)
                continue
            events.extend(self._build_dirty_prim_events(prim_path, prim, eps_trs))

        if self._full_sdf_spec_scan or self._dirty_sdf_specs or self._dirty_sdf_subtrees:
            events.extend(
                self._build_sdf_spec_events(
                    self.stage.GetEditTarget().GetLayer(),
                    full_scan=self._full_sdf_spec_scan,
                )
            )
        return events

    def build_events_for_dirty(self, eps_trs: float = 1e-9) -> list[dict]:
        """Build one transaction from edits authored into one edit target.

        USD notices are synchronous, so the edit target active during each
        change is captured before callers can switch the stage elsewhere.
        A transaction containing edits from more than one layer is rejected.
        """
        if self._edit_target_conflict:
            raise RuntimeError("one emitter batch contains edits from multiple USD layers")

        source_target = self._pending_edit_target or self.stage.GetEditTarget()
        original_target = self.stage.GetEditTarget()
        changed_target = source_target != original_target
        if changed_target:
            self.stage.SetEditTarget(source_target)

        succeeded = False
        try:
            events = self._build_events_for_dirty_current_target(eps_trs)
            succeeded = True
            return events
        finally:
            if changed_target:
                self.stage.SetEditTarget(original_target)
            if succeeded:
                self._pending_edit_target = None
                self._edit_target_conflict = False
                self._full_sdf_spec_scan = False
