"""Apply events to a Usd.Stage — the core of the framework.

Defines how protocol events map to USD mutations. Used by:
- Server (authoritative stage)
- Headless receivers
- Any USD-based consumer

All functions require pxr (OpenUSD Python bindings).
"""

from __future__ import annotations

from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from .protocol import (
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_SET_GPRIM_ATTRS,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_VARIANT_SELECTIONS,
    K_SET_VISIBILITY,
    K_SET_XFORM_MATRICES,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
    STRUCTURAL_EVENT_KINDS,
)


def get_or_define_prim(stage: Usd.Stage, prim_path: str, type_name: str = "Xform") -> Usd.Prim:
    """Get existing prim or define a new one. Idempotent."""
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        prim = stage.DefinePrim(prim_path, type_name)
    return prim


def find_op(xf: UsdGeom.Xformable, op_base: str) -> UsdGeom.XformOp | None:
    """Find an xform op by its base name (e.g. 'translate', 'orient', 'scale').

    Handles USD version differences in GetOpName() by matching against the
    attribute name (xformOp:translate, etc.).
    """
    target = f"xformOp:{op_base}"
    for op in xf.GetOrderedXformOps():
        attr_name = op.GetAttr().GetName()
        if attr_name == target or attr_name.startswith(target + ":"):
            return op
    return None


def ensure_canonical_ops(stage: Usd.Stage, prim_path: str):
    """Ensure canonical xform ops exist on prim: translate, orient (quatf), scale.

    Returns (prim, xformable, translate_op, orient_op, scale_op).
    Enforces xformOpOrder = [translate, orient, scale].
    """
    prim = get_or_define_prim(stage, prim_path, "Xform")
    xf = UsdGeom.Xformable(prim)

    t = find_op(xf, "translate")
    o = find_op(xf, "orient")
    s = find_op(xf, "scale")

    if t is None or o is None or s is None:
        # Composed prims (e.g. children of a reference) may lack an authored
        # spec on the edit target layer.  OverridePrim creates an 'over' spec
        # so AddXformOp can author attributes.
        stage.OverridePrim(prim_path)

    if t is None:
        t = xf.AddTranslateOp()
    if o is None:
        o = xf.AddOrientOp()
    if s is None:
        s = xf.AddScaleOp()

    desired = [t, o, s]
    cur = xf.GetOrderedXformOps()
    if [op.GetAttr().GetPath() for op in cur] != [op.GetAttr().GetPath() for op in desired]:
        xf.SetXformOpOrder(desired)

    return prim, xf, t, o, s


def quatf_from_wxyz(q) -> Gf.Quatf:
    """Convert [w, x, y, z] list to Gf.Quatf."""
    w, x, y, z = map(float, q)
    return Gf.Quatf(w, Gf.Vec3f(x, y, z))


def _ensure_primvar_attr(prim: Usd.Prim, name: str, meta: dict,
                         pvapi: UsdGeom.PrimvarsAPI) -> Usd.Attribute | None:
    """Create a primvar attribute from metadata if it doesn't exist yet.

    Returns the attribute (newly created or existing), or None on failure.
    """
    sdf_type = Sdf.ValueTypeNames.Find(meta["typeName"])
    if not sdf_type:
        return None
    pv_name = name[len("primvars:"):]
    interp = meta.get("interpolation", "")
    pv = pvapi.CreatePrimvar(pv_name, sdf_type, interp)
    return pv.GetAttr()


def _set_gprim_attr(prim: Usd.Prim, name: str, value) -> None:
    """Set a single attribute on a typed gprim, coercing to the schema-defined type."""
    attr = prim.GetAttribute(name)
    if not attr or not attr.IsValid():
        return
    type_name = str(attr.GetTypeName())

    if isinstance(value, list):
        if type_name in ("float3[]", "vector3f[]", "normal3f[]", "point3f[]", "color3f[]"):
            arr = Vt.Vec3fArray([Gf.Vec3f(*v) for v in value])
            attr.Set(arr)
        elif type_name in ("float2[]", "texCoord2f[]"):
            arr = Vt.Vec2fArray([Gf.Vec2f(*v) for v in value])
            attr.Set(arr)
        elif type_name == "int[]":
            attr.Set(Vt.IntArray(value))
        elif type_name == "float[]":
            attr.Set(Vt.FloatArray(value))
        else:
            attr.Set(value)
    else:
        attr.Set(value)


def _apply_set_xform_trs(stage: Usd.Stage, ev: dict) -> None:
    prim_path = ev["prim"]
    fields = ev.get("fields", [])
    _, _, t_op, o_op, s_op = ensure_canonical_ops(stage, prim_path)

    if "t" in fields:
        x, y, z = ev["t"]
        t_op.Set(Gf.Vec3d(float(x), float(y), float(z)))
    if "r" in fields:
        o_op.Set(quatf_from_wxyz(ev["r"]))
    if "s" in fields:
        x, y, z = ev["s"]
        s_op.Set(Gf.Vec3d(float(x), float(y), float(z)))


def _apply_rename_prim(stage: Usd.Stage, ev: dict) -> None:
    prim = stage.GetPrimAtPath(ev["prim"])
    if prim and prim.IsValid():
        editor = Usd.NamespaceEditor(stage)
        editor.RenamePrim(prim, ev["new_name"])
        editor.ApplyEdits()


def _apply_set_visibility(stage: Usd.Stage, ev: dict) -> None:
    prim = stage.GetPrimAtPath(ev["prim"])
    if prim and prim.IsValid():
        imageable = UsdGeom.Imageable(prim)
        vis_value = "inherited" if ev.get("visible", True) else "invisible"
        imageable.GetVisibilityAttr().Set(vis_value)


