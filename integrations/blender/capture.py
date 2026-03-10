"""Blender depsgraph -> emitter bridge.

Captures Blender object transform edits and sends them over the network
via the openusdconnect protocol. Also handles USD import tagging via USDHook
and local diff capture to session layer.
"""

from __future__ import annotations

import time
import json
import socket
import logging
from math import degrees
from typing import Optional

from openusdconnect.emitter import near_list

import bpy

try:
    from pxr import Usd, Sdf, UsdGeom, Gf, Tf
    PXR_AVAILABLE = True
except Exception as e:
    PXR_AVAILABLE = False
    _PXR_IMPORT_ERROR = e

LOG = logging.getLogger(__name__)

DEFAULT_COALESCE_SECONDS = 0.15


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
def _ensure_scene_props():
    S = bpy.types.Scene
    if not hasattr(S, "usd_connect_base_usd_path"):
        S.usd_connect_base_usd_path = bpy.props.StringProperty(
            name="Base USD File",
            description="Path to the base USD file (.usda/.usd/.usdc)",
            subtype="FILE_PATH",
            default="",
        )
    if not hasattr(S, "usd_connect_emit_to_file"):
        S.usd_connect_emit_to_file = bpy.props.BoolProperty(
            name="Emit to File", default=False,
        )
    if not hasattr(S, "usd_connect_emit_file_path"):
        S.usd_connect_emit_file_path = bpy.props.StringProperty(
            name="Diff Output File", subtype="FILE_PATH", default="",
        )
    if not hasattr(S, "usd_connect_coalesce_seconds"):
        S.usd_connect_coalesce_seconds = bpy.props.FloatProperty(
            name="Coalesce (sec)", default=DEFAULT_COALESCE_SECONDS,
            min=0.0, max=5.0,
        )
    if not hasattr(S, "usd_connect_import_skip_leaf_geom"):
        S.usd_connect_import_skip_leaf_geom = bpy.props.BoolProperty(
            name="Skip Leaf /Geom Prim Paths", default=True,
        )
    # Network emitter props
    if not hasattr(S, "usd_connect_emit_host"):
        S.usd_connect_emit_host = bpy.props.StringProperty(
            name="Server Host", default="127.0.0.1",
        )
    if not hasattr(S, "usd_connect_emit_port"):
        S.usd_connect_emit_port = bpy.props.IntProperty(
            name="Server Port", default=7200, min=1, max=65535,
        )
    if not hasattr(S, "usd_connect_emit_hz"):
        S.usd_connect_emit_hz = bpy.props.FloatProperty(
            name="Send Rate (Hz)", default=60.0, min=1.0, max=120.0,
        )
    if not hasattr(S, "usd_connect_net_emitter_running"):
        S.usd_connect_net_emitter_running = bpy.props.BoolProperty(
            name="Net Emitter Running", default=False,
        )
    if not hasattr(S, "usd_connect_auto_track"):
        S.usd_connect_auto_track = bpy.props.BoolProperty(
            name="Auto-track New Objects",
            description=(
                "Automatically register objects as USD prims when they are "
                "first manipulated, using the root prim path below"
            ),
            default=False,
        )
    if not hasattr(S, "usd_connect_auto_track_root"):
        S.usd_connect_auto_track_root = bpy.props.StringProperty(
            name="Root Prim",
            description="Parent prim path for auto-tracked objects (e.g. /World)",
            default="/World",
        )


# ---------------------------------------------------------------------------
# USDHook for import-time prim path tagging
# ---------------------------------------------------------------------------
class USD_CONNECT_Hook(bpy.types.USDHook):
    bl_idname = "usd_connect.hook"
    bl_label = "USD Connect Hook"

    @staticmethod
    def on_import(import_context):
        try:
            prim_map = import_context.get_prim_map()
        except Exception:
            prim_map = None

        try:
            stage = import_context.get_stage()
        except Exception:
            stage = None

        stage_id = None
        if stage:
            try:
                stage_id = stage.GetRootLayer().identifier
            except Exception:
                pass

        if not prim_map:
            print("[USD Connect Hook] No prim map available; nothing tagged.")
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
                    print(f"[USD Connect Hook] Auto-track root set to {root_path}")
            except Exception as e:
                print(f"[USD Connect Hook] Could not infer root prim: {e}")

        skip_leaf_geom = bool(getattr(bpy.context.scene, "usd_connect_import_skip_leaf_geom", True))

        tagged = 0
        for prim_path, data_blocks in prim_map.items():
            prim_path_str = str(prim_path)
            if skip_leaf_geom and prim_path_str.endswith("/Geom"):
                continue
            for db in data_blocks:
                if isinstance(db, bpy.types.Object):
                    db["usd_prim_path"] = prim_path_str
                    if stage_id:
                        db["usd_stage_id"] = stage_id
                    tagged += 1

        print(f"[USD Connect Hook] Tagged {tagged} objects with usd_prim_path")
        return True


