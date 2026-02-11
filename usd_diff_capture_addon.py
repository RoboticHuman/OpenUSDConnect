# SPDX-License-Identifier: MIT
"""
Blender Add-on: USD Diff Capture (Delta Layer) + USDHook Import Tagging
Blender target: 4.4+

What this add-on does (so far)
==============================
1) Import USD with a USDHook that tags created Blender Objects with the *true* USD prim path:
      obj["usd_prim_path"] = "/World/Sphere"
   This eliminates brittle name-guessing and fixes the "Geom / Geom_001" problem.

2) Open a *base* USD file as a Usd.Stage (file path / asset-resolver friendly),
   then capture edits into the stage SessionLayer (delta layer). "Diff" = SessionLayer.

3) Listen to Blender depsgraph updates; when an object with usd_prim_path changes transform,
   author xform ops to that prim path in the delta layer and allow emitting the delta as .usda.

4) Emit diff:
      delta_layer.ExportToString()
   Print to console and optionally write to disk.

Important current limitations
=============================
- Only transform (TRS) edits are mirrored.
- Only objects that have obj["usd_prim_path"] are considered (robust mode).
- By default we SKIP prim paths that end with "/Geom" (leaf geometry prims) during import tagging,
  because for most workflows you want to author transforms on the Xform prim (e.g. /World/Sphere).
  You can disable that with a setting in the UI.

How to test quickly (demo)
==========================
1) Save test_scene.usda to disk (use the demo file we provided earlier).
2) Install this add-on (Preferences > Add-ons > Install...).
3) In the 3D Viewport press N, open "USD Diff" tab.
4) Click "Import USD (with prim tagging)" and choose test_scene.usda.
5) Set "Base USD File" to that same test_scene.usda.
6) Click "Start Capture".
7) Move Sphere/Cube in Blender.
8) Click "Emit Diff" and check your system console output:
   - Windows: Window > Toggle System Console
   - macOS/Linux: run Blender from a terminal

Notes on USDHook wiring
=======================
Blender’s usd_import operator has hook-related properties that can vary.
This add-on attempts several likely property names and falls back gracefully.
If it fails on your build, use the "Debug: Print USD Import Props" button to see the keys.
"""

bl_info = {
    "name": "USD Diff Capture (Delta Layer + USDHook tagging)",
    "author": "ChatGPT",
    "version": (0, 3, 0),
    "blender": (4, 4, 0),
    "location": "View3D > Sidebar > USD Diff",
    "description": "Captures Blender transform edits into a USD session-layer delta and emits it as diffs, with USDHook-based prim path tagging on import.",
    "category": "Import-Export",
}

import bpy
import time
from math import degrees

# Make bundled `pxr` module available if Blender provides it this way (Blender 4.4 docs show this).
try:
    bpy.utils.expose_bundled_modules()
except Exception:
    pass

try:
    from pxr import Usd, Sdf, UsdGeom, Gf, Tf
    PXR_AVAILABLE = True
except Exception as e:
    PXR_AVAILABLE = False
    _PXR_IMPORT_ERROR = e

DEFAULT_COALESCE_SECONDS = 0.15


