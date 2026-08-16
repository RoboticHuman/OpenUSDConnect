"""T3: Two Teapots Material identity separation.

Verifies two references to the same asset get separate materials,
each with intact node trees and Base Color connections.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import bpy
from helpers import TestHarness

ASSET = "./assets/full_assets/Teapot/Teapot.usd"

harness = TestHarness("2-TEAPOTS")

_step = 0
def _run():
    global _step
    if _step == 0:
        harness.setup()
        _step = 1
        return 2.0
    elif _step == 1:
        harness.send_payload("/World/Teapot", ASSET, "/Teapot")
        _step = 2
        return 5.0
    elif _step == 2:
        harness.send_payload("/Teapot", ASSET, "/Teapot")
        _step = 3
        return 5.0
    elif _step == 3:
        # Both should have separate Ceramic materials
        ceramics = [m for m in bpy.data.materials if "Ceramic" in m.name]
        paths = [m.get("usd_material_path", "") for m in ceramics]
        harness.log(f"  Ceramic materials: {[m.name for m in ceramics]}")
        harness.log(f"  Paths: {paths}")

        if len(ceramics) < 2:
            harness._fail(f"Expected 2+ Ceramic materials, got {len(ceramics)}")
        else:
            harness._pass(f"{len(ceramics)} separate Ceramic materials")

        # Both should have Base Color linked
        for m in ceramics:
            harness.check_connection(m.name, "Base Color", linked=True)

        # Different usd_material_path tags
        unique_paths = {m.get("usd_material_path", "") for m in ceramics} - {""}
        if len(unique_paths) >= 2:
            harness._pass(f"Paths are unique: {unique_paths}")
        else:
            harness._fail(f"Paths not unique: {paths}")

        # Object naming includes parent context
        geom_objs = [o.name for o in bpy.data.objects if "Geometry" in o.name]
        harness.log(f"  Geometry objects: {geom_objs}")
        bare = [n for n in geom_objs if n == "Geometry"]
        if bare:
            harness._fail("Bare 'Geometry' name found (no parent context)")
        else:
            harness._pass("Object names include parent context")

        harness.done()
        return None

bpy.app.timers.register(_run, first_interval=2.0)
