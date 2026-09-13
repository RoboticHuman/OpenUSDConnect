"""Apply protocol events to a ``Usd.Stage``."""

from __future__ import annotations

import logging

import numpy as np
from pxr import Gf, Sdf, Sdr, Usd, UsdGeom, UsdShade, Vt

from . import events as _events
from .connectable_attrs import ConnectableAttr
from .events import Event, register_applier
from .protocol_constants import (
    CREATE_KINDS,
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_ERASE_TIME_SAMPLES,
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
    REL_MATERIAL_BINDING,
    STRUCTURAL_EVENT_KINDS,
)
from .sdf_arc_state import apply_arc_state

LOG = logging.getLogger(__name__)

_TIME_DEFAULT = Usd.TimeCode.Default()


def _timecode(ev: dict) -> Usd.TimeCode:
    """Return ``Usd.TimeCode(ev["time"])`` or ``Usd.TimeCode.Default()`` when absent."""
    t = ev.get("time")
    if t is None:
        return _TIME_DEFAULT
    return Usd.TimeCode(float(t))


def get_or_define_prim(
    stage: Usd.Stage,
    prim_path: str,
    type_name: str = "Xform",
    *,
    ensure_local_definition: bool = False,
) -> Usd.Prim:
    """Return a prim, optionally ensuring a local ``def`` in the edit target."""
    prim = stage.GetPrimAtPath(prim_path)
    if not prim:
        return stage.DefinePrim(prim_path, type_name)
    if not ensure_local_definition:
        return prim

    edit_target = stage.GetEditTarget()
    spec_path = edit_target.MapToSpecPath(Sdf.Path(prim_path))
    if spec_path.isEmpty:
        raise ValueError(f"Edit target cannot map prim path {prim_path!r}")

    layer = edit_target.GetLayer()
    spec = layer.GetPrimAtPath(spec_path)
    needs_definition = spec is None or spec.specifier != Sdf.SpecifierDef
    needs_type = bool(type_name) and (spec is None or spec.typeName != type_name)
    if needs_definition or needs_type:
        with Sdf.ChangeBlock():
            if spec is None:
                spec = Sdf.CreatePrimInLayer(layer, spec_path)
            spec.specifier = Sdf.SpecifierDef
            if needs_type:
                spec.typeName = type_name
        prim = stage.GetPrimAtPath(prim_path)
    return prim


def find_op(xf: UsdGeom.Xformable, op_base: str) -> UsdGeom.XformOp | None:
    """Return a canonical xform op by its base name."""
    attr = xf.GetPrim().GetAttribute(f"xformOp:{op_base}")
    if attr:
        return UsdGeom.XformOp(attr)
    return None


_xform_path_cache: dict[str, tuple[Sdf.Path, Sdf.Path, Sdf.Path, Sdf.Path]] = {}


def _get_xform_paths(
    prim_path: Sdf.Path,
) -> tuple[Sdf.Path, Sdf.Path, Sdf.Path, Sdf.Path]:
    """Return cached translate, orient, scale, and order property paths."""
    cache_key = str(prim_path)
    paths = _xform_path_cache.get(cache_key)
    if paths is None:
        paths = (
            prim_path.AppendProperty("xformOp:translate"),
            prim_path.AppendProperty("xformOp:orient"),
            prim_path.AppendProperty("xformOp:scale"),
            prim_path.AppendProperty("xformOpOrder"),
        )
        _xform_path_cache[cache_key] = paths
    return paths


_XFORM_OP_SPECS = [
    ("xformOp:translate", Sdf.ValueTypeNames.Double3),
    ("xformOp:orient", Sdf.ValueTypeNames.Quatf),
    ("xformOp:scale", Sdf.ValueTypeNames.Float3),
]
_CANONICAL_XFORM_OP_ORDER = [name for name, _type_name in _XFORM_OP_SPECS]


def _author_canonical_ops(
    layer: Sdf.Layer,
    prim_path: Sdf.Path,
    property_paths: tuple[Sdf.Path, Sdf.Path, Sdf.Path, Sdf.Path],
) -> None:
    """Author missing canonical op specs and their order in ``layer``."""
    prim_spec = layer.GetPrimAtPath(prim_path)
    *op_paths, order_path = property_paths
    with Sdf.ChangeBlock():
        if prim_spec is None:
            # A composed prim still needs a local over to hold these opinions.
            prim_spec = Sdf.CreatePrimInLayer(layer, prim_path)
        for property_path, (name, type_name) in zip(
            op_paths,
            _XFORM_OP_SPECS,
            strict=True,
        ):
            if layer.GetAttributeAtPath(property_path) is None:
                Sdf.AttributeSpec(prim_spec, name, type_name)

        order_attr = layer.GetAttributeAtPath(order_path)
        if order_attr is None:
            order_attr = Sdf.AttributeSpec(
                prim_spec,
                "xformOpOrder",
                Sdf.ValueTypeNames.TokenArray,
            )
            order_attr.SetInfo("variability", Sdf.VariabilityUniform)
        order_attr.default = _CANONICAL_XFORM_OP_ORDER