# -----------------------------------------------------------------------------
# Scene properties
# -----------------------------------------------------------------------------
def _ensure_scene_props():
    if not hasattr(bpy.types.Scene, "usd_diff_base_usd_path"):
        bpy.types.Scene.usd_diff_base_usd_path = bpy.props.StringProperty(
            name="Base USD File",
            description="Path to the base USD file (.usda/.usd/.usdc) opened as the stage root layer",
            subtype="FILE_PATH",
            default="",
        )
    if not hasattr(bpy.types.Scene, "usd_diff_emit_to_file"):
        bpy.types.Scene.usd_diff_emit_to_file = bpy.props.BoolProperty(
            name="Emit to File",
            description="Also write emitted diff payload to a file",
            default=False,
        )
    if not hasattr(bpy.types.Scene, "usd_diff_emit_file_path"):
        bpy.types.Scene.usd_diff_emit_file_path = bpy.props.StringProperty(
            name="Diff Output File",
            description="Where to write the emitted diff (.usda) if 'Emit to File' is enabled",
            subtype="FILE_PATH",
            default="",
        )
    if not hasattr(bpy.types.Scene, "usd_diff_coalesce_seconds"):
        bpy.types.Scene.usd_diff_coalesce_seconds = bpy.props.FloatProperty(
            name="Coalesce (sec)",
            description="Batch internal updates within this time window (does not auto-emit)",
            default=DEFAULT_COALESCE_SECONDS,
            min=0.0,
            max=5.0,
        )
    if not hasattr(bpy.types.Scene, "usd_diff_import_skip_leaf_geom"):
        bpy.types.Scene.usd_diff_import_skip_leaf_geom = bpy.props.BoolProperty(
            name="Skip Leaf /Geom Prim Paths",
            description="During USD import tagging, skip prim paths ending with '/Geom' to prefer Xform prims",
            default=True,
        )


# -----------------------------------------------------------------------------
# USDHook for import-time tagging (robust identity, no name guessing)
# -----------------------------------------------------------------------------
class USD_DIFF_Hook(bpy.types.USDHook):
    """
    Tags imported Blender Objects with USD prim paths using Blender's import_context.get_prim_map().
    This is the robust way to map Blender edits back to USD prims without guessing.
    """
    bl_idname = "usd_diff.hook"
    bl_label = "USD Diff Hook"

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
                stage_id = None

        if not prim_map:
            print("[USD Diff Hook] No prim map available; nothing tagged.")
            return True

        skip_leaf_geom = bool(getattr(bpy.context.scene, "usd_diff_import_skip_leaf_geom", True))

        tagged = 0
        for prim_path, data_blocks in prim_map.items():
            prim_path_str = str(prim_path)

            # Common USD pattern: /XformPrim/Geom is a leaf geometry prim; for TRS diffs you usually
            # want the parent Xform prim, not the leaf. Skipping this avoids Geom, Geom_001 noise.
            if skip_leaf_geom and prim_path_str.endswith("/Geom"):
                continue

            # data_blocks is a list of bpy.types.ID
            for db in data_blocks:
                if isinstance(db, bpy.types.Object):
                    db["usd_prim_path"] = prim_path_str
                    if stage_id:
                        db["usd_stage_id"] = stage_id
                    tagged += 1

        print(f"[USD Diff Hook] Tagged {tagged} objects with usd_prim_path")
        return True


