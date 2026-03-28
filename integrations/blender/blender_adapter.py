"""BlenderAdapter — applies incoming USD sync events to Blender objects.

Implements the DCCAdapter interface for Blender. Finds objects by
obj["usd_prim_path"] custom property and sets location/rotation/scale.
"""

from __future__ import annotations

import logging
import os

try:
    import bpy
    import mathutils

    BPY_AVAILABLE = True
    _IDENTITY_4X4 = mathutils.Matrix.Identity(4)
    _IDENTITY_QUAT = mathutils.Quaternion()
except Exception:
    BPY_AVAILABLE = False
    _IDENTITY_4X4 = None
    _IDENTITY_QUAT = None

from openusdconnect.adapters import DCCAdapter
from openusdconnect.axis_conversion import (
    compose_axis_rotation,
    needs_conversion,
    yup_to_zup_quat,
    yup_to_zup_scale,
    yup_to_zup_vec,
)

# Custom property marking objects imported via bpy.ops.wm.usd_import.
_PROP_USD_IMPORTED = "_usd_imported"


def _has_axis_rotation(obj) -> bool:
    """Return True if obj's world transform includes non-identity rotation.

    Uses matrix_world so it catches axis-conversion rotation from any
    ancestor, not just the direct parent.
    """
    if not BPY_AVAILABLE or obj is None:
        return False
    return obj.matrix_world.to_quaternion() != _IDENTITY_QUAT

from .shader_mapper import create_default_registry

LOG = logging.getLogger(__name__)