def ensure_canonical_ops(stage: Usd.Stage, prim_path: str, op_cache=None):
    """Ensure a local translate/orient/scale stack and return its op handles.

    Existing composed ops are re-authored in the current edit target. A cache
    hit is valid only when that target already owns the complete stack.
    """
    prim = stage.GetPrimAtPath(prim_path)
    if not prim:
        prim = stage.DefinePrim(prim_path, "Xform")
    xf = UsdGeom.Xformable(prim)

    edit_target = stage.GetEditTarget()
    spec_path = edit_target.MapToSpecPath(Sdf.Path(prim_path))
    if spec_path.isEmpty:
        raise ValueError(f"Edit target cannot map prim path {prim_path!r}")
    property_paths = _get_xform_paths(spec_path)
    layer = edit_target.GetLayer()
    *op_paths, order_path = property_paths
    order_spec = layer.GetAttributeAtPath(order_path)
    has_local_ops = all(layer.GetAttributeAtPath(path) is not None for path in op_paths)
    has_canonical_order = (
        order_spec is not None and order_spec.default == _CANONICAL_XFORM_OP_ORDER
    )
    has_local_stack = has_local_ops and has_canonical_order

    if has_local_stack:
        cached = op_cache.get(prim_path) if op_cache is not None else None
        if cached and all(op is not None for op in cached):
            return prim, xf, cached[0], cached[1], cached[2]
    else:
        _author_canonical_ops(layer, spec_path, property_paths)

    # Resolve by property name: a stronger layer may mask this target's
    # xformOpOrder while its local values still need to be authored.
    t = find_op(xf, "translate")
    o = find_op(xf, "orient")
    s = find_op(xf, "scale")

    return prim, xf, t, o, s


def quatf_from_wxyz(q) -> Gf.Quatf:
    """Convert [w, x, y, z] list to Gf.Quatf."""
    w, x, y, z = map(float, q)
    return Gf.Quatf(w, Gf.Vec3f(x, y, z))


def _ensure_primvar_attr(
    name: str, meta: dict, pvapi: UsdGeom.PrimvarsAPI
) -> Usd.Attribute | None:
    """Create a primvar attribute from metadata if it doesn't exist yet.

    Returns the attribute (newly created or existing), or None on failure.
    """
    sdf_type = Sdf.ValueTypeNames.Find(meta["typeName"])
    if not sdf_type:
        return None
    pv_name = name[len("primvars:") :]
    interp = meta.get("interpolation", "")
    pv = pvapi.CreatePrimvar(pv_name, sdf_type, interp)
    return pv.GetAttr()


_VEC3F_ARRAY_TYPES = frozenset(
    {"float3[]", "vector3f[]", "normal3f[]", "point3f[]", "color3f[]"}
)
_VEC2F_ARRAY_TYPES = frozenset({"float2[]", "texCoord2f[]"})
_VEC3F_TYPES = frozenset({"float3", "vector3f", "normal3f", "point3f", "color3f"})
_VEC2F_TYPES = frozenset({"float2", "texCoord2f"})


def _coerce_numpy_gprim_value(type_name: str, value: np.ndarray):
    if type_name in _VEC3F_ARRAY_TYPES:
        array = value.reshape(-1, 3).astype(np.float32, copy=False)
        return Vt.Vec3fArray.FromNumpy(array)
    if type_name in _VEC2F_ARRAY_TYPES:
        array = value.reshape(-1, 2).astype(np.float32, copy=False)
        return Vt.Vec2fArray.FromNumpy(array)
    if type_name == "int[]":
        return Vt.IntArray.FromNumpy(value.ravel().astype(np.int32, copy=False))
    if type_name == "float[]":
        return Vt.FloatArray.FromNumpy(value.ravel().astype(np.float32, copy=False))
    if type_name in _VEC3F_TYPES and value.size == 3:
        # Boost.Python vector constructors do not accept numpy scalar types.
        x, y, z = value.flat
        return Gf.Vec3f(float(x), float(y), float(z))
    if type_name in _VEC2F_TYPES and value.size == 2:
        x, y = value.flat
        return Gf.Vec2f(float(x), float(y))
    if type_name == "double3" and value.size == 3:
        x, y, z = value.flat
        return Gf.Vec3d(float(x), float(y), float(z))
    return value.tolist()


def _coerce_list_gprim_value(type_name: str, value: list):
    if type_name in _VEC3F_ARRAY_TYPES:
        return Vt.Vec3fArray([Gf.Vec3f(*item) for item in value])
    if type_name in _VEC2F_ARRAY_TYPES:
        return Vt.Vec2fArray([Gf.Vec2f(*item) for item in value])
    if type_name == "int[]":
        return Vt.IntArray(value)
    if type_name == "float[]":
        return Vt.FloatArray(value)
    if type_name in _VEC3F_TYPES and len(value) == 3:
        return Gf.Vec3f(*value)
    if type_name in _VEC2F_TYPES and len(value) == 2:
        return Gf.Vec2f(*value)
    if type_name == "double3" and len(value) == 3:
        return Gf.Vec3d(*value)
    return value


def _set_gprim_attr(prim: Usd.Prim, name: str, value, time: Usd.TimeCode = _TIME_DEFAULT) -> None:
    """Set an existing gprim attribute using its declared USD type."""
    attr = prim.GetAttribute(name)
    if not attr:
        return
    type_name = str(attr.GetTypeName())
    if isinstance(value, np.ndarray):
        value = _coerce_numpy_gprim_value(type_name, value)
    elif isinstance(value, list):
        value = _coerce_list_gprim_value(type_name, value)
    attr.Set(value, time)


