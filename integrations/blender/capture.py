"""Blender depsgraph -> emitter bridge.

Captures Blender object transform edits and sends them over the network
via the openusdconnect protocol. Also handles USD import tagging via USDHook
and local diff capture to session layer.

Architecture:
  Blender depsgraph change
    -> BlenderStageAuthor writes to local USD stage
      -> Usd.Notice.ObjectsChanged fires
        -> NoticeEmitter builds protocol events
          -> NetworkSender sends events over TCP
"""

from __future__ import annotations

import logging
import os
import socket

import bpy

try:
    import mathutils

    _IDENTITY_4X4 = mathutils.Matrix.Identity(4)
except ImportError:
    _IDENTITY_4X4 = None

from openusdconnect.axis_conversion import (
    strip_axis_rotation,
    zup_to_yup_quat,
    zup_to_yup_scale,
    zup_to_yup_vec,
)

try:
    from pxr import Gf, Sdf, Usd, UsdGeom

    PXR_AVAILABLE = True
except Exception as e:
    PXR_AVAILABLE = False
    _PXR_IMPORT_ERROR = e

LOG = logging.getLogger(__name__)

DEFAULT_COALESCE_SECONDS = 0.15


def _compute_local_trs(obj):
    """Local-to-parent TRS decomposition. Returns (loc, rot_quat, scl)."""
    if obj.parent:
        local_matrix = obj.parent.matrix_world.inverted_safe() @ obj.matrix_world
    else:
        local_matrix = obj.matrix_world.copy()
    return local_matrix.decompose()


_USD_MESH_TYPES = ("Sphere", "Cube", "Cylinder", "Cone", "Capsule")


def _infer_usd_type(obj) -> str:
    """Infer the USD typeName for a Blender object at auto-track time.

    Checks the mesh data name prefix against known parametric types
    (Sphere, Cube, etc.). Falls back to 'Mesh' for mesh objects or
    'Xform' for non-mesh.
    """
    if obj.type != "MESH" or obj.data is None:
        return "Xform"
    # Check mesh data name as a hint (works for freshly-created primitives)
    mesh_name = obj.data.name
    for usd_type in _USD_MESH_TYPES:
        if mesh_name.startswith(usd_type):
            return usd_type
    return "Mesh"


def sanitize_usd_name(name: str) -> str:
    """Convert a Blender object name to a valid USD identifier.

    USD identifiers must match [_a-zA-Z][_a-zA-Z0-9]*.
    Spaces, dots, and other invalid chars are replaced with underscores.
    """
    import re

    result = re.sub(r"[^_a-zA-Z0-9]", "_", name)
    if result and result[0].isdigit():
        result = "_" + result
    return result or "_unnamed"


# ---------------------------------------------------------------------------
# Scene properties
# ---------------------------------------------------------------------------
_SCENE_PROPS = [
    ("usd_connect_base_usd_path", bpy.props.StringProperty, {
        "name": "Base USD File",
        "description": "Path to the base USD file (.usda/.usd/.usdc)",
        "subtype": "FILE_PATH",
        "default": "",
    }),
    ("usd_connect_emit_to_file", bpy.props.BoolProperty, {
        "name": "Emit to File",
        "default": False,
    }),
    ("usd_connect_emit_file_path", bpy.props.StringProperty, {
        "name": "Diff Output File",
        "subtype": "FILE_PATH",
        "default": "",
    }),
    ("usd_connect_coalesce_seconds", bpy.props.FloatProperty, {
        "name": "Coalesce (sec)",
        "default": DEFAULT_COALESCE_SECONDS,
        "min": 0.0,
        "max": 5.0,
    }),
    ("usd_connect_import_skip_leaf_geom", bpy.props.BoolProperty, {
        "name": "Skip Leaf /Geom Prim Paths",
        "default": True,
    }),
    ("usd_connect_emit_host", bpy.props.StringProperty, {
        "name": "Server Host",
        "default": "127.0.0.1",
    }),
    ("usd_connect_emit_port", bpy.props.IntProperty, {
        "name": "Server Port",
        "default": 7200,
        "min": 1,
        "max": 65535,
    }),
    ("usd_connect_emit_hz", bpy.props.FloatProperty, {
        "name": "Send Rate (Hz)",
        "default": 60.0,
        "min": 1.0,
        "max": 120.0,
    }),
    ("usd_connect_net_emitter_running", bpy.props.BoolProperty, {
        "name": "Net Emitter Running",
        "default": False,
    }),
    ("usd_connect_auto_track", bpy.props.BoolProperty, {
        "name": "Auto-track New Objects",
        "description": (
            "Automatically register objects as USD prims when they are "
            "first manipulated, using the root prim path below"
        ),
        "default": False,
    }),
    ("usd_connect_auto_track_root", bpy.props.StringProperty, {
        "name": "Root Prim",
        "description": "Parent prim path for auto-tracked objects (e.g. /World)",
        "default": "/World",
    }),
    ("usd_connect_asset_root", bpy.props.StringProperty, {
        "name": "Asset Root",
        "description": "Root directory for resolving relative asset paths in set_reference events",
        "subtype": "DIR_PATH",
        "default": "",
    }),
]


