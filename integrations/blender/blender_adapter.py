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

LOG = logging.getLogger(__name__)


class BlenderAdapter(DCCAdapter):
    """Applies incoming events to Blender scene objects.

    Finds objects by usd_prim_path custom property via a cached index.
    Rotation payload is quaternion [w,x,y,z]; converts to object's rotation_mode.
    """

    def __init__(self):
        self._prim_cache: dict[str, object] = {}  # prim_path -> bpy.types.Object
        self._imported_refs: dict[str, str] = {}  # prim_path -> asset_path
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

    def ensure_prim(self, prim_path: str, type_name: str = "Xform") -> bool:
        if not BPY_AVAILABLE:
            LOG.info("BlenderAdapter.ensure_prim dry: %s", prim_path)
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
        except Exception as e:
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
        # Payloads are unloaded by default — don't import anything.
        # Users opt-in to load payloads when ready.
        LOG.info(
            "BlenderAdapter: payload arc set on %s (%d entries, unloaded)",
            prim_path,
            len(payloads),
        )
        return True