@register_applier(K_SET_XFORM_TRS)
def _apply_set_xform_trs(stage: Usd.Stage, ev: dict, op_cache=None) -> None:
    prim_path = ev["prim"]
    cached = op_cache.get(prim_path) if op_cache else None
    if cached:
        t_op, o_op, s_op = cached
    else:
        prim = stage.GetPrimAtPath(prim_path)
        if not prim:
            return
        xf = UsdGeom.Xformable(prim)
        t_op = find_op(xf, "translate")
        o_op = find_op(xf, "orient")
        s_op = find_op(xf, "scale")
        if op_cache is not None:
            op_cache[prim_path] = (t_op, o_op, s_op)

    fields = ev.get("fields", [])
    tc = _timecode(ev)
    if "t" in fields and t_op:
        x, y, z = ev["t"]
        t_op.Set(Gf.Vec3d(float(x), float(y), float(z)), tc)
    if "r" in fields and o_op:
        o_op.Set(quatf_from_wxyz(ev["r"]), tc)
    if "s" in fields and s_op:
        x, y, z = ev["s"]
        s_op.Set(Gf.Vec3d(float(x), float(y), float(z)), tc)


@register_applier(K_RENAME_PRIM)
def _apply_rename_prim(stage: Usd.Stage, ev: dict) -> None:
    new_name = ev["new_name"]
    if not Sdf.Path.IsValidIdentifier(new_name):
        raise ValueError(f"invalid prim name {new_name!r}")

    edit_target = stage.GetEditTarget()
    spec_path = edit_target.MapToSpecPath(Sdf.Path(ev["prim"]))
    layer = edit_target.GetLayer()
    if spec_path.isEmpty or layer.GetPrimAtPath(spec_path) is None:
        return

    edits = Sdf.BatchNamespaceEdit()
    edits.Add(Sdf.NamespaceEdit.Rename(spec_path, new_name))
    if not layer.Apply(edits):
        raise RuntimeError(f"failed to rename Sdf prim spec {spec_path}")


@register_applier(K_SET_VISIBILITY)
def _apply_set_visibility(stage: Usd.Stage, ev: dict) -> None:
    prim = stage.GetPrimAtPath(ev["prim"])
    if prim:
        imageable = UsdGeom.Imageable(prim)
        vis_value = "inherited" if ev.get("visible", True) else "invisible"
        imageable.GetVisibilityAttr().Set(vis_value, _timecode(ev))


@register_applier(K_SET_GPRIM_ATTRS)
def _apply_set_gprim_attrs(stage: Usd.Stage, ev: dict) -> None:
    prim = stage.GetPrimAtPath(ev["prim"])
    if not prim:
        return
    primvar_meta = ev.get("primvar_meta", {})
    pvapi = UsdGeom.PrimvarsAPI(prim) if primvar_meta else None
    tc = _timecode(ev)

    for attr_name, attr_value in ev.get("attrs", {}).items():
        meta = primvar_meta.get(attr_name)
        # Create non-schema primvar attributes that don't exist yet
        if meta and not prim.GetAttribute(attr_name):
            _ensure_primvar_attr(attr_name, meta, pvapi)
        _set_gprim_attr(prim, attr_name, attr_value, tc)

    # Set interpolation on primvars needed for schema-defined primvars
    # (e.g. displayColor) where the default interpolation differs from
    # the authored value.
    if pvapi:
        for attr_name, meta in primvar_meta.items():
            interp = meta.get("interpolation")
            if interp:
                pv_name = attr_name[len("primvars:") :]
                pv = pvapi.GetPrimvar(pv_name)
                if pv:
                    pv.SetInterpolation(interp)

    # Set interpolation metadata on non-primvar attributes (e.g. normals).
    attr_interp = ev.get("attr_interp", {})
    for attr_name, interp in attr_interp.items():
        attr = prim.GetAttribute(attr_name)
        if attr:
            attr.SetMetadata("interpolation", interp)


@register_applier(K_SET_INSTANCEABLE)
def _apply_set_instanceable(stage: Usd.Stage, ev: dict) -> None:
    prim = get_or_define_prim(stage, ev["prim"])
    prim.SetInstanceable(bool(ev["instanceable"]))


def _vec3f_array(value) -> Vt.Vec3fArray:
    return Vt.Vec3fArray.FromNumpy(np.asarray(value, dtype=np.float32).reshape(-1, 3))


@register_applier(K_SET_POINT_INSTANCER)
def _apply_set_point_instancer(stage: Usd.Stage, ev: dict) -> None:
    """Author PointInstancer state on an existing prim.

    Requires the prim to exist: the emitter pairs first-encounter events
    with an ensure_prim, which the structural pass applies first. Only
    value writes happen here (no prim creation).

    Orientations arrive as float32 wxyz rows and are authored to
    orientationsf (lossless for the wire format, wins value resolution
    over quath orientations).
    """
    prim = stage.GetPrimAtPath(ev["prim"])
    if not prim:
        return
    pi = UsdGeom.PointInstancer(prim)
    if not pi:
        return
    fields = ev.get("fields", [])
    tc = _timecode(ev)
    if "prototypes" in fields:
        pi.CreatePrototypesRel().SetTargets([Sdf.Path(p) for p in ev["prototypes"]])
    if "proto_indices" in fields:
        pi.CreateProtoIndicesAttr().Set(
            Vt.IntArray.FromNumpy(np.asarray(ev["proto_indices"], dtype=np.int32).ravel()), tc
        )
    if "positions" in fields:
        pi.CreatePositionsAttr().Set(_vec3f_array(ev["positions"]), tc)
    if "orientations" in fields:
        wire = np.asarray(ev["orientations"], dtype=np.float32).reshape(-1, 4)
        pi.CreateOrientationsfAttr().Set(Vt.QuatfArray.FromNumpy(wire[:, [1, 2, 3, 0]]), tc)
    if "scales" in fields:
        pi.CreateScalesAttr().Set(_vec3f_array(ev["scales"]), tc)
    if "velocities" in fields:
        pi.CreateVelocitiesAttr().Set(_vec3f_array(ev["velocities"]), tc)
    if "accelerations" in fields:
        pi.CreateAccelerationsAttr().Set(_vec3f_array(ev["accelerations"]), tc)
    if "angular_velocities" in fields:
        pi.CreateAngularVelocitiesAttr().Set(_vec3f_array(ev["angular_velocities"]), tc)
    if "ids" in fields:
        pi.CreateIdsAttr().Set(
            Vt.Int64Array.FromNumpy(np.asarray(ev["ids"], dtype=np.int64).ravel()), tc
        )
    if "invisible_ids" in fields:
        pi.CreateInvisibleIdsAttr().Set(
            Vt.Int64Array.FromNumpy(np.asarray(ev["invisible_ids"], dtype=np.int64).ravel()), tc
        )
    if "inactive_ids" in fields:
        # Prim metadata, not an attribute: uniform over time, authored as an
        # explicit list op so the receiver mirrors the sender's resolved set.
        prim.SetMetadata(
            "inactiveIds",
            Sdf.Int64ListOp.CreateExplicit([int(i) for i in ev["inactive_ids"]]),
        )


