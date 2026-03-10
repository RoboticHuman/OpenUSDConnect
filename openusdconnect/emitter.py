"""Stage change detection and event building.

NoticeEmitter watches a Usd.Stage via Usd.Notice.ObjectsChanged,
tracks dirty prims, snapshots TRS transforms, and builds partial-diff
events ready to send over the network.

DCC-agnostic — works on any Usd.Stage regardless of what's authoring to it.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set

from pxr import Usd, UsdGeom, Gf, Tf, Sdf


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

    Usage:
        emitter = NoticeEmitter(stage)
        # ... something authors to stage ...
        events = emitter.build_events_for_dirty()
        # events is a list of event dicts ready to wrap in a txn
    """

    def __init__(self, stage: Usd.Stage):
        self.stage = stage
        self.dirty: Set[str] = set()
        self.listener = Tf.Notice.Register(
            Usd.Notice.ObjectsChanged, self._on_changed, stage
        )
        self.cache = UsdGeom.XformCache(Usd.TimeCode.Default())
        self.last_sent_trs: Dict[str, Dict[str, List[float]]] = {}
        self.last_sent_mats: Dict[str, Dict[str, List[float]]] = {}

    def _on_changed(self, notice, stage):
        for p in notice.GetResyncedPaths():
            prim_path = _prim_path_from_notice_path(str(p))
            if prim_path:
                self.dirty.add(prim_path)
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
        optionally set_xform_matrices) ready to wrap in a transaction.
        """
        events: List[dict] = []
        dirty_now = list(self.dirty)
        self.dirty.clear()

        for prim_path in dirty_now:
            snap = self.snapshot_prim(prim_path)
            if snap is None:
                continue

            # Idempotent prim + ops setup
            events.append({"k": "ensure_prim", "prim": prim_path, "typeName": "Xform"})
            events.append({"k": "ensure_xform_ops", "prim": prim_path})

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