# ---------------------------------------------------------------------------
# Core USD delta capture engine (local session layer diffs)
# ---------------------------------------------------------------------------
class _UsdDeltaEngine:
    """Opens base USD stage, captures Blender transform edits into SessionLayer."""

    def __init__(self, base_usd_path: str, coalesce_seconds: float):
        if not PXR_AVAILABLE:
            raise RuntimeError(f"OpenUSD 'pxr' not available: {_PXR_IMPORT_ERROR}")

        self.base_usd_path = (base_usd_path or "").strip()
        self.coalesce_seconds = max(0.0, float(coalesce_seconds))

        if not self.base_usd_path:
            raise RuntimeError("Base USD file path is empty.")

        self.base_layer = Sdf.Layer.FindOrOpen(self.base_usd_path)
        if self.base_layer is None:
            raise RuntimeError(f"Failed to open base USD layer: {self.base_usd_path}")

        self.stage = Usd.Stage.Open(self.base_layer)
        if self.stage is None:
            raise RuntimeError("Failed to open USD stage from base layer")

        self.base_stage = Usd.Stage.Open(self.base_layer)
        self.delta_layer = self.stage.GetSessionLayer()
        self.stage.SetEditTarget(Usd.EditTarget(self.delta_layer))

        self._last_matrix = {}
        self._pending_paths = set()
        self._dirty = False
        self._next_emit_time = 0.0
        self.enabled = False

    def initialize_baseline(self):
        for obj in bpy.context.scene.objects:
            if "usd_prim_path" not in obj:
                continue
            m = tuple(v for row in obj.matrix_world for v in row)
            self._last_matrix[obj.name] = m

    def usd_path_for_object(self, obj) -> Optional[str]:
        if obj is None:
            return None
        p = obj.get("usd_prim_path")
        if not p:
            return None
        p = str(p).strip()
        if not p.startswith("/"):
            return None
        return p

    def _get_or_add_op(self, xf, op_type):
        for op in xf.GetOrderedXformOps():
            if op.GetOpType() == op_type:
                return op
        return xf.AddXformOp(op_type)

    def author_xform_from_object(self, obj):
        """Mirror Blender object TRS into USD xform ops using quaternion rotation."""
        path = self.usd_path_for_object(obj)
        if not path:
            return

        base_prim = self.base_stage.GetPrimAtPath(path)
        if not base_prim or not base_prim.IsValid():
            return
        if not UsdGeom.Xformable(base_prim):
            return

        # Compute local-to-parent transform
        if obj.parent:
            parent_inv = obj.parent.matrix_world.inverted_safe()
            local_matrix = parent_inv @ obj.matrix_world
        else:
            local_matrix = obj.matrix_world.copy()

        # Decompose to TRS (quaternion rotation)
        loc, rot_quat, scl = local_matrix.decompose()

        with Usd.EditContext(self.stage, self.delta_layer):
            prim = self.stage.GetPrimAtPath(path)
            if not prim or not prim.IsValid():
                return

            xf = UsdGeom.Xformable(prim)
            if not xf:
                return

            t_op = self._get_or_add_op(xf, UsdGeom.XformOp.TypeTranslate)
            o_op = self._get_or_add_op(xf, UsdGeom.XformOp.TypeOrient)
            s_op = self._get_or_add_op(xf, UsdGeom.XformOp.TypeScale)

            t_op.Set(Gf.Vec3d(loc.x, loc.y, loc.z))
            o_op.Set(Gf.Quatf(rot_quat.w, Gf.Vec3f(rot_quat.x, rot_quat.y, rot_quat.z)))
            s_op.Set(Gf.Vec3d(scl.x, scl.y, scl.z))

        self._pending_paths.add(path)
        self._dirty = True

    def export_delta_as_string(self) -> str:
        return self.delta_layer.ExportToString()

    def clear_delta(self):
        self.delta_layer.Clear()
        self._pending_paths.clear()
        self._dirty = False

    def mark_for_emit(self):
        now = time.time()
        self._next_emit_time = max(self._next_emit_time, now + self.coalesce_seconds)

    def on_depsgraph_update(self, depsgraph):
        if not self.enabled:
            return
        self.on_depsgraph_update_from_list(list(depsgraph.updates))

    def on_depsgraph_update_from_list(self, updates):
        """Process a pre-collected list of depsgraph updates."""
        if not self.enabled:
            return
        any_change = False
        for update in updates:
            id_data = update.id
            if not isinstance(id_data, bpy.types.Object):
                continue
            obj = id_data
            if "usd_prim_path" not in obj:
                continue
            m = tuple(v for row in obj.matrix_world for v in row)
            last = self._last_matrix.get(obj.name)
            if last != m:
                self._last_matrix[obj.name] = m
                self.author_xform_from_object(obj)
                any_change = True
        if any_change:
            self.mark_for_emit()

    def on_timer_tick(self):
        if not self.enabled:
            return None
        return 0.1