def _apply_set_gprim_attrs(stage: Usd.Stage, ev: dict) -> None:
    prim = stage.GetPrimAtPath(ev["prim"])
    if not prim or not prim.IsValid():
        return
    primvar_meta = ev.get("primvar_meta", {})
    pvapi = UsdGeom.PrimvarsAPI(prim) if primvar_meta else None

    for attr_name, attr_value in ev.get("attrs", {}).items():
        meta = primvar_meta.get(attr_name)
        # Create non-schema primvar attributes that don't exist yet
        if meta and not prim.GetAttribute(attr_name).IsValid():
            _ensure_primvar_attr(prim, attr_name, meta, pvapi)
        _set_gprim_attr(prim, attr_name, attr_value)

    # Set interpolation on primvars — needed for schema-defined primvars
    # (e.g. displayColor) where the default interpolation differs from
    # the authored value, and for newly created primvars where CreatePrimvar
    # already set it (harmless no-op in that case).
    if pvapi:
        for attr_name, meta in primvar_meta.items():
            interp = meta.get("interpolation")
            if interp:
                pv_name = attr_name[len("primvars:"):]
                pv = pvapi.GetPrimvar(pv_name)
                if pv:
                    pv.SetInterpolation(interp)


def _apply_set_variant_selections(stage: Usd.Stage, ev: dict) -> None:
    prim_path = ev["prim"]
    prim = get_or_define_prim(stage, prim_path)
    vsets = prim.GetVariantSets()
    for set_name, variant_name in ev.get("selections", {}).items():
        if vsets.HasVariantSet(set_name):
            vsets.GetVariantSet(set_name).SetVariantSelection(variant_name)


def _apply_set_reference(stage: Usd.Stage, ev: dict) -> None:
    prim_path = ev["prim"]
    prim = get_or_define_prim(stage, prim_path)
    refs = prim.GetReferences()
    refs.ClearReferences()

    for ref_entry in ev.get("refs", []):
        asset_path = ref_entry.get("asset_path", "")
        prim_path_ref = ref_entry.get("prim_path", "")
        if asset_path:
            if prim_path_ref:
                refs.AddReference(asset_path, prim_path_ref)
            else:
                refs.AddReference(asset_path)
        elif prim_path_ref:
            refs.AddInternalReference(Sdf.Path(prim_path_ref))


def _apply_set_payload(stage: Usd.Stage, ev: dict) -> None:
    prim_path = ev["prim"]
    prim = get_or_define_prim(stage, prim_path)
    payloads = prim.GetPayloads()
    payloads.ClearPayloads()

    for entry in ev.get("payloads", []):
        asset_path = entry.get("asset_path", "")
        prim_path_ref = entry.get("prim_path", "")
        if asset_path:
            if prim_path_ref:
                payloads.AddPayload(asset_path, prim_path_ref)
            else:
                payloads.AddPayload(asset_path)
        elif prim_path_ref:
            payloads.AddInternalPayload(Sdf.Path(prim_path_ref))


def _apply_load_payload(stage: Usd.Stage, ev: dict) -> None:
    stage.Load(Sdf.Path(ev["prim"]))


def _apply_unload_payload(stage: Usd.Stage, ev: dict) -> None:
    stage.Unload(Sdf.Path(ev["prim"]))


_EVENT_DISPATCH: dict[str, callable] = {
    K_SET_VARIANT_SELECTIONS: _apply_set_variant_selections,
    K_SET_XFORM_TRS: _apply_set_xform_trs,
    K_RENAME_PRIM: _apply_rename_prim,
    K_SET_VISIBILITY: _apply_set_visibility,
    K_SET_GPRIM_ATTRS: _apply_set_gprim_attrs,
    K_SET_REFERENCE: _apply_set_reference,
    K_SET_PAYLOAD: _apply_set_payload,
    K_LOAD_PAYLOAD: _apply_load_payload,
    K_UNLOAD_PAYLOAD: _apply_unload_payload,
}


def apply_event(stage: Usd.Stage, ev: dict) -> None:
    """Apply a single event dict to a USD stage.

    Handles all event types: ensure_prim, ensure_xform_ops, set_xform_trs,
    set_xform_matrices, delete_prim, set_visibility, set_gprim_attrs,
    set_reference.
    """
    k = ev.get("k")

    if k == K_ENSURE_PRIM:
        get_or_define_prim(stage, ev["prim"], ev.get("typeName", "Xform"))
        return

    if k == K_ENSURE_XFORM_OPS:
        ensure_canonical_ops(stage, ev["prim"])
        return

    if k == K_SET_XFORM_MATRICES:
        return

    if k == K_DELETE_PRIM:
        stage.RemovePrim(ev["prim"])
        return

    if k == K_DEACTIVATE_PRIM:
        prim = stage.GetPrimAtPath(ev["prim"])
        if prim and prim.IsValid():
            prim.SetActive(ev.get("active", False))
        return

    handler = _EVENT_DISPATCH.get(k)
    if handler is not None:
        handler(stage, ev)


def apply_events(stage: Usd.Stage, events: list) -> None:
    """Apply a list of events. Structural events (ensure_prim, ensure_xform_ops)
    are applied first outside a ChangeBlock, then value-setting events are
    applied inside a ChangeBlock for atomicity."""
    # Structural events first (DefinePrim can fail inside ChangeBlock in some USD builds)
    for ev in events:
        if ev.get("k") in STRUCTURAL_EVENT_KINDS:
            apply_event(stage, ev)
    # Value-setting events in a ChangeBlock
    with Sdf.ChangeBlock():
        for ev in events:
            if ev.get("k") not in STRUCTURAL_EVENT_KINDS:
                apply_event(stage, ev)