def _ensure_scene_props():
    S = bpy.types.Scene
    for attr, prop_type, kwargs in _SCENE_PROPS:
        if not hasattr(S, attr):
            setattr(S, attr, prop_type(**kwargs))


# ---------------------------------------------------------------------------
# USDHook for import-time prim path tagging
# ---------------------------------------------------------------------------
class USD_CONNECT_Hook(bpy.types.USDHook):
    bl_idname = "usd_connect.hook"
    bl_label = "USD Connect Hook"

    # Set True by _import_ref_asset to suppress hook tagging during
    # reference/payload imports — the adapter handles tagging with
    # correct composed scene paths after import completes.
    _skip_tagging: bool = False

    @staticmethod
    def on_import(import_context):
        if USD_CONNECT_Hook._skip_tagging:
            return True
        try:
            prim_map = import_context.get_prim_map()
        except Exception:
            LOG.debug("USDHook: prim_map not available from import_context")
            prim_map = None

        try:
            stage = import_context.get_stage()
        except Exception:
            LOG.debug("USDHook: stage not available from import_context")
            stage = None

        stage_id = None
        if stage:
            try:
                stage_id = stage.GetRootLayer().identifier
            except Exception:
                LOG.debug("USDHook: could not extract stage_id")

        if not prim_map:
            LOG.info("USDHook: No prim map available; nothing tagged.")
            return True

        # Infer root prim from the stage's default prim and update auto_track_root
        if stage:
            try:
                default_prim = stage.GetDefaultPrim()
                if default_prim and default_prim.IsValid():
                    root_path = str(default_prim.GetPath())
                else:
                    # Fall back to the first root-level prim
                    root_prims = [p for p in stage.GetPseudoRoot().GetChildren()]
                    root_path = str(root_prims[0].GetPath()) if root_prims else None
                if root_path:
                    bpy.context.scene.usd_connect_auto_track_root = root_path
                    LOG.info("USDHook: Auto-track root set to %s", root_path)
            except Exception as e:
                LOG.warning("USDHook: Could not infer root prim: %s", e)

        skip_leaf_geom = bool(getattr(bpy.context.scene, "usd_connect_import_skip_leaf_geom", True))

        tagged = 0
        for prim_path, data_blocks in prim_map.items():
            prim_path_str = str(prim_path)
            if skip_leaf_geom and prim_path_str.endswith("/Geom"):
                continue
            # Look up prim type from the stage
            prim_type_name = ""
            if stage:
                try:
                    prim = stage.GetPrimAtPath(prim_path_str)
                    if prim and prim.IsValid():
                        prim_type_name = prim.GetTypeName()
                except Exception:
                    LOG.debug("USDHook: could not look up prim type for %s", prim_path_str)

            for db in data_blocks:
                if isinstance(db, bpy.types.Object):
                    db["usd_prim_path"] = prim_path_str
                    if prim_type_name:
                        db["usd_type_name"] = prim_type_name
                    if stage_id:
                        db["usd_stage_id"] = stage_id
                    tagged += 1

        LOG.info("USDHook: Tagged %d objects with usd_prim_path", tagged)
        return True


