"""Apply events to a Usd.Stage the core of the framework.

Defines how protocol events map to USD mutations. Used by:
- Server (authoritative stage)
- Headless receivers
- Any USD-based consumer

All functions require pxr (OpenUSD Python bindings).
"""

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

# Module-level singleton `Usd.TimeCode.Default()` is immutable, so the
# usual mutable-default-arg footgun doesn't apply, but ruff's B008 still
# flags the call. One shared instance keeps signatures clean.
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
    """Get existing prim or define a new one. Idempotent.

    Existing composed prims are returned without changing their ownership.
    ``ensure_local_definition`` is reserved for ``ensure_prim`` events, which
    represent an authored local definition and therefore create or update its
    def spec.
    """
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
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
    """Return the named xform op via direct attribute lookup.

    Canonical ops always live at ``xformOp:translate``,
    ``xformOp:orient``, ``xformOp:scale`` no scan needed.
    """
    attr = xf.GetPrim().GetAttribute(f"xformOp:{op_base}")
    if attr and attr.IsValid():
        return UsdGeom.XformOp(attr)
    return None


_xform_path_cache: dict[str, tuple] = {}


def _get_xform_paths(prim_path: str):
    """Return cached (translate, orient, scale, order) Sdf.Path objects.

    Sdf.Path construction from strings is ~17 µs per call.  Caching the
    parsed paths eliminates repeated string→path parsing on the hot path.
    The cache is safe to share: Sdf.Path is immutable and stateless.
    """
    paths = _xform_path_cache.get(prim_path)
    if paths is None:
        pp = Sdf.Path(prim_path)
        paths = (
            pp.AppendProperty("xformOp:translate"),
            pp.AppendProperty("xformOp:orient"),
            pp.AppendProperty("xformOp:scale"),
            pp.AppendProperty("xformOpOrder"),
        )
        _xform_path_cache[prim_path] = paths
    return paths


_XFORM_OP_SPECS = [
    ("xformOp:translate", Sdf.ValueTypeNames.Double3),
    ("xformOp:orient", Sdf.ValueTypeNames.Quatf),
    ("xformOp:scale", Sdf.ValueTypeNames.Float3),
]


def ensure_canonical_ops(stage: Usd.Stage, prim_path: str, op_cache=None):
    """Ensure canonical xform ops exist on prim: translate, orient (quatf), scale.

    Returns (prim, xformable, translate_op, orient_op, scale_op).
    Enforces xformOpOrder = [translate, orient, scale].

    When per-client layers are in use, ops may already exist on the
    composed stage (from another client's layer) but not in the current
    edit target. This function re-authors the op specs locally while
    preserving the prim's ownership: an existing composed prim receives
    an ``over`` unless an earlier ensure_prim deliberately authored a
    local ``def``.

    If *op_cache* has a hit and the edit target already has the ops,
    returns cached op handles directly (avoids 3x find_op per txn).
    Op handles are composed-stage references valid for any edit target.
    """
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        prim = stage.DefinePrim(prim_path, "Xform")
    xf = UsdGeom.Xformable(prim)

    # Pre-built Sdf.Path objects avoids ~70 µs of string→path parsing.
    path_t, path_o, path_s, path_order = _get_xform_paths(prim_path)

    # Check whether the edit target layer already has the ops.
    layer = stage.GetEditTarget().GetLayer()
    has_local_ops = (
        layer.GetAttributeAtPath(path_t) is not None
        and layer.GetAttributeAtPath(path_o) is not None
        and layer.GetAttributeAtPath(path_s) is not None
    )

    if has_local_ops:
        cached = op_cache.get(prim_path) if op_cache is not None else None
        if cached and cached[0] is not None:
            return prim, xf, cached[0], cached[1], cached[2]
    else:
        # Ops missing from edit target author attribute specs and
        # xformOpOrder directly via Sdf so each layer is self-contained.
        # The ChangeBlock batches the spec authoring into one
        # change-processing round; authored individually, every spec
        # creation and field write pays its own stage recomposition.
        layer_spec = layer.GetPrimAtPath(prim_path)
        with Sdf.ChangeBlock():
            if layer_spec is None:
                # OverridePrim is a no-op when the prim already exists only
                # through composition. CreatePrimInLayer explicitly authors
                # the local over needed to hold these xform opinions.
                layer_spec = Sdf.CreatePrimInLayer(layer, prim_path)
            for attr_name, type_name in _XFORM_OP_SPECS:
                if not layer_spec.GetAttributeAtPath(Sdf.Path(prim_path).AppendProperty(attr_name)):
                    Sdf.AttributeSpec(layer_spec, attr_name, type_name)

            if not layer_spec.GetAttributeAtPath(path_order):
                order_attr = Sdf.AttributeSpec(
                    layer_spec,
                    "xformOpOrder",
                    Sdf.ValueTypeNames.TokenArray,
                )
                order_attr.SetInfo("variability", Sdf.VariabilityUniform)
            else:
                order_attr = layer_spec.GetAttributeAtPath(path_order)
            order_attr.default = ["xformOp:translate", "xformOp:orient", "xformOp:scale"]

    # Single iteration over ops instead of 3× find_op.
    t = o = s = None
    for op in xf.GetOrderedXformOps():
        name = op.GetAttr().GetName()
        if name == "xformOp:translate":
            t = op
        elif name == "xformOp:orient":
            o = op
        elif name == "xformOp:scale":
            s = op

    return prim, xf, t, o, s