class BlenderAdapter(DCCAdapter):
    """Applies incoming events to Blender scene objects.

    Finds objects by usd_prim_path custom property via a cached index.
    Rotation payload is quaternion [w,x,y,z]; converts to object's rotation_mode.
    """

    def __init__(self, scene_up_axis: str = "Y"):
        from openusdconnect.prim_registry import PrimRegistry

        self._registry = PrimRegistry(scan_fn=self._scan_scene_object)
        self._pending_payloads: dict[str, list] = {}  # prim_path -> payload list
        self._needs_axis_conv: bool = needs_conversion(scene_up_axis)
        self._shader_registry = create_default_registry()
        # Rebuild caches from scene so a fresh adapter (after receiver reset)
        # knows about objects that persist from a previous session.
        if BPY_AVAILABLE:
            for obj in bpy.data.objects:
                pp = obj.get("usd_prim_path")
                if pp:
                    self._registry.register(pp, obj)
                    if obj.get("usd_type_name") == "Reference":
                        ref_asset = obj.get("usd_ref_asset", "")
                        if ref_asset:
                            self._registry.set_imported_ref(
                                pp, ref_asset, obj.get("usd_ref_prim", ""),
                            )

    @staticmethod
    def _scan_scene_object(prim_path: str):
        """Fallback scan: find a Blender object by usd_prim_path property."""
        if not BPY_AVAILABLE:
            return None
        for obj in bpy.data.objects:
            if obj.get("usd_prim_path") == prim_path:
                return obj
        return None

    def _find_object_by_prim(self, prim_path: str):
        """Look up the Blender object for a USD prim path."""
        if not BPY_AVAILABLE:
            return None
        return self._registry.find(prim_path)

    def _link_object(self, obj):
        """Link an object into the scene collection.

        In timer callbacks bpy.context.collection may be unavailable,
        so we go straight for the scene master collection which is always valid.
        """
        linked = False
        # Try scene master collection first (always available)
        try:
            bpy.context.scene.collection.objects.link(obj)
            linked = True
        except RuntimeError:
            pass
        # Fallback: try context collection
        if not linked:
            try:
                col = bpy.context.collection
                col.objects.link(obj)
                linked = True
            except RuntimeError:
                pass
        if not linked:
            prim = obj.get("usd_prim_path")
            LOG.error("Failed to link object %s for prim %s", obj.name, prim)

    def _create_mesh_primitive(self, name: str, type_name: str):
        """Create a Blender mesh primitive using bpy.data (no operator context needed).

        Returns a new bpy.types.Object with the mesh, or None on failure.
        """
        import math

        import bmesh

        mesh = bpy.data.meshes.new(name + "_mesh")
        bm = bmesh.new()
        try:
            if type_name == "Sphere":
                bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=16, radius=1.0)
            elif type_name == "Cube":
                bmesh.ops.create_cube(bm, size=2.0)
            elif type_name == "Cylinder":
                bmesh.ops.create_cone(
                    bm, segments=32, radius1=1.0, radius2=1.0, depth=2.0, cap_ends=True
                )
            elif type_name == "Cone":
                bmesh.ops.create_cone(
                    bm, segments=32, radius1=1.0, radius2=0.0, depth=2.0, cap_ends=True
                )
            else:
                bm.free()
                return None
            # Generate UV coordinates for texture mapping
            uv_layer = bm.loops.layers.uv.new("st")
            for face in bm.faces:
                for loop in face.loops:
                    co = loop.vert.co
                    # Spherical projection — works for all primitive shapes
                    u = 0.5 + math.atan2(co.x, co.y) / (2 * math.pi)
                    v = 0.5 + math.asin(max(-1, min(1, co.z))) / math.pi
                    loop[uv_layer].uv = (u, v)
            bm.to_mesh(mesh)
        finally:
            bm.free()
        mesh.update()
        return bpy.data.objects.new(name, mesh)

    def _create_blender_object(self, prim_path: str, type_name: str):
        """Create the appropriate Blender object for a USD prim type.

        Returns a new bpy.types.Object tagged with prim_path and type_name.
        """
        name = prim_path.strip("/").replace("/", "_") or prim_path
        LOG.info("ensure_prim: creating %s '%s' for %s", type_name, name, prim_path)

        new = None
        if type_name in ("Sphere", "Cube", "Cylinder", "Cone"):
            new = self._create_mesh_primitive(name, type_name)
        if new is None and type_name == "Mesh":
            new = bpy.data.objects.new(name, bpy.data.meshes.new(name + "_mesh"))
        if new is None:
            new = bpy.data.objects.new(name, None)  # Xform or unknown → Empty

        new["usd_prim_path"] = prim_path
        new["usd_type_name"] = type_name
        return new

    def _parent_to_ancestor(self, obj, prim_path: str):
        """Parent obj under the correct ancestor based on prim_path hierarchy.

        Also moves the child into the parent's collection to avoid duplicates
        under the scene root collection.
        """
        parent_path = prim_path.rsplit("/", 1)[0]
        if not parent_path:
            return
        parent_obj = self._find_object_by_prim(parent_path)
        if parent_obj is None:
            return

        obj.parent = parent_obj
        obj.matrix_parent_inverse = _IDENTITY_4X4.copy()

        # Move to parent's collection
        parent_cols = parent_obj.users_collection
        target_col = None
        for col in parent_cols:
            if col != bpy.context.scene.collection:
                target_col = col
                break
        if target_col:
            try:
                target_col.objects.link(obj)
                bpy.context.scene.collection.objects.unlink(obj)
            except RuntimeError:
                LOG.warning("Failed to move %s to collection %s", obj.name, target_col.name)
        LOG.info("ensure_prim: parented '%s' under '%s'", obj.name, parent_obj.name)

    # Prim types that don't need scene objects — handled by their
    # dedicated event handlers (set_shader_input, set_material_binding).
    _NON_SCENE_TYPES = {"Material", "Shader", "NodeGraph", "Scope"}

    def ensure_prim(self, prim_path: str, type_name: str = "Xform") -> bool:
        if not BPY_AVAILABLE:
            LOG.info("BlenderAdapter.ensure_prim dry: %s", prim_path)
            return True
        if type_name in self._NON_SCENE_TYPES:
            LOG.info("ensure_prim: skipping non-scene type %s for %s", type_name, prim_path)
            return True
        obj = self._find_object_by_prim(prim_path)
        if obj:
            return True
        # Skip creation for prims that are composed children of a reference
        # import — the import already created them.
        if self._registry.is_reference_child(prim_path):
            return True

        new = self._create_blender_object(prim_path, type_name)
        self._link_object(new)
        self._registry.register(prim_path, new)
        self._parent_to_ancestor(new, prim_path)

        LOG.info("ensure_prim: linked %s '%s' for %s", type_name, new.name, prim_path)
        return True

    def ensure_xform_ops(self, prim_path: str) -> bool:
        if not BPY_AVAILABLE:
            return True
        obj = self._find_object_by_prim(prim_path)
        if obj is not None and obj.parent is not None:
            # Normalize MPI to Identity so obj.location/rotation/scale are in
            # parent-local space — matching what the emitter sends.
            # Imported objects may have non-identity MPI from Blender's
            # USD importer (set to parent.world.inverse() at import time).
            #
            # World-preserving: compensate matrix_basis so matrix_world
            # doesn't jump.  matrix_world = parent.world @ MPI @ basis,
            # so new_basis = old_MPI @ old_basis keeps the product stable.
            old_mpi = obj.matrix_parent_inverse.copy()
            if old_mpi == _IDENTITY_4X4:
                return True  # already identity, nothing to do
            old_basis = obj.matrix_basis.copy()
            obj.matrix_parent_inverse = _IDENTITY_4X4.copy()
            obj.matrix_basis = old_mpi @ old_basis
        return True

    def set_xform_trs(self, prim_path: str, payload: dict) -> bool:
        obj = self._find_object_by_prim(prim_path)
        if obj is None:
            LOG.warning("BlenderAdapter: object not found for prim %s", prim_path)
            return False

        fields = payload.get("fields", [])
        is_imported = obj.get(_PROP_USD_IMPORTED, False)
        parent_handles = _has_axis_rotation(obj.parent)
        # Axis conversion for T and S applies to ALL objects (imported or not)
        # when the scene is Y-up and no ancestor handles the rotation.
        # The is_imported flag only affects rotation handling (compose_axis_rotation
        # vs yup_to_zup_quat) — it should not skip T/S conversion.
        convert = self._needs_axis_conv and not parent_handles

        if "t" in fields and "t" in payload:
            t = payload["t"]
            tx, ty, tz = float(t[0]), float(t[1]), float(t[2])
            if convert:
                tx, ty, tz = yup_to_zup_vec(tx, ty, tz)
            obj.location = (tx, ty, tz)

        if "r" in fields and "r" in payload:
            r = payload["r"]
            rw, rx, ry, rz = float(r[0]), float(r[1]), float(r[2]), float(r[3])
            # Imported roots carry Rx(90°) for geometry display; compose it
            # onto the incoming USD rotation when the parent doesn't already.
            if is_imported and self._needs_axis_conv and not parent_handles:
                rw, rx, ry, rz = compose_axis_rotation(rw, rx, ry, rz)
            elif convert:
                rw, rx, ry, rz = yup_to_zup_quat(rw, rx, ry, rz)
            if BPY_AVAILABLE:
                q = mathutils.Quaternion((rw, rx, ry, rz))
                if obj.rotation_mode == "QUATERNION":
                    obj.rotation_quaternion = q
                else:
                    e = q.to_euler(obj.rotation_mode)
                    obj.rotation_euler = e

        if "s" in fields and "s" in payload:
            s = payload["s"]
            sx, sy, sz = float(s[0]), float(s[1]), float(s[2])
            if convert:
                sx, sy, sz = yup_to_zup_scale(sx, sy, sz)
            obj.scale = (sx, sy, sz)

        return True

    def set_xform_matrices(self, prim_path: str, payload: dict) -> bool:
        # Diagnostic only
        return True

    def delete_prim(self, prim_path: str) -> bool:
        if not BPY_AVAILABLE:
            return True
        obj = self._find_object_by_prim(prim_path)
        if not obj:
            return False
        try:
            self._registry.unregister(prim_path)
            for col in obj.users_collection:
                col.objects.unlink(obj)
            bpy.data.objects.remove(obj)
            return True
        except (ReferenceError, RuntimeError):
            LOG.exception("Failed to delete object for prim %s", prim_path)
            return False

    def deactivate_prim(self, prim_path: str, active: bool = False) -> bool:
        if not BPY_AVAILABLE:
            return True
        obj = self._find_object_by_prim(prim_path)
        if not obj:
            LOG.warning("BlenderAdapter.deactivate_prim: object not found for %s", prim_path)
            return False
        try:
            obj.hide_set(not active)
            obj.hide_render = not active
            return True
        except RuntimeError:
            LOG.exception("Failed to deactivate object for prim %s", prim_path)
            return False

    def rename_prim(self, prim_path: str, new_name: str) -> bool:
        if not BPY_AVAILABLE:
            return True
        obj = self._find_object_by_prim(prim_path)
        if not obj:
            LOG.warning("BlenderAdapter.rename_prim: object not found for %s", prim_path)
            return False
        parent = prim_path.rsplit("/", 1)[0]
        new_path = f"{parent}/{new_name}"
        obj["usd_prim_path"] = new_path
        self._registry.rename(prim_path, new_path)
        LOG.info(
            "BlenderAdapter: renamed prim path %s -> %s on object %s",
            prim_path,
            new_path,
            obj.name,
        )
        return True

    def set_visibility(self, prim_path: str, visible: bool) -> bool:
        if not BPY_AVAILABLE:
            return True
        obj = self._find_object_by_prim(prim_path)
        if not obj:
            LOG.warning("BlenderAdapter.set_visibility: object not found for %s", prim_path)
            return False
        try:
            obj.hide_viewport = not visible
            obj.hide_set(not visible)
            return True
        except RuntimeError:
            LOG.exception("Failed to set visibility for prim %s", prim_path)
            return False

    def set_gprim_attrs(self, prim_path: str, attrs: dict) -> bool:
        if not BPY_AVAILABLE:
            return True
        obj = self._find_object_by_prim(prim_path)
        if not obj:
            LOG.warning("BlenderAdapter.set_gprim_attrs: object not found for %s", prim_path)
            return False

        # Raw mesh topology: points + faceVertexCounts + faceVertexIndices
        if (
            "points" in attrs
            and "faceVertexCounts" in attrs
            and "faceVertexIndices" in attrs
        ):
            self._apply_mesh_topology(obj, attrs)

        # Standalone primvar:st update (without full topology rebuild)
        elif "primvars:st" in attrs and obj.type == "MESH" and obj.data:
            st = attrs["primvars:st"]
            mesh = obj.data
            uv_layer = mesh.uv_layers.get("st") or mesh.uv_layers.new(name="st")
            for i, uv in enumerate(st):
                if i < len(uv_layer.data):
                    uv_layer.data[i].uv = tuple(uv)

        # Map USD parametric attrs to Blender object scale
        usd_type = obj.get("usd_type_name", "")
        if usd_type == "Sphere" and "radius" in attrs:
            r = float(attrs["radius"])
            obj.scale = (r, r, r)
        elif usd_type == "Cube" and "size" in attrs:
            s = float(attrs["size"]) / 2.0  # Blender default cube is 2 units
            obj.scale = (s, s, s)
        elif usd_type in ("Cylinder", "Cone"):
            if "radius" in attrs:
                r = float(attrs["radius"])
                obj.scale = (r, r, obj.scale[2])
            if "height" in attrs:
                h = float(attrs["height"]) / 2.0
                obj.scale = (obj.scale[0], obj.scale[1], h)
        LOG.info("BlenderAdapter: set gprim attrs %s on %s", attrs, prim_path)
        return True

    def _apply_mesh_topology(self, obj, attrs: dict) -> None:
        """Build Blender mesh geometry from USD mesh topology attributes."""
        points = attrs["points"]
        face_counts = attrs["faceVertexCounts"]
        face_indices = attrs["faceVertexIndices"]

        # Convert faceVertexCounts + faceVertexIndices → face tuples
        faces = []
        idx = 0
        for count in face_counts:
            faces.append(tuple(face_indices[idx : idx + count]))
            idx += count

        verts = [tuple(p) for p in points]

        if obj.type == "MESH" and obj.data:
            mesh = obj.data
            mesh.clear_geometry()
        else:
            mesh = bpy.data.meshes.new(obj.name + "_mesh")
            obj.data = mesh

        mesh.from_pydata(verts, [], faces)
        mesh.update()

        # Apply primvars:st as UV layer if present
        st = attrs.get("primvars:st")
        if st and mesh.polygons:
            uv_layer = mesh.uv_layers.new(name="st")
            # faceVarying: one UV per loop (face-vertex)
            for i, uv in enumerate(st):
                if i < len(uv_layer.data):
                    uv_layer.data[i].uv = tuple(uv)

        LOG.info(
            "BlenderAdapter: built mesh topology for %s (%d verts, %d faces)",
            obj.get("usd_prim_path", obj.name),
            len(verts),
            len(faces),
        )


    def set_material_binding(self, prim_path: str, material_path: str) -> bool:
        if not BPY_AVAILABLE:
            return True
        if not material_path:
            obj = self._find_object_by_prim(prim_path)
            if obj and obj.data and obj.data.materials:
                obj.data.materials.clear()
            return True
        # Always create and tag the material — shader enrichment needs
        # the usd_material_path tag even when the mesh isn't found yet.
        mat_name = material_path.rsplit("/", 1)[-1]
        mat = bpy.data.materials.get(mat_name)
        if not mat:
            mat = bpy.data.materials.new(name=mat_name)
            mat.use_nodes = True
        mat["usd_material_path"] = material_path
        # Assign to the target object if it exists
        obj = self._find_object_by_prim(prim_path)
        if not obj or not obj.data:
            return True
        if not obj.data.materials:
            obj.data.materials.append(mat)
        else:
            obj.data.materials[0] = mat
        LOG.info("set_material_binding: %s -> %s", prim_path, mat_name)
        return True

    def set_shader_input(self, prim_path: str, shader_id: str,
                         inputs: dict, input_types: dict) -> bool:
        if not BPY_AVAILABLE:
            return True
        mapper = self._shader_registry.get(shader_id)
        if not mapper:
            LOG.info("set_shader_input: unsupported shader %s", shader_id)
            return True

        # Cache the shader_id for later use by set_shader_connection
        self._registry.set_shader(prim_path, shader_id=shader_id)

        if mapper.is_multi_node:
            return self._apply_multi_node_shader(prim_path, mapper, inputs)
        return self._apply_single_node_shader(prim_path, mapper, inputs)

    def _apply_single_node_shader(self, prim_path, mapper, inputs):
        """Apply shader inputs to a single Blender node."""
        mat, node = self._get_or_create_shader_node_for_input(
            prim_path, mapper.node_type,
        )
        if not mat or not node:
            return False

        node["usd_shader_path"] = prim_path
        node["usd_shader_id"] = mapper.shader_id

        for usd_name, value in inputs.items():
            mapper.apply_value(
                node, usd_name, value,
                resolve_asset=self._resolve_asset_path,
            )
        mapper.post_apply(node, inputs)

        LOG.info("set_shader_input: %s on %s", list(inputs.keys()), node.name)
        return True

    def _prepare_tree_for_network(self, tree):
        """Remove the default Principled BSDF so the mapper starts clean."""
        for node in list(tree.nodes):
            if node.type == "BSDF_PRINCIPLED":
                tree.nodes.remove(node)

    def _wire_surface_to_material_output(self, tree, output_map):
        """Connect the 'out' socket from output_map to Material Output."""
        surface_socket = output_map.get("out")
        if not surface_socket:
            return
        out_node = None
        for node in tree.nodes:
            if node.type == "OUTPUT_MATERIAL":
                out_node = node
                break
        if not out_node:
            out_node = tree.nodes.new(type="ShaderNodeOutputMaterial")
        if "Surface" in out_node.inputs:
            tree.links.new(surface_socket, out_node.inputs["Surface"])

    def _apply_multi_node_shader(self, prim_path, mapper, inputs):
        """Apply shader inputs via a multi-node network."""
        mat = self._find_material_for_shader(prim_path, create=True)
        if not mat.use_nodes:
            mat.use_nodes = True

        # Reuse cached socket maps if the network was already created and
        # the sockets are still valid (not from a deleted material).
        input_map = self._registry.get_shader(prim_path).get("input_map")
        if input_map is not None:
            try:
                _ = next(iter(input_map.values())).node
            except (ReferenceError, StopIteration):
                input_map = None

        if input_map is None:
            tree = mat.node_tree
            self._prepare_tree_for_network(tree)

            nodes, input_map, output_map = mapper.create_network(
                tree, inputs,
                resolve_asset=self._resolve_asset_path,
            )
            if not nodes:
                LOG.warning("create_network returned no nodes for %s", prim_path)
                return True

            node_name = prim_path.rsplit("/", 1)[-1]
            nodes[0].name = node_name
            nodes[0].label = node_name
            nodes[0]["usd_shader_path"] = prim_path
            nodes[0]["usd_shader_id"] = mapper.shader_id
            self._wire_surface_to_material_output(tree, output_map)

            self._registry.set_shader(prim_path, output_map=output_map, input_map=input_map)

        # Apply input values to the mapped sockets
        for usd_name, value in inputs.items():
            socket = input_map.get(usd_name)
            if socket is None:
                continue
            if isinstance(value, (list, tuple)) and len(value) == 3:
                # RGBA sockets need alpha appended; Vector sockets take 3 values
                if socket.type == "RGBA":
                    socket.default_value = (*value, 1.0)
                else:
                    socket.default_value = tuple(value)
            elif isinstance(value, (list, tuple)) and len(value) == 4:
                socket.default_value = tuple(value)
            else:
                socket.default_value = value

        LOG.info(
            "set_shader_input (multi-node): %s on %s",
            list(inputs.keys()), prim_path,
        )
        return True

    def _find_material_for_shader(self, prim_path: str, create: bool = False):
        """Find the Blender material that owns a shader prim path.

        Checks usd_material_path tags first, then walks up path segments
        by name.  Handles nested Material/NodeGraph/Shader structures.
        """
        for m in bpy.data.materials:
            mp = m.get("usd_material_path", "")
            if mp and prim_path.startswith(mp + "/"):
                return m
        path = prim_path
        while "/" in path:
            path = path.rsplit("/", 1)[0]
            candidate = path.rsplit("/", 1)[-1]
            mat = bpy.data.materials.get(candidate)
            if mat:
                return mat
        if create:
            mat_name = prim_path.rsplit("/", 1)[0].rsplit("/", 1)[-1]
            mat = bpy.data.materials.new(name=mat_name)
            mat.use_nodes = True
            return mat
        return None

    def _get_or_create_shader_node_for_input(self, prim_path, node_type):
        """Find/create the material and target node for a shader input event."""
        mat = self._find_material_for_shader(prim_path, create=True)
        if not mat.use_nodes:
            mat.use_nodes = True

        node_name = prim_path.rsplit("/", 1)[-1]
        tree = mat.node_tree

        # For Principled BSDF, reuse the existing default node
        if node_type == "ShaderNodeBsdfPrincipled":
            for n in tree.nodes:
                if n.type == "BSDF_PRINCIPLED":
                    n.name = node_name
                    return mat, n
            LOG.warning("No Principled BSDF in %s", mat_name)
            return mat, None

        # For other nodes, find by name or create
        node = tree.nodes.get(node_name)
        if not node:
            node = tree.nodes.new(type=node_type)
            node.name = node_name
            node.label = node_name
        return mat, node

    # USD output name → Blender socket name
    _OUTPUT_SOCKET_MAP = {
        ("ShaderNodeTexImage", "rgb"): "Color",
        ("ShaderNodeTexImage", "r"): "Color",
        ("ShaderNodeTexImage", "a"): "Alpha",
        ("ShaderNodeUVMap", "result"): "UV",
    }

    def set_shader_connection(self, prim_path: str,
                              connections: dict,
                              disconnections: list | None = None) -> bool:
        if not BPY_AVAILABLE:
            return True
        mat = self._find_material_for_shader(prim_path)
        if not mat or not mat.use_nodes:
            LOG.warning(
                "set_shader_connection: no material for %s", prim_path,
            )
            return False

        tree = mat.node_tree
        target_node = self._find_shader_node(tree, prim_path)

        # Multi-node mappers cache per-socket maps so connections route
        # to preprocessing nodes (e.g., base_color → Mix.B, not BSDF)
        cached_inputs = self._registry.get_shader(prim_path).get("input_map")

        for input_name, conn in connections.items():
            source_prim = conn["source_prim"]
            source_output = conn["source_output"]

            source_node = self._find_or_create_shader_node(
                tree, source_prim,
            )

            # Resolve source socket — check multi-node output map first
            cached_outputs = self._registry.get_shader(source_prim).get("output_map")
            if cached_outputs and source_output in cached_outputs:
                src_socket = cached_outputs[source_output]
            elif source_node:
                src_socket = self._get_output_socket(
                    source_node, source_output,
                )
            else:
                continue

            # Resolve target socket — check multi-node input map first
            if cached_inputs and input_name in cached_inputs:
                tgt_socket = cached_inputs[input_name]
            elif target_node:
                tgt_socket = self._get_input_socket(
                    target_node, input_name,
                )
            else:
                continue

            if src_socket and tgt_socket:
                tree.links.new(src_socket, tgt_socket)

        for input_name in disconnections or []:
            if cached_inputs and input_name in cached_inputs:
                socket = cached_inputs[input_name]
                for link in list(socket.links):
                    tree.links.remove(link)
            elif target_node and input_name in target_node.inputs:
                for link in list(target_node.inputs[input_name].links):
                    tree.links.remove(link)

        LOG.info(
            "set_shader_connection: %s on %s",
            list(connections.keys()), mat.name,
        )
        return True

    def _find_shader_node(self, tree, prim_path: str):
        """Find a Blender node by its USD prim path tag."""
        node_name = prim_path.rsplit("/", 1)[-1]
        return tree.nodes.get(node_name)

    def _find_or_create_shader_node(self, tree, prim_path: str):
        """Find or create a Blender shader node for a USD shader prim."""
        node_name = prim_path.rsplit("/", 1)[-1]
        existing = tree.nodes.get(node_name)
        if existing:
            return existing
        shader_id = self._registry.get_shader(prim_path).get("shader_id", "")
        node_type = self._shader_registry.get_node_type(shader_id)
        if not node_type:
            LOG.info("Unknown shader %s for node creation", shader_id)
            return None
        node = tree.nodes.new(type=node_type)
        node.name = node_name
        node.label = node_name
        return node

    def _get_output_socket(self, node, usd_output_name: str):
        """Map USD output name to Blender output socket."""
        key = (node.bl_idname, usd_output_name)
        socket_name = self._OUTPUT_SOCKET_MAP.get(key)
        if socket_name and socket_name in node.outputs:
            return node.outputs[socket_name]
        # Fallback: try the first output
        if node.outputs:
            return node.outputs[0]
        return None

    def _get_input_socket(self, node, usd_input_name: str):
        """Map USD input name to Blender input socket."""
        # Check the shader_id cache to find the right mapper
        for _, sc in self._registry.iter_shaders():
            sid = sc.get("shader_id")
            if not sid:
                continue
            mapper = self._shader_registry.get(sid)
            if mapper and mapper.node_type == node.bl_idname:
                blender_name = mapper.get_native_input(usd_input_name)
                if blender_name and not blender_name.startswith("_"):
                    if blender_name in node.inputs:
                        return node.inputs[blender_name]
                break
        # Direct name match fallback
        if usd_input_name in node.inputs:
            return node.inputs[usd_input_name]
        # UV input on texture nodes
        if usd_input_name == "st" and "Vector" in node.inputs:
            return node.inputs["Vector"]
        return None

    def _resolve_asset_path(self, asset_path: str) -> str | None:
        """Resolve a possibly-relative asset path to an absolute file path.

        Returns the resolved path, or None if the file doesn't exist.
        """
        resolved = asset_path
        if not os.path.isabs(resolved):
            asset_root = getattr(bpy.context.scene, "usd_connect_asset_root", "")
            if not asset_root:
                base_path = getattr(
                    bpy.context.scene, "usd_connect_base_usd_path", ""
                )
                if base_path:
                    asset_root = os.path.dirname(bpy.path.abspath(base_path))
            if asset_root:
                resolved = os.path.join(asset_root, resolved)
            else:
                resolved = os.path.abspath(resolved)

        resolved = os.path.normpath(resolved)
        if not os.path.isfile(resolved):
            LOG.warning("BlenderAdapter: file not found: %s", resolved)
            return None
        return resolved

    def _import_ref_asset(self, container, prim_path, resolved, prim_path_ref):
        """Import a USD asset and merge the imported root into *container*.

        The container (created by ensure_prim) is consumed: the imported
        root takes over its prim_path, position, and parent — becoming
        the single object that represents the prim.

        Returns (new_objs, merged) where merged is the new prim
        representation, or None if no objects were imported.
        """
        LOG.info("set_reference: importing %s for %s", resolved, prim_path)
        before = set(bpy.data.objects)

        # Suppress USDHook tagging during this import — the adapter
        # handles tagging with correct composed scene paths below.
        from .capture import USD_CONNECT_Hook

        USD_CONNECT_Hook._skip_root_inference = True

        import_kwargs = {
            "filepath": resolved,
            "import_guide": False,
            "import_visible_only": True,
        }
        if prim_path_ref:
            import_kwargs["prim_path_mask"] = prim_path_ref

        window = bpy.context.window
        if window is None:
            windows = list(bpy.context.window_manager.windows)
            window = windows[0] if windows else None

        # EXEC_DEFAULT bypasses the invoke callback which defers execution
        # via the event loop.  Without it, the import may not complete before
        # we check for newly created objects below.
        if window is not None:
            with bpy.context.temp_override(window=window):
                bpy.ops.wm.usd_import('EXEC_DEFAULT', **import_kwargs)
        else:
            bpy.ops.wm.usd_import('EXEC_DEFAULT', **import_kwargs)

        USD_CONNECT_Hook._skip_root_inference = False

        new_objs = set(bpy.data.objects) - before
        if not new_objs:
            LOG.warning(
                "BlenderAdapter: no objects imported from %s", resolved
            )
            return set(), None

        LOG.info("set_reference: imported %d objects from %s", len(new_objs), resolved)

        imported_roots = [
            obj
            for obj in new_objs
            if obj.parent is None or obj.parent not in new_objs
        ]

        # Remap file-internal paths to composed scene paths.
        # e.g. "/Teapot/teapot_MeshShape" → "/World/Teapot/teapot_MeshShape".
        # Imported roots are registered in the cache but their usd_prim_path
        # is NOT overwritten — the merge below handles that.
        imported_root_set = set(imported_roots)
        for obj in new_objs:
            hook_path = obj.get("usd_prim_path", "")
            if hook_path and prim_path_ref and hook_path.startswith(prim_path_ref):
                composed_path = prim_path + hook_path[len(prim_path_ref):]
            else:
                obj_name = obj.name.replace(".", "_")
                composed_path = f"{prim_path}/{obj_name}"
            if obj in imported_root_set:
                # Register the child path so lookups (e.g. set_material_binding)
                # can find the merged root by its original mesh path.
                if composed_path != prim_path:
                    self._registry.register(composed_path, obj)
            else:
                obj["usd_prim_path"] = composed_path
                self._registry.register(composed_path, obj)

        # Merge: replace the container with the first imported root.
        # The root becomes the prim representation — it holds both the
        # container's position and the import-time Rx(90°) axis rotation.
        merged = None
        if imported_roots:
            root = imported_roots[0]

            # Transfer container's properties to the imported root
            root["usd_prim_path"] = prim_path
            root[_PROP_USD_IMPORTED] = True
            for key in ("usd_type_name", "usd_ref_asset", "usd_ref_prim"):
                val = container.get(key)
                if val is not None:
                    root[key] = val

            # Copy container's location (the prim's position from set_xform_trs)
            root.location = container.location.copy()

            # Reparent root under container's parent
            container_parent = container.parent
            root.parent = container_parent
            root.matrix_parent_inverse = container.matrix_parent_inverse.copy()

            # Avoid double Rx(90°): if the parent chain already has axis
            # rotation, reset the root's own import-time rotation.
            if _has_axis_rotation(container_parent):
                root.rotation_quaternion = (1, 0, 0, 0)
                root.rotation_euler = (0, 0, 0)
                root.rotation_axis_angle = (0, 0, 1, 0)

            # Move any additional imported roots under the same parent
            for extra_root in imported_roots[1:]:
                extra_root.parent = root
                extra_root.matrix_parent_inverse = _IDENTITY_4X4.copy()

            # Remove the container — frees its name for the merged root.
            container_name = container.name
            self._registry.unregister(prim_path)
            for col in container.users_collection:
                col.objects.unlink(container)
            bpy.data.objects.remove(container)

            # The imported root may have a suffixed name (e.g. "Teapot.001")
            # because the container existed during import.  Now that the
            # container is gone, reclaim the original name.
            if root.name != container_name:
                root.name = container_name

            # Register the merged root as the prim representation.
            self._registry.register(prim_path, root)
            merged = root

        return new_objs, merged

    @staticmethod
    def _is_under_material(prim) -> bool:
        """Return True if prim is a descendant of a UsdShade.Material."""
        from pxr import UsdShade

        ancestor = prim.GetParent()
        while ancestor and ancestor.IsValid():
            if ancestor.IsA(UsdShade.Material):
                return True
            ancestor = ancestor.GetParent()
        return False

    def _enrich_materialx_from_import(self, resolved, prim_path, prim_path_ref):
        """Read MaterialX materials from an imported USD file and apply them.

        Opens the file via pxr, walks Material/Shader prims, and applies
        them through the adapter's shader pipeline so ActivisionMtlxMapper
        creates proper node networks for shaders that Blender's built-in
        USD importer doesn't handle (e.g., ND_standard_surface_surfaceshader).
        """
        try:
            from pxr import Usd, UsdShade

            from openusdconnect.emitter import (
                _read_material_binding,
                _read_shader_inputs,
            )
        except ImportError:
            return

        try:
            stage = Usd.Stage.Open(resolved)
        except Exception:
            LOG.debug("Could not open %s for MaterialX enrichment", resolved)
            return

        # Walk the full stage — materials may live in sibling scopes.
        root_prim = stage.GetPseudoRoot()
        if not root_prim or not root_prim.IsValid():
            return

        def _remap(file_path):
            """Remap a USD file path to the composed scene path."""
            if prim_path_ref and file_path.startswith(prim_path_ref):
                return prim_path + file_path[len(prim_path_ref):]
            return prim_path + "/" + file_path.lstrip("/")

        # Collect material bindings, shader inputs, and connections
        # in event-kind order: bindings after shader inputs (matching protocol)
        shader_events = []
        connection_events = []
        binding_events = []

        for prim in Usd.PrimRange(root_prim):
            file_path = str(prim.GetPath())
            scene_path = _remap(file_path)

            binding_target = _read_material_binding(stage, file_path)
            if binding_target:
                binding_events.append((scene_path, _remap(binding_target)))

            if prim.IsA(UsdShade.Shader) and self._is_under_material(prim):
                sid, inputs, itypes, conns = _read_shader_inputs(stage, file_path)
                if sid:
                    shader_events.append((scene_path, sid, inputs, itypes))
                    if conns:
                        remapped_conns = {}
                        for inp_name, conn in conns.items():
                            remapped_conns[inp_name] = {
                                "source_prim": _remap(conn["source_prim"]),
                                "source_output": conn["source_output"],
                            }
                        connection_events.append((scene_path, remapped_conns))

        # Bindings first so materials are tagged with usd_material_path
        # before set_shader_input looks them up.
        for scene_path, target in binding_events:
            self.set_material_binding(scene_path, target)
        # Blender's USD importer already handles UsdPreviewSurface shaders.
        # When a material also has a MaterialX shader (multi-node), prefer
        # the MaterialX version — it's more expressive and isn't handled
        # by the importer.  Applying both would overwrite the node tree.
        multi_node_mats = set()
        for scene_path, sid, inputs, itypes in shader_events:
            mapper = self._shader_registry.get(sid)
            if mapper and mapper.is_multi_node:
                mat_path = scene_path.rsplit("/", 1)[0]
                multi_node_mats.add(mat_path)
        for scene_path, sid, inputs, itypes in shader_events:
            mapper = self._shader_registry.get(sid)
            if mapper and not mapper.is_multi_node:
                mat_path = scene_path.rsplit("/", 1)[0]
                if mat_path in multi_node_mats:
                    continue
            self.set_shader_input(scene_path, sid, inputs, itypes)
        for scene_path, conns in connection_events:
            self.set_shader_connection(scene_path, conns)

        # Fix missing textures — Blender's USD importer creates Image Texture
        # nodes but often fails to resolve relative texture paths. Walk the
        # stage for texture shaders and load the images using pxr's resolved paths.
        self._fix_missing_textures(stage, root_prim, prim_path, _remap)

        if shader_events:
            LOG.info(
                "Post-import enrichment: applied %d shaders from %s",
                len(shader_events), resolved,
            )

    def _fix_missing_textures(self, stage, root_prim, prim_path, remap_fn):
        """Load images for texture nodes that Blender's importer left empty.

        Builds a lookup of resolved texture paths from the USD stage, then
        walks Blender materials for Image Texture nodes with no image and
        matches them by node name.
        """
        from pxr import Usd, UsdShade

        # Build map: node_name → resolved file path from USD stage
        resolved_textures = {}
        for prim in Usd.PrimRange(root_prim):
            if not prim.IsA(UsdShade.Shader):
                continue
            shader = UsdShade.Shader(prim)
            sid = shader.GetIdAttr().Get() or ""
            if "Texture" not in sid and "image" not in sid:
                continue
            file_inp = shader.GetInput("file")
            if not file_inp:
                continue
            asset_val = file_inp.Get()
            if not asset_val:
                continue
            resolved_path = getattr(asset_val, "resolvedPath", "")
            if resolved_path and os.path.isfile(resolved_path):
                resolved_textures[prim.GetName()] = resolved_path

        if not resolved_textures:
            return

        # Walk all Blender materials for empty Image Texture nodes
        loaded = 0
        for mat in bpy.data.materials:
            if not mat.use_nodes or not mat.node_tree:
                continue
            for node in mat.node_tree.nodes:
                if node.bl_idname != "ShaderNodeTexImage":
                    continue
                if node.image is not None:
                    continue
                tex_path = resolved_textures.get(node.name)
                if not tex_path:
                    continue
                img = bpy.data.images.get(os.path.basename(tex_path))
                if not img:
                    img = bpy.data.images.load(tex_path)
                node.image = img
                loaded += 1

        if loaded:
            LOG.info("Loaded %d missing textures from %s", loaded, stage.GetRootLayer().identifier)

    def set_reference(self, prim_path: str, refs: list) -> bool:
        if not BPY_AVAILABLE:
            LOG.info("BlenderAdapter.set_reference dry: %s", prim_path)
            return True

        if len(refs) == 0:
            self._remove_imported_ref_children(prim_path)
            return True

        # Resolve all asset paths up front so we can bail if none exist
        resolved_refs = []
        for ref_entry in refs:
            asset_path = ref_entry.get("asset_path", "")
            prim_path_ref = ref_entry.get("prim_path", "")
            resolved = self._resolve_asset_path(asset_path)
            if resolved is not None:
                resolved_refs.append((resolved, prim_path_ref))

        if not resolved_refs:
            return False

        # Dedup: skip import when children already exist in the scene.
        # Covers two cases:
        #   1. Adapter previously imported the asset (_imported_refs has entry)
        #   2. User imported the base scene before the adapter existed
        #      (e.g. single-instance emitter+receiver loopback) — children
        #      exist but _imported_refs is empty.
        if self._ref_children_exist(prim_path):
            prev_entry = self._registry.get_imported_ref(prim_path)
            prev_asset = prev_entry[0] if prev_entry else None
            if prev_asset is None or prev_asset == resolved_refs[0][0]:
                # Same asset (or unknown) and children present — skip import.
                # Tag the container for future dedup.
                container = self._find_object_by_prim(prim_path)
                if container is not None and len(resolved_refs) == 1:
                    container["usd_type_name"] = "Reference"
                    container["usd_ref_asset"] = resolved_refs[0][0]
                    self._registry.set_imported_ref(prim_path, resolved_refs[0][0], resolved_refs[0][1])
                LOG.info(
                    "set_reference: children already exist for %s, skipping re-import",
                    prim_path,
                )
                return True
            # Different asset — remove old children, re-import below
            self._remove_imported_ref_children(prim_path)
        elif self._registry.get_imported_ref(prim_path) is not None:
            # Had a previous import but children are gone — remove stale entry
            self._remove_imported_ref_children(prim_path)

        # Find or create the container object for this prim_path.
        container = self._find_object_by_prim(prim_path)
        if container is None:
            name = prim_path.strip("/").replace("/", "_") or prim_path
            container = bpy.data.objects.new(name, None)
            container["usd_prim_path"] = prim_path
            container["usd_type_name"] = "Reference"
            self._link_object(container)
            self._registry.register(prim_path, container)
            parent_path = prim_path.rsplit("/", 1)[0]
            if parent_path:
                parent_obj = self._find_object_by_prim(parent_path)
                if parent_obj is not None:
                    container.parent = parent_obj
                    container.matrix_parent_inverse = _IDENTITY_4X4.copy()
        else:
            container["usd_type_name"] = "Reference"

        total_imported = 0
        prim_obj = container  # may be replaced by merge
        for resolved, prim_path_ref in resolved_refs:
            new_objs, merged = self._import_ref_asset(
                prim_obj, prim_path, resolved, prim_path_ref
            )
            total_imported += len(new_objs)
            if merged is not None:
                prim_obj = merged  # container was consumed by merge
            self._enrich_materialx_from_import(resolved, prim_path, prim_path_ref)

        # Record composed children so ensure_prim doesn't create duplicates
        # when the emitter sends events for prims the import already created.
        from . import capture
        if capture._state.author is not None:
            from pxr import Usd
            stage = capture._state.author.stage
            ref_prim = stage.GetPrimAtPath(prim_path)
            if ref_prim and ref_prim.IsValid():
                for child in Usd.PrimRange(ref_prim):
                    cp = str(child.GetPath())
                    if cp != prim_path:
                        self._registry._ref_children.add(cp)

        # Tag prim object with resolved asset (for dedup on single-ref)
        if len(resolved_refs) == 1:
            prim_obj["usd_ref_asset"] = resolved_refs[0][0]
            prim_obj["usd_ref_prim"] = resolved_refs[0][1]
            self._registry.set_imported_ref(prim_path, resolved_refs[0][0], resolved_refs[0][1])

        LOG.info(
            "BlenderAdapter.set_reference: imported %d objects for %s",
            total_imported,
            prim_path,
        )
        return True

    def _ref_children_exist(self, prim_path: str) -> bool:
        """Check if at least one child object from a previous reference import still exists."""
        container = self._find_object_by_prim(prim_path)
        if container is None:
            return False
        return self._registry.children_exist(prim_path)

    def _remove_imported_ref_children(self, prim_path: str):
        """Remove child objects previously imported for a reference prim (keep container)."""
        if not BPY_AVAILABLE:
            return
        to_remove = self._registry.pop_children(prim_path)
        # Single O(N) scan — cached refs may be stale if objects were
        # deleted by undo, user action, or merge.
        path_to_obj = {
            o.get("usd_prim_path"): o
            for o in bpy.data.objects
            if o.get("usd_prim_path") in to_remove
        }
        for pp in to_remove:
            obj = path_to_obj.get(pp)
            if obj is None:
                continue
            for col in obj.users_collection:
                col.objects.unlink(obj)
            bpy.data.objects.remove(obj)

        # Clean up emitter state so _detect_deletions doesn't find stale
        # references and trigger a storm of deactivation events.
        from . import capture

        if capture._state.author is not None:
            capture._state.author.purge_prim_refs(prefix)
        if capture._state.notice_emitter is not None:
            for pp in to_remove:
                capture._state.notice_emitter._purge_caches(pp)

        self._registry.clear_imported_ref(prim_path)

    def set_payload(self, prim_path: str, payloads: list) -> bool:
        self._pending_payloads[prim_path] = payloads
        LOG.info(
            "BlenderAdapter: payload arc set on %s (%d entries, unloaded)",
            prim_path,
            len(payloads),
        )
        return True

    def load_payload(self, prim_path: str) -> bool:
        if not BPY_AVAILABLE:
            LOG.info("BlenderAdapter.load_payload dry: %s", prim_path)
            return True
        payloads = self._pending_payloads.get(prim_path, [])
        if not payloads:
            LOG.warning("BlenderAdapter.load_payload: no pending payloads for %s", prim_path)
            return False
        # Dedup: skip if children already exist (same pattern as set_reference)
        if self._ref_children_exist(prim_path):
            # Still populate _imported_refs so set_variant_selections can
            # find the asset on resync (adapter was reset but objects persist).
            if self._registry.get_imported_ref(prim_path) is None:
                for entry in payloads:
                    asset_path = entry.get("asset_path", "")
                    prim_path_ref = entry.get("prim_path", "")
                    resolved = self._resolve_asset_path(asset_path)
                    if resolved is not None:
                        self._registry.set_imported_ref(prim_path, resolved, prim_path_ref)
                        break
            LOG.info("load_payload: children already exist for %s, skipping", prim_path)
            return True
        # Resolve and import
        prim_obj = self._find_object_by_prim(prim_path)
        if prim_obj is None:
            self.ensure_prim(prim_path)
            prim_obj = self._find_object_by_prim(prim_path)
        for entry in payloads:
            asset_path = entry.get("asset_path", "")
            prim_path_ref = entry.get("prim_path", "")
            resolved = self._resolve_asset_path(asset_path)
            if resolved is not None:
                new_objs, merged = self._import_ref_asset(
                    prim_obj, prim_path, resolved, prim_path_ref,
                )
                if merged is not None:
                    prim_obj = merged
                self._enrich_materialx_from_import(resolved, prim_path, prim_path_ref)
                # Track the imported asset so set_variant_selections can
                # find it later for re-import with the new variant applied.
                prim_obj["usd_ref_asset"] = resolved
                prim_obj["usd_ref_prim"] = prim_path_ref
                self._registry.set_imported_ref(prim_path, resolved, prim_path_ref)
        LOG.info("BlenderAdapter.load_payload: loaded %s", prim_path)
        return True

    def unload_payload(self, prim_path: str) -> bool:
        if not BPY_AVAILABLE:
            LOG.info("BlenderAdapter.unload_payload dry: %s", prim_path)
            return True
        self._remove_imported_ref_children(prim_path)
        LOG.info("BlenderAdapter.unload_payload: unloaded %s", prim_path)
        return True

    def _create_variant_stage(self, asset_path, prim_path_ref, selections):
        """Create a temporary USD file with variant selections pre-applied."""
        import tempfile

        try:
            from pxr import Usd
        except ImportError:
            return None

        try:
            stage = Usd.Stage.Open(asset_path)
        except Exception:
            LOG.debug("Could not open %s for variant stage", asset_path)
            return None

        root_path = prim_path_ref or str(stage.GetDefaultPrim().GetPath())
        prim = stage.GetPrimAtPath(root_path)
        if not prim or not prim.IsValid():
            return None

        vsets = prim.GetVariantSets()
        for name, value in selections.items():
            if vsets.HasVariantSet(name):
                vsets.GetVariantSet(name).SetVariantSelection(value)

        # Write the temp file next to the asset so relative texture paths
        # resolve correctly.  Fall back to the system temp dir if the asset
        # directory isn't writable (common on Windows where USD's safe-write
        # rename can fail with "Access is denied").
        asset_dir = os.path.dirname(asset_path)
        for tmp_dir in (asset_dir, None):
            try:
                tmp = tempfile.NamedTemporaryFile(
                    suffix=".usda", delete=False, dir=tmp_dir,
                )
                tmp_path = tmp.name
                tmp.close()
                stage.Export(tmp_path)
                return tmp_path
            except OSError as e:
                LOG.warning("_create_variant_stage: export to %s failed: %s", tmp_dir, e)
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        LOG.warning("_create_variant_stage: could not export variant stage for %s", asset_path)
        return None

    def set_variant_selections(self, prim_path: str, selections: dict[str, str]) -> bool:
        """Apply variant selection changes.

        If the prim has previously imported children (from a reference or
        payload), remove them and re-import with the new variant applied.
        """
        if not BPY_AVAILABLE:
            LOG.info("BlenderAdapter.set_variant_selections dry: %s -> %s", prim_path, selections)
            return True

        prev_entry = self._registry.get_imported_ref(prim_path)
        if prev_entry and self._ref_children_exist(prim_path):
            prev_asset, prev_prim_ref = prev_entry
            self._remove_imported_ref_children(prim_path)
            container = self._find_object_by_prim(prim_path)
            if container is not None:
                temp_path = self._create_variant_stage(
                    prev_asset, prev_prim_ref, selections,
                )
                import_path = temp_path or prev_asset
                _objs, _merged = self._import_ref_asset(
                    container, prim_path, import_path, prev_prim_ref,
                )
                self._enrich_materialx_from_import(
                    import_path, prim_path, prev_prim_ref,
                )
                self._registry.set_imported_ref(prim_path, prev_asset, prev_prim_ref)
                if temp_path:
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
            LOG.info(
                "set_variant_selections: re-imported %s after variant change %s",
                prim_path, selections,
            )
        else:
            LOG.info(
                "BlenderAdapter.set_variant_selections: %s -> %s",
                prim_path, selections,
            )
        return True
