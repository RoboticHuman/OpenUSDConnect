"""Blender depsgraph -> emitter bridge.

Captures Blender object transform edits and sends them over the network
via the openusdconnect protocol. Also handles USD import tagging via USDHook
and local diff capture to session layer.

Architecture:
  Blender depsgraph change
    -> BlenderStageAuthor writes to local USD stage
      -> Usd.Notice.ObjectsChanged fires
        -> NoticeEmitter builds protocol events
          -> EventSender sends events over TCP
"""

from __future__ import annotations

import logging
import time

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
from openusdconnect.sender import EventSender

from . import SESSION_ORIGIN, STABLE_CLIENT_ID

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
    if obj.type == "CAMERA":
        return "Camera"
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
    (
        "usd_connect_base_usd_path",
        bpy.props.StringProperty,
        {
            "name": "Base USD File",
            "description": "Path to the base USD file (.usda/.usd/.usdc)",
            "subtype": "FILE_PATH",
            "default": "",
        },
    ),
    (
        "usd_connect_emit_to_file",
        bpy.props.BoolProperty,
        {
            "name": "Emit to File",
            "default": False,
        },
    ),
    (
        "usd_connect_emit_file_path",
        bpy.props.StringProperty,
        {
            "name": "Diff Output File",
            "subtype": "FILE_PATH",
            "default": "",
        },
    ),
    (
        "usd_connect_coalesce_seconds",
        bpy.props.FloatProperty,
        {
            "name": "Coalesce (sec)",
            "default": DEFAULT_COALESCE_SECONDS,
            "min": 0.0,
            "max": 5.0,
        },
    ),
    (
        "usd_connect_emit_host",
        bpy.props.StringProperty,
        {
            "name": "Server Host",
            "default": "127.0.0.1",
        },
    ),
    (
        "usd_connect_emit_port",
        bpy.props.IntProperty,
        {
            "name": "Server Port",
            "default": 7200,
            "min": 1,
            "max": 65535,
        },
    ),
    (
        "usd_connect_emit_hz",
        bpy.props.FloatProperty,
        {
            "name": "Send Rate (Hz)",
            "default": 60.0,
            "min": 1.0,
            "max": 120.0,
        },
    ),
    (
        "usd_connect_net_emitter_running",
        bpy.props.BoolProperty,
        {
            "name": "Net Emitter Running",
            "default": False,
        },
    ),
    (
        "usd_connect_auto_track",
        bpy.props.BoolProperty,
        {
            "name": "Auto-track New Objects",
            "description": (
                "Automatically register objects as USD prims when they are "
                "first manipulated, using the root prim path below"
            ),
            "default": False,
        },
    ),
    (
        "usd_connect_auto_track_root",
        bpy.props.StringProperty,
        {
            "name": "Root Prim",
            "description": "Parent prim path for auto-tracked objects (e.g. /World)",
            "default": "/World",
        },
    ),
    (
        "usd_connect_asset_root",
        bpy.props.StringProperty,
        {
            "name": "Asset Root",
            "description": (
                "Root directory for resolving relative asset paths in set_reference events"
            ),
            "subtype": "DIR_PATH",
            "default": "",
        },
    ),
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

    # Set True by _import_ref_asset during reference/payload imports so
    # the hook still tags usd_prim_path but doesn't override auto_track_root
    # with the referenced file's default prim.
    _skip_root_inference: bool = False

    @staticmethod
    def on_import(import_context):
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

        # Infer root prim from the stage's default prim and update auto_track_root.
        # Skip during reference imports (_skip_root_inference) — the referenced file's
        # default prim shouldn't override the main scene's auto_track_root.
        if stage and not USD_CONNECT_Hook._skip_root_inference:
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

        tagged = 0
        for prim_path, data_blocks in prim_map.items():
            prim_path_str = str(prim_path)
            # Look up prim type from the stage
            prim_type_name = ""
            if stage:
                try:
                    prim = stage.GetPrimAtPath(prim_path_str)
                    if prim and prim.IsValid():
                        prim_type_name = prim.GetTypeName()
                except Exception:
                    LOG.debug("USDHook: could not look up prim type for %s", prim_path_str)

            from .blender_adapter import _PROP_USD_IMPORTED

            for db in data_blocks:
                if isinstance(db, bpy.types.Object):
                    db["usd_prim_path"] = prim_path_str
                    if prim_type_name:
                        db["usd_type_name"] = prim_type_name
                    if stage_id:
                        db["usd_stage_id"] = stage_id
                    db[_PROP_USD_IMPORTED] = True
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

        self._needs_axis_conv: bool = needs_conversion(UsdGeom.GetStageUpAxis(self.stage))

        self._last_matrix: dict = {}
        self._prim_refs: dict = {}  # prim_path -> bpy.types.Object reference
        self._used_prim_paths: set = set()
        self.auto_track: bool = False
        self.enabled: bool = False
        self._applying_remote: bool = False
        self._unloaded_payload_roots: set[str] = set()  # prim paths with unloaded payloads

        from .shader_mapper import create_default_registry

        self._shader_registry = create_default_registry()
        self._last_shader_values: dict[str, dict] = {}  # shader_path -> {usd_name: value}
        self._shader_input_maps: dict[str, dict] = {}  # shader_path -> {usd_name: socket}

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
        return any(prim_path.startswith(root + "/") for root in self._unloaded_payload_roots)

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
            # T and S always need Z-up → Y-up conversion.
            tx, ty, tz = zup_to_yup_vec(tx, ty, tz)
            sx, sy, sz = zup_to_yup_scale(sx, sy, sz)
            if is_imported:
                # Imported root: strip Rx(90°) display rotation
                rw, rx, ry, rz = strip_axis_rotation(rw, rx, ry, rz)
            else:
                # Normal object: full Z-up → Y-up rotation
                rw, rx, ry, rz = zup_to_yup_quat(rw, rx, ry, rz)

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

    def _author_camera_attrs(self, prim_path: str, obj):
        """Author bpy.types.Camera fields onto the USD camera prim.

        Inverse of BlenderAdapter._apply_camera_attrs: Blender mm/metres →
        USD tenths-of-stage-unit / stage units. The emitter's CameraAttrsChannel
        picks up the changed attrs and emits a ``set_gprim_attrs`` event.
        """
        if obj.type != "CAMERA" or obj.data is None:
            return
        prim = self.stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid():
            return
        cam = UsdGeom.Camera(prim)
        if not cam:
            return

        cam_data = obj.data
        meters_per_unit = float(UsdGeom.GetStageMetersPerUnit(self.stage))
        scene_scale = meters_per_unit
        tenth_unit_to_mm = 100.0 * scene_scale

        cam.CreateProjectionAttr().Set(
            "perspective" if cam_data.type == "PERSP" else "orthographic"
        )
        cam.CreateFocalLengthAttr().Set(float(cam_data.lens) / tenth_unit_to_mm)
        cam.CreateHorizontalApertureAttr().Set(float(cam_data.sensor_width) / tenth_unit_to_mm)
        cam.CreateVerticalApertureAttr().Set(float(cam_data.sensor_height) / tenth_unit_to_mm)

        sensor_size = max(cam_data.sensor_width, cam_data.sensor_height)
        if sensor_size > 0:
            cam.CreateHorizontalApertureOffsetAttr().Set(
                float(cam_data.shift_x) * sensor_size / tenth_unit_to_mm
            )
            cam.CreateVerticalApertureOffsetAttr().Set(
                float(cam_data.shift_y) * sensor_size / tenth_unit_to_mm
            )

        cam.CreateClippingRangeAttr().Set(
            Gf.Vec2f(
                float(cam_data.clip_start) / scene_scale,
                float(cam_data.clip_end) / scene_scale,
            )
        )

        if getattr(cam_data.dof, "use_dof", False):
            cam.CreateFStopAttr().Set(float(cam_data.dof.aperture_fstop))
            cam.CreateFocusDistanceAttr().Set(
                float(cam_data.dof.focus_distance) / scene_scale
            )
        else:
            cam.CreateFStopAttr().Set(0.0)

    # Blender node type → USD shader ID for reverse lookup
    _NODE_TYPE_TO_SHADER_ID = {
        "BSDF_PRINCIPLED": "UsdPreviewSurface",
    }

    def on_material_update(self, materials):
        """Author Blender material node changes to the emitter's USD stage.

        Called by _depsgraph_handler when bpy.types.Material updates are
        detected.  Reads current node socket values via the shader mapper
        and writes them to the corresponding UsdShade.Shader inputs on the
        emitter's stage.  The NoticeEmitter then picks up the USD change
        and emits set_connectable_input events through the normal pipeline.
        """
        if not self.enabled or self._applying_remote:
            return
        for mat in materials:
            orig = getattr(mat, "original", mat)
            if not orig.node_tree:
                continue
            mat_path = orig.get("usd_material_path")
            for node in orig.node_tree.nodes:
                shader_path = node.get("usd_shader_path")
                shader_id = node.get("usd_shader_id")
                # Fallback: derive from material path + node type
                if not shader_path and mat_path:
                    shader_id = self._NODE_TYPE_TO_SHADER_ID.get(node.type)
                    if shader_id:
                        shader_path = self._find_shader_on_stage(
                            mat_path,
                            shader_id,
                        )
                if not shader_path or not shader_id:
                    continue
                mapper = self._shader_registry.get(shader_id)
                if not mapper:
                    continue

                if mapper.is_multi_node:
                    input_map = self._shader_input_maps.get(shader_path)
                    if not input_map:
                        continue
                    try:
                        next(iter(input_map.values())).node  # noqa: B018
                    except ReferenceError:
                        self._shader_input_maps.pop(shader_path, None)
                        continue
                    values = mapper.read_all_inputs(input_map=input_map)
                elif hasattr(mapper, "read_all_inputs"):
                    values = mapper.read_all_inputs(node)
                else:
                    continue

                if values:
                    self._author_shader_inputs(shader_path, values, shader_id=shader_id)

    def _find_shader_on_stage(self, mat_path: str, shader_id: str) -> str | None:
        """Find a shader prim under a material on the emitter's stage."""
        from pxr import Usd, UsdShade

        mat_prim = self.stage.GetPrimAtPath(mat_path)
        if not mat_prim or not mat_prim.IsValid():
            return None
        for child in Usd.PrimRange(mat_prim):
            if child.IsA(UsdShade.Shader):
                shader = UsdShade.Shader(child)
                sid = shader.GetIdAttr().Get() or ""
                if sid == shader_id:
                    return str(child.GetPath())
        return None

    def _author_shader_inputs(self, shader_path, values, shader_id=""):
        """Write only changed shader input values to the emitter's USD stage.

        Compares against ``_last_shader_values`` (the baseline from the
        previous read) to detect actual user edits — same pattern as
        ``_last_matrix`` for transforms.  On first encounter, seeds the
        baseline and skips authoring (all values are "initial state").
        """
        from pxr import UsdShade

        from openusdconnect.event_apply import _set_connectable_input_value

        last = self._last_shader_values.get(shader_path)
        if last is None:
            self._last_shader_values[shader_path] = dict(values)
            return

        changed = {k: v for k, v in values.items() if last.get(k) != v}
        if not changed:
            return

        prim = self.stage.GetPrimAtPath(shader_path)
        if not prim or not prim.IsValid():
            prim = self.stage.DefinePrim(shader_path, "Shader")
            if not prim or not prim.IsValid():
                return

        self.stage.OverridePrim(shader_path)
        shader = UsdShade.Shader(prim)
        # Ensure info:id is authored so the emitter's _read_shader_inputs
        # can identify the shader type when building events.
        if shader_id and not shader.GetIdAttr().Get():
            shader.CreateIdAttr(shader_id)
        connectable = UsdShade.ConnectableAPI(prim)
        for usd_name, value in changed.items():
            type_name = self._reverse_input_type(connectable, prim, usd_name, value)
            _set_connectable_input_value(connectable, usd_name, value, type_name)

        self._last_shader_values[shader_path] = dict(values)

    @staticmethod
    def _reverse_input_type(connectable, prim, usd_name, value) -> str:
        """USD type name for a reverse-authored shader input.

        The type authored on the synced prim wins (values written by sync
        carry the source's exact Sdf type), then the shader's Sdr node
        definition, then a value-shape heuristic for inputs the stage has
        never seen.
        """
        from openusdconnect.connectable_attrs import input_attr
        from openusdconnect.event_apply import _resolve_shader_port_type

        existing = connectable.GetInput(usd_name)
        if existing:
            return str(existing.GetAttr().GetTypeName())
        sdr_type = _resolve_shader_port_type(prim, input_attr(usd_name))
        if sdr_type is not None:
            return str(sdr_type)
        if isinstance(value, str):
            return "string"
        if isinstance(value, list) and len(value) == 3:
            return "color3f"
        return "float"

    def purge_prim_refs(self, prefix: str):
        """Remove tracked references for prims matching a path prefix."""
        to_purge = [pp for pp in self._prim_refs if pp.startswith(prefix)]
        for pp in to_purge:
            self._prim_refs.pop(pp, None)
            self._last_matrix.pop(pp, None)
            self._used_prim_paths.discard(pp)
        shader_purge = [sp for sp in self._shader_input_maps if sp.startswith(prefix)]
        for sp in shader_purge:
            self._shader_input_maps.pop(sp, None)
            self._last_shader_values.pop(sp, None)

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
            obj.get("usd_prim_path"): obj for obj in bpy.data.objects if obj.get("usd_prim_path")
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
            # Cameras carry typed-schema attrs (lens, aperture, ...) on their
            # data block; the matrix gate alone misses pure data edits.
            data_dirty = obj.type == "CAMERA" and getattr(update, "is_updated_geometry", False)
            if last == m and self.stage.GetPrimAtPath(prim_path) and not data_dirty:
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

            if obj.type == "CAMERA":
                self._author_camera_attrs(prim_path, obj)

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
# Module-level state
# ---------------------------------------------------------------------------
class _State:
    """Encapsulates the runtime state for the capture subsystem.

    Holds the three collaborating objects that form the emit pipeline:
    author -> notice_emitter -> sender.
    """

    __slots__ = (
        "author", "notice_emitter", "sender", "_last_send_time", "_last_seen_frame",
    )

    def __init__(self):
        self.author: BlenderStageAuthor | None = None
        self.notice_emitter = None  # Optional[NoticeEmitter]
        self.sender: EventSender | None = None
        self._last_send_time: float = 0.0
        # Tracks scene.frame_current across depsgraph ticks so we can tell a
        # frame-driven eval (F-curve evaluation during playback or scrub)
        # apart from a real user edit.
        self._last_seen_frame: int | None = None


