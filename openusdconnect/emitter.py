"""Stage change detection and event building.

NoticeEmitter watches a Usd.Stage via Usd.Notice.ObjectsChanged,
tracks dirty prims, snapshots TRS transforms, and builds partial-diff
events ready to send over the network.

DCC-agnostic — works on any Usd.Stage regardless of what's authoring to it.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from pxr import Usd, UsdGeom, Gf, Tf, Sdf

# PrimResyncType enum for classifying resync notices.
# Not available in all USD builds (e.g. Blender's bundled pxr).
try:
    _PrimResyncType = Usd.Notice.ObjectsChanged.PrimResyncType
except AttributeError:
    _PrimResyncType = None


def mat_to_16(m: Gf.Matrix4d) -> List[float]:
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


def near_list(a: Optional[List[float]], b: Optional[List[float]], eps: float) -> bool:
    """Check if two float lists are element-wise within epsilon."""
    if a is None or b is None or len(a) != len(b):
        return False
    return all(abs(float(x) - float(y)) <= eps for x, y in zip(a, b))


def _prim_path_from_notice_path(path_str: str) -> Optional[str]:
    """Convert a USD notice path to a prim path.

    Property paths like '/World/Sphere.xformOp:translate' become '/World/Sphere'.
    Prim paths pass through unchanged.
    """
    if not path_str.startswith("/"):
        return None
    if "." in path_str:
        return path_str.split(".", 1)[0]
    return path_str


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

    def __init__(self, stage: Usd.Stage):
        self.stage = stage
        self.dirty: Set[str] = set()
        self._known_prims: Set[str] = set()
        self._deleted_prims: Set[str] = set()
        self._deactivated_prims: Set[str] = set()
        self._renamed_prims: List[Tuple[str, str]] = []  # (old_path, new_path)
        self._suppressed: bool = False
        self.listener = Tf.Notice.Register(
            Usd.Notice.ObjectsChanged, self._on_changed, stage
        )
        self.cache = UsdGeom.XformCache(Usd.TimeCode.Default())
        self.last_sent_trs: Dict[str, Dict[str, List[float]]] = {}
        self.last_sent_mats: Dict[str, Dict[str, List[float]]] = {}
        self.last_sent_visibility: Dict[str, str] = {}

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

    def _on_changed(self, notice, stage):
        if self._suppressed:
            return

        # Process resync paths — classify into creation/deletion/rename/change
        for p in notice.GetResyncedPaths():
            path_str = str(p)
            prim_path = _prim_path_from_notice_path(path_str)
            if not prim_path:
                continue

            if _PrimResyncType is not None:
                # Full classification via GetPrimResyncType (USD 0.26+)
                sdf_path = Sdf.Path(prim_path)
                resync_info = notice.GetPrimResyncType(sdf_path)
                resync_type = resync_info[0]
                associated_path = str(resync_info[1]) if len(resync_info) > 1 else ""

                if resync_type == _PrimResyncType.Delete:
                    self._deleted_prims.add(prim_path)
                elif resync_type == _PrimResyncType.RenameSource:
                    if associated_path and associated_path != ".":
                        self._renamed_prims.append((prim_path, associated_path))
                elif resync_type == _PrimResyncType.RenameDestination:
                    pass  # Handled via RenameSource
                else:
                    # "Other" — could be creation, deactivation, or normal change
                    prim = self.stage.GetPrimAtPath(prim_path)
                    if prim and prim.IsValid():
                        if not prim.IsActive() and prim_path in self._known_prims:
                            self._deactivated_prims.add(prim_path)
                        else:
                            self.dirty.add(prim_path)
                    else:
                        if prim_path in self._known_prims:
                            self._deleted_prims.add(prim_path)
            else:
                # Fallback for older USD builds without PrimResyncType
                prim = self.stage.GetPrimAtPath(prim_path)
                if prim and prim.IsValid():
                    if not prim.IsActive() and prim_path in self._known_prims:
                        self._deactivated_prims.add(prim_path)
                    else:
                        self.dirty.add(prim_path)
                else:
                    if prim_path in self._known_prims:
                        self._deleted_prims.add(prim_path)

        # Process info-only changes (property value changes on existing prims)
        for p in notice.GetChangedInfoOnlyPaths():
            prim_path = _prim_path_from_notice_path(str(p))
            if prim_path:
                self.dirty.add(prim_path)

    def mark_dirty(self, prim_path: str):
        """Manually mark a prim as dirty (useful for DCC integrations)."""
        self.dirty.add(prim_path)

    def snapshot_prim(self, prim_path: str) -> Optional[dict]:
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

    def build_events_for_dirty(
        self, eps_trs: float = 1e-9, eps_mat: float = 1e-12, include_matrices: bool = True
    ) -> List[dict]:
        """Build events for all dirty prims, diffing against last-sent state.

        Returns a list of event dicts (ensure_prim, ensure_xform_ops, set_xform_trs,
        rename_prim, deactivate_prim, optionally set_xform_matrices) ready to wrap
        in a transaction.

        Processing order: renames first, then deactivations/deletions, then TRS.
        """
        events: List[dict] = []

        # --- Renames ---
        renamed_now = list(self._renamed_prims)
        self._renamed_prims.clear()
        for old_path, new_path in renamed_now:
            old_name = old_path.rsplit("/", 1)[-1]
            new_name = new_path.rsplit("/", 1)[-1]
            events.append({"k": "rename_prim", "prim": old_path, "new_name": new_name})
            # Update caches to new path
            if old_path in self._known_prims:
                self._known_prims.discard(old_path)
                self._known_prims.add(new_path)
            if old_path in self.last_sent_trs:
                self.last_sent_trs[new_path] = self.last_sent_trs.pop(old_path)
            if old_path in self.last_sent_mats:
                self.last_sent_mats[new_path] = self.last_sent_mats.pop(old_path)
            if old_path in self.last_sent_visibility:
                self.last_sent_visibility[new_path] = self.last_sent_visibility.pop(old_path)
            # If dirty set had old_path, replace with new_path
            if old_path in self.dirty:
                self.dirty.discard(old_path)
                self.dirty.add(new_path)

        # --- Deactivations (from SetActive(False)) + Deletions ---
        deactivated_now = self._deactivated_prims | self._deleted_prims
        self._deactivated_prims.clear()
        self._deleted_prims.clear()
        for prim_path in deactivated_now:
            events.append({"k": "deactivate_prim", "prim": prim_path, "active": False})
            self._known_prims.discard(prim_path)
            self.last_sent_trs.pop(prim_path, None)
            self.last_sent_mats.pop(prim_path, None)
            self.last_sent_visibility.pop(prim_path, None)
            self.dirty.discard(prim_path)

        # --- Dirty prims (creation + TRS changes) ---
        # Sort by path depth so parents are emitted before children.
        # This ensures the receiver creates the hierarchy top-down.
        dirty_now = sorted(self.dirty, key=lambda p: p.count("/"))
        self.dirty.clear()

        for prim_path in dirty_now:
            snap = self.snapshot_prim(prim_path)
            if snap is None:
                continue

            # Only emit structural events on first encounter
            if prim_path not in self._known_prims:
                prim = self.stage.GetPrimAtPath(prim_path)
                type_name = "Xform"
                if prim and prim.IsValid():
                    tn = prim.GetTypeName()
                    if tn:
                        type_name = tn
                events.append({"k": "ensure_prim", "prim": prim_path, "typeName": type_name})
                events.append({"k": "ensure_xform_ops", "prim": prim_path})
                self._known_prims.add(prim_path)

            # TRS partial diff
            last = self.last_sent_trs.get(prim_path, {})
            fields = []
            payload = {"k": "set_xform_trs", "prim": prim_path, "fields": fields}

            if not near_list(snap["t"], last.get("t"), eps_trs):
                fields.append("t")
                payload["t"] = snap["t"]

            if not near_list(snap["r"], last.get("r"), eps_trs):
                fields.append("r")
                payload["r"] = snap["r"]

            if not near_list(snap["s"], last.get("s"), eps_trs):
                fields.append("s")
                payload["s"] = snap["s"]

            if fields:
                events.append(payload)
                self.last_sent_trs[prim_path] = {
                    "t": snap["t"],
                    "r": snap["r"],
                    "s": snap["s"],
                }

            # Visibility diff
            prim = self.stage.GetPrimAtPath(prim_path)
            if prim and prim.IsValid():
                imageable = UsdGeom.Imageable(prim)
                vis_attr = imageable.GetVisibilityAttr()
                if vis_attr and vis_attr.IsValid():
                    vis_val = vis_attr.Get() or "inherited"
                    last_vis = self.last_sent_visibility.get(prim_path)
                    if vis_val != last_vis:
                        events.append({
                            "k": "set_visibility",
                            "prim": prim_path,
                            "visible": vis_val != "invisible",
                        })
                        self.last_sent_visibility[prim_path] = vis_val

            # Optional matrices event (diagnostic)
            if include_matrices:
                lastm = self.last_sent_mats.get(prim_path, {})
                if not near_list(snap["local_m16"], lastm.get("local"), eps_mat) or \
                   not near_list(snap["world_m16"], lastm.get("world"), eps_mat):
                    events.append({
                        "k": "set_xform_matrices",
                        "prim": prim_path,
                        "local_m": snap["local_m16"],
                        "world_m": snap["world_m16"],
                    })
                    self.last_sent_mats[prim_path] = {
                        "local": snap["local_m16"],
                        "world": snap["world_m16"],
                    }

        return events
