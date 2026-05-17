"""T5: Camera scene — UsdGeomCamera replication into Blender.

Verifies:
1. Reference to teapotScene_camera.usd brings /Cameras/mainCamera/mono in as
   a Blender CAMERA object
2. The camera's typed attrs (focalLength, clippingRange, projection) land on
   bpy.types.Camera with the unit conversion that matches Blender's own USD
   importer for a metersPerUnit=1.0 stage
3. Setting it as the active scene camera is a one-line bpy call (the
   ``active camera`` choice is local Blender state, not USD scene data)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import bpy
from helpers import TestHarness

ASSET = "./assets/intent-vfx/scenes/teapotScene_camera.usd"
SCENE_ROOT = "/World/Scene"

harness = TestHarness("CAMERA")

_step = 0
_retries = 0


def _find_imported_camera():
    """Return the CAMERA object brought in under SCENE_ROOT by the import.

    Filters out Blender's default scene camera, which is not tagged with a
    ``usd_prim_path`` and so can never satisfy the prefix check.
    """
    for obj in bpy.data.objects:
        if obj.type != "CAMERA":
            continue
        pp = obj.get("usd_prim_path", "")
        if pp.startswith(SCENE_ROOT + "/") or pp == SCENE_ROOT:
            return obj
    return None


def _check_camera_field(cam_obj, field, expected, tolerance=1e-3):
    actual = getattr(cam_obj.data, field, None)
    if actual is None:
        harness._fail(f"camera.{field} not found on {cam_obj.name}")
        return
    if isinstance(expected, str):
        if actual != expected:
            harness._fail(f"camera.{field}={actual!r}, expected {expected!r}")
        else:
            harness._pass(f"camera.{field}={actual!r}")
        return
    if abs(float(actual) - expected) > tolerance:
        harness._fail(f"camera.{field}={actual}, expected {expected}")
    else:
        harness._pass(f"camera.{field}={actual}")


def _run():
    global _step, _retries

    try:
        if _step == 0:
            harness.setup()
            _step = 1
            return 2.0

        elif _step == 1:
            harness.send_reference("/World/Scene", ASSET)
            _step = 2
            _retries = 0
            return 5.0

        elif _step == 2:
            cam_obj = _find_imported_camera()
            if cam_obj is None:
                _retries += 1
                if _retries > 10:
                    objs = [
                        (o.name, o.type, o.get("usd_prim_path", ""))
                        for o in bpy.data.objects
                    ]
                    harness._fail(
                        f"No CAMERA object tagged under {SCENE_ROOT} appeared "
                        f"(objects: {objs})"
                    )
                    _step = 99
                    return 1.0
                return 2.0
            harness._pass(
                f"Camera arrived: {cam_obj.name} (type={cam_obj.type}, "
                f"usd_prim_path={cam_obj.get('usd_prim_path')})"
            )
            _step = 3
            return 1.0

        elif _step == 3:
            # Stage uses metersPerUnit=1.0 (Blender default scene scale_length).
            # scene_scale = 1.0, tenth_unit_to_mm = 100.
            #   focalLength=32  → lens     = 32 * 100   = 3200 mm
            #   clippingRange   → clip_*   = value * 1   = pass-through
            #   projection      → camera.type
            # Matches what Blender's own File→Import→USD does for this asset.
            cam_obj = _find_imported_camera()
            _check_camera_field(cam_obj, "lens", 3200.0, tolerance=0.5)
            _check_camera_field(cam_obj, "clip_start", 0.1, tolerance=1e-3)
            _check_camera_field(cam_obj, "clip_end", 100000.0, tolerance=1.0)
            _check_camera_field(cam_obj, "type", "PERSP")
            _step = 4
            return 1.0

        elif _step == 4:
            # Two separate things in Blender:
            #   1. scene.camera     — which camera renders (F12) and is
            #                         "the active scene camera"
            #   2. view_perspective — what the 3D viewport is looking through
            # Setting #1 alone leaves the viewport on its previous perspective.
            # To actually look through the camera we also flip every VIEW_3D
            # space to CAMERA — the equivalent of Numpad 0.
            cam_obj = _find_imported_camera()
            bpy.context.scene.camera = cam_obj
            if bpy.context.scene.camera is not cam_obj:
                harness._fail("Failed to set active scene camera")
                _step = 99
                return 1.0
            harness._pass(f"Active scene camera set to {cam_obj.name}")

            switched = 0
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type != "VIEW_3D":
                        continue
                    for space in area.spaces:
                        if space.type == "VIEW_3D":
                            space.region_3d.view_perspective = "CAMERA"
                            switched += 1
            if switched == 0:
                harness._fail("No VIEW_3D area found to switch to camera view")
            else:
                harness._pass(f"Viewport(s) switched to camera view ({switched})")
            _step = 99
            return 1.0

        elif _step == 99:
            harness.done()
            return None

    except Exception as e:
        import traceback
        harness.log(f"ERROR in step {_step}: {e}")
        traceback.print_exc()
        harness._fail(str(e))
        harness.done()
        return None


bpy.app.timers.register(_run, first_interval=2.0)