# -----------------------------------------------------------------------------
# Core USD delta capture engine
# -----------------------------------------------------------------------------
class _UsdDeltaEngine:
    """
    - Opens base USD as stage root layer (file-backed)
    - Uses stage SessionLayer as diff buffer
    - Mirrors Blender object TRS into USD Xform ops at obj['usd_prim_path']
    - Emits diff as SessionLayer .usda
    """
    def __init__(self, base_usd_path: str, coalesce_seconds: float):
        if not PXR_AVAILABLE:
            raise RuntimeError(f"OpenUSD 'pxr' not available: {_PXR_IMPORT_ERROR}")

        self.base_usd_path = (base_usd_path or "").strip()
        self.coalesce_seconds = max(0.0, float(coalesce_seconds))

        if not self.base_usd_path:
            raise RuntimeError("Base USD file path is empty. Set 'Base USD File' first.")

        self.base_layer = Sdf.Layer.FindOrOpen(self.base_usd_path)
        if self.base_layer is None:
            raise RuntimeError(f"Failed to open base USD layer: {self.base_usd_path}")

        # Stage we author into (base + session)
        self.stage = Usd.Stage.Open(self.base_layer)
        if self.stage is None:
            raise RuntimeError("Failed to open USD stage from base layer")

        # A base-only stage for validation (no session opinions)
        self.base_stage = Usd.Stage.Open(self.base_layer)

        # Diff buffer: session layer (strongest)
        self.delta_layer = self.stage.GetSessionLayer()
        self.stage.SetEditTarget(Usd.EditTarget(self.delta_layer))

        # Optional notice for debugging
        self._notice_key = Tf.Notice.Register(
            Usd.Notice.ObjectsChanged, self._on_objects_changed, self.stage
        )

        # Tracking
        self._last_matrix = {}     # object name -> tuple(16 floats) of matrix_world
        self._pending_paths = set()
        self._dirty = False
        self._next_emit_time = 0.0
        self.enabled = False

    def _on_objects_changed(self, notice, stage):
        # Comment out if too noisy
        changed = notice.GetChangedInfoOnlyPaths()
        if changed:
            print("[USD Notice] Changed paths:")
            for p in changed:
                print("  -", p)
    

    def initialize_baseline(self):
        """
        Capture current transform state of all tagged objects.
        This prevents false "changes" when the engine restarts.
        """
        for obj in bpy.context.scene.objects:
            if "usd_prim_path" not in obj:
                continue
            m = tuple(v for row in obj.matrix_world for v in row)
            self._last_matrix[obj.name] = m

    # ---------- Mapping ----------
    def usd_path_for_object(self, obj: bpy.types.Object) -> str | None:
        """
        Robust mode: only accept stored prim path from USDHook import tagging.
        No fallback guessing.
        """
        if obj is None:
            return None
        p = obj.get("usd_prim_path")
        if not p:
            return None
        p = str(p).strip()
        if not p.startswith("/"):
            return None
        return p

    # ---------- Authoring ----------
    def _get_or_add_op(self, xf: UsdGeom.Xformable, op_type):
        for op in xf.GetOrderedXformOps():
            if op.GetOpType() == op_type:
                return op
        return xf.AddXformOp(op_type)

    # def author_xform_from_object(self, obj: bpy.types.Object):
    #     """
    #     Mirrors Blender object TRS into USD xform ops on the prim path.
    #     Safe behavior:
    #       - Requires obj['usd_prim_path']
    #       - Requires the prim to exist in the BASE stage (no accidental creation)
    #     """
    #     path = self.usd_path_for_object(obj)
    #     if not path:
    #         return

    #     # Validate prim exists in base (prevents accidental /World/Geom_001 creation)
    #     base_prim = self.base_stage.GetPrimAtPath(path)
    #     if not base_prim or not base_prim.IsValid():
    #         return

    #     # Only author to Xformable prims
    #     if not UsdGeom.Xformable(base_prim):
    #         return

    #     loc = obj.location
    #     rot = obj.rotation_euler  # radians
    #     scl = obj.scale

    #     with Usd.EditContext(self.stage, self.delta_layer):
    #         prim = self.stage.GetPrimAtPath(path)
    #         if not prim or not prim.IsValid():
    #             # Since base prim exists, composed prim should exist too; but be safe.
    #             return

    #         xf = UsdGeom.Xformable(prim)
    #         if not xf:
    #             return

    #         t_op = self._get_or_add_op(xf, UsdGeom.XformOp.TypeTranslate)
    #         r_op = self._get_or_add_op(xf, UsdGeom.XformOp.TypeRotateXYZ)
    #         s_op = self._get_or_add_op(xf, UsdGeom.XformOp.TypeScale)

    #         t_op.Set(Gf.Vec3d(loc.x, loc.y, loc.z))
    #         r_op.Set(Gf.Vec3f(degrees(rot.x), degrees(rot.y), degrees(rot.z)))
    #         s_op.Set(Gf.Vec3f(scl.x, scl.y, scl.z))

    #     self._pending_paths.add(path)
    #     self._dirty = True

    def author_xform_from_object(self, obj: bpy.types.Object):
        """
        Mirrors Blender object TRS into USD xform ops on the prim path.
        Now writes local-to-parent transforms (USD convention).
        """
        path = self.usd_path_for_object(obj)
        if not path:
            return

        # Validate prim exists in base (prevents accidental /World/Geom_001 creation)
        base_prim = self.base_stage.GetPrimAtPath(path)
        if not base_prim or not base_prim.IsValid():
            return

        # Only author to Xformable prims
        if not UsdGeom.Xformable(base_prim):
            return

        # --- Compute local-to-parent transform ---
        # If object has a parent, compute local matrix relative to parent
        if obj.parent:
            parent_matrix = obj.parent.matrix_world
            parent_inv = parent_matrix.inverted_safe()
            local_matrix = parent_inv @ obj.matrix_world
        else:
            local_matrix = obj.matrix_world.copy()

        # Decompose local matrix to TRS
        loc, rot, scl = local_matrix.decompose()
        # rot is a Quaternion; convert to Euler in Blender's order
        rot_euler = rot.to_euler(obj.rotation_mode)

        with Usd.EditContext(self.stage, self.delta_layer):
            prim = self.stage.GetPrimAtPath(path)
            if not prim or not prim.IsValid():
                return

            xf = UsdGeom.Xformable(prim)
            if not xf:
                return

            t_op = self._get_or_add_op(xf, UsdGeom.XformOp.TypeTranslate)
            r_op = self._get_or_add_op(xf, UsdGeom.XformOp.TypeRotateXYZ)
            s_op = self._get_or_add_op(xf, UsdGeom.XformOp.TypeScale)

            t_op.Set(Gf.Vec3d(loc.x, loc.y, loc.z))
            r_op.Set(Gf.Vec3f(degrees(rot_euler.x), degrees(rot_euler.y), degrees(rot_euler.z)))
            s_op.Set(Gf.Vec3f(scl.x, scl.y, scl.z))

        self._pending_paths.add(path)
        self._dirty = True

    # ---------- Delta emission ----------
    def export_delta_as_string(self) -> str:
        return self.delta_layer.ExportToString()

    def clear_delta(self):
        self.delta_layer.Clear()
        self._pending_paths.clear()
        self._dirty = False

    # ---------- Coalescing ----------
    def mark_for_emit(self):
        now = time.time()
        self._next_emit_time = max(self._next_emit_time, now + self.coalesce_seconds)

    def on_depsgraph_update(self, depsgraph: bpy.types.Depsgraph):
        if not self.enabled:
            return

        any_change = False
        for update in depsgraph.updates:
            id_data = update.id
            if not isinstance(id_data, bpy.types.Object):
                continue
            obj = id_data

            # Only track objects we can map back to USD
            if "usd_prim_path" not in obj:
                continue

            # Matrix change detection
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
        # no auto-emit yet
        return 0.1


