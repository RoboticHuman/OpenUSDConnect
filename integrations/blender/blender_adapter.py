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
except Exception:
    BPY_AVAILABLE = False

from openusdconnect.adapters import DCCAdapter

LOG = logging.getLogger(__name__)


class BlenderAdapter(DCCAdapter):
    """Applies incoming events to Blender scene objects.

    Finds objects by usd_prim_path custom property.
    Rotation payload is quaternion [w,x,y,z]; converts to object's rotation_mode.
    """

    def _find_object_by_prim(self, prim_path: str) -> Optional[object]:
        if not BPY_AVAILABLE:
            return None
        for obj in bpy.data.objects:
            if obj.get("usd_prim_path") == prim_path:
                return obj
        return None

    def ensure_prim(self, prim_path: str, type_name: str = "Xform") -> bool:
        if not BPY_AVAILABLE:
            LOG.info("BlenderAdapter.ensure_prim dry: %s", prim_path)
            return True
        obj = self._find_object_by_prim(prim_path)
        if obj:
            return True
        # Create placeholder Empty
        name = prim_path.strip("/").replace("/", "_") or prim_path
        new = bpy.data.objects.new(name, None)
        new["usd_prim_path"] = prim_path
        try:
            col = bpy.context.collection
            col.objects.link(new)
        except Exception:
            try:
                bpy.context.scene.collection.objects.link(new)
            except Exception:
                LOG.exception("Failed to link object for prim %s", prim_path)
        LOG.info("BlenderAdapter: created placeholder %s for %s", name, prim_path)
        return True

    def ensure_xform_ops(self, prim_path: str) -> bool:
        # Blender objects have implicit TRS
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

        # Refresh viewport
        try:
            if bpy.context.view_layer:
                bpy.context.view_layer.update()
        except Exception:
            pass

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
            for col in obj.users_collection:
                col.objects.unlink(obj)
            bpy.data.objects.remove(obj)
            return True
        except Exception:
            LOG.exception("Failed to delete object for prim %s", prim_path)
            return False