# ---------------------------------------------------------------------------
# BlenderStageAuthor — authors Blender depsgraph changes to a local USD stage
# ---------------------------------------------------------------------------
class BlenderStageAuthor:
    """Owns a local USD stage and authors Blender depsgraph changes to it.

    Transforms depsgraph updates into USD stage edits. The resulting
    Usd.Notice.ObjectsChanged notices drive a NoticeEmitter, which
    builds protocol events for network transport.
    """

    def __init__(self, base_usd_path: str):
        if not PXR_AVAILABLE:
            raise RuntimeError(f"OpenUSD 'pxr' not available: {_PXR_IMPORT_ERROR}")

        self.base_usd_path = (base_usd_path or "").strip()

        if not self.base_usd_path:
            raise RuntimeError("Base USD file path is empty.")

        self.base_layer = Sdf.Layer.FindOrOpen(self.base_usd_path)
        if self.base_layer is None:
            raise RuntimeError(f"Failed to open base USD layer: {self.base_usd_path}")

        self.stage = Usd.Stage.Open(self.base_layer)
        if self.stage is None:
            raise RuntimeError("Failed to open USD stage from base layer")

        self.delta_layer = self.stage.GetSessionLayer()
        self.stage.SetEditTarget(Usd.EditTarget(self.delta_layer))

        # Axis conversion: detect whether the stage is Y-up (needs Z↔Y swap)
        from openusdconnect.axis_conversion import needs_conversion

        self._needs_axis_conv: bool = needs_conversion(
            UsdGeom.GetStageUpAxis(self.stage)
        )

        self._last_matrix: dict = {}
        self._prim_refs: dict = {}  # prim_path -> bpy.types.Object reference
        self._used_prim_paths: set = set()
        self.auto_track: bool = False
        self.enabled: bool = False
        self._applying_remote: bool = False
        self._unloaded_payload_roots: set[str] = set()  # prim paths with unloaded payloads

    def initialize_baseline(self):
        """Snapshot current scene transforms for change detection."""
        for obj in bpy.context.scene.objects:
            if "usd_prim_path" not in obj:
                continue
            src = getattr(obj, "matrix_basis", obj.matrix_world)
            m = tuple(v for row in src for v in row)
            self._last_matrix[obj.name] = m

    def seed_used_paths(self):
        """Seed used-paths from scene objects (one-time O(N) scan)."""
        self._used_prim_paths = {
            obj.get("usd_prim_path") for obj in bpy.data.objects if obj.get("usd_prim_path")
        }

    def _resolve_prim_path(self, obj):
        """Resolve the prim path for a Blender object.

        Returns (prim_path, type_name) or (None, None) if the object
        should not be tracked.

        NOTE: *obj* may be the evaluated (CoW) copy from depsgraph.updates.
        Custom property reads MUST go through ``obj.original`` because
        the evaluated copy can have stale values — e.g. when
        _import_ref_asset overwrites USDHook's tags after the last
        depsgraph evaluation.  Writes also go to ``obj.original`` so
        they persist after the next evaluation cycle.
        """
        orig = getattr(obj, "original", obj)
        prim_path = orig.get("usd_prim_path")
        type_name = None
        if not prim_path:
            # Reverse-lookup: is this object already tracked by reference?
            # _prim_refs stores originals, so compare against obj.original.
            for pp, ref in self._prim_refs.items():
                if ref is orig:
                    prim_path = pp
                    break
        if not prim_path:
            if not self.auto_track:
                return None, None
            parent = getattr(obj, "parent", None)
            if parent is None:
                return None, None
            parent_orig = getattr(parent, "original", parent)
            parent_prim = parent_orig.get("usd_prim_path")
            if not parent_prim:
                return None, None
            usd_name = sanitize_usd_name(obj.name)
            prim_path = f"{parent_prim}/{usd_name}"
            if prim_path in self._used_prim_paths:
                i = 1
                while f"{prim_path}_{i}" in self._used_prim_paths:
                    i += 1
                prim_path = f"{prim_path}_{i}"
            self._used_prim_paths.add(prim_path)
            type_name = _infer_usd_type(obj)
            # Write to the original data-block so properties persist.
            # depsgraph.updates gives us evaluated copies — writes on those
            # are discarded after the next evaluation cycle.
            original = getattr(obj, "original", obj)
            original["usd_prim_path"] = prim_path
            original["usd_type_name"] = type_name
            LOG.info("Auto-tracked %r -> %s (%s)", obj.name, prim_path, type_name)
        return prim_path, type_name

    def _is_under_unloaded_payload(self, prim_path: str) -> bool:
        """Check if prim_path is under a known unloaded payload root."""
        return any(
            prim_path.startswith(root + "/")
            for root in self._unloaded_payload_roots
        )

    def _ensure_ancestors_on_stage(self, obj):
        """Ensure all ancestor prims exist on the local stage with xform ops.

        Walks from obj.parent upward collecting ancestors that haven't been
        tracked yet (not in _prim_refs), then defines/ensures them top-down.
        This handles both missing prims AND prims that exist on the base
        layer but lack canonical xform ops.
        """
        ancestors = []
        ancestor = obj.parent
        while ancestor is not None:
            pp = ancestor.get("usd_prim_path")
            if not pp:
                break
            if pp in self._prim_refs:
                break  # already tracked — already has ops
            ancestors.append((ancestor, pp))
            ancestor = ancestor.parent

        # Define/ensure top-down — skip prims that already exist via
        # composition (payload/reference children can't be redefined).
        for anc_obj, anc_path in reversed(ancestors):
            prim = self.stage.GetPrimAtPath(anc_path)
            if prim and prim.IsValid():
                pass  # already exists (composed or defined)
            else:
                tn = anc_obj.get("usd_type_name", "Xform")
                self.stage.DefinePrim(anc_path, tn)
            self._ensure_xform_ops(anc_path)
            self._author_xform(anc_path, anc_obj)
            self._prim_refs[anc_path] = anc_obj

    def _ensure_xform_ops(self, prim_path: str):
        """Ensure canonical xform ops (translate, orient, scale) exist on prim."""
        prim = self.stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid():
            return
        xf = UsdGeom.Xformable(prim)
        from openusdconnect.event_apply import find_op

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

    def _author_xform(self, prim_path: str, obj):
        """Author TRS from Blender object to the local USD stage."""
        prim = self.stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid():
            return

        loc, rot_quat, scl = _compute_local_trs(obj)

        from .blender_adapter import _PROP_USD_IMPORTED, _has_axis_rotation

        orig = getattr(obj, "original", obj)
        is_imported = orig.get(_PROP_USD_IMPORTED, False)
        parent_orig = getattr(obj, "parent", None)
        if parent_orig is not None:
            parent_orig = getattr(parent_orig, "original", parent_orig)
        parent_handles = _has_axis_rotation(parent_orig)

        tx, ty, tz = loc.x, loc.y, loc.z
        rw, rx, ry, rz = rot_quat.w, rot_quat.x, rot_quat.y, rot_quat.z
        sx, sy, sz = scl.x, scl.y, scl.z

        if self._needs_axis_conv and not parent_handles:
            if is_imported:
                # Imported root: strip Rx(90°) display rotation, keep T/S
                rw, rx, ry, rz = strip_axis_rotation(rw, rx, ry, rz)
            else:
                # Normal object: full Z-up → Y-up basis change
                tx, ty, tz = zup_to_yup_vec(tx, ty, tz)
                rw, rx, ry, rz = zup_to_yup_quat(rw, rx, ry, rz)
                sx, sy, sz = zup_to_yup_scale(sx, sy, sz)

        xf = UsdGeom.Xformable(prim)
        from openusdconnect.event_apply import find_op

        t_op = find_op(xf, "translate")
        o_op = find_op(xf, "orient")
        s_op = find_op(xf, "scale")

        if t_op:
            t_op.Set(Gf.Vec3d(tx, ty, tz))
        if o_op:
            o_op.Set(Gf.Quatf(rw, Gf.Vec3f(rx, ry, rz)))
        if s_op:
            s_op.Set(Gf.Vec3d(sx, sy, sz))

    def purge_prim_refs(self, prefix: str):
        """Remove tracked references for prims matching a path prefix."""
        to_purge = [pp for pp in self._prim_refs if pp.startswith(prefix)]
        for pp in to_purge:
            self._prim_refs.pop(pp, None)
            self._last_matrix.pop(pp, None)
            self._used_prim_paths.discard(pp)

    def _detect_deletions(self):
        """Check stored object references for deleted objects (ReferenceError).

        After undo, Blender invalidates Python references but the object
        still exists in bpy.data.objects with a new pointer.  Re-acquire
        the reference in that case instead of deactivating the prim.
        """
        stale_prims = []
        for pp, obj_ref in list(self._prim_refs.items()):
            try:
                _ = obj_ref.name
            except ReferenceError:
                stale_prims.append(pp)

        if not stale_prims:
            return

        # Single O(N) scan instead of O(N) per stale prim
        path_to_obj = {
            obj.get("usd_prim_path"): obj
            for obj in bpy.data.objects
            if obj.get("usd_prim_path")
        }

        for prim_path in stale_prims:
            found = path_to_obj.get(prim_path)
            if found is not None:
                # Re-acquire reference — object survived undo
                self._prim_refs[prim_path] = found
                LOG.info("Re-acquired reference for %s after undo", prim_path)
            else:
                # Truly deleted
                self._used_prim_paths.discard(prim_path)
                self._prim_refs.pop(prim_path, None)
                self._last_matrix.pop(prim_path, None)
                prim = self.stage.GetPrimAtPath(prim_path)
                if prim and prim.IsValid():
                    prim.SetActive(False)
                LOG.info("Object deleted: deactivating prim %s", prim_path)

    def on_depsgraph_update(self, updates: list):
        """Main entry: author depsgraph changes to the local USD stage.

        This triggers Usd.Notice.ObjectsChanged, which the NoticeEmitter
        picks up to build protocol events.
        """
        if not self.enabled or self._applying_remote:
            return

        # 1. Deletion detection
        self._detect_deletions()

        # 2. Process each updated object
        for update in updates:
            id_data = update.id
            if not isinstance(id_data, bpy.types.Object):
                continue
            obj = id_data

            prim_path, type_name = self._resolve_prim_path(obj)
            if not prim_path:
                continue

            # Store the *original* (non-evaluated) object reference for
            # deletion detection and identity lookups in _resolve_prim_path.
            self._prim_refs[prim_path] = getattr(obj, "original", obj)

            # Check if transform changed. Prefer matrix_basis (local)
            # to avoid re-emitting children when only the parent moved.
            src = getattr(obj, "matrix_basis", obj.matrix_world)
            m = tuple(v for row in src for v in row)
            last = self._last_matrix.get(obj.name)
            if last == m and self.stage.GetPrimAtPath(prim_path):
                continue
            self._last_matrix[obj.name] = m

            # Skip prims under unloaded payloads — they can't be defined
            # on the stage.  This window exists between an unload_payload and
            # the next load_payload when they arrive in separate transactions.
            if self._is_under_unloaded_payload(prim_path):
                continue

            # Ensure ancestors exist (structural — OUTSIDE ChangeBlock)
            self._ensure_ancestors_on_stage(obj)

            # Ensure prim exists on stage
            prim = self.stage.GetPrimAtPath(prim_path)
            if not prim or not prim.IsValid():
                obj_orig = getattr(obj, "original", obj)
                tn = type_name if type_name else obj_orig.get("usd_type_name", "Xform")
                self.stage.DefinePrim(prim_path, tn)

            # ALWAYS ensure canonical xform ops before authoring
            self._ensure_xform_ops(prim_path)

            # Author TRS
            self._author_xform(prim_path, obj)

    def send_rename(self, old_path: str, new_name: str):
        """Rename prim on local stage via NamespaceEditor.

        Triggers ObjectsChanged notice, which NoticeEmitter picks up
        as a rename event.
        """
        prim = self.stage.GetPrimAtPath(old_path)
        if not prim or not prim.IsValid():
            return

        parent = old_path.rsplit("/", 1)[0]
        new_path = f"{parent}/{new_name}"

        editor = Usd.NamespaceEditor(self.stage)
        editor.RenamePrim(prim, new_name)
        if editor.ApplyEdits():
            # Update internal caches
            if old_path in self._prim_refs:
                self._prim_refs[new_path] = self._prim_refs.pop(old_path)
            self._used_prim_paths.discard(old_path)
            self._used_prim_paths.add(new_path)

    def export_delta_as_string(self) -> str:
        """For Emit Diff feature — export session layer as USDA string."""
        return self.delta_layer.ExportToString()

    def clear_delta(self):
        """Clear the session layer delta."""
        self.delta_layer.Clear()