_ENGINE: _UsdDeltaEngine | None = None


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
            base_usd_path=scene.usd_diff_base_usd_path,
            coalesce_seconds=scene.usd_diff_coalesce_seconds,
        )
    return _ENGINE


# -----------------------------------------------------------------------------
# Handlers / Timer glue
# -----------------------------------------------------------------------------
def _depsgraph_handler(scene, depsgraph):
    try:
        if _ENGINE is None:
            return
        # Skip if only the scene itself was modified (e.g., scene property changes)
        # We only care about Object transforms
        has_object_updates = any(
            isinstance(update.id, bpy.types.Object) 
            for update in depsgraph.updates
        )
        if not has_object_updates:
            return
        _ENGINE.on_depsgraph_update(depsgraph)
    except Exception as e:
        print("[USD Diff] depsgraph handler error:", e)


def _timer_tick():
    try:
        if _ENGINE is None:
            return None
        return _ENGINE.on_timer_tick()
    except Exception as e:
        print("[USD Diff] timer error:", e)
        return 0.5


def _remove_handler():
    try:
        bpy.app.handlers.depsgraph_update_post.remove(_depsgraph_handler)
    except ValueError:
        pass


# -----------------------------------------------------------------------------
# Operators
# -----------------------------------------------------------------------------
class USD_DIFF_OT_delete_selected(bpy.types.Operator):
    bl_idname = "usd_diff.delete_selected"
    bl_label = "Delete USD Prim (selected)"
    def execute(self, context):
        obj = context.active_object
        if not obj or "usd_prim_path" not in obj:
            self.report({"ERROR"}, "No usd_prim_path on active object")
            return {"CANCELLED"}
        eng = _get_engine(context)
        if not eng:
            self.report({"ERROR"}, "Engine not running")
            return {"CANCELLED"}
        if eng.delete_prim(obj["usd_prim_path"]):
            self.report({"INFO"}, f"Deleted prim {obj['usd_prim_path']} in session layer")
            return {"FINISHED"}
        else:
            self.report({"ERROR"}, "Delete failed (prim may not exist in base)")
            return {"CANCELLED"}