_state = _State()


def _try_send_dirty_events():
    """Build and send dirty events if emitter and sender are both connected."""
    if _state.notice_emitter is None or _state.sender is None or _state.sender.sock is None:
        return
    events = _state.notice_emitter.build_events_for_dirty()
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


def seed_emitter_caches_for_import(prim_path: str):
    """Seed emitter caches for a prim hierarchy after receiver import.

    When the receiver imports objects (via load_payload or set_reference),
    the emitter has never seen those objects.  Without seeding, the first
    user interaction triggers the depsgraph handler to process every object,
    author to the stage, and send a storm of events to the server.

    Seeds both BlenderStageAuthor caches (matrix, prim refs) and
    NoticeEmitter caches (known prims, prim cache) so the emitter treats
    the hierarchy as already-tracked.
    """
    author = _state.author
    ne = _state.notice_emitter
    if author is None:
        return

    prefix = prim_path + "/"

    # --- Seed BlenderStageAuthor caches from Blender objects ---
    for obj in bpy.data.objects:
        pp = obj.get("usd_prim_path")
        if not pp:
            continue
        if pp != prim_path and not pp.startswith(prefix):
            continue
        src = getattr(obj, "matrix_basis", obj.matrix_world)
        m = tuple(v for row in src for v in row)
        author._last_matrix[obj.name] = m
        author._prim_refs[pp] = obj
        author._used_prim_paths.add(pp)

    # --- Seed NoticeEmitter caches from emitter's stage ---
    if ne is not None:
        ne.seed_prim_cache(author.stage, prim_path)


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
    # Distinguish F-curve-driven evals (playback / scrub) from user edits.
    # A frame change before this depsgraph eval means the property mutations
    # come from F-curve evaluation; the playback leader broadcasts
    # PlaybackControl(set_time, ...) separately, so capture should not also
    # emit default-time SetXformTrs/etc. overwrites that would corrupt the
    # mirror stage and flood the wire.
    current_frame = scene.frame_current
    last_frame = _state._last_seen_frame
    _state._last_seen_frame = current_frame
    if last_frame is not None and current_frame != last_frame:
        return

    try:
        updates = list(depsgraph.updates)

        has_object_updates = any(isinstance(update.id, bpy.types.Object) for update in updates)
        has_material_updates = any(isinstance(update.id, bpy.types.Material) for update in updates)
        if not has_object_updates and not has_material_updates:
            return

        if _state.author is not None and _state.author.enabled:
            if has_object_updates:
                _state.author.auto_track = getattr(scene, "usd_connect_auto_track", False)
                obj_count = sum(1 for u in updates if isinstance(u.id, bpy.types.Object))
                _state.author.on_depsgraph_update(updates)
                dirty_count = len(_state.notice_emitter.dirty) if _state.notice_emitter else 0
                if dirty_count > 0:
                    LOG.debug("depsgraph: %d obj updates, %d dirty prims", obj_count, dirty_count)

            if has_material_updates:
                mat_updates = [u.id for u in updates if isinstance(u.id, bpy.types.Material)]
                _state.author.on_material_update(mat_updates)

            coalesce = getattr(scene, "usd_connect_coalesce_seconds", DEFAULT_COALESCE_SECONDS)
            now = time.time()
            if coalesce <= 0 or (now - _state._last_send_time) >= coalesce:
                _try_send_dirty_events()
                _state._last_send_time = now

    except Exception:
        LOG.exception("depsgraph handler error")