# ---------------------------------------------------------------------------
# NetworkSender — thin TCP sender for protocol events
# ---------------------------------------------------------------------------
class NetworkSender:
    """Thin TCP connection for sending protocol events to the server."""

    def __init__(self, host: str, port: int, client_id: str | None = None):
        self.host = host
        self.port = port
        self.client_id = client_id or f"blender-emitter-{os.getpid()}"
        self.sock: socket.socket | None = None

        # Lazy import to support vendored openusdconnect
        from openusdconnect.protocol import make_hello, make_quit, make_txn
        from openusdconnect.transport import send_line as _send_line

        self._make_hello = make_hello
        self._make_txn = make_txn
        self._make_quit = make_quit
        self._send_line = _send_line

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port))
        self._send_line(self.sock, self._make_hello("emitter", client_id=self.client_id))
        LOG.info("Network sender connected to %s:%d", self.host, self.port)

    def disconnect(self):
        if self.sock:
            try:
                self._send_line(self.sock, self._make_quit())
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def send_events(self, events: list):
        if not self.sock or not events:
            return
        txn = self._make_txn(self.client_id, events)
        try:
            self._send_line(self.sock, txn)
        except Exception:
            LOG.exception("Failed to send events")
            self.disconnect()


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
class _State:
    """Encapsulates the runtime state for the capture subsystem.

    Holds the three collaborating objects that form the emit pipeline:
    author -> notice_emitter -> sender.
    """

    __slots__ = ("author", "notice_emitter", "sender")

    def __init__(self):
        self.author: BlenderStageAuthor | None = None
        self.notice_emitter = None  # Optional[NoticeEmitter]
        self.sender: NetworkSender | None = None


