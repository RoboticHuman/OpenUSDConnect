"""T2: Teapot — Variant switching with interleaved live editing.

Verifies:
1. Default variant (Utah) → Ceramic material with primvar Base Color
2. Edit Ceramic metallic via CLI → value applies
3. Switch to Fancy → PorcelainFlowers bound
4. Edit PorcelainFlowers roughness via CLI → value applies
5. Switch back to Utah → Ceramic rebound with metallic edit retained
6. Switch to Fancy again → PorcelainFlowers with roughness edit retained
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import bpy
from helpers import TestHarness

ASSET = "./assets/full_assets/Teapot/Teapot.usd"

harness = TestHarness("TEAPOT")

_step = 0
_retries = 0


def _find_geometry_material():
    """Return the material name assigned to the Geometry object."""
    for obj in bpy.data.objects:
        if "Geometry" not in obj.name:
            continue
        if not obj.data or not hasattr(obj.data, "materials"):
            continue
        for m in obj.data.materials:
            if m:
                return m.name
    return None


def _get_bsdf_value(mat_name_contains, input_name):
    """Read a Principled BSDF input value from a material."""
    for m in bpy.data.materials:
        if mat_name_contains not in m.name or not m.node_tree:
            continue
        for n in m.node_tree.nodes:
            if n.type == "BSDF_PRINCIPLED" and input_name in n.inputs:
                return n.inputs[input_name].default_value
    return None


def _send_shader_edit(prim_path, info_id, inputs, input_types):
    """Send a set_shader_input event via CLI."""
    harness._send([{
        "k": "set_connectable_input",
        "prim": prim_path,
        "info_id": info_id,
        "inputs": inputs,
        "input_types": input_types,
    }])


def _check_value(mat_name, input_name, expected, tolerance=0.05):
    """Assert a BSDF input is close to the expected value."""
    actual = _get_bsdf_value(mat_name, input_name)
    if actual is None:
        harness._fail(f"{mat_name}.{input_name}: not found")
        return
    if abs(actual - expected) < tolerance:
        harness._pass(f"{mat_name}.{input_name}={actual:.3f} (expected {expected})")
    else:
        harness._fail(f"{mat_name}.{input_name}={actual:.3f} != {expected}")


def _run():
    global _step, _retries

    try:
        if _step == 0:
            harness.setup()
            _step = 1
            return 2.0

        elif _step == 1:
            # Load teapot
            harness.send_payload("/World/Teapot", ASSET, "/Teapot")
            _step = 2
            return 8.0

        elif _step == 2:
            # --- Phase 1: Default variant, verify Ceramic ---
            harness.log("--- Phase 1: Default variant (Utah) ---")
            harness.check_binding("Geometry", "Ceramic")
            harness.check_connection("Ceramic", "Base Color", linked=True)
            _step = 3
            return 1.0

        elif _step == 3:
            # --- Phase 2: Edit Ceramic metallic to 0.8 ---
            harness.log("--- Phase 2: Edit Ceramic metallic=0.8 ---")
            _send_shader_edit(
                "/World/Teapot/Materials/Ceramic/UsdPreview/usdpreviewsurface",
                "UsdPreviewSurface",
                {"metallic": 0.8},
                {"metallic": "float"},
            )
            _step = 4
            return 3.0

        elif _step == 4:
            _check_value("Ceramic", "Metallic", 0.8)
            _step = 5
            return 1.0

        elif _step == 5:
            # --- Phase 3: Switch to Fancy ---
            harness.log("--- Phase 3: Switch to Fancy ---")
            harness.send_variant("/World/Teapot", {"modelVariant": "Fancy"})
            _step = 6
            _retries = 0
            return 5.0

        elif _step == 6:
            bound = _find_geometry_material()
            if bound and "Porcelain" in bound:
                harness._pass(f"Fancy: bound to {bound}")
                _step = 7
                return 1.0
            _retries += 1
            if _retries > 10:
                harness._fail(f"Fancy: still bound to {bound}")
                _step = 99
                return 1.0
            return 2.0

        elif _step == 7:
            # --- Phase 4: Edit PorcelainFlowers roughness to 0.3 ---
            harness.log("--- Phase 4: Edit PorcelainFlowers roughness=0.3 ---")
            # Find the PorcelainFlowers shader path
            _send_shader_edit(
                "/World/Teapot/Materials/PorcelainFlowers/UsdPreview/usdpreviewsurface",
                "UsdPreviewSurface",
                {"roughness": 0.3},
                {"roughness": "float"},
            )
            _step = 8
            return 3.0

        elif _step == 8:
            _check_value("PorcelainFlowers", "Roughness", 0.3)
            _step = 9
            return 1.0

        elif _step == 9:
            # --- Phase 5: Switch back to Utah ---
            harness.log("--- Phase 5: Switch back to Utah ---")
            harness.send_variant("/World/Teapot", {"modelVariant": "Utah"})
            _step = 10
            _retries = 0
            return 5.0

        elif _step == 10:
            bound = _find_geometry_material()
            if bound and "Ceramic" in bound:
                harness._pass(f"Utah: rebound to {bound}")
                # Verify metallic edit survived the variant round-trip
                harness.log("--- Phase 5b: Verify Ceramic metallic retained ---")
                _check_value("Ceramic", "Metallic", 0.8)
                _step = 11
                return 1.0
            _retries += 1
            if _retries > 10:
                harness._fail(f"Utah: still bound to {bound}")
                _step = 99
                return 1.0
            return 2.0

        elif _step == 11:
            # --- Phase 6: Switch to Fancy again ---
            harness.log("--- Phase 6: Switch to Fancy again ---")
            harness.send_variant("/World/Teapot", {"modelVariant": "Fancy"})
            _step = 12
            _retries = 0
            return 5.0

        elif _step == 12:
            bound = _find_geometry_material()
            if bound and "Porcelain" in bound:
                harness._pass(f"Fancy again: bound to {bound}")
                # Verify roughness edit survived
                harness.log("--- Phase 6b: Verify PorcelainFlowers roughness retained ---")
                _check_value("PorcelainFlowers", "Roughness", 0.3)
                _step = 99
                return 1.0
            _retries += 1
            if _retries > 10:
                harness._fail(f"Fancy again: still bound to {bound}")
                _step = 99
                return 1.0
            return 2.0

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