# ---------------------------------------------------------------------------
# Network Emitter — sends captured edits to the server
# ---------------------------------------------------------------------------
class _NetworkEmitter:
    """Wraps a TCP connection and sends TRS events built from Blender depsgraph changes.

    Caches last-sent TRS per prim and only sends:
    - ensure_prim + ensure_xform_ops on first encounter
    - set_xform_trs with only the changed fields (partial diff)
    """

    EPS = 1e-7  # tolerance for float comparison

    def __init__(self, host: str, port: int, client_id: str = "blender-emitter", hz: float = 60.0,
                 auto_track: bool = False, auto_track_root: str = "/World"):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.hz = hz
        self.auto_track = auto_track
        self.auto_track_root = auto_track_root.rstrip("/")
        self.sock: Optional[socket.socket] = None
        self._applying_remote = False  # feedback loop guard
        self._known_prims: set = set()  # prims we've already sent ensure_prim for
        self._last_sent: dict = {}  # prim_path -> {"t": [...], "r": [...], "s": [...]}
        self._tracked_objects: dict = {}  # obj.name -> prim_path (for deletion detection)

        # Lazy import to support vendored openusdconnect
        from openusdconnect.protocol import make_hello, make_txn, make_quit
        from openusdconnect.transport import send_line as _send_line
        self._make_hello = make_hello
        self._make_txn = make_txn
        self._make_quit = make_quit
        self._send_line = _send_line

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port))
        self._send_line(self.sock, self._make_hello("emitter"))
        self._known_prims.clear()
        self._last_sent.clear()
        self._tracked_objects.clear()
        print(f"[USD Connect] Network emitter connected to {self.host}:{self.port}")

    def disconnect(self):
        if self.sock:
            try:
                self._send_line(self.sock, self._make_quit())
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
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

    def build_events_from_depsgraph(self, depsgraph) -> list:
        """Build protocol events from Blender depsgraph updates."""
        return self.build_events_from_updates(list(depsgraph.updates))

    def build_events_from_updates(self, updates) -> list:
        """Build protocol events from a pre-collected list of depsgraph updates.

        Only sends ensure_prim/ensure_xform_ops once per prim.
        Only sends changed TRS fields (partial diff against last-sent cache).
        Detects deleted objects and emits deactivate_prim events.
        """
        if self._applying_remote:
            return []

        events = []

        # Deletion detection: check if any tracked objects have been removed from the scene
        existing_obj_names = {obj.name for obj in bpy.data.objects}
        deleted_names = [name for name in self._tracked_objects if name not in existing_obj_names]
        for name in deleted_names:
            prim_path = self._tracked_objects.pop(name)
            self._known_prims.discard(prim_path)
            self._last_sent.pop(prim_path, None)
            events.append({"k": "deactivate_prim", "prim": prim_path, "active": False})
            print(f"[USD Connect] Object deleted: {name!r} → deactivate_prim {prim_path}")

        for update in updates:
            id_data = update.id
            if not isinstance(id_data, bpy.types.Object):
                continue
            obj = id_data
            prim_path = obj.get("usd_prim_path")
            if not prim_path:
                if not self.auto_track:
                    continue
                # Auto-assign a prim path from the object name
                usd_name = sanitize_usd_name(obj.name)
                prim_path = f"{self.auto_track_root}/{usd_name}"
                obj["usd_prim_path"] = prim_path
                print(f"[USD Connect] Auto-tracked {obj.name!r} → {prim_path}")

            # Track this object for future deletion detection
            self._tracked_objects[obj.name] = prim_path

            # Compute local transform
            if obj.parent:
                parent_inv = obj.parent.matrix_world.inverted_safe()
                local_matrix = parent_inv @ obj.matrix_world
            else:
                local_matrix = obj.matrix_world.copy()

            loc, rot_quat, scl = local_matrix.decompose()

            t = [loc.x, loc.y, loc.z]
            r = [rot_quat.w, rot_quat.x, rot_quat.y, rot_quat.z]
            s = [scl.x, scl.y, scl.z]

            # Only send structural events on first encounter
            if prim_path not in self._known_prims:
                events.append({"k": "ensure_prim", "prim": prim_path, "typeName": "Xform"})
                events.append({"k": "ensure_xform_ops", "prim": prim_path})
                self._known_prims.add(prim_path)

            # Diff against last-sent values
            last = self._last_sent.get(prim_path, {})
            fields = []
            payload = {"k": "set_xform_trs", "prim": prim_path, "fields": fields}

            if not near_list(t, last.get("t"), self.EPS):
                fields.append("t")
                payload["t"] = t
            if not near_list(r, last.get("r"), self.EPS):
                fields.append("r")
                payload["r"] = r
            if not near_list(s, last.get("s"), self.EPS):
                fields.append("s")
                payload["s"] = s

            if fields:
                events.append(payload)
                self._last_sent[prim_path] = {"t": t, "r": r, "s": s}

        return events

    def send_rename(self, prim_path: str, new_name: str):
        """Send a rename_prim event and update internal caches."""
        if not self.sock:
            return
        # Update caches: old_path -> new_path
        parent = prim_path.rsplit("/", 1)[0]
        new_path = f"{parent}/{new_name}"

        if prim_path in self._known_prims:
            self._known_prims.discard(prim_path)
            self._known_prims.add(new_path)
        if prim_path in self._last_sent:
            self._last_sent[new_path] = self._last_sent.pop(prim_path)
        for obj_name, path in list(self._tracked_objects.items()):
            if path == prim_path:
                self._tracked_objects[obj_name] = new_path

        self.send_events([{"k": "rename_prim", "prim": prim_path, "new_name": new_name}])