_state = _State()


def _try_send_dirty_events(include_matrices: bool = False):
    """Build and send dirty events if emitter and sender are both connected."""
    if (
        _state.notice_emitter is None
        or _state.sender is None
        or _state.sender.sock is None
    ):
        return
    events = _state.notice_emitter.build_events_for_dirty(include_matrices=include_matrices)
    if events:
        from collections import Counter
        kinds = Counter(e["k"] for e in events)
        LOG.debug("sending %d events: %s", len(events), dict(kinds))
        _state.sender.send_events(events)


def set_emitter_feedback_guard(value: bool):
    """Set the feedback-loop guard to prevent echo during remote event application."""
    if _state.notice_emitter is not None:
        if value:
            _state.notice_emitter.suppress()
        else:
            _state.notice_emitter.unsuppress()
    if _state.author is not None:
        _state.author._applying_remote = value


def _reset_stage_author():
    if _state.author is not None:
        try:
            _state.author.enabled = False
        except Exception:
            pass
    _state.author = None
    if _state.notice_emitter is not None:
        _state.notice_emitter.cleanup()
        _state.notice_emitter = None


def _get_stage_author(context) -> BlenderStageAuthor:
    if _state.author is None:
        scene = context.scene
        _state.author = BlenderStageAuthor(
            base_usd_path=scene.usd_connect_base_usd_path,
        )
    return _state.author


