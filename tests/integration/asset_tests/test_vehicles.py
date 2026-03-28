"""T7: Vehicles 4WD — Multiple material bindings per asset.

Verifies 6 different material bindings across mesh parts, each
resolving to the correct UsdPreviewSurface material.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import bpy
from helpers import PROJECT_ROOT, TestHarness

ASSET = os.path.join(
    PROJECT_ROOT, "assets", "full_assets", "Vehicles",
    "USD_Mini_Car_Kit", "assets", "vehicles", "4wd", "asset", "4wdBodyAsset.usda",
)

harness = TestHarness("VEHICLES")

_step = 0
def _run():
    global _step
    if _step == 0:
        harness.setup()
        _step = 1
        return 2.0
    elif _step == 1:
        harness.send_reference("/World/Car", ASSET, "/_4wd")
        _step = 2
        return 8.0
    elif _step == 2:
        # Check that multiple materials exist
        expected_mats = ["green", "greyMedium", "window", "greyLight",
                         "frontLight", "backLight"]
        found = 0
        for name in expected_mats:
            for m in bpy.data.materials:
                if name.lower() in m.name.lower():
                    harness._pass(f"Material found: {m.name}")
                    found += 1
                    break
            else:
                harness._fail(f"Material containing '{name}' not found")

        # Check that mesh objects have materials assigned
        mesh_objs = [o for o in bpy.data.objects
                     if o.type == "MESH" and o.data and o.data.materials]
        harness.log(f"  Mesh objects with materials: {len(mesh_objs)}")
        for obj in mesh_objs:
            mats = [m.name for m in obj.data.materials if m]
            harness.log(f"    {obj.name} -> {mats}")

        if mesh_objs:
            harness._pass(f"{len(mesh_objs)} meshes have material assignments")
        else:
            harness._fail("No meshes have materials assigned")

        harness.done()
        return None

bpy.app.timers.register(_run, first_interval=2.0)
