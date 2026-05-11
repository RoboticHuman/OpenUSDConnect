"""Apply events to a Usd.Stage — the core of the framework.

Defines how protocol events map to USD mutations. Used by:
- Server (authoritative stage)
- Headless receivers
- Any USD-based consumer

All functions require pxr (OpenUSD Python bindings).
"""

from __future__ import annotations

from pxr import Gf, Sdf, Sdr, Usd, UsdGeom, UsdShade, Vt

from .protocol import (
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_SET_GPRIM_ATTRS,
    K_SET_MATERIAL_BINDING,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_SHADER_CONNECTION,
    K_SET_SHADER_INPUT,
    K_SET_VARIANT_SELECTIONS,
    K_SET_VISIBILITY,
    K_SET_XFORM_MATRICES,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
    REL_MATERIAL_BINDING,
    STRUCTURAL_EVENT_KINDS,
    split_qualified_attr,
)


def get_or_define_prim(stage: Usd.Stage, prim_path: str, type_name: str = "Xform") -> Usd.Prim:
    """Get existing prim or define a new one. Idempotent.

    When the prim already exists on the composed stage but the current
    edit target layer has no spec for it, a 'def' spec is created in the
    edit target so the prim survives if stronger layers are muted or
    removed.  Without this, the edit target would only get an 'over'
    on the first attribute write, which is invisible without a 'def'
    elsewhere.
    """
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        prim = stage.DefinePrim(prim_path, type_name)
    else:
        # Prim exists on composed stage — ensure a def spec exists
        # in the current edit target layer (DefinePrim is a no-op if
        # the prim already exists, so we go through Sdf directly).
        layer = stage.GetEditTarget().GetLayer()
        if not layer.GetPrimAtPath(prim_path):
            spec = Sdf.CreatePrimInLayer(layer, prim_path)
            if spec:
                spec.specifier = Sdf.SpecifierDef
                if type_name:
                    spec.typeName = type_name
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
    edit target.  This function checks the edit target layer and
    re-authors ops locally so each layer is self-contained — a layer
    can be muted or removed without losing xform op definitions.

    If *op_cache* has a hit and the edit target already has the ops,
    returns cached op handles directly (avoids 3x find_op per txn).
    Op handles are composed-stage references — valid for any edit target.
    """
    prim = get_or_define_prim(stage, prim_path, "Xform")
    xf = UsdGeom.Xformable(prim)

    # Pre-built Sdf.Path objects — avoids ~70 µs of string→path parsing.
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
        # Ops missing from edit target — author attribute specs and
        # xformOpOrder directly via Sdf so each layer is self-contained.
        stage.OverridePrim(prim_path)
        layer_spec = layer.GetPrimAtPath(prim_path)
        for attr_name, type_name in _XFORM_OP_SPECS:
            if not layer_spec.GetAttributeAtPath(
                Sdf.Path(prim_path).AppendProperty(attr_name)
            ):
                Sdf.AttributeSpec(layer_spec, attr_name, type_name)

        if not layer_spec.GetAttributeAtPath(path_order):
            order_attr = Sdf.AttributeSpec(
                layer_spec, "xformOpOrder", Sdf.ValueTypeNames.TokenArray,
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
    """Set a single attribute on a typed gprim, coercing to the schema-defined type.

    Accepts Python lists (legacy/dict path) and numpy arrays (zero-copy path).
    When value is a numpy array, uses Vt.*Array.FromNumpy() for bulk conversion
    without per-element Python iteration.
    """
    import numpy as np

    attr = prim.GetAttribute(name)
    if not attr or not attr.IsValid():
        return
    type_name = str(attr.GetTypeName())

    # numpy array fast path — bulk conversion via FromNumpy
    if isinstance(value, np.ndarray):
        if type_name in ("float3[]", "vector3f[]", "normal3f[]", "point3f[]", "color3f[]"):
            arr = value.reshape(-1, 3).astype(np.float32, copy=False)
            attr.Set(Vt.Vec3fArray.FromNumpy(arr))
        elif type_name in ("float2[]", "texCoord2f[]"):
            arr = value.reshape(-1, 2).astype(np.float32, copy=False)
            attr.Set(Vt.Vec2fArray.FromNumpy(arr))
        elif type_name == "int[]":
            attr.Set(Vt.IntArray.FromNumpy(value.ravel().astype(np.int32, copy=False)))
        elif type_name == "float[]":
            attr.Set(Vt.FloatArray.FromNumpy(value.ravel().astype(np.float32, copy=False)))
        elif type_name in ("float3", "vector3f", "normal3f", "point3f",
                          "color3f") and value.size == 3:
            attr.Set(Gf.Vec3f(*value.flat))
        elif type_name in ("float2", "texCoord2f") and value.size == 2:
            attr.Set(Gf.Vec2f(*value.flat))
        elif type_name == "double3" and value.size == 3:
            attr.Set(Gf.Vec3d(*value.flat))
        else:
            attr.Set(value.tolist())
        return

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
        elif type_name in ("float3", "vector3f", "normal3f", "point3f",
                          "color3f") and len(value) == 3:
            attr.Set(Gf.Vec3f(*value))
        elif type_name in ("float2", "texCoord2f") and len(value) == 2:
            attr.Set(Gf.Vec2f(*value))
        elif type_name == "double3" and len(value) == 3:
            attr.Set(Gf.Vec3d(*value))
        else:
            attr.Set(value)
    else:
        attr.Set(value)


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
    if "t" in fields and t_op:
        x, y, z = ev["t"]
        t_op.Set(Gf.Vec3d(float(x), float(y), float(z)))
    if "r" in fields and o_op:
        o_op.Set(quatf_from_wxyz(ev["r"]))
    if "s" in fields and s_op:
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
    # the authored value.
    if pvapi:
        for attr_name, meta in primvar_meta.items():
            interp = meta.get("interpolation")
            if interp:
                pv_name = attr_name[len("primvars:"):]
                pv = pvapi.GetPrimvar(pv_name)
                if pv:
                    pv.SetInterpolation(interp)

    # Set interpolation metadata on non-primvar attributes (e.g. normals).
    attr_interp = ev.get("attr_interp", {})
    for attr_name, interp in attr_interp.items():
        attr = prim.GetAttribute(attr_name)
        if attr and attr.IsValid():
            attr.SetMetadata("interpolation", interp)


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


def _apply_set_material_binding(stage: Usd.Stage, ev: dict) -> None:
    """Bind or unbind a material to a geometry prim.

    Uses direct relationship authoring so the binding works even if
    the target material prim hasn't been created yet (USD relationships
    can target non-existent prims).
    """
    prim = get_or_define_prim(stage, ev["prim"])
    material_path = ev.get("material_path", "")

    UsdShade.MaterialBindingAPI.Apply(prim)
    binding_rel = prim.GetRelationship(REL_MATERIAL_BINDING)
    if not binding_rel or not binding_rel.IsValid():
        binding_rel = prim.CreateRelationship(REL_MATERIAL_BINDING)
    binding_rel.ClearTargets(removeSpec=False)
    if material_path:
        binding_rel.AddTarget(Sdf.Path(material_path))


def _set_shader_input_value(connectable: UsdShade.ConnectableAPI, name: str,
                            value, type_name: str) -> None:
    """Set a single input on a UsdShade connectable, creating it if needed.

    Works on Shader, NodeGraph, and Material — all share GetInput / CreateInput
    through ConnectableAPI.
    """
    sdf_type = Sdf.ValueTypeNames.Find(type_name)
    if not sdf_type:
        return
    inp = connectable.GetInput(name)
    if not inp:
        inp = connectable.CreateInput(name, sdf_type)

    if isinstance(value, list):
        if type_name in ("color3f", "float3", "normal3f"):
            inp.Set(Gf.Vec3f(*value))
        elif type_name in ("float2", "texCoord2f"):
            inp.Set(Gf.Vec2f(*value))
        elif type_name in ("float4", "color4f"):
            inp.Set(Gf.Vec4f(*value))
        else:
            inp.Set(value)
    else:
        if type_name == "float":
            inp.Set(float(value))
        elif type_name == "int":
            inp.Set(int(value))
        else:
            inp.Set(value)


def _apply_set_shader_input(stage: Usd.Stage, ev: dict) -> None:
    """Apply shader-id and authored input values to a UsdShade connectable.

    Polymorphic over Shader, NodeGraph, and Material.  Authors `info:id`
    when the prim is a Shader and a non-empty shader_id is provided;
    NodeGraph/Material carry no info:id.
    """
    prim = get_or_define_prim(stage, ev["prim"], "Shader")
    shader_id = ev.get("shader_id", "")
    if shader_id and prim.IsA(UsdShade.Shader):
        UsdShade.Shader(prim).CreateIdAttr(shader_id)

    connectable = UsdShade.ConnectableAPI(prim)
    inputs = ev.get("inputs", {})
    input_types = ev.get("input_types", {})
    for name, value in inputs.items():
        type_name = input_types.get(name, "float")
        _set_shader_input_value(connectable, name, value, type_name)


def _resolve_sdr_type_for_attr(prim: Usd.Prim, side: str, base_name: str):
    """Resolve the USD type of a Shader's input/output via Sdr's NodeDef.

    *side* is "input" or "output". Returns None if the prim isn't a Shader
    or the NodeDef / port isn't registered — callers fall back to other
    sources of truth (the connected source's type, or Sdf.ValueTypeNames.Token).
    """
    if not prim or not prim.IsA(UsdShade.Shader):
        return None
    shader = UsdShade.Shader(prim)
    shader_id = shader.GetIdAttr().Get() if shader.GetIdAttr() else ""
    if not shader_id:
        return None
    node = Sdr.Registry().GetShaderNodeByIdentifier(shader_id)
    if node is None:
        return None
    port = node.GetShaderInput(base_name) if side == "input" \
        else node.GetShaderOutput(base_name)
    if port is None:
        return None
    return port.GetTypeAsSdfType().GetSdfType()


def _get_or_create_connectable_port(connectable: UsdShade.ConnectableAPI,
                                     side: str, base_name: str, fallback_type):
    """Return the named input/output, creating it with the right type if absent.

    Type resolution prefers Sdr (when the prim is a Shader with a known
    info:id), then *fallback_type* (typically the type of the other end of
    the connection), then Token.
    """
    if side == "input":
        port = connectable.GetInput(base_name)
        if port:
            return port
        sdr_type = _resolve_sdr_type_for_attr(
            connectable.GetPrim(), "input", base_name,
        )
        return connectable.CreateInput(
            base_name, sdr_type or fallback_type or Sdf.ValueTypeNames.Token,
        )
    # output
    port = connectable.GetOutput(base_name)
    if port:
        return port
    sdr_type = _resolve_sdr_type_for_attr(
        connectable.GetPrim(), "output", base_name,
    )
    return connectable.CreateOutput(
        base_name, sdr_type or fallback_type or Sdf.ValueTypeNames.Token,
    )


def _apply_set_shader_connection(stage: Usd.Stage, ev: dict) -> None:
    """Apply a batch of shader/nodegraph connection edges to the stage.

    Each entry in `connections` is keyed by a namespace-qualified attribute
    name on `ev["prim"]` (e.g. "inputs:diffuseColor", "outputs:surface") and
    valued by `{source_prim, source_attr}` where `source_attr` is similarly
    qualified.  Mirrors USD's `.connect` authoring shape — the connection
    record lives on the local attribute and points upstream.
    """
    prim = stage.GetPrimAtPath(ev["prim"])
    if not prim or not prim.IsValid():
        return
    local_connectable = UsdShade.ConnectableAPI(prim)

    for local_attr, conn in ev.get("connections", {}).items():
        local_side, local_base = split_qualified_attr(local_attr)
        if not local_side:
            continue  # malformed — caller-validated, defensive

        source_attr = conn["source_attr"]
        source_side, source_base = split_qualified_attr(source_attr)
        if not source_side:
            continue

        # Source prim: define as Shader if missing (set_shader_input
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
            source_connectable, source_side, source_base, fallback_type=None,
        )
        local_port = _get_or_create_connectable_port(
            local_connectable, local_side, local_base,
            fallback_type=source_port.GetTypeName(),
        )
        local_port.ConnectToSource(source_port)

    for local_attr in ev.get("disconnections", []):
        side, base = split_qualified_attr(local_attr)
        if not side:
            continue
        port = (local_connectable.GetInput(base) if side == "input"
                else local_connectable.GetOutput(base))
        if port:
            port.DisconnectSource()


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
    K_SET_MATERIAL_BINDING: _apply_set_material_binding,
    K_SET_SHADER_INPUT: _apply_set_shader_input,
    K_SET_SHADER_CONNECTION: _apply_set_shader_connection,
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


def apply_events(stage: Usd.Stage, events: list, op_cache=None) -> None:
    """Apply a list of events to a USD stage.

    Structural events are applied outside a ChangeBlock, then value-setting
    events inside one for atomicity.

    *op_cache* is an optional dict-like mapping prim_path to
    (translate_op, orient_op, scale_op).  Pass a persistent cache
    (e.g. ``cachetools.LRUCache``) to avoid repeated ``find_op`` lookups
    across calls.
    """
    if op_cache is None:
        op_cache = {}

    # Structural ops outside ChangeBlock (DefinePrim fails inside on our USD build).
    for ev in events:
        k = ev.get("k")
        if k not in STRUCTURAL_EVENT_KINDS:
            continue
        if k == K_ENSURE_XFORM_OPS:
            _prim, _xf, t, o, s = ensure_canonical_ops(
                stage, ev["prim"], op_cache=op_cache,
            )
            op_cache[ev["prim"]] = (t, o, s)
        else:
            apply_event(stage, ev)

    # Value-setting ops inside ChangeBlock.
    with Sdf.ChangeBlock():
        for ev in events:
            k = ev.get("k")
            if k in STRUCTURAL_EVENT_KINDS:
                continue
            if k == K_SET_XFORM_TRS:
                _apply_set_xform_trs(stage, ev, op_cache)
            elif k in (K_DELETE_PRIM, K_RENAME_PRIM):
                op_cache.pop(ev.get("prim"), None)
                apply_event(stage, ev)
            else:
                apply_event(stage, ev)


class _AtomicApply:
    """Context manager for atomic event application with rollback.

    Snapshots the edit target layer on enter. If the block raises,
    restores the snapshot so the stage returns to its pre-apply state.
    Exceptions propagate — __exit__ returns False.
    """

    __slots__ = ("_layer", "_backup")

    def __init__(self, stage: Usd.Stage):
        self._layer = stage.GetEditTarget().GetLayer()
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


def atomic_apply(stage: Usd.Stage):
    """Return a context manager for atomic event application.

    Usage::

        with atomic_apply(stage):
            apply_events(stage, events)

    On success, changes persist. On failure, the edit target layer
    is restored to its state before the block — partial applies
    are rolled back. Uses ``Sdf.Layer.TransferContent()`` for the
    snapshot/restore.
    """
    return _AtomicApply(stage)