# ---------------------------------------------------------------------------
# Depsgraph handler / Timer
# ---------------------------------------------------------------------------
def _depsgraph_handler(scene, depsgraph):
    try:
        updates = list(depsgraph.updates)

        has_object_updates = any(isinstance(update.id, bpy.types.Object) for update in updates)
        if not has_object_updates:
            return

        if _state.author is not None and _state.author.enabled:
            _state.author.auto_track = getattr(scene, "usd_connect_auto_track", False)
            obj_count = sum(1 for u in updates if isinstance(u.id, bpy.types.Object))
            _state.author.on_depsgraph_update(updates)
            dirty_count = len(_state.notice_emitter.dirty) if _state.notice_emitter else 0
            if dirty_count > 0:
                LOG.debug("depsgraph: %d obj updates, %d dirty prims", obj_count, dirty_count)
            _try_send_dirty_events(include_matrices=False)

    except Exception:
        LOG.exception("depsgraph handler error")


def _timer_tick():
    try:
        if _state.author is None:
            return None
        if not _state.author.enabled:
            return None
        return 0.1
    except Exception:
        LOG.exception("timer error")
        return 0.5


def _remove_handler():
    try:
        bpy.app.handlers.depsgraph_update_post.remove(_depsgraph_handler)
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------
class USD_CONNECT_OT_import_with_hook(bpy.types.Operator):
    bl_idname = "usd_connect.import_with_hook"
    bl_label = "Import USD (with prim tagging)"
    bl_description = "Import USD and tag imported objects with their USD prim paths"
    filepath: bpy.props.StringProperty(subtype="FILE_PATH")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        if not PXR_AVAILABLE:
            self.report({"ERROR"}, f"OpenUSD 'pxr' not available: {_PXR_IMPORT_ERROR}")
            return {"CANCELLED"}
        try:
            bpy.ops.wm.usd_import(filepath=self.filepath)
            context.scene.usd_connect_base_usd_path = self.filepath
            # Apply MaterialX materials that Blender's importer doesn't handle
            from .blender_adapter import BlenderAdapter

            adapter = BlenderAdapter()
            adapter._enrich_materialx_from_import(self.filepath, "", "")
            self.report({"INFO"}, "USD imported with prim tagging")
        except Exception as e:
            self.report({"ERROR"}, f"USD import failed: {e}")
            return {"CANCELLED"}
        return {"FINISHED"}


