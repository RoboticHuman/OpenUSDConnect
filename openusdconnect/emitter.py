"""Stage change detection and event building.

NoticeEmitter watches a Usd.Stage via Usd.Notice.ObjectsChanged,
tracks dirty prims, snapshots TRS transforms, and builds partial-diff
events ready to send over the network.

DCC-agnostic — works on any Usd.Stage regardless of what's authoring to it.
"""

from __future__ import annotations

from pxr import Gf, Sdf, Tf, Usd, UsdGeom

from .protocol import (
    K_DEACTIVATE_PRIM,
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
    PRIMVAR_PREFIX,
)

# Per-prim cache keys — use these instead of raw strings to catch typos.
_C_TRS = "trs"
_C_MATS = "mats"
_C_VISIBILITY = "visibility"
_C_REFERENCES = "references"
_C_PAYLOADS = "payloads"
_C_PAYLOAD_LOADED = "payload_loaded"
_C_VARIANT_SELECTIONS = "variant_selections"
_C_GPRIM_ATTRS = "gprim_attrs"

# Attribute prefixes that have dedicated event channels or are not geometry.
_SKIP_ATTR_PREFIXES = ("xformOp:",)

# Individual attributes to skip:
#   visibility, xformOpOrder — have dedicated event channels
#   extent     — bounding box, computed from geometry by USD
#   proxyPrim  — relationship target, not a value attribute
_SKIP_ATTR_NAMES = frozenset({
    "visibility", "xformOpOrder", "extent", "proxyPrim",
})


def _should_track_attr(attr_name: str) -> bool:
    """Return True if this attribute should be tracked as a gprim attr.

    Excludes attributes handled by dedicated channels (xformOps, visibility),
    computed attributes (extent), and rendering hints (purpose, proxyPrim).
    Primvars (primvars:st, primvars:displayColor, etc.) ARE tracked.
    """
    if attr_name in _SKIP_ATTR_NAMES:
        return False
    for prefix in _SKIP_ATTR_PREFIXES:
        if attr_name.startswith(prefix):
            return False
    return True


def _usd_value_to_python(val):
    """Convert a USD attribute value to a JSON-serializable Python type.

    Handles scalars, GfVec types, and VtArrays (including arrays of vectors).
    Returns None for unsupported types so the caller can skip them.
    """
    if val is None:
        return None
    # Simple scalars
    if isinstance(val, (int, float, bool, str)):
        return val
    # GfVec types → list of floats
    for vec_type in (Gf.Vec2d, Gf.Vec2f, Gf.Vec3d, Gf.Vec3f, Gf.Vec4d, Gf.Vec4f):
        if isinstance(val, vec_type):
            return [float(v) for v in val]
    # VtArray types (Vec3fArray, IntArray, FloatArray, etc.)
    # Detected by type name ending in "Array" — no shared base class in pxr.
    type_name = type(val).__name__
    if type_name.endswith("Array"):
        result = []
        for elem in val:
            converted = _usd_value_to_python(elem)
            if converted is None:
                return None  # unsupported element type, skip entire array
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


# PrimResyncType enum for classifying resync notices.
# Not available in all USD builds (e.g. Blender's bundled pxr).
try:
    _PrimResyncType = Usd.Notice.ObjectsChanged.PrimResyncType
except AttributeError:
    _PrimResyncType = None


def mat_to_16(m: Gf.Matrix4d) -> list[float]:
    """Convert a Gf.Matrix4d to a flat 16-element row-major list."""
    out = []
    for r in range(4):
        row = m.GetRow(r)
        out.extend([float(row[0]), float(row[1]), float(row[2]), float(row[3])])
    return out


def as_matrix(ret):
    """Handle USD binding variants: matrix or (matrix, resets...) tuple."""
    return ret[0] if isinstance(ret, tuple) else ret


def decompose_trs_from_matrix(m: Gf.Matrix4d):
    """Decompose a 4x4 matrix into translation, quaternion rotation, and scale.

    Returns:
        (t, r, s) where:
        - t = [x, y, z] translation
        - r = [w, x, y, z] quaternion rotation
        - s = [x, y, z] scale
    """
    tr = Gf.Transform()
    tr.SetMatrix(m)
    t = Gf.Vec3d(tr.GetTranslation())
    rot = tr.GetRotation()  # Gf.Rotation (axis-angle)
    s = Gf.Vec3d(tr.GetScale())

    # Convert rotation to quaternion
    qd = rot.GetQuat()
    w = float(qd.GetReal())
    iv = qd.GetImaginary()
    x, y, z = float(iv[0]), float(iv[1]), float(iv[2])

    return (
        [float(t[0]), float(t[1]), float(t[2])],
        [w, x, y, z],
        [float(s[0]), float(s[1]), float(s[2])],
    )


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
        for item in arc_list.prependedItems:
            result.append((item.assetPath, str(item.primPath)))
        for item in arc_list.explicitItems:
            result.append((item.assetPath, str(item.primPath)))
        for item in arc_list.appendedItems:
            result.append((item.assetPath, str(item.primPath)))
    return result