@register_applier(K_ERASE_TIME_SAMPLES)
def _apply_erase_time_samples(stage, ev):
    from .time_sample_delta import erase_time_samples

    _events.get(K_ERASE_TIME_SAMPLES).validate(ev)
    erase_time_samples(stage.GetEditTarget().GetLayer(), ev)


@register_applier(K_SET_SDF_SPEC_FIELDS)
def _apply_set_sdf_spec_fields(stage: Usd.Stage, ev: dict) -> None:
    from .sdf_spec_delta import apply_spec_delta

    apply_spec_delta(stage, ev)


@register_applier(K_REPLACE_SDF_LAYER_CONTENT)
def _apply_replace_sdf_layer_content(stage: Usd.Stage, ev: dict) -> None:
    from .sdf_spec_delta import apply_layer_content_replacement

    apply_layer_content_replacement(stage.GetEditTarget().GetLayer(), ev)


@register_applier(K_SET_SUBLAYERS)
def _apply_set_sublayers(stage: Usd.Stage, ev: dict) -> None:
    from .shared_layer_graph import apply_sublayer_entries

    apply_sublayer_entries(stage.GetEditTarget().GetLayer(), ev.get("sublayers", ()))


@register_applier(K_SET_STAGE_METADATA)
def _apply_set_stage_metadata(stage: Usd.Stage, ev: dict) -> None:
    """Write stage-level metadata. Only keys present in ``ev`` are touched."""
    if "timeCodesPerSecond" in ev:
        stage.SetTimeCodesPerSecond(float(ev["timeCodesPerSecond"]))
    if "framesPerSecond" in ev:
        stage.SetFramesPerSecond(float(ev["framesPerSecond"]))
    if "startTimeCode" in ev:
        stage.SetStartTimeCode(float(ev["startTimeCode"]))
    if "endTimeCode" in ev:
        stage.SetEndTimeCode(float(ev["endTimeCode"]))
    if "metersPerUnit" in ev:
        UsdGeom.SetStageMetersPerUnit(stage, float(ev["metersPerUnit"]))
    if "upAxis" in ev and ev["upAxis"]:
        UsdGeom.SetStageUpAxis(stage, ev["upAxis"])


@register_applier(K_SET_VARIANT_SELECTIONS)
def _apply_set_variant_selections(stage: Usd.Stage, ev: dict) -> None:
    prim_path = ev["prim"]
    prim = get_or_define_prim(stage, prim_path)
    vsets = prim.GetVariantSets()
    # Selections are plain prim metadata: authoring one for a variant set
    # that an arc has not composed yet is valid and takes effect when the
    # set arrives, so apply order against set_reference does not matter.
    for set_name, variant_name in ev.get("selections", {}).items():
        variant_set = vsets.GetVariantSet(set_name)
        if variant_name:
            variant_set.SetVariantSelection(variant_name)
        else:
            variant_set.ClearVariantSelection()


@register_applier(K_SET_REFERENCE)
def _apply_set_reference(stage: Usd.Stage, ev: dict) -> None:
    prim_path = ev["prim"]
    apply_arc_state(
        stage,
        prim_path,
        ev.get("refs", []),
        authored=ev.get("list_op_authored"),
        explicit=bool(ev.get("list_op_explicit", False)),
        arc_attr="referenceList",
    )


@register_applier(K_SET_PAYLOAD)
def _apply_set_payload(stage: Usd.Stage, ev: dict) -> None:
    prim_path = ev["prim"]
    apply_arc_state(
        stage,
        prim_path,
        ev.get("payloads", []),
        authored=ev.get("list_op_authored"),
        explicit=bool(ev.get("list_op_explicit", False)),
        arc_attr="payloadList",
    )


@register_applier(K_LOAD_PAYLOAD)
def _apply_load_payload(stage: Usd.Stage, ev: dict) -> None:
    stage.Load(Sdf.Path(ev["prim"]))


@register_applier(K_UNLOAD_PAYLOAD)
def _apply_unload_payload(stage: Usd.Stage, ev: dict) -> None:
    stage.Unload(Sdf.Path(ev["prim"]))