class USD_CONNECT_OT_start_capture(bpy.types.Operator):
    bl_idname = "usd_connect.start_capture"
    bl_label = "Start Capture"
    bl_description = "Start capturing Blender transform edits into USD session-layer delta"

    def execute(self, context):
        if not PXR_AVAILABLE:
            self.report({"ERROR"}, f"OpenUSD 'pxr' not available: {_PXR_IMPORT_ERROR}")
            return {"CANCELLED"}
        try:
            author = _get_stage_author(context)
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        author.enabled = True
        author.initialize_baseline()
        _remove_handler()
        bpy.app.handlers.depsgraph_update_post.append(_depsgraph_handler)
        bpy.app.timers.register(_timer_tick, first_interval=0.1)
        self.report({"INFO"}, f"Capture started (base: {author.base_usd_path})")
        return {"FINISHED"}


class USD_CONNECT_OT_stop_capture(bpy.types.Operator):
    bl_idname = "usd_connect.stop_capture"
    bl_label = "Stop Capture"

    def execute(self, context):
        if _state.author is not None:
            _state.author.enabled = False
        _remove_handler()
        self.report({"INFO"}, "Capture stopped")
        return {"FINISHED"}


class USD_CONNECT_OT_emit_diff(bpy.types.Operator):
    bl_idname = "usd_connect.emit_diff"
    bl_label = "Emit Diff"
    bl_description = "Export the USD session layer delta as .usda"

    def execute(self, context):
        if not PXR_AVAILABLE:
            self.report({"ERROR"}, f"OpenUSD 'pxr' not available: {_PXR_IMPORT_ERROR}")
            return {"CANCELLED"}
        if _state.author is None:
            self.report({"ERROR"}, "Capture not running. Start capture first.")
            return {"CANCELLED"}
        payload = _state.author.export_delta_as_string()
        print("\n========== USD DELTA (SESSION LAYER) BEGIN ==========")
        print(payload)
        print("=========== USD DELTA (SESSION LAYER) END ===========\n")

        scene = context.scene
        if scene.usd_connect_emit_to_file:
            out_path = (scene.usd_connect_emit_file_path or "").strip()
            if not out_path:
                self.report({"WARNING"}, "Emit-to-file enabled but no output path set.")
                return {"FINISHED"}
            try:
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(payload)
                self.report({"INFO"}, f"Diff written to {out_path}")
            except Exception as e:
                self.report({"ERROR"}, f"Failed to write diff: {e}")
        return {"FINISHED"}


class USD_CONNECT_OT_clear_diff(bpy.types.Operator):
    bl_idname = "usd_connect.clear_diff"
    bl_label = "Clear Diff"

    def execute(self, context):
        if _state.author is None:
            self.report({"ERROR"}, "Capture not running.")
            return {"CANCELLED"}
        _state.author.clear_delta()
        self.report({"INFO"}, "Delta layer cleared.")
        return {"FINISHED"}


class USD_CONNECT_OT_connect_emitter(bpy.types.Operator):
    bl_idname = "usd_connect.connect_emitter"
    bl_label = "Connect Emitter"
    bl_description = "Connect to sync server and start sending transform events"

    def execute(self, context):
        scene = context.scene
        if _state.sender is not None and _state.sender.sock is not None:
            self.report({"INFO"}, "Already connected")
            return {"CANCELLED"}
        try:
            if _state.author is None:
                _state.author = BlenderStageAuthor(
                    base_usd_path=scene.usd_connect_base_usd_path,
                )
            _state.author.enabled = True
            _state.author.auto_track = scene.usd_connect_auto_track
            _state.author.initialize_baseline()
            _state.author.seed_used_paths()

            from openusdconnect.emitter import NoticeEmitter

            _state.notice_emitter = NoticeEmitter(_state.author.stage)

            _state.sender = NetworkSender(
                host=scene.usd_connect_emit_host,
                port=scene.usd_connect_emit_port,
            )
            _state.sender.connect()

            _remove_handler()
            bpy.app.handlers.depsgraph_update_post.append(_depsgraph_handler)
            scene.usd_connect_net_emitter_running = True
            host = scene.usd_connect_emit_host
            port = scene.usd_connect_emit_port
            self.report({"INFO"}, f"Connected to {host}:{port}")
        except Exception as e:
            self.report({"ERROR"}, f"Connection failed: {e}")
            return {"CANCELLED"}
        return {"FINISHED"}