# ---------------------------------------------------------------------------
# Module-level engine / emitter state
# ---------------------------------------------------------------------------
_ENGINE: Optional[_UsdDeltaEngine] = None
_NET_EMITTER: Optional[_NetworkEmitter] = None


def _reset_engine():
    global _ENGINE
    if _ENGINE is not None:
        try:
            _ENGINE.enabled = False
        except Exception:
            pass
    _ENGINE = None


def _get_engine(context) -> _UsdDeltaEngine:
    global _ENGINE
    if _ENGINE is None:
        scene = context.scene
        _ENGINE = _UsdDeltaEngine(
            base_usd_path=scene.usd_connect_base_usd_path,
            coalesce_seconds=scene.usd_connect_coalesce_seconds,
        )
    return _ENGINE


# ---------------------------------------------------------------------------
# Depsgraph handler / Timer
# ---------------------------------------------------------------------------
def _depsgraph_handler(scene, depsgraph):
    try:
        # Collect updates into a list so we can iterate multiple times
        # (depsgraph.updates may only be iterable once per callback)
        updates = list(depsgraph.updates)

        has_object_updates = any(
            isinstance(update.id, bpy.types.Object)
            for update in updates
        )
        if not has_object_updates:
            return

        # Local capture
        if _ENGINE is not None and _ENGINE.enabled:
            _ENGINE.on_depsgraph_update_from_list(updates)

        # Network emitter
        if _NET_EMITTER is not None and _NET_EMITTER.sock is not None:
            if not _NET_EMITTER._applying_remote:
                events = _NET_EMITTER.build_events_from_updates(updates)
                if events:
                    _NET_EMITTER.send_events(events)
    except Exception as e:
        print("[USD Connect] depsgraph handler error:", e)
        import traceback
        traceback.print_exc()