def quatf_from_wxyz(q) -> Gf.Quatf:
    """Convert [w, x, y, z] list to Gf.Quatf."""
    w, x, y, z = map(float, q)
    return Gf.Quatf(w, Gf.Vec3f(x, y, z))


def _ensure_primvar_attr(
    prim: Usd.Prim, name: str, meta: dict, pvapi: UsdGeom.PrimvarsAPI
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


def _set_gprim_attr(prim: Usd.Prim, name: str, value, time: Usd.TimeCode = _TIME_DEFAULT) -> None:
    """Set a single attribute on a typed gprim, coercing to the schema-defined type.

    Numpy arrays take a zero-copy ``Vt.*Array.FromNumpy`` path; Python lists are
    converted via ``Gf``/``Vt`` constructors. ``time`` selects the time sample.
    """
    import numpy as np

    attr = prim.GetAttribute(name)
    if not attr or not attr.IsValid():
        return
    type_name = str(attr.GetTypeName())

    # numpy array fast path bulk conversion via FromNumpy
    if isinstance(value, np.ndarray):
        if type_name in ("float3[]", "vector3f[]", "normal3f[]", "point3f[]", "color3f[]"):
            arr = value.reshape(-1, 3).astype(np.float32, copy=False)
            attr.Set(Vt.Vec3fArray.FromNumpy(arr), time)
        elif type_name in ("float2[]", "texCoord2f[]"):
            arr = value.reshape(-1, 2).astype(np.float32, copy=False)
            attr.Set(Vt.Vec2fArray.FromNumpy(arr), time)
        elif type_name == "int[]":
            attr.Set(Vt.IntArray.FromNumpy(value.ravel().astype(np.int32, copy=False)), time)
        elif type_name == "float[]":
            attr.Set(Vt.FloatArray.FromNumpy(value.ravel().astype(np.float32, copy=False)), time)
        # Gf vector constructors reject numpy scalar dtypes through their
        # Boost.Python bindings even though `value.flat` would otherwise be
        # a lazy iterator destructure and cast to Python float, same
        # shape as _apply_set_xform_trs.
        elif (
            type_name in ("float3", "vector3f", "normal3f", "point3f", "color3f")
            and value.size == 3
        ):
            x, y, z = value.flat
            attr.Set(Gf.Vec3f(float(x), float(y), float(z)), time)
        elif type_name in ("float2", "texCoord2f") and value.size == 2:
            x, y = value.flat
            attr.Set(Gf.Vec2f(float(x), float(y)), time)
        elif type_name == "double3" and value.size == 3:
            x, y, z = value.flat
            attr.Set(Gf.Vec3d(float(x), float(y), float(z)), time)
        else:
            attr.Set(value.tolist(), time)
        return

    if isinstance(value, list):
        if type_name in ("float3[]", "vector3f[]", "normal3f[]", "point3f[]", "color3f[]"):
            arr = Vt.Vec3fArray([Gf.Vec3f(*v) for v in value])
            attr.Set(arr, time)
        elif type_name in ("float2[]", "texCoord2f[]"):
            arr = Vt.Vec2fArray([Gf.Vec2f(*v) for v in value])
            attr.Set(arr, time)
        elif type_name == "int[]":
            attr.Set(Vt.IntArray(value), time)
        elif type_name == "float[]":
            attr.Set(Vt.FloatArray(value), time)
        elif (
            type_name in ("float3", "vector3f", "normal3f", "point3f", "color3f")
            and len(value) == 3
        ):
            attr.Set(Gf.Vec3f(*value), time)
        elif type_name in ("float2", "texCoord2f") and len(value) == 2:
            attr.Set(Gf.Vec2f(*value), time)
        elif type_name == "double3" and len(value) == 3:
            attr.Set(Gf.Vec3d(*value), time)
        else:
            attr.Set(value, time)
    else:
        attr.Set(value, time)


@register_applier(K_SET_XFORM_TRS)
def _apply_set_xform_trs(stage: Usd.Stage, ev: dict, op_cache=None) -> None:
    prim_path = ev["prim"]
    cached = op_cache.get(prim_path) if op_cache else None
    if cached:
        t_op, o_op, s_op = cached
    else:
        prim = stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid():
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
    if prim and prim.IsValid():
        imageable = UsdGeom.Imageable(prim)
        vis_value = "inherited" if ev.get("visible", True) else "invisible"
        imageable.GetVisibilityAttr().Set(vis_value, _timecode(ev))


@register_applier(K_SET_GPRIM_ATTRS)
def _apply_set_gprim_attrs(stage: Usd.Stage, ev: dict) -> None:
    prim = stage.GetPrimAtPath(ev["prim"])
    if not prim or not prim.IsValid():
        return
    primvar_meta = ev.get("primvar_meta", {})
    pvapi = UsdGeom.PrimvarsAPI(prim) if primvar_meta else None
    tc = _timecode(ev)

    for attr_name, attr_value in ev.get("attrs", {}).items():
        meta = primvar_meta.get(attr_name)
        # Create non-schema primvar attributes that don't exist yet
        if meta and not prim.GetAttribute(attr_name).IsValid():
            _ensure_primvar_attr(prim, attr_name, meta, pvapi)
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
        if attr and attr.IsValid():
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
    if not prim or not prim.IsValid():
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
    if not binding_rel or not binding_rel.IsValid():
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
        if not prim or not prim.IsValid():
            prim = get_or_define_prim(stage, ev["prim"], "Shader")
        if prim.IsA(UsdShade.Shader):
            shader = UsdShade.Shader(prim)
            id_attr = shader.GetIdAttr()
            if not id_attr or id_attr.Get() != info_id:
                shader.CreateIdAttr(info_id)
    else:
        prim = stage.GetPrimAtPath(ev["prim"])
        if not prim or not prim.IsValid():
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
    if not prim or not prim.IsValid():
        return None
    id_attr = prim.GetAttribute("info:id")
    shader_id = id_attr.Get() if id_attr and id_attr.IsValid() else ""
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
    if not prim or not prim.IsValid():
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
        if not source_prim or not source_prim.IsValid():
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
    if ensure_definition or not prim or not prim.IsValid():
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
    if prim and prim.IsValid():
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
    if prim and prim.IsValid():
        if prim.IsInstanceProxy():
            LOG.debug("dropping %s for instance proxy %s", ev.get("k"), path)
            return True
        return False
    # The path does not compose; prims cannot be created beneath an
    # instance either, so check the nearest existing ancestor.
    parent = Sdf.Path(path).GetParentPath()
    while parent and parent != Sdf.Path.absoluteRootPath:
        p = stage.GetPrimAtPath(parent)
        if p and p.IsValid():
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


def apply_events(
    stage: Usd.Stage,
    events: list[Event],
    op_cache=None,
    *,
    prevalidated: bool = False,
) -> None:
    """Apply a list of events to a USD stage.

    Ordering: within a segment, callers may pass events in any order. delete_prim
    and rename_prim are sequencing barriers; the events between two barriers form
    a segment applied prim-creating-kinds first (ancestors before descendants via
    path depth), then the remaining structural kinds, then value-setting ops, so
    the dependency that matters (a prim exists before anything authors on it)
    holds regardless of shuffled input. Barriers are never reordered, so a
    delete-then-recreate of the same path survives even when a whole replay
    backlog is applied in one batch.

    Events apply outside ``Sdf.ChangeBlock`` because the appliers resolve and
    mutate through ``Usd`` APIs. ``SdfChangeBlock`` permits direct ``Sdf``
    authoring only; downstream ``Usd`` queries while a block is open are unsafe.
    delete_prim/rename_prim remain sequencing barriers so later events see the
    composed result of every event before them.

    *op_cache* is an optional dict-like mapping prim_path to
    (translate_op, orient_op, scale_op).  Pass a persistent cache
    (e.g. ``cachetools.LRUCache``) to avoid repeated ``find_op`` lookups
    across calls.

    *prevalidated* is for callers that already validated every exact Sdf
    event in the larger transaction before splitting it into apply runs.
    """
    if op_cache is None:
        op_cache = {}

    if not prevalidated:
        from .sdf_spec_delta import validate_spec_delta

        for event in events:
            if event.get("k") == K_SET_SDF_SPEC_FIELDS:
                validate_spec_delta(event)

    def _apply_segment(segment: list) -> None:
        # A prim must exist before anything authors on it: prim-creating kinds
        # first (ancestors before descendants via path depth), then the other
        # structural kinds, then value-setting ops.  Structural events each
        # pay their own stage recomposition (they are create / delete / rename /
        # arc operations that modify the prim index).  Value events only write
        # typed values on already-established prims and attributes, so they
        # do not benefit from a ChangeBlock (attr.Set uses a fast incremental
        # Sdf path); an explicit ChangeBlock here would also be unsafe per the
        # SdfChangeBlock contract which forbids Usd queries inside the block.
        structural = [ev for ev in segment if ev.get("k") in STRUCTURAL_EVENT_KINDS]

        # An empty-type ensure carrying only API schemas represents metadata
        # on an already composed prim. Apply it after reference/payload arcs
        # so it creates an over instead of a standalone typeless def.
        def _is_api_over(ev):
            return (
                ev.get("k") == K_ENSURE_PRIM
                and not ev.get("typeName")
                and bool(ev.get("api_schemas"))
            )

        create = [ev for ev in structural if ev.get("k") in CREATE_KINDS and not _is_api_over(ev)]
        create.sort(key=lambda ev: ev.get("prim", "").count("/"))
        modify = [ev for ev in structural if ev.get("k") not in CREATE_KINDS or _is_api_over(ev)]
        for ev in create + modify:
            if ev.get("k") == K_ENSURE_XFORM_OPS:
                if _is_instance_proxy_target(stage, ev):
                    continue
                _prim, _xf, t, o, s = ensure_canonical_ops(
                    stage,
                    ev["prim"],
                    op_cache=op_cache,
                )
                op_cache[ev["prim"]] = (t, o, s)
            else:
                apply_event(stage, ev)
        value = [ev for ev in segment if ev.get("k") not in STRUCTURAL_EVENT_KINDS]
        for run_ev in value:
            if run_ev.get("k") == K_SET_XFORM_TRS:
                if not _is_instance_proxy_target(stage, run_ev):
                    _apply_set_xform_trs(stage, run_ev, op_cache)
            else:
                apply_event(stage, run_ev)

    # Split the batch at namespace edits so structural ops are never hoisted
    # across a delete/rename. Without this, a delete received before a same-path
    # recreate would let the recreate's structural ops run first and the delete
    # would then clobber the recreated prim. That silently breaks a fresh client
    # replaying the whole backlog in one batch (live clients escape it only
    # because each transaction arrives as its own batch).
    segment: list = []
    for ev in events:
        k = ev.get("k")
        if k in (K_DELETE_PRIM, K_RENAME_PRIM):
            if segment:
                _apply_segment(segment)
                segment = []
            op_cache.pop(ev.get("prim"), None)
            apply_event(stage, ev)
        else:
            segment.append(ev)
    if segment:
        _apply_segment(segment)


class _AtomicApply:
    """Context manager for atomic event application with rollback.

    Snapshots the edit target layer on enter. If the block raises,
    restores the snapshot so the stage returns to its pre-apply state.
    Exceptions propagate __exit__ returns False.
    """

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
    """Rollback scoped to the prim specs a batch may author on.

    TransferContent copies the whole edit layer, which costs O(layer)
    per batch; this variant copies only the touched prim subtrees, so
    the snapshot stays O(touched prims). The caller must pass every
    prim path the events can author on. Specs that did not exist before
    the block are removed on rollback (tracked by their highest
    pre-block-missing ancestor, so created ancestor chains go too).
    """

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
        if kind == K_SET_SDF_SPEC_FIELDS:
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


def atomic_apply(stage: Usd.Stage, prim_paths=None):
    """Return a context manager for atomic event application.

    Usage::

        with atomic_apply(stage):
            apply_events(stage, events)

    On success, changes persist. On failure, the edit target layer
    is restored to its state before the block partial applies
    are rolled back.

    With *prim_paths* (an iterable of every prim path the events can
    author on), only those prim specs are backed up O(touched prims)
    instead of an O(layer) ``TransferContent`` snapshot. Pass ``None``
    when the batch can write outside prim scopes (stage metadata) or
    the touched set is unknown.
    """
    if prim_paths is not None:
        return _ScopedAtomicApply(stage, prim_paths)
    return _AtomicApply(stage)


def atomic_apply_layer(layer: Sdf.Layer, prim_paths=None):
    """Atomic apply snapshot for a layer even while it is muted from a stage."""
    if prim_paths is not None:
        if any(Sdf.Path(path) == Sdf.Path.absoluteRootPath for path in prim_paths):
            return _AtomicApply(layer)
        return _ScopedAtomicApply(layer, prim_paths)
    return _AtomicApply(layer)