def _read_references(stage, prim_path):
    """Read reference arcs authored on this stage's own layers."""
    return _read_composition_arcs(stage, prim_path, "referenceList")


def _read_payloads(stage, prim_path):
    """Read payload arcs authored on this stage's own layers."""
    return _read_composition_arcs(stage, prim_path, "payloadList")


def _read_variant_selections(stage, prim_path):
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


class NoticeEmitter:
    """Watches a Usd.Stage for changes and builds idempotent transform events.

    Detects creation, deletion, deactivation, and renames via
    ``notice.GetPrimResyncType()`` on resync paths. Supports a suppress
    flag for feedback-loop prevention.

    Usage:
        emitter = NoticeEmitter(stage)
        # ... something authors to stage ...
        events = emitter.build_events_for_dirty()
        # events is a list of event dicts ready to wrap in a txn
    """

    def __init__(self, stage: Usd.Stage, attr_filter=None):
        """
        Args:
            stage: The Usd.Stage to watch.
            attr_filter: Optional callable(attr_name: str) -> bool.
                Controls which attributes are tracked for gprim attr diffing.
                Return True to track, False to skip. If None, uses the
                default _should_track_attr which skips xformOps, visibility,
                extent, etc. Primvars ARE tracked by default.
        """
        self._attr_filter = attr_filter or _should_track_attr
        self.stage = stage
        self.dirty: set[str] = set()
        self._known_prims: set[str] = set()
        self._deleted_prims: set[str] = set()
        self._deactivated_prims: set[str] = set()
        self._renamed_prims: list[tuple[str, str]] = []  # (old_path, new_path)
        self._suppressed: bool = False
        self.listener = Tf.Notice.Register(Usd.Notice.ObjectsChanged, self._on_changed, stage)
        self.cache = UsdGeom.XformCache(Usd.TimeCode.Default())
        # Per-prim diff cache. Each prim_path maps to a dict with keys:
        #   trs, mats, visibility, references, payloads, payload_loaded,
        #   variant_selections, gprim_attrs
        # Adding a new cache key only requires updating the diff code —
        # _migrate_caches and _purge_caches handle all keys automatically.
        self._prim_cache: dict[str, dict] = {}
        self._dirty_attrs: dict[str, set[str]] = {}

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
        self._dirty_attrs.clear()
        self.dirty.clear()

    def suppress(self):
        """Suppress notice collection (feedback guard)."""
        self._suppressed = True

    def unsuppress(self):
        """Resume notice collection."""
        self._suppressed = False

    def clear_all(self):
        """Flush all dirty/deleted/renamed sets without building events."""
        self.dirty.clear()
        self._deleted_prims.clear()
        self._deactivated_prims.clear()
        self._renamed_prims.clear()
        self._dirty_attrs.clear()

    def _classify_resync(self, notice, prim_path: str) -> str | None:
        """Classify a resync path into an action.

        Returns "rename", "delete", "deactivate", "dirty", or None (skip).
        For renames, also appends to self._renamed_prims as a side effect.
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
            if not prim.IsActive() and prim_path in self._known_prims:
                return "deactivate"
            return "dirty"
        if prim_path in self._known_prims:
            return "delete"
        return None

    def _on_changed(self, notice, stage):
        if self._suppressed:
            return

        for p in notice.GetResyncedPaths():
            prim_path = _prim_path_from_notice_path(str(p))
            if not prim_path:
                continue
            action = self._classify_resync(notice, prim_path)
            if action == "delete":
                self._deleted_prims.add(prim_path)
            elif action == "deactivate":
                self._deactivated_prims.add(prim_path)
            elif action == "dirty":
                self.dirty.add(prim_path)

        for p in notice.GetChangedInfoOnlyPaths():
            path_str = str(p)
            prim_path = _prim_path_from_notice_path(path_str)
            if prim_path:
                self.dirty.add(prim_path)
                # Track specific attribute names for gprim attr diffing
                if "." in path_str:
                    attr_name = path_str.split(".", 1)[1]
                    if self._attr_filter(attr_name):
                        self._dirty_attrs.setdefault(prim_path, set()).add(attr_name)

    def mark_dirty(self, prim_path: str):
        """Manually mark a prim as dirty (useful for DCC integrations)."""
        self.dirty.add(prim_path)

    def snapshot_prim(self, prim_path: str) -> dict | None:
        """Snapshot the current local transform of a prim as TRS + matrices."""
        prim = self.stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid():
            return None

        xf = UsdGeom.Xformable(prim)
        local_ret = xf.GetLocalTransformation(Usd.TimeCode.Default())
        local_m = as_matrix(local_ret)

        self.cache.SetTime(Usd.TimeCode.Default())
        world_m = self.cache.GetLocalToWorldTransform(prim)

        t, r, s = decompose_trs_from_matrix(local_m)

        return {
            "local_m16": mat_to_16(local_m),
            "world_m16": mat_to_16(world_m),
            "t": t,
            "r": r,
            "s": s,
        }

    def _migrate_caches(self, old_path: str, new_path: str):
        """Migrate all per-prim caches from old_path to new_path."""
        if old_path in self._known_prims:
            self._known_prims.discard(old_path)
            self._known_prims.add(new_path)
        if old_path in self._prim_cache:
            self._prim_cache[new_path] = self._prim_cache.pop(old_path)

    def _purge_caches(self, prim_path: str):
        """Remove all per-prim caches for a deactivated/deleted prim."""
        self._known_prims.discard(prim_path)
        self._prim_cache.pop(prim_path, None)
        self._dirty_attrs.pop(prim_path, None)
        self.dirty.discard(prim_path)

    def _build_rename_events(self) -> list[dict]:
        """Build rename events and migrate caches."""
        events: list[dict] = []
        renamed_now = list(self._renamed_prims)
        self._renamed_prims.clear()
        for old_path, new_path in renamed_now:
            new_name = new_path.rsplit("/", 1)[-1]
            events.append({"k": K_RENAME_PRIM, "prim": old_path, "new_name": new_name})
            self._migrate_caches(old_path, new_path)
            if old_path in self.dirty:
                self.dirty.discard(old_path)
                self.dirty.add(new_path)
        return events

    def _build_deactivation_events(self) -> list[dict]:
        """Build deactivation events for deactivated and deleted prims."""
        events: list[dict] = []
        deactivated_now = self._deactivated_prims | self._deleted_prims
        self._deactivated_prims.clear()
        self._deleted_prims.clear()
        for prim_path in deactivated_now:
            events.append({"k": K_DEACTIVATE_PRIM, "prim": prim_path, "active": False})
            self._purge_caches(prim_path)
        return events

    def _build_dirty_prim_events(
        self, prim_path: str, snap: dict, eps_trs: float, eps_mat: float,
        include_matrices: bool,
    ) -> list[dict]:
        """Build events for a single dirty prim: structural, ref, TRS, visibility, matrices."""
        events: list[dict] = []
        pc = self._prim_cache.setdefault(prim_path, {})

        # Structural events on first encounter
        if prim_path not in self._known_prims:
            prim = self.stage.GetPrimAtPath(prim_path)
            type_name = "Xform"
            if prim and prim.IsValid():
                tn = prim.GetTypeName()
                if tn:
                    type_name = tn
            events.append({"k": K_ENSURE_PRIM, "prim": prim_path, "typeName": type_name})
            events.append({"k": K_ENSURE_XFORM_OPS, "prim": prim_path})
            self._known_prims.add(prim_path)

        # Variant selection diff (V before R in LIVERPS)
        current_vsel = _read_variant_selections(self.stage, prim_path)
        last_vsel = pc.get(_C_VARIANT_SELECTIONS, {})
        if current_vsel != last_vsel:
            events.append({
                "k": K_SET_VARIANT_SELECTIONS,
                "prim": prim_path,
                "selections": dict(current_vsel),
            })
            pc[_C_VARIANT_SELECTIONS] = current_vsel

        # Reference diff
        current_refs = _read_references(self.stage, prim_path)
        last_refs = pc.get(_C_REFERENCES, [])
        if current_refs != last_refs:
            ref_ev = {"k": K_SET_REFERENCE, "prim": prim_path, "refs": []}
            for asset_path, ref_prim_path in current_refs:
                entry: dict = {"asset_path": asset_path}
                if ref_prim_path:
                    entry["prim_path"] = ref_prim_path
                ref_ev["refs"].append(entry)
            events.append(ref_ev)
            pc[_C_REFERENCES] = current_refs

        # Payload diff
        current_payloads = _read_payloads(self.stage, prim_path)
        last_payloads = pc.get(_C_PAYLOADS, [])
        if current_payloads != last_payloads:
            pay_ev: dict = {"k": K_SET_PAYLOAD, "prim": prim_path, "payloads": []}
            for asset_path, pay_prim_path in current_payloads:
                entry: dict = {"asset_path": asset_path}
                if pay_prim_path:
                    entry["prim_path"] = pay_prim_path
                pay_ev["payloads"].append(entry)
            events.append(pay_ev)
            pc[_C_PAYLOADS] = current_payloads

        # Payload load-state diff
        prim = self.stage.GetPrimAtPath(prim_path)
        if prim and prim.IsValid() and prim.HasAuthoredPayloads():
            is_loaded = prim.IsLoaded()
            was_loaded = pc.get(_C_PAYLOAD_LOADED)
            if is_loaded != was_loaded:
                if is_loaded:
                    events.append({"k": K_LOAD_PAYLOAD, "prim": prim_path})
                else:
                    events.append({"k": K_UNLOAD_PAYLOAD, "prim": prim_path})
                pc[_C_PAYLOAD_LOADED] = is_loaded

        # TRS partial diff
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

        # Visibility diff — only emit if the attr is explicitly authored
        prim = self.stage.GetPrimAtPath(prim_path)
        if prim and prim.IsValid():
            imageable = UsdGeom.Imageable(prim)
            vis_attr = imageable.GetVisibilityAttr()
            if vis_attr and vis_attr.IsValid() and vis_attr.IsAuthored():
                vis_val = vis_attr.Get() or "inherited"
                last_vis = pc.get(_C_VISIBILITY)
                if vis_val != last_vis:
                    events.append({
                        "k": K_SET_VISIBILITY,
                        "prim": prim_path,
                        "visible": vis_val != "invisible",
                    })
                    pc[_C_VISIBILITY] = vis_val

        # Gprim attribute diff
        prim = self.stage.GetPrimAtPath(prim_path)
        if prim and prim.IsValid():
            dirty_attr_names = self._dirty_attrs.pop(prim_path, set())
            last_attrs = pc.get(_C_GPRIM_ATTRS, {})

            # When no specific attrs were flagged by the notice (first encounter,
            # variant switch, or any resync), scan all authored trackable attrs
            # and diff against the cache.
            if not dirty_attr_names:
                for attr in prim.GetAttributes():
                    name = attr.GetName()
                    if attr.IsAuthored() and self._attr_filter(name):
                        dirty_attr_names.add(name)

            changed_attrs = {}
            primvar_meta = {}
            attr_interp = {}
            pvapi = None  # lazy — only created if a primvar actually changed
            for attr_name in dirty_attr_names:
                attr = prim.GetAttribute(attr_name)
                if not attr or not attr.IsValid():
                    continue
                val = _usd_value_to_python(attr.Get())
                if val is None:
                    continue
                if val != last_attrs.get(attr_name):
                    changed_attrs[attr_name] = val
                    if attr_name.startswith(PRIMVAR_PREFIX):
                        # Primvar: include USD type name and interpolation so
                        # the receiver can create non-schema primvars with the
                        # exact type.
                        if pvapi is None:
                            pvapi = UsdGeom.PrimvarsAPI(prim)
                        pv = pvapi.GetPrimvar(attr_name[len(PRIMVAR_PREFIX):])
                        if pv:
                            meta = {"typeName": str(attr.GetTypeName())}
                            if pv.HasAuthoredInterpolation():
                                meta["interpolation"] = str(pv.GetInterpolation())
                            primvar_meta[attr_name] = meta
                    else:
                        # Non-primvar: capture authored interpolation metadata
                        # (e.g. normals has per-attr interpolation).
                        interp = attr.GetMetadata("interpolation")
                        if interp:
                            attr_interp[attr_name] = str(interp)

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

        # Optional matrices event (diagnostic)
        if include_matrices:
            last_mats = pc.get(_C_MATS, {})
            if not near_list(snap["local_m16"], last_mats.get("local"), eps_mat) or not near_list(
                snap["world_m16"], last_mats.get("world"), eps_mat
            ):
                events.append({
                    "k": K_SET_XFORM_MATRICES,
                    "prim": prim_path,
                    "local_m": snap["local_m16"],
                    "world_m": snap["world_m16"],
                })
                pc[_C_MATS] = {
                    "local": snap["local_m16"],
                    "world": snap["world_m16"],
                }

        return events

    def build_events_for_dirty(
        self, eps_trs: float = 1e-9, eps_mat: float = 1e-12, include_matrices: bool = True
    ) -> list[dict]:
        """Build events for all dirty prims, diffing against last-sent state.

        Returns a list of event dicts (ensure_prim, ensure_xform_ops, set_xform_trs,
        rename_prim, deactivate_prim, optionally set_xform_matrices) ready to wrap
        in a transaction.

        Processing order: renames first, then deactivations/deletions, then TRS.
        """
        events: list[dict] = []

        events.extend(self._build_rename_events())
        events.extend(self._build_deactivation_events())

        # Dirty prims (creation + TRS changes)
        # Sort by path depth so parents are emitted before children.
        dirty_now = sorted(self.dirty, key=lambda p: p.count("/"))
        self.dirty.clear()

        for prim_path in dirty_now:
            snap = self.snapshot_prim(prim_path)
            if snap is None:
                continue
            events.extend(
                self._build_dirty_prim_events(prim_path, snap, eps_trs, eps_mat, include_matrices)
            )

        return events
