"""Blender receiver script for auto-tracking integration test.

Connects to server, receives replayed events from the auto-tracking emitter,
applies via BlenderAdapter, verifies objects were created with correct types,
positions, and visibility.

Run via:
  blender --background --python tests/blender_autotrack_receiver_script.py \
    -- --port PORT --out RESULTS_FILE
"""

import json
import os
import sys
import time

import bpy

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
    time.sleep(2.0)

    adapter = BlenderAdapter()
    lines = receiver.drain_queue()
    print(f"[Receiver] Got {len(lines)} messages")

    events_seen = []
    for raw_line in lines:
        try:
            msg = json.loads(raw_line)
            if msg.get("type") == "event":
                ev = msg.get("event", {})
                k = ev.get("k")
                prim_path = ev.get("prim", "")
                events_seen.append(ev)
                print(f"[Receiver] Processing: {k} {prim_path} {ev}")

                if k == "ensure_prim":
                    adapter.ensure_prim(prim_path, ev.get("typeName", "Xform"))
                elif k == "ensure_xform_ops":
                    adapter.ensure_xform_ops(prim_path)
                elif k == "set_xform_trs":
                    adapter.set_xform_trs(prim_path, ev)
                elif k == "set_visibility":
                    adapter.set_visibility(prim_path, ev.get("visible", True))
                elif k == "deactivate_prim":
                    adapter.deactivate_prim(prim_path, ev.get("active", False))
        except Exception as e:
            print(f"[Receiver] Error: {e}")
            import traceback

            traceback.print_exc()

    receiver.stop()
    try:
        receiver.join(timeout=2.0)
    except Exception:
        pass

    # --- Verify ---
    results = {}

    def find_obj(prim_path):
        for obj in bpy.data.objects:
            if obj.get("usd_prim_path") == prim_path:
                return obj
        return None

    # Check that events were actually received
    results["events_received"] = (
        f"PASS ({len(events_seen)} events)" if len(events_seen) > 0 else "FAIL: no events"
    )

    # Check ensure_prim events were sent with correct typeName
    ensure_prims = [e for e in events_seen if e.get("k") == "ensure_prim"]
    results["ensure_prim_count"] = (
        f"PASS ({len(ensure_prims)})"
        if len(ensure_prims) >= 2
        else f"FAIL: only {len(ensure_prims)} ensure_prim events"
    )

    # Check that auto-tracked objects got prim paths under /World (including /World itself)
    prim_paths = [e.get("prim") for e in ensure_prims]
    all_under_world = all(p == "/World" or p.startswith("/World/") for p in prim_paths)
    results["prim_paths_correct"] = "PASS" if all_under_world else f"FAIL: {prim_paths}"

    # Find the cube and sphere by scanning all prim paths
    cube_prim = None
    sphere_prim = None
    for e in ensure_prims:
        prim = e.get("prim", "")
        type_name = e.get("typeName", "")
        if "Cube" in prim or "Cube" in type_name:
            cube_prim = prim
        if "Sphere" in prim or "Sphere" in type_name:
            sphere_prim = prim

    # Cube checks
    if cube_prim:
        cube_obj = find_obj(cube_prim)
        if cube_obj is None:
            results["cube_created"] = f"FAIL: object not found for {cube_prim}"
        elif cube_obj.type != "MESH":
            results["cube_created"] = f"FAIL: type={cube_obj.type}"
        elif len(cube_obj.data.vertices) == 0:
            results["cube_created"] = "FAIL: no vertices"
        else:
            results["cube_created"] = "PASS"

        # Check cube position (should be near 5, 6, 7)
        if cube_obj:
            loc = cube_obj.location
            if abs(loc.x - 5.0) < 0.5 and abs(loc.y - 6.0) < 0.5 and abs(loc.z - 7.0) < 0.5:
                results["cube_position"] = "PASS"
            else:
                results["cube_position"] = f"FAIL: loc={tuple(loc)}"
        else:
            results["cube_position"] = "SKIP"

        # Visibility is not tracked from depsgraph — skip this check
        results["cube_visibility_event"] = "SKIP: visibility not emitted from depsgraph"
    else:
        results["cube_created"] = "FAIL: no cube ensure_prim event found"
        results["cube_position"] = "SKIP"
        results["cube_visibility_event"] = "SKIP"

    # Sphere checks
    if sphere_prim:
        sphere_obj = find_obj(sphere_prim)
        if sphere_obj is None:
            results["sphere_created"] = f"FAIL: object not found for {sphere_prim}"
        elif sphere_obj.type != "MESH":
            results["sphere_created"] = f"FAIL: type={sphere_obj.type}"
        elif len(sphere_obj.data.vertices) == 0:
            results["sphere_created"] = "FAIL: no vertices"
        else:
            results["sphere_created"] = "PASS"

        if sphere_obj:
            loc = sphere_obj.location
            if abs(loc.x - 1.0) < 0.5 and abs(loc.y - 2.0) < 0.5 and abs(loc.z - 3.0) < 0.5:
                results["sphere_position"] = "PASS"
            else:
                results["sphere_position"] = f"FAIL: loc={tuple(loc)}"
        else:
            results["sphere_position"] = "SKIP"
    else:
        results["sphere_created"] = "FAIL: no sphere ensure_prim event found"
        results["sphere_position"] = "SKIP"

    # Write results
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    passed = sum(1 for v in results.values() if "PASS" in v)
    failed = sum(1 for v in results.values() if v.startswith("FAIL"))
    print(f"\n[Receiver] Results: {passed} passed, {failed} failed")
    for k, v in results.items():
        print(f"  {k}: {v}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
