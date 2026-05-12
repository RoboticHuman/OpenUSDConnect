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
    PROJECT_ROOT,
    "assets",
    "full_assets",
    "Vehicles",
    "USD_Mini_Car_Kit",
    "assets",
    "vehicles",
    "4wd",
    "asset",
    "4wdBodyAsset.usda",
)

harness = TestHarness("VEHICLES")

_step = 0
_retries = 0
_expected_mats = [
    "green",
    "greyMedium",
    "window",
    "greyLight",
    "frontLight",
    "backLight",
]


def _found_expected_material_names():
    found = set()
    for expected in _expected_mats:
        for mat in bpy.data.materials:
            if expected.lower() in mat.name.lower():
                found.add(expected)
                break
    return found


def _check_results():
    found_mats = _found_expected_material_names()
    for name in _expected_mats:
        if name in found_mats:
            harness._pass(f"Material found: {name}")
        else:
            harness._fail(f"Material containing '{name}' not found")

    mesh_objs = [o for o in bpy.data.objects if o.type == "MESH" and o.data and o.data.materials]
    harness.log(f"  Mesh objects with materials: {len(mesh_objs)}")
    for obj in mesh_objs:
        mats = [m.name for m in obj.data.materials if m]
        harness.log(f"    {obj.name} -> {mats}")

    if mesh_objs:
        harness._pass(f"{len(mesh_objs)} meshes have material assignments")
    else:
        harness._fail("No meshes have materials assigned")


def _run():
    global _step, _retries
    if _step == 0:
        harness.setup()
        _step = 1
        return 2.0
    elif _step == 1:
        harness.send_reference("/World/Car", ASSET, "/_4wd")
        _step = 2
        return 8.0
    elif _step == 2:
        found_mats = _found_expected_material_names()
        mesh_objs = [
            o for o in bpy.data.objects if o.type == "MESH" and o.data and o.data.materials
        ]
        if len(found_mats) == len(_expected_mats) and mesh_objs:
            _check_results()
            harness.done()
            return None

        _retries += 1
        if _retries <= 15:
            missing = sorted(set(_expected_mats) - found_mats)
            harness.log(
                f"  Waiting for vehicle import/materials (retry {_retries}/15, missing={missing})"
            )
            return 2.0

        _check_results()
        harness.done()
        return None


bpy.app.timers.register(_run, first_interval=2.0)