@register_applier(K_SET_MATERIAL_BINDING)
def _apply_set_material_binding(stage: Usd.Stage, ev: dict) -> None:
    """Bind or unbind a material to a geometry prim.

    Uses direct relationship authoring so the binding works even if
    the target material prim hasn't been created yet (USD relationships
    can target non-existent prims). ``material_purpose`` picks the slot:
    empty = ``material:binding``; ``preview`` / ``full`` author the
    purpose-suffixed rels.
    """
    prim = get_or_define_prim(stage, ev["prim"])
    material_path = ev.get("material_path", "")
    purpose = ev.get("material_purpose", "") or ""

    UsdShade.MaterialBindingAPI.Apply(prim)
    rel_name = REL_MATERIAL_BINDING + (f":{purpose}" if purpose else "")
    binding_rel = prim.GetRelationship(rel_name)
    if not binding_rel:
        binding_rel = prim.CreateRelationship(rel_name)
    binding_rel.ClearTargets(removeSpec=False)
    if material_path:
        binding_rel.AddTarget(Sdf.Path(material_path))


def _set_connectable_input_value(
    connectable: UsdShade.ConnectableAPI,
    name: str,
    value,
    type_name: str,
    time: Usd.TimeCode = _TIME_DEFAULT,
) -> None:
    """Set a single input on a UsdShade connectable, creating it if needed.

    Works on Shader, NodeGraph, Material, and UsdLux lights all share
    GetInput / CreateInput through ConnectableAPI. ``time`` selects the USD
    time sample; default writes the static opinion.
    """
    sdf_type = Sdf.ValueTypeNames.Find(type_name)
    if not sdf_type:
        return
    inp = connectable.GetInput(name)
    if not inp:
        inp = connectable.CreateInput(name, sdf_type)

    if type_name == "asset" and isinstance(value, str):
        # Empty string clears the asset path.
        inp.Set(Sdf.AssetPath(value) if value else Sdf.AssetPath(), time)
        return

    if isinstance(value, list):
        if type_name in ("color3f", "float3", "normal3f", "point3f", "vector3f"):
            inp.Set(Gf.Vec3f(*value), time)
        elif type_name in ("color3d", "double3", "normal3d", "point3d", "vector3d"):
            inp.Set(Gf.Vec3d(*value), time)
        elif type_name in ("float2", "texCoord2f", "double2"):
            inp.Set(Gf.Vec2f(*value) if type_name != "double2" else Gf.Vec2d(*value), time)
        elif type_name in ("float4", "color4f", "double4"):
            inp.Set(Gf.Vec4f(*value) if type_name != "double4" else Gf.Vec4d(*value), time)
        elif type_name == "matrix4d" and len(value) == 16:
            m = Gf.Matrix4d(*value)
            inp.Set(m, time)
        elif type_name == "matrix3d" and len(value) == 9:
            inp.Set(Gf.Matrix3d(*value), time)
        elif type_name == "matrix2d" and len(value) == 4:
            inp.Set(Gf.Matrix2d(*value), time)
        elif type_name == "int[]":
            inp.Set(Vt.IntArray([int(v) for v in value]), time)
        elif type_name == "float[]":
            inp.Set(Vt.FloatArray([float(v) for v in value]), time)
        elif type_name == "token[]":
            inp.Set(Vt.TokenArray([str(v) for v in value]), time)
        elif type_name == "string[]":
            inp.Set(Vt.StringArray([str(v) for v in value]), time)
        else:
            inp.Set(value, time)
    else:
        if type_name == "float":
            inp.Set(float(value), time)
        elif type_name == "int":
            inp.Set(int(value), time)
        else:
            inp.Set(value, time)


@register_applier(K_SET_CONNECTABLE_INPUT)
def _apply_set_connectable_input(stage: Usd.Stage, ev: dict) -> None:
    """Apply info_id + authored input values to a UsdShade connectable.

    Shader, NodeGraph, Material, and UsdLux lights all expose interface
    inputs through ConnectableAPI. When ``info_id`` is non-empty, the
    target is treated as a Shader (creating one if absent for the legacy
    Sdr-shader fallback path). When ``info_id`` is empty and the composed prim
    is not available yet, an untyped ``over`` carries the input opinion without
    claiming the weaker prim's schema type.
    """
    info_id = ev.get("info_id", "")
    if info_id:
        prim = stage.GetPrimAtPath(ev["prim"])
        if not prim:
            prim = get_or_define_prim(stage, ev["prim"], "Shader")
        if prim.IsA(UsdShade.Shader):
            shader = UsdShade.Shader(prim)
            id_attr = shader.GetIdAttr()
            if not id_attr or id_attr.Get() != info_id:
                shader.CreateIdAttr(info_id)
    else:
        prim = stage.GetPrimAtPath(ev["prim"])
        if not prim:
            if not ev.get("inputs"):
                return
            prim = stage.OverridePrim(ev["prim"])

    connectable = UsdShade.ConnectableAPI(prim)
    inputs = ev.get("inputs", {})
    input_types = ev.get("input_types", {})
    tc = _timecode(ev)
    for name, value in inputs.items():
        type_name = input_types.get(name, "float")
        _set_connectable_input_value(connectable, name, value, type_name, tc)


def _resolve_shader_port_type(prim: Usd.Prim, attr: ConnectableAttr):
    """Resolve a Shader input/output type from its Sdr NodeDef.

    Untyped overrides may carry ``info:id`` without redundantly authoring a
    Shader type opinion. Returns None when no registered node can be resolved.
    """
    if not prim:
        return None
    id_attr = prim.GetAttribute("info:id")
    shader_id = id_attr.Get() if id_attr else ""
    if not shader_id:
        return None
    node = Sdr.Registry().GetShaderNodeByIdentifier(shader_id)
    if node is None:
        return None
    port = (
        node.GetShaderInput(attr.base_name)
        if attr.is_input
        else node.GetShaderOutput(attr.base_name)
    )
    if port is None:
        return None
    return port.GetTypeAsSdfType().GetSdfType()


