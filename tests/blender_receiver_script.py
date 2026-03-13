"""Blender receiver script for integration test.

Connects to the sync server, receives replayed events, applies them via
BlenderAdapter, verifies objects were created correctly, writes results to file.
Run via:
  blender --background --python tests/blender_receiver_script.py \
    -- --port PORT --out RESULTS_FILE
"""

import json
import os
import sys
import time

import bpy

# Add project root to sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from integrations.blender.blender_adapter import BlenderAdapter
from openusdconnect.receiver import ReceiverThread


def main():
    argv = sys.argv
    port = 7200
    out_path = ""
    if "--" in argv:
        script_args = argv[argv.index("--") + 1 :]
        for i, arg in enumerate(script_args):
            if arg == "--port" and i + 1 < len(script_args):
                port = int(script_args[i + 1])
            if arg == "--out" and i + 1 < len(script_args):
                out_path = script_args[i + 1]

    if not out_path:
        print("[Receiver] ERROR: --out required")
        sys.exit(1)

    print(f"[Receiver] Connecting to 127.0.0.1:{port}")
    receiver = ReceiverThread(host="127.0.0.1", port=port, sync_from=1)
    receiver.start()

    # Wait for events to arrive via replay
    time.sleep(2.0)

    # Drain queue and process events
    adapter = BlenderAdapter()
    lines = receiver.drain_queue()
    print(f"[Receiver] Got {len(lines)} messages from queue")

    for raw_line in lines:
        try:
            msg = json.loads(raw_line)
            if msg.get("type") == "event":
                ev = msg.get("event", {})
                k = ev.get("k")
                prim_path = ev.get("prim", "")
                print(f"[Receiver] Processing: {k} {prim_path}")

                if k == "ensure_prim":
                    adapter.ensure_prim(prim_path, ev.get("typeName", "Xform"))
                elif k == "ensure_xform_ops":
                    adapter.ensure_xform_ops(prim_path)
                elif k == "set_xform_trs":
                    adapter.set_xform_trs(prim_path, ev)
                elif k == "set_visibility":
                    adapter.set_visibility(prim_path, ev.get("visible", True))
        except Exception as e:
            print(f"[Receiver] Error processing: {e}")

    receiver.stop()
    try:
        receiver.join(timeout=2.0)
    except Exception:
        pass

    # --- Verify results ---
    results = {}

    def find_obj(prim_path):
        for obj in bpy.data.objects:
            if obj.get("usd_prim_path") == prim_path:
                return obj
        return None

    # Test 1: Sphere exists, is MESH with vertices, correct position and scale
    sphere = find_obj("/World/TestSphere")
    if sphere is None:
        results["sphere_exists"] = "FAIL: not found"
    elif sphere.type != "MESH":
        results["sphere_exists"] = f"FAIL: type={sphere.type}"
    elif len(sphere.data.vertices) == 0:
        results["sphere_exists"] = "FAIL: no vertices"
    else:
        results["sphere_exists"] = "PASS"

    if sphere:
        loc = sphere.location
        if abs(loc.x - 3.0) < 0.1 and abs(loc.y - 4.0) < 0.1 and abs(loc.z - 5.0) < 0.1:
            results["sphere_location"] = "PASS"
        else:
            results["sphere_location"] = f"FAIL: loc={tuple(loc)}"

        scl = sphere.scale
        if abs(scl.x - 2.0) < 0.1 and abs(scl.y - 2.0) < 0.1 and abs(scl.z - 2.0) < 0.1:
            results["sphere_scale"] = "PASS"
        else:
            results["sphere_scale"] = f"FAIL: scale={tuple(scl)}"
    else:
        results["sphere_location"] = "SKIP"
        results["sphere_scale"] = "SKIP"

    # Test 2: Cube exists, is MESH, correct position, hidden
    cube = find_obj("/World/TestCube")
    if cube is None:
        results["cube_exists"] = "FAIL: not found"
    elif cube.type != "MESH":
        results["cube_exists"] = f"FAIL: type={cube.type}"
    elif len(cube.data.vertices) == 0:
        results["cube_exists"] = "FAIL: no vertices"
    else:
        results["cube_exists"] = "PASS"

    if cube:
        loc = cube.location
        if abs(loc.x - 10.0) < 0.1:
            results["cube_location"] = "PASS"
        else:
            results["cube_location"] = f"FAIL: loc={tuple(loc)}"

        if cube.hide_viewport:
            results["cube_hidden"] = "PASS"
        else:
            results["cube_hidden"] = "FAIL: hide_viewport=False"
    else:
        results["cube_location"] = "SKIP"
        results["cube_hidden"] = "SKIP"

    # Test 3: Cone exists, is MESH
    cone = find_obj("/World/TestCone")
    if cone is None:
        results["cone_exists"] = "FAIL: not found"
    elif cone.type != "MESH":
        results["cone_exists"] = f"FAIL: type={cone.type}"
    elif len(cone.data.vertices) == 0:
        results["cone_exists"] = "FAIL: no vertices"
    else:
        results["cone_exists"] = "PASS"

    # Test 4: Xform exists, is EMPTY
    xform = find_obj("/World/TestXform")
    if xform is None:
        results["xform_exists"] = "FAIL: not found"
    elif xform.type != "EMPTY":
        results["xform_exists"] = f"FAIL: type={xform.type}"
    else:
        results["xform_exists"] = "PASS"

    # Write results
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    passed = sum(1 for v in results.values() if v == "PASS")
    failed = sum(1 for v in results.values() if v.startswith("FAIL"))
    print(f"\n[Receiver] Results: {passed} passed, {failed} failed")
    for k, v in results.items():
        print(f"  {k}: {v}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
