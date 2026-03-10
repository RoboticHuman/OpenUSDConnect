"""Apply events to a Usd.Stage — the core of the framework.

Defines how protocol events map to USD mutations. Used by:
- Server (authoritative stage)
- Headless receivers
- Any USD-based consumer

All functions require pxr (OpenUSD Python bindings).
"""

from __future__ import annotations

from typing import Optional

from pxr import Usd, UsdGeom, Gf, Sdf


def get_or_define_prim(stage: Usd.Stage, prim_path: str, type_name: str = "Xform") -> Usd.Prim:
    """Get existing prim or define a new one. Idempotent."""
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        prim = stage.DefinePrim(prim_path, type_name)
    return prim


def find_op(xf: UsdGeom.Xformable, op_base: str) -> Optional[UsdGeom.XformOp]:
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


def apply_event(stage: Usd.Stage, ev: dict) -> None:
    """Apply a single event dict to a USD stage.

    Handles all event types: ensure_prim, ensure_xform_ops, set_xform_trs,
    set_xform_matrices, delete_prim.
    """
    k = ev.get("k")

    if k == "ensure_prim":
        get_or_define_prim(stage, ev["prim"], ev.get("typeName", "Xform"))
        return

    if k == "ensure_xform_ops":
        ensure_canonical_ops(stage, ev["prim"])
        return

    if k == "set_xform_trs":
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
        return

    if k == "set_xform_matrices":
        # Diagnostic/optional — server stores canonical TRS, not matrices
        return

    if k == "delete_prim":
        prim_path = ev["prim"]
        stage.RemovePrim(prim_path)
        return

    if k == "deactivate_prim":
        prim = stage.GetPrimAtPath(ev["prim"])
        if prim and prim.IsValid():
            prim.SetActive(ev.get("active", False))
        return

    if k == "rename_prim":
        prim = stage.GetPrimAtPath(ev["prim"])
        if prim and prim.IsValid():
            editor = Usd.NamespaceEditor(stage)
            editor.RenamePrim(prim, ev["new_name"])
            editor.ApplyEdits()
        return


def apply_events(stage: Usd.Stage, events: list) -> None:
    """Apply a list of events. Structural events (ensure_prim, ensure_xform_ops)
    are applied first outside a ChangeBlock, then value-setting events are
    applied inside a ChangeBlock for atomicity."""
    # Structural events first (DefinePrim can fail inside ChangeBlock in some USD builds)
    structural = {"ensure_prim", "ensure_xform_ops"}
    for ev in events:
        if ev.get("k") in structural:
            apply_event(stage, ev)
    # Value-setting events in a ChangeBlock
    with Sdf.ChangeBlock():
        for ev in events:
            if ev.get("k") not in structural:
                apply_event(stage, ev)