def _timer_tick():
    try:
        if _ENGINE is None:
            return None
        return _ENGINE.on_timer_tick()
    except Exception as e:
        print("[USD Connect] timer error:", e)
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
        _reset_engine()
        try:
            eng = _get_engine(context)
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        eng.enabled = True
        eng.initialize_baseline()
        _remove_handler()
        bpy.app.handlers.depsgraph_update_post.append(_depsgraph_handler)
        bpy.app.timers.register(_timer_tick, first_interval=0.1)
        self.report({"INFO"}, f"Capture started (base: {eng.base_usd_path})")
        return {"FINISHED"}


class USD_CONNECT_OT_stop_capture(bpy.types.Operator):
    bl_idname = "usd_connect.stop_capture"
    bl_label = "Stop Capture"

    def execute(self, context):
        if _ENGINE is not None:
            _ENGINE.enabled = False
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
        if _ENGINE is None:
            self.report({"ERROR"}, "Engine not running. Start capture first.")
            return {"CANCELLED"}
        payload = _ENGINE.export_delta_as_string()
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
        if _ENGINE is None:
            self.report({"ERROR"}, "Engine not running.")
            return {"CANCELLED"}
        _ENGINE.clear_delta()
        self.report({"INFO"}, "Delta layer cleared.")
        return {"FINISHED"}


class USD_CONNECT_OT_connect_emitter(bpy.types.Operator):
    bl_idname = "usd_connect.connect_emitter"
    bl_label = "Connect Emitter"
    bl_description = "Connect to sync server and start sending transform events"

    def execute(self, context):
        global _NET_EMITTER
        scene = context.scene
        if _NET_EMITTER is not None and _NET_EMITTER.sock is not None:
            self.report({"INFO"}, "Already connected")
            return {"CANCELLED"}
        try:
            _NET_EMITTER = _NetworkEmitter(
                host=scene.usd_connect_emit_host,
                port=scene.usd_connect_emit_port,
                hz=scene.usd_connect_emit_hz,
                auto_track=scene.usd_connect_auto_track,
                auto_track_root=scene.usd_connect_auto_track_root,
            )
            _NET_EMITTER.connect()
            # Ensure depsgraph handler is registered
            _remove_handler()
            bpy.app.handlers.depsgraph_update_post.append(_depsgraph_handler)
            scene.usd_connect_net_emitter_running = True
            self.report({"INFO"}, f"Connected to {scene.usd_connect_emit_host}:{scene.usd_connect_emit_port}")
        except Exception as e:
            self.report({"ERROR"}, f"Connection failed: {e}")
            return {"CANCELLED"}
        return {"FINISHED"}


class USD_CONNECT_OT_disconnect_emitter(bpy.types.Operator):
    bl_idname = "usd_connect.disconnect_emitter"
    bl_label = "Disconnect Emitter"

    def execute(self, context):
        global _NET_EMITTER
        if _NET_EMITTER is not None:
            _NET_EMITTER.disconnect()
            _NET_EMITTER = None
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

        if _NET_EMITTER is not None and _NET_EMITTER.sock is not None:
            _NET_EMITTER.send_rename(old_path, new_name)

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
    _reset_engine()
    global _NET_EMITTER
    if _NET_EMITTER is not None:
        _NET_EMITTER.disconnect()
        _NET_EMITTER = None
    for c in reversed(_CAPTURE_CLASSES):
        bpy.utils.unregister_class(c)
    for prop_name in (
        "usd_connect_base_usd_path", "usd_connect_emit_to_file",
        "usd_connect_emit_file_path", "usd_connect_coalesce_seconds",
        "usd_connect_import_skip_leaf_geom", "usd_connect_emit_host",
        "usd_connect_emit_port", "usd_connect_emit_hz",
        "usd_connect_net_emitter_running",
        "usd_connect_auto_track", "usd_connect_auto_track_root",
    ):
        if hasattr(bpy.types.Scene, prop_name):
            try:
                delattr(bpy.types.Scene, prop_name)
            except Exception:
                pass