class USD_CONNECT_OT_disconnect_emitter(bpy.types.Operator):
    bl_idname = "usd_connect.disconnect_emitter"
    bl_label = "Disconnect Emitter"

    def execute(self, context):
        if _state.sender is not None:
            _state.sender.disconnect()
            _state.sender = None
        if _state.notice_emitter is not None:
            _state.notice_emitter.cleanup()
            _state.notice_emitter = None
        _state.author = None
        context.scene.usd_connect_net_emitter_running = False
        self.report({"INFO"}, "Emitter disconnected")
        return {"FINISHED"}


class USD_CONNECT_OT_rename_prim(bpy.types.Operator):
    bl_idname = "usd_connect.rename_prim"
    bl_label = "Rename USD Prim"
    bl_description = (
        "Rename the USD prim for the active object. "
        "Updates the prim path on the server via rename_prim event."
    )

    new_name: bpy.props.StringProperty(
        name="New Name",
        description="New prim name (leaf segment, not full path)",
        default="",
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and "usd_prim_path" in obj

    def invoke(self, context, event):
        obj = context.active_object
        current_path = obj.get("usd_prim_path", "")
        self.new_name = current_path.rsplit("/", 1)[-1] if "/" in current_path else current_path
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        obj = context.active_object
        old_path = obj.get("usd_prim_path", "")
        if not old_path:
            self.report({"ERROR"}, "Active object has no usd_prim_path")
            return {"CANCELLED"}

        new_name = self.new_name.strip()
        if not new_name:
            self.report({"ERROR"}, "New name cannot be empty")
            return {"CANCELLED"}

        parent = old_path.rsplit("/", 1)[0]
        new_path = f"{parent}/{new_name}"

        if _state.author is not None:
            _state.author.send_rename(old_path, new_name)
            _try_send_dirty_events(include_matrices=False)

        obj["usd_prim_path"] = new_path
        self.report({"INFO"}, f"Renamed: {old_path} → {new_path}")
        return {"FINISHED"}


class USD_CONNECT_OT_print_import_props(bpy.types.Operator):
    bl_idname = "usd_connect.print_import_props"
    bl_label = "Debug: Print USD Import Props"

    def execute(self, context):
        try:
            keys = list(bpy.ops.wm.usd_import.get_rna_type().properties.keys())
            print("[usd_import props]", keys)
            self.report({"INFO"}, "Printed to console")
        except Exception as e:
            self.report({"ERROR"}, f"Failed: {e}")
        return {"FINISHED"}


_CAPTURE_CLASSES = (
    USD_CONNECT_Hook,
    USD_CONNECT_OT_import_with_hook,
    USD_CONNECT_OT_start_capture,
    USD_CONNECT_OT_stop_capture,
    USD_CONNECT_OT_emit_diff,
    USD_CONNECT_OT_clear_diff,
    USD_CONNECT_OT_connect_emitter,
    USD_CONNECT_OT_disconnect_emitter,
    USD_CONNECT_OT_rename_prim,
    USD_CONNECT_OT_print_import_props,
)


def register():
    _ensure_scene_props()
    for c in _CAPTURE_CLASSES:
        bpy.utils.register_class(c)


def unregister():
    _remove_handler()
    _reset_stage_author()
    if _state.sender is not None:
        _state.sender.disconnect()
        _state.sender = None
    for c in reversed(_CAPTURE_CLASSES):
        bpy.utils.unregister_class(c)
    for attr, _, _ in _SCENE_PROPS:
        if hasattr(bpy.types.Scene, attr):
            try:
                delattr(bpy.types.Scene, attr)
            except Exception:
                pass