class USD_DIFF_OT_print_usd_import_props(bpy.types.Operator):
    bl_idname = "usd_diff.print_usd_import_props"
    bl_label = "Debug: Print USD Import Props"
    bl_description = "Print wm.usd_import operator properties to the console (helps wire hooks)"

    def execute(self, context):
        try:
            keys = list(bpy.ops.wm.usd_import.get_rna_type().properties.keys())
            print("[usd_import props]", keys)
            self.report({"INFO"}, "Printed wm.usd_import props to console")
        except Exception as e:
            self.report({"ERROR"}, f"Failed to query usd_import props: {e}")
        return {"FINISHED"}


class USD_DIFF_OT_import_with_hook(bpy.types.Operator):
    bl_idname = "usd_diff.import_with_hook"
    bl_label = "Import USD (with prim tagging)"
    bl_description = "Import USD and tag imported objects with their USD prim paths via USDHook"
    filepath: bpy.props.StringProperty(subtype="FILE_PATH")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        if not PXR_AVAILABLE:
            self.report({"ERROR"}, f"OpenUSD 'pxr' not available: {_PXR_IMPORT_ERROR}")
            return {"CANCELLED"}

        last_err = None
        # If we got here, hook param name didn't match on this build.
        # Import without hook as fallback, but tell user how to debug.
        try:
            bpy.ops.wm.usd_import(filepath=self.filepath)
            context.scene.usd_diff_base_usd_path = self.filepath

            self.report(
                {"WARNING"},
                "Imported USD without hook (couldn't find hook property). Use 'Debug: Print USD Import Props'.",
            )
        except Exception as e:
            self.report({"ERROR"}, f"USD import failed: {e}. Hook error was: {last_err}")
            return {"CANCELLED"}

        return {"FINISHED"}


class USD_DIFF_OT_start(bpy.types.Operator):
    bl_idname = "usd_diff.start"
    bl_label = "Start Capture"
    bl_description = "Open the base USD stage and start capturing Blender transform edits into the USD session-layer delta"

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

        self.report({"INFO"}, f"USD diff capture started (base: {eng.base_usd_path}).")
        return {"FINISHED"}


class USD_DIFF_OT_stop(bpy.types.Operator):
    bl_idname = "usd_diff.stop"
    bl_label = "Stop Capture"
    bl_description = "Stop capturing changes"

    def execute(self, context):
        if _ENGINE is not None:
            _ENGINE.enabled = False
        _remove_handler()
        self.report({"INFO"}, "USD diff capture stopped.")
        return {"FINISHED"}


class USD_DIFF_OT_emit(bpy.types.Operator):
    bl_idname = "usd_diff.emit"
    bl_label = "Emit Diff"
    bl_description = "Export the USD session layer (delta) as .usda, print it, and optionally write to disk"

    def execute(self, context):
        if not PXR_AVAILABLE:
            self.report({"ERROR"}, f"OpenUSD 'pxr' not available: {_PXR_IMPORT_ERROR}")
            return {"CANCELLED"}

        if _ENGINE is None:
            self.report({"ERROR"}, "Engine not running. Click 'Start Capture' first.")
            return {"CANCELLED"}

        payload = _ENGINE.export_delta_as_string()

        print("\n========== USD DELTA (SESSION LAYER) BEGIN ==========")
        print(payload)
        print("=========== USD DELTA (SESSION LAYER) END ===========\n")

        scene = context.scene
        if scene.usd_diff_emit_to_file:
            out_path = (scene.usd_diff_emit_file_path or "").strip()
            if not out_path:
                self.report({"WARNING"}, "Emit-to-file is enabled but no output path is set.")
                return {"FINISHED"}
            try:
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(payload)
                self.report({"INFO"}, f"Diff written to {out_path}")
            except Exception as e:
                self.report({"ERROR"}, f"Failed to write diff: {e}")

        return {"FINISHED"}


