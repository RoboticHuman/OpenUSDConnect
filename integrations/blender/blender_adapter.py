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
except Exception:
    BPY_AVAILABLE = False
    _IDENTITY_4X4 = None

from openusdconnect.adapters import DCCAdapter

from .shader_mapper import create_default_registry

LOG = logging.getLogger(__name__)


class BlenderAdapter(DCCAdapter):
    """Applies incoming events to Blender scene objects.

    Finds objects by usd_prim_path custom property via a cached index.
    Rotation payload is quaternion [w,x,y,z]; converts to object's rotation_mode.
    """

    def __init__(self):
        self._prim_cache: dict[str, object] = {}  # prim_path -> bpy.types.Object
        self._imported_refs: dict[str, str] = {}  # prim_path -> asset_path
        self._pending_payloads: dict[str, list] = {}  # prim_path -> payload list
        self._shader_registry = create_default_registry()
        # Rebuild caches from scene so a fresh adapter (after receiver reset)
        # knows about objects that persist from a previous session.
        if BPY_AVAILABLE:
            for obj in bpy.data.objects:
                pp = obj.get("usd_prim_path")
                if pp:
                    self._prim_cache[pp] = obj
                    if obj.get("usd_type_name") == "Reference":
                        ref_asset = obj.get("usd_ref_asset", "")
                        if ref_asset:
                            self._imported_refs[pp] = ref_asset

    def _find_object_by_prim(self, prim_path: str) -> object | None:
        if not BPY_AVAILABLE:
            return None
        # Check cache first
        obj = self._prim_cache.get(prim_path)
        if obj is not None:
            try:
                # Verify the object is still valid and has the right prim path
                if obj.get("usd_prim_path") == prim_path:
                    return obj
            except ReferenceError:
                pass
            del self._prim_cache[prim_path]
        # Fall back to scan and cache the result
        for obj in bpy.data.objects:
            if obj.get("usd_prim_path") == prim_path:
                self._prim_cache[prim_path] = obj
                return obj
        return None

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

        new = self._create_blender_object(prim_path, type_name)
        self._link_object(new)
        self._prim_cache[prim_path] = new
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

        if "t" in fields and "t" in payload:
            t = payload["t"]
            obj.location = (float(t[0]), float(t[1]), float(t[2]))

        if "r" in fields and "r" in payload:
            r = payload["r"]  # [w,x,y,z]
            if BPY_AVAILABLE:
                q = mathutils.Quaternion((float(r[0]), float(r[1]), float(r[2]), float(r[3])))
                if obj.rotation_mode == "QUATERNION":
                    obj.rotation_quaternion = q
                else:
                    e = q.to_euler(obj.rotation_mode)
                    obj.rotation_euler = e

        if "s" in fields and "s" in payload:
            s = payload["s"]
            obj.scale = (float(s[0]), float(s[1]), float(s[2]))

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
            self._prim_cache.pop(prim_path, None)
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
        self._prim_cache.pop(prim_path, None)
        self._prim_cache[new_path] = obj
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
        obj = self._find_object_by_prim(prim_path)
        if not obj:
            LOG.warning("set_material_binding: no object for %s", prim_path)
            return False
        if not material_path:
            if obj.data and obj.data.materials:
                obj.data.materials.clear()
            return True
        # Derive Blender material name from prim path
        mat_name = material_path.rsplit("/", 1)[-1]
        mat = bpy.data.materials.get(mat_name)
        if not mat:
            mat = bpy.data.materials.new(name=mat_name)
            mat.use_nodes = True
        if not obj.data:
            return False
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
        self._prim_cache[prim_path + ":shader_id"] = shader_id

        # Find or create the material and node
        mat, node = self._get_or_create_shader_node_for_input(
            prim_path, mapper.node_type,
        )
        if not mat or not node:
            return False

        for usd_name, value in inputs.items():
            mapper.apply_value(
                node, usd_name, value,
                resolve_asset=self._resolve_asset_path,
            )
        mapper.post_apply(node, inputs)

        LOG.info("set_shader_input: %s on %s", list(inputs.keys()), node.name)
        return True

    def _get_or_create_shader_node_for_input(self, prim_path, node_type):
        """Find/create the material and target node for a shader input event."""
        mat_path = prim_path.rsplit("/", 1)[0]
        mat_name = mat_path.rsplit("/", 1)[-1]
        mat = bpy.data.materials.get(mat_name)
        if not mat:
            mat = bpy.data.materials.new(name=mat_name)
            mat.use_nodes = True
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
        # Find the material this shader belongs to
        mat_path = prim_path.rsplit("/", 1)[0]
        mat_name = mat_path.rsplit("/", 1)[-1]
        mat = bpy.data.materials.get(mat_name)
        if not mat or not mat.use_nodes:
            LOG.warning(
                "set_shader_connection: material %s not found", mat_name,
            )
            return False

        tree = mat.node_tree
        # Find the target node (the shader prim this event is for)
        target_node = self._find_shader_node(tree, prim_path)

        for input_name, conn in connections.items():
            source_prim = conn["source_prim"]
            source_output = conn["source_output"]

            # Find or create the source node
            source_node = self._find_or_create_shader_node(
                tree, source_prim,
            )
            if not source_node or not target_node:
                continue

            # Find the correct sockets
            src_socket = self._get_output_socket(
                source_node, source_output,
            )
            tgt_socket = self._get_input_socket(
                target_node, input_name,
            )
            if src_socket and tgt_socket:
                tree.links.new(src_socket, tgt_socket)

        for input_name in disconnections or []:
            if target_node and input_name in target_node.inputs:
                for link in list(target_node.inputs[input_name].links):
                    tree.links.remove(link)

        LOG.info(
            "set_shader_connection: %s on %s",
            list(connections.keys()), mat_name,
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
        shader_id = self._prim_cache.get(
            prim_path + ":shader_id", "",
        )
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
        for key, val in self._prim_cache.items():
            if not key.endswith(":shader_id"):
                continue
            mapper = self._shader_registry.get(val)
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
        """Import a single USD asset and parent results under *container*.

        Returns the set of newly created Blender objects (may be empty).
        """
        LOG.info("set_reference: importing %s for %s", resolved, prim_path)
        before = set(bpy.data.objects)

        try:
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

            if window is not None:
                with bpy.context.temp_override(window=window):
                    bpy.ops.wm.usd_import(**import_kwargs)
            else:
                bpy.ops.wm.usd_import(**import_kwargs)
        except Exception:
            LOG.exception(
                "BlenderAdapter: USD import failed for %s", resolved
            )
            return set()

        new_objs = set(bpy.data.objects) - before
        if not new_objs:
            LOG.warning(
                "BlenderAdapter: no objects imported from %s", resolved
            )
            return set()

        LOG.info("set_reference: imported %d objects from %s", len(new_objs), resolved)

        imported_roots = [
            obj
            for obj in new_objs
            if obj.parent is None or obj.parent not in new_objs
        ]

        for obj in new_objs:
            obj_name = obj.name.replace(".", "_")
            child_path = f"{prim_path}/{obj_name}"
            obj["usd_prim_path"] = child_path
            self._prim_cache[child_path] = obj

        for root in imported_roots:
            world = root.matrix_world.copy()
            root.parent = container
            root.matrix_parent_inverse = _IDENTITY_4X4.copy()
            root.matrix_basis = container.matrix_world.inverted_safe() @ world

        return new_objs

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
            prev = self._imported_refs.get(prim_path)
            if prev is None or prev == resolved_refs[0][0]:
                # Same asset (or unknown) and children present — skip import.
                # Tag the container for future dedup.
                container = self._find_object_by_prim(prim_path)
                if container is not None and len(resolved_refs) == 1:
                    container["usd_type_name"] = "Reference"
                    container["usd_ref_asset"] = resolved_refs[0][0]
                    self._imported_refs[prim_path] = resolved_refs[0][0]
                LOG.info(
                    "set_reference: children already exist for %s, skipping re-import",
                    prim_path,
                )
                return True
            # Different asset — remove old children, re-import below
            self._remove_imported_ref_children(prim_path)
        elif prim_path in self._imported_refs:
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
            self._prim_cache[prim_path] = container
            parent_path = prim_path.rsplit("/", 1)[0]
            if parent_path:
                parent_obj = self._find_object_by_prim(parent_path)
                if parent_obj is not None:
                    container.parent = parent_obj
                    container.matrix_parent_inverse = _IDENTITY_4X4.copy()
        else:
            container["usd_type_name"] = "Reference"

        total_imported = 0
        for resolved, prim_path_ref in resolved_refs:
            new_objs = self._import_ref_asset(
                container, prim_path, resolved, prim_path_ref
            )
            total_imported += len(new_objs)

        # Tag container with resolved asset (for dedup on single-ref)
        if len(resolved_refs) == 1:
            container["usd_ref_asset"] = resolved_refs[0][0]
            self._imported_refs[prim_path] = resolved_refs[0][0]

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
        prefix = prim_path + "/"
        return any(pp.startswith(prefix) for pp in self._prim_cache)

    def _remove_imported_ref_children(self, prim_path: str):
        """Remove child objects previously imported for a reference prim (keep container)."""
        if not BPY_AVAILABLE:
            return
        prefix = prim_path + "/"
        to_remove = [
            (pp, obj) for pp, obj in self._prim_cache.items()
            if pp.startswith(prefix)
        ]
        for pp, obj in to_remove:
            del self._prim_cache[pp]
            try:
                for col in obj.users_collection:
                    col.objects.unlink(obj)
                bpy.data.objects.remove(obj)
            except ReferenceError:
                pass  # already deleted
        self._imported_refs.pop(prim_path, None)

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
            LOG.info("load_payload: children already exist for %s, skipping", prim_path)
            return True
        # Resolve and import
        container = self._find_object_by_prim(prim_path)
        if container is None:
            self.ensure_prim(prim_path)
            container = self._find_object_by_prim(prim_path)
        for entry in payloads:
            asset_path = entry.get("asset_path", "")
            prim_path_ref = entry.get("prim_path", "")
            resolved = self._resolve_asset_path(asset_path)
            if resolved is not None:
                self._import_ref_asset(container, prim_path, resolved, prim_path_ref)
        LOG.info("BlenderAdapter.load_payload: loaded %s", prim_path)
        return True

    def unload_payload(self, prim_path: str) -> bool:
        if not BPY_AVAILABLE:
            LOG.info("BlenderAdapter.unload_payload dry: %s", prim_path)
            return True
        self._remove_imported_ref_children(prim_path)
        LOG.info("BlenderAdapter.unload_payload: unloaded %s", prim_path)
        return True

    def set_variant_selections(self, prim_path: str, selections: dict[str, str]) -> bool:
        """Apply variant selection changes.

        If the prim has previously imported children (from a reference or
        payload), remove them and re-import so Blender reflects the new
        variant's geometry.  Otherwise just log — attribute-level changes
        (radius, transforms, etc.) arrive as separate events.
        """
        if not BPY_AVAILABLE:
            LOG.info("BlenderAdapter.set_variant_selections dry: %s -> %s", prim_path, selections)
            return True

        # If this prim has imported children from a ref/payload, re-import
        # to pick up the variant's composed geometry.
        prev_asset = self._imported_refs.get(prim_path)
        if prev_asset and self._ref_children_exist(prim_path):
            self._remove_imported_ref_children(prim_path)
            container = self._find_object_by_prim(prim_path)
            if container is not None:
                self._import_ref_asset(container, prim_path, prev_asset, "")
                self._imported_refs[prim_path] = prev_asset
            LOG.info(
                "set_variant_selections: re-imported %s after variant change %s",
                prim_path, selections,
            )
        else:
            LOG.info(
                "BlenderAdapter.set_variant_selections: %s -> %s", prim_path, selections,
            )
        return True
