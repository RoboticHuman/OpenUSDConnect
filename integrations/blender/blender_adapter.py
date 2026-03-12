"""BlenderAdapter — applies incoming USD sync events to Blender objects.

Implements the DCCAdapter interface for Blender. Finds objects by
obj["usd_prim_path"] custom property and sets location/rotation/scale.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

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
        self._prim_cache: Dict[str, object] = {}  # prim_path -> bpy.types.Object

    def _find_object_by_prim(self, prim_path: str) -> Optional[object]:
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
        except Exception:
            pass
        # Fallback: try context collection
        if not linked:
            try:
                col = bpy.context.collection
                col.objects.link(obj)
                linked = True
            except Exception:
                pass
        if not linked:
            print(f"[USD Connect] ERROR: Failed to link object {obj.name} for prim {obj.get('usd_prim_path')}")
            LOG.error("Failed to link object %s for prim %s", obj.name, obj.get("usd_prim_path"))

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
                bmesh.ops.create_cone(bm, segments=32, radius1=1.0, radius2=1.0,
                                      depth=2.0, cap_ends=True)
            elif type_name == "Cone":
                bmesh.ops.create_cone(bm, segments=32, radius1=1.0, radius2=0.0,
                                      depth=2.0, cap_ends=True)
            else:
                bm.free()
                return None
            bm.to_mesh(mesh)
        finally:
            bm.free()
        mesh.update()
        return bpy.data.objects.new(name, mesh)

    def ensure_prim(self, prim_path: str, type_name: str = "Xform") -> bool:
        if not BPY_AVAILABLE:
            LOG.info("BlenderAdapter.ensure_prim dry: %s", prim_path)
            return True
        obj = self._find_object_by_prim(prim_path)
        if obj:
            return True
        name = prim_path.strip("/").replace("/", "_") or prim_path
        print(f"[USD Connect] ensure_prim: creating {type_name} '{name}' for {prim_path}")

        # Create the object based on type
        new = None
        if type_name in ("Sphere", "Cube", "Cylinder", "Cone"):
            new = self._create_mesh_primitive(name, type_name)
        if new is None and type_name == "Mesh":
            new = bpy.data.objects.new(name, bpy.data.meshes.new(name + "_mesh"))
        if new is None:
            new = bpy.data.objects.new(name, None)  # Xform or unknown → Empty

        new["usd_prim_path"] = prim_path
        new["usd_type_name"] = type_name
        self._link_object(new)
        self._prim_cache[prim_path] = new

        # Parent under the correct ancestor so local transforms are correct.
        # e.g. /World/Cube_1 → parent is the object with usd_prim_path="/World"
        parent_path = prim_path.rsplit("/", 1)[0]
        if parent_path:
            parent_obj = self._find_object_by_prim(parent_path)
            if parent_obj is not None:
                new.parent = parent_obj
                # Identity parent inverse so location/rotation/scale are local-to-parent
                # (matching what the emitter sends)
                new.matrix_parent_inverse = _IDENTITY_4X4.copy()
                # Move the child into the parent's collection so it doesn't
                # appear as a duplicate entry under the scene root collection.
                parent_cols = parent_obj.users_collection
                target_col = None
                for col in parent_cols:
                    if col != bpy.context.scene.collection:
                        target_col = col
                        break
                if target_col:
                    try:
                        target_col.objects.link(new)
                        bpy.context.scene.collection.objects.unlink(new)
                    except Exception:
                        LOG.warning("Failed to move %s to collection %s", new.name, target_col.name)
                print(f"[USD Connect] ensure_prim: parented '{new.name}' under '{parent_obj.name}'")

        print(f"[USD Connect] ensure_prim: linked {type_name} '{new.name}' for {prim_path}")
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

    def set_xform_trs(self, prim_path: str, payload: Dict) -> bool:
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
                if obj.rotation_mode == 'QUATERNION':
                    obj.rotation_quaternion = q
                else:
                    e = q.to_euler(obj.rotation_mode)
                    obj.rotation_euler = e

        if "s" in fields and "s" in payload:
            s = payload["s"]
            obj.scale = (float(s[0]), float(s[1]), float(s[2]))

        return True

    def set_xform_matrices(self, prim_path: str, payload: Dict) -> bool:
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
        except Exception:
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
        except Exception:
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
        LOG.info("BlenderAdapter: renamed prim path %s -> %s on object %s", prim_path, new_path, obj.name)
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
        except Exception:
            LOG.exception("Failed to set visibility for prim %s", prim_path)
            return False

    def set_gprim_attrs(self, prim_path: str, attrs: Dict) -> bool:
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

    def set_reference(self, prim_path: str, asset_path: str, prim_path_ref: str = "") -> bool:
        # Stub — future: import the referenced USD asset into Blender
        LOG.info("BlenderAdapter.set_reference: %s -> %s (prim_path=%s) [stub]",
                 prim_path, asset_path, prim_path_ref)
        return True