def _get_or_create_connectable_port(
    connectable: UsdShade.ConnectableAPI,
    attr: ConnectableAttr,
    fallback_type,
):
    """Return the named input/output, creating it with the right type if absent.

    Type resolution prefers Sdr (when the prim is a Shader with a known
    info:id), then *fallback_type* (typically the type of the other end of
    the connection), then Token.
    """
    if attr.is_input:
        port = connectable.GetInput(attr.base_name)
        if port:
            return port
        sdr_type = _resolve_shader_port_type(connectable.GetPrim(), attr)
        return connectable.CreateInput(
            attr.base_name,
            sdr_type or fallback_type or Sdf.ValueTypeNames.Token,
        )
    # output
    port = connectable.GetOutput(attr.base_name)
    if port:
        return port
    sdr_type = _resolve_shader_port_type(connectable.GetPrim(), attr)
    return connectable.CreateOutput(
        attr.base_name,
        sdr_type or fallback_type or Sdf.ValueTypeNames.Token,
    )


def _parse_connectable_attr(attr_name: str) -> ConnectableAttr:
    """Parse a protocol connectable attr name and fail fast on contract violations."""
    parsed = ConnectableAttr.from_qualified_name(attr_name)
    if parsed is None:
        raise ValueError(
            "Connectable connection attributes must be qualified as "
            f"'inputs:<name>' or 'outputs:<name>', got {attr_name!r}"
        )
    return parsed


def _get_connectable_port(
    connectable: UsdShade.ConnectableAPI,
    attr: ConnectableAttr,
):
    """Return an existing connectable input/output without creating it."""
    return (
        connectable.GetInput(attr.base_name)
        if attr.is_input
        else connectable.GetOutput(attr.base_name)
    )


@register_applier(K_SET_CONNECTABLE_CONNECTION)
def _apply_set_connectable_connection(stage: Usd.Stage, ev: dict) -> None:
    """Apply a batch of UsdShade.ConnectableAPI connection edges to the stage.

    Each entry in `connections` is keyed by a namespace-qualified attribute
    name on `ev["prim"]` (e.g. "inputs:diffuseColor", "outputs:surface") and
    valued by `{source_prim, source_attr}` where `source_attr` is similarly
    qualified.  Mirrors USD's `.connect` authoring shape the connection
    record lives on the local attribute and points upstream.
    """
    prim = stage.GetPrimAtPath(ev["prim"])
    if not prim:
        return
    local_connectable = UsdShade.ConnectableAPI(prim)

    for local_attr, conn in ev.get("connections", {}).items():
        local_pa = _parse_connectable_attr(local_attr)
        source_attr = conn["source_attr"]
        source_pa = _parse_connectable_attr(source_attr)

        # Source prim: define as Shader if missing (set_connectable_input
        # for the source will arrive later in the same txn or a
        # subsequent one).
        source_prim_path = conn["source_prim"]
        source_prim = stage.GetPrimAtPath(source_prim_path)
        if not source_prim:
            source_prim = get_or_define_prim(stage, source_prim_path, "Shader")
        source_connectable = UsdShade.ConnectableAPI(source_prim)

        # Source first so we can use its declared type as the fallback for
        # the local end when Sdr can't resolve the local side.
        source_port = _get_or_create_connectable_port(
            source_connectable,
            source_pa,
            fallback_type=None,
        )
        local_port = _get_or_create_connectable_port(
            local_connectable,
            local_pa,
            fallback_type=source_port.GetTypeName(),
        )
        local_port.ConnectToSource(source_port)

    for local_attr in ev.get("disconnections", []):
        local_pa = _parse_connectable_attr(local_attr)
        port = _get_connectable_port(local_connectable, local_pa)
        if port:
            port.DisconnectSource()


def _apply_api_schemas(prim: Usd.Prim, names: list[str]) -> None:
    """Apply a list of API schema names (single- or multi-apply) to a prim.

    Format mirrors prim.GetAppliedSchemas(): bare names for single-apply,
    "Name:instance" for multi-apply. Unknown names log a warning and are
    skipped never authors a phantom apiSchemas entry. Additive only.
    """
    if not names:
        return
    for name in names:
        schema_name, _, instance = name.partition(":")
        tf_type = Usd.SchemaRegistry.GetTypeFromSchemaTypeName(schema_name)
        if not tf_type:
            LOG.warning("Unknown API schema %r skipping", schema_name)
            continue
        if instance:
            prim.ApplyAPI(tf_type, instance)
        else:
            prim.ApplyAPI(tf_type)


@register_applier(K_ENSURE_PRIM)
def _apply_ensure_prim(stage: Usd.Stage, ev: dict) -> None:
    type_name = ev["typeName"]
    ensure_definition = bool(type_name) or not ev.get("api_schemas")
    prim = stage.GetPrimAtPath(ev["prim"])
    if ensure_definition or not prim:
        prim = get_or_define_prim(
            stage,
            ev["prim"],
            type_name,
            ensure_local_definition=ensure_definition,
        )
    _apply_api_schemas(prim, ev.get("api_schemas", []))


@register_applier(K_ENSURE_XFORM_OPS)
def _apply_ensure_xform_ops(stage: Usd.Stage, ev: dict) -> None:
    ensure_canonical_ops(stage, ev["prim"])