def _timer_tick():
    """Flush remaining dirty prims after the user stops interacting."""
    try:
        if _state.author is None or not _state.author.enabled:
            return 0.25
        if _state.notice_emitter and _state.notice_emitter.dirty:
            _try_send_dirty_events()
            _state._last_send_time = time.time()
        return 0.25
    except Exception:
        LOG.exception("timer error")
        return 0.5


def _remove_handler():
    try:
        bpy.app.handlers.depsgraph_update_post.remove(_depsgraph_handler)
    except ValueError:
        pass
    if bpy.app.timers.is_registered(_timer_tick):
        bpy.app.timers.unregister(_timer_tick)


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
        bpy.app.timers.register(_timer_tick, first_interval=0.25)
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

            _state.sender = EventSender(
                host=scene.usd_connect_emit_host,
                port=scene.usd_connect_emit_port,
                client_id=STABLE_CLIENT_ID,
                origin=SESSION_ORIGIN,
            )
            if not _state.sender.connect():
                reason = (
                    "auth rejected" if _state.sender.auth_rejected else "could not connect"
                )
                _state.sender = None
                self.report({"ERROR"}, f"Connection failed: {reason}")
                return {"CANCELLED"}

            _remove_handler()
            bpy.app.handlers.depsgraph_update_post.append(_depsgraph_handler)
            bpy.app.timers.register(_timer_tick, first_interval=0.25)
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
        _remove_handler()
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
            _try_send_dirty_events()

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