class USD_DIFF_OT_clear(bpy.types.Operator):
    bl_idname = "usd_diff.clear"
    bl_label = "Clear Diff"
    bl_description = "Clear the session layer (diff buffer)"

    def execute(self, context):
        if not PXR_AVAILABLE:
            self.report({"ERROR"}, f"OpenUSD 'pxr' not available: {_PXR_IMPORT_ERROR}")
            return {"CANCELLED"}

        if _ENGINE is None:
            self.report({"ERROR"}, "Engine not running. Click 'Start Capture' first.")
            return {"CANCELLED"}

        _ENGINE.clear_delta()
        self.report({"INFO"}, "Delta layer cleared.")
        return {"FINISHED"}


# -----------------------------------------------------------------------------
# UI Panel
# -----------------------------------------------------------------------------
class USD_DIFF_PT_panel(bpy.types.Panel):
    bl_label = "USD Diff"
    bl_idname = "USD_DIFF_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "USD Diff"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        if not PXR_AVAILABLE:
            layout.label(text="OpenUSD (pxr) not available", icon="ERROR")
            layout.label(text=str(_PXR_IMPORT_ERROR))
            return

        layout.label(text="Import")
        layout.operator("usd_diff.import_with_hook", icon="IMPORT")
        layout.prop(scene, "usd_diff_import_skip_leaf_geom")
        layout.operator("usd_diff.print_usd_import_props", icon="CONSOLE")

        layout.separator()
        layout.label(text="Base Stage")
        layout.prop(scene, "usd_diff_base_usd_path")

        layout.separator()
        row = layout.row()
        running = bool(_ENGINE and _ENGINE.enabled)
        if running:
            row.operator("usd_diff.stop", icon="PAUSE")
        else:
            row.operator("usd_diff.start", icon="PLAY")

        if _ENGINE is not None:
            box = layout.box()
            box.label(text="Engine Status")
            box.label(text=f"Running: {bool(_ENGINE.enabled)}")
            box.label(text=f"Base: {_ENGINE.base_usd_path}")
            box.label(text=f"Buffered prims: {len(_ENGINE._pending_paths)}")
            box.label(text=f"Dirty: {_ENGINE._dirty}")

        layout.separator()
        layout.label(text="Emit / Clear")
        layout.operator("usd_diff.emit", icon="EXPORT")
        layout.operator("usd_diff.clear", icon="TRASH")

        col = layout.column(align=True)
        col.prop(scene, "usd_diff_emit_to_file")
        if scene.usd_diff_emit_to_file:
            col.prop(scene, "usd_diff_emit_file_path")

        layout.separator()
        layout.label(text="Options")
        layout.prop(scene, "usd_diff_coalesce_seconds")


# -----------------------------------------------------------------------------
# Registration
# -----------------------------------------------------------------------------
_CLASSES = (
    USD_DIFF_Hook,
    USD_DIFF_OT_print_usd_import_props,
    USD_DIFF_OT_import_with_hook,
    USD_DIFF_OT_start,
    USD_DIFF_OT_stop,
    USD_DIFF_OT_emit,
    USD_DIFF_OT_clear,
    USD_DIFF_PT_panel,
)

def register():
    _ensure_scene_props()
    for c in _CLASSES:
        bpy.utils.register_class(c)

def unregister():
    _remove_handler()
    _reset_engine()
    for c in reversed(_CLASSES):
        bpy.utils.unregister_class(c)

    for prop_name in (
        "usd_diff_base_usd_path",
        "usd_diff_emit_to_file",
        "usd_diff_emit_file_path",
        "usd_diff_coalesce_seconds",
        "usd_diff_import_skip_leaf_geom",
    ):
        if hasattr(bpy.types.Scene, prop_name):
            try:
                delattr(bpy.types.Scene, prop_name)
            except Exception:
                pass

if __name__ == "__main__":
    register()