@register_applier(K_DELETE_PRIM)
def _apply_delete_prim(stage: Usd.Stage, ev: dict) -> None:
    stage.RemovePrim(ev["prim"])


@register_applier(K_DEACTIVATE_PRIM)
def _apply_deactivate_prim(stage: Usd.Stage, ev: dict) -> None:
    prim = stage.GetPrimAtPath(ev["prim"])
    if prim:
        prim.SetActive(ev.get("active", False))


def _is_instance_proxy_target(stage: Usd.Stage, ev: dict) -> bool:
    """True when the event targets a prim beneath a scenegraph instance.

    The spec forbids overrides on instance descendants, so such events are
    dropped. Reachable via a cross-client race: one client toggles
    instanceable while another client's edits to the children are still
    in flight.
    """
    path = ev.get("prim")
    if not path:
        return False
    # No prototypes means no instances and no proxies anywhere; skip the
    # far costlier per-prim resolution.
    if not stage.GetPrototypes():
        return False
    prim = stage.GetPrimAtPath(path)
    if prim:
        if prim.IsInstanceProxy():
            LOG.debug("dropping %s for instance proxy %s", ev.get("k"), path)
            return True
        return False
    # The path does not compose; prims cannot be created beneath an
    # instance either, so check the nearest existing ancestor.
    parent = Sdf.Path(path).GetParentPath()
    while parent and parent != Sdf.Path.absoluteRootPath:
        p = stage.GetPrimAtPath(parent)
        if p:
            if p.IsInstance() or p.IsInstanceProxy():
                LOG.debug("dropping %s under instance %s", ev.get("k"), parent)
                return True
            return False
        parent = parent.GetParentPath()
    return False


def apply_event(stage: Usd.Stage, ev: Event) -> None:
    """Apply a single event dict to a USD stage."""
    spec = _events.get(ev.get("k"))
    if spec is None or spec.apply is None:
        raise ValueError(f"unsupported USD event kind {ev.get('k')!r}")
    if _is_instance_proxy_target(stage, ev):
        return
    spec.apply(stage, ev)


def _validate_spec_events(events: list[Event]) -> None:
    from .sdf_spec_delta import validate_spec_delta

    for event in events:
        if event.get("k") == K_SET_SDF_SPEC_FIELDS:
            validate_spec_delta(event)
        elif event.get("k") == K_ERASE_TIME_SAMPLES:
            _events.get(K_ERASE_TIME_SAMPLES).validate(event)


def _is_api_schema_over(event: Event) -> bool:
    return (
        event.get("k") == K_ENSURE_PRIM
        and not event.get("typeName")
        and bool(event.get("api_schemas"))
    )


def _creates_prim(event: Event) -> bool:
    return event.get("k") in CREATE_KINDS and not _is_api_schema_over(event)


def _apply_segment(stage: Usd.Stage, events: list[Event], op_cache) -> None:
    """Apply one barrier-free segment in dependency order."""
    structural = [event for event in events if event.get("k") in STRUCTURAL_EVENT_KINDS]
    create = [
        event
        for event in structural
        if _creates_prim(event)
    ]
    create.sort(key=lambda event: event.get("prim", "").count("/"))
    modify = [
        event
        for event in structural
        if not _creates_prim(event) and not _is_api_schema_over(event)
    ]
    api_schema_overs = [event for event in structural if _is_api_schema_over(event)]

    for event in (*create, *modify, *api_schema_overs):
        if event.get("k") != K_ENSURE_XFORM_OPS:
            apply_event(stage, event)
            continue
        if _is_instance_proxy_target(stage, event):
            continue
        _prim, _xformable, translate, orient, scale = ensure_canonical_ops(
            stage,
            event["prim"],
            op_cache=op_cache,
        )
        op_cache[event["prim"]] = (translate, orient, scale)

    for event in events:
        if event.get("k") in STRUCTURAL_EVENT_KINDS:
            continue
        if event.get("k") == K_SET_XFORM_TRS:
            if not _is_instance_proxy_target(stage, event):
                _apply_set_xform_trs(stage, event, op_cache)
        else:
            apply_event(stage, event)


def apply_events(
    stage: Usd.Stage,
    events: list[Event],
    op_cache=None,
    *,
    prevalidated: bool = False,
) -> None:
    """Apply events in dependency order while preserving replacement barriers.

    Creates precede other structural events and values within each segment.
    Deletes, renames, and exact sample edits keep their input position.
    Appliers remain outside ``Sdf.ChangeBlock`` because they query the composed
    stage through ``Usd``.
    ``op_cache`` may persist canonical op handles across calls on one stage; it
    must not be shared across stages. Set ``prevalidated`` only when exact Sdf
    events were already validated together.
    """
    from .time_sample_delta import is_sample_history_barrier

    if op_cache is None:
        op_cache = {}

    if not prevalidated:
        _validate_spec_events(events)

    segment: list[Event] = []
    for event in events:
        kind = event.get("k")
        if kind in (K_DELETE_PRIM, K_RENAME_PRIM) or is_sample_history_barrier(event):
            if segment:
                _apply_segment(stage, segment, op_cache)
                segment = []
            op_cache.pop(event.get("prim"), None)
            apply_event(stage, event)
        else:
            segment.append(event)
    if segment:
        _apply_segment(stage, segment, op_cache)


class _AtomicApply:
    """Roll back the full edit-target layer when a block raises."""

    __slots__ = ("_layer", "_backup")

    def __init__(self, stage_or_layer):
        self._layer = (
            stage_or_layer
            if isinstance(stage_or_layer, Sdf.Layer)
            else stage_or_layer.GetEditTarget().GetLayer()
        )
        self._backup = None

    def __enter__(self):
        self._backup = Sdf.Layer.CreateAnonymous("txn-backup")
        self._backup.TransferContent(self._layer)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self._layer.TransferContent(self._backup)
        self._backup = None
        return False


class _ScopedAtomicApply:
    """Roll back only the identity-mapped prim subtrees touched by a block."""

    __slots__ = ("_layer", "_paths", "_backup", "_saved", "_created_roots")

    def __init__(self, stage_or_layer, prim_paths):
        self._layer = (
            stage_or_layer
            if isinstance(stage_or_layer, Sdf.Layer)
            else stage_or_layer.GetEditTarget().GetLayer()
        )
        self._paths = [Sdf.Path(p) for p in prim_paths if p]
        self._backup = None
        self._saved: list = []
        self._created_roots: list = []

    def _covered(self, path: Sdf.Path) -> bool:
        return any(path.HasPrefix(p) for p in (*self._saved, *self._created_roots))

    def __enter__(self):
        self._backup = Sdf.Layer.CreateAnonymous("txn-backup")
        self._saved = []
        self._created_roots = []
        for path in self._paths:
            if self._covered(path):
                continue
            if self._layer.GetPrimAtPath(path):
                parent = path.GetParentPath()
                if str(parent) and parent != Sdf.Path.absoluteRootPath:
                    Sdf.CreatePrimInLayer(self._backup, parent)
                Sdf.CopySpec(self._layer, path, self._backup, path)
                self._saved.append(path)
            else:
                root = path
                while True:
                    parent = root.GetParentPath()
                    if parent == Sdf.Path.absoluteRootPath or self._layer.GetPrimAtPath(parent):
                        break
                    root = parent
                self._created_roots.append(root)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            with Sdf.ChangeBlock():
                for root in self._created_roots:
                    spec = self._layer.GetPrimAtPath(root)
                    if spec:
                        parent = root.GetParentPath()
                        if parent == Sdf.Path.absoluteRootPath:
                            del self._layer.rootPrims[root.name]
                        else:
                            del self._layer.GetPrimAtPath(parent).nameChildren[root.name]
                for path in self._saved:
                    Sdf.CopySpec(self._backup, path, self._layer, path)
        self._backup = None
        return False


def atomic_apply_prim_paths(events) -> list[str] | None:
    """Return the layer prims that fully contain a batch's possible writes.

    ``None`` means the batch can modify layer-wide state and therefore needs
    a full-layer snapshot. Exact Sdf events are scoped from ``spec_path``, not
    their composed routing path. For inactive variant specs, backing up the
    prim that owns the first variant selection captures the variant subtree
    that actually lives in the layer.
    """
    paths: list[str] = []
    for event in events:
        kind = event.get("k")
        if kind in (K_SET_STAGE_METADATA, K_REPLACE_SDF_LAYER_CONTENT, K_SET_SUBLAYERS):
            return None
        if kind in (K_SET_SDF_SPEC_FIELDS, K_ERASE_TIME_SAMPLES):
            if event.get("spec_kind") == "layer":
                return None
            spec_path = Sdf.Path(event.get("spec_path", ""))
            if spec_path.isEmpty:
                return None
            variant_owner = next(
                (
                    prefix.GetPrimPath()
                    for prefix in spec_path.GetPrefixes()
                    if prefix.IsPrimVariantSelectionPath()
                ),
                None,
            )
            prim_path = variant_owner or spec_path.GetPrimPath()
            if prim_path.isEmpty or prim_path == Sdf.Path.absoluteRootPath:
                return None
            paths.append(str(prim_path))
            continue

        prim_path = event.get("prim")
        if not prim_path or prim_path == "/":
            return None
        paths.append(prim_path)
        if kind == K_RENAME_PRIM:
            parent = prim_path.rsplit("/", 1)[0]
            new_name = event.get("new_name", "")
            if not new_name:
                return None
            paths.append(f"{parent}/{new_name}" if parent else f"/{new_name}")
        elif kind == K_SET_CONNECTABLE_CONNECTION:
            for connection in event.get("connections", {}).values():
                source = connection.get("source_prim")
                if source:
                    paths.append(source)
    return paths


def _has_mapped_prim_scope(stage: Usd.Stage, prim_paths) -> bool:
    edit_target = stage.GetEditTarget()
    for raw_path in prim_paths:
        scene_path = Sdf.Path(raw_path)
        if edit_target.MapToSpecPath(scene_path) != scene_path:
            return True
    return False


def atomic_apply(stage: Usd.Stage, prim_paths=None):
    """Return a rollback context for the current edit target.

    Identity-mapped prim scopes use a focused snapshot. The caller must include
    every prim subtree the block may write; omitted paths cannot be restored.
    Layer-wide writes, unknown scopes, and composition-mapped edit targets
    snapshot the full layer.
    """
    if prim_paths is None:
        return _AtomicApply(stage)

    paths = tuple(prim_paths)
    if _has_mapped_prim_scope(stage, paths):
        return _AtomicApply(stage)
    return _ScopedAtomicApply(stage, paths)


def atomic_apply_layer(layer: Sdf.Layer, prim_paths=None):
    """Atomic apply snapshot for a layer even while it is muted from a stage."""
    if prim_paths is None:
        return _AtomicApply(layer)
    paths = tuple(prim_paths)
    if any(Sdf.Path(path) == Sdf.Path.absoluteRootPath for path in paths):
        return _AtomicApply(layer)
    return _ScopedAtomicApply(layer, paths)
