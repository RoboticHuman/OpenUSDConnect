"""Blender role-flip test — Phase 3: Receive all events and verify no axis flip.

Imports the same Y-up USD scene, receives ALL events (Phase 1 + Phase 2),
and verifies final positions for both:
- /World/Cube (imported object) → should end at local (10, 11, 12)
- /World/NewCube (auto-tracked, received object) → should end at local (20, 21, 22)

The NewCube check is the critical axis-flip test: this object was created on
Instance A, received on Instance B, then moved by B as emitter. If the axis
flip bug is present, the local Y/Z values will be swapped or negated.

Run via: blender --background --python tests/blender_roleflip_verifier_script.py -- --port PORT --scene PATH --out RESULTS
"""

import sys
import os
import json
import time

import bpy

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)


def main():
    argv = sys.argv
    port = 7200
    scene_path = ""
    out_path = ""
    if "--" in argv:
        script_args = argv[argv.index("--") + 1:]
        for i, arg in enumerate(script_args):
            if arg == "--port" and i + 1 < len(script_args):
                port = int(script_args[i + 1])
            if arg == "--scene" and i + 1 < len(script_args):
                scene_path = script_args[i + 1]
            if arg == "--out" and i + 1 < len(script_args):
                out_path = script_args[i + 1]

    if not scene_path or not out_path:
        print("[RoleFlip Verifier] ERROR: --scene and --out required")
        sys.exit(1)

    import mathutils
    identity = mathutils.Matrix.Identity(4)

    # Clear and import
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete()

    from integrations.blender.capture import USD_CONNECT_Hook, _ensure_scene_props
    _ensure_scene_props()
    bpy.context.scene.usd_connect_import_skip_leaf_geom = False
    bpy.utils.register_class(USD_CONNECT_Hook)

    print(f"[RoleFlip Verifier] Importing {scene_path}")
    bpy.ops.wm.usd_import(filepath=scene_path)
    bpy.context.view_layer.update()

    # Receive ALL events (Phase 1 + Phase 2)
    from openusdconnect.receiver import ReceiverThread
    from integrations.blender.blender_adapter import BlenderAdapter

    receiver = ReceiverThread(host="127.0.0.1", port=port, sync_from=1)
    receiver.start()
    time.sleep(2.0)

    adapter = BlenderAdapter()
    lines = receiver.drain_queue()
    print(f"[RoleFlip Verifier] Received {len(lines)} messages")

    events_applied = 0
    for raw_line in lines:
        try:
            msg = json.loads(raw_line)
            if msg.get("type") == "event":
                ev = msg.get("event", {})
                k = ev.get("k")
                prim = ev.get("prim", "")
                print(f"[RoleFlip Verifier] Applying: {k} {prim} "
                      f"{ev.get('t', '')}")
                if k == "ensure_prim":
                    adapter.ensure_prim(prim, ev.get("typeName", "Xform"))
                elif k == "ensure_xform_ops":
                    adapter.ensure_xform_ops(prim)
                elif k == "set_xform_trs":
                    adapter.set_xform_trs(prim, ev)
                elif k == "set_visibility":
                    adapter.set_visibility(prim, ev.get("visible", True))
                events_applied += 1
        except Exception as e:
            print(f"[RoleFlip Verifier] Error: {e}")
            import traceback
            traceback.print_exc()

    receiver.stop()
    try:
        receiver.join(timeout=2.0)
    except Exception:
        pass

    bpy.context.view_layer.update()

    results = {}
    results["events_applied"] = f"PASS ({events_applied})" if events_applied > 0 else "FAIL: no events"

    def find_by_prim(prim_path):
        for obj in bpy.data.objects:
            if obj.get("usd_prim_path") == prim_path:
                return obj
        return None

    tol = 0.5

    # --- Check /World/Cube (imported object, moved by both phases) ---
    cube = find_by_prim("/World/Cube")
    if cube is None:
        results["cube_found"] = "FAIL: /World/Cube not found"
        results["cube_final_position"] = "SKIP"
        results["cube_no_axis_flip"] = "SKIP"
    else:
        results["cube_found"] = "PASS"
        loc = cube.location
        world = cube.matrix_world.translation
        print(f"[RoleFlip Verifier] Cube final local: {[round(v,3) for v in loc]}")
        print(f"[RoleFlip Verifier] Cube final world: {[round(v,3) for v in world]}")

        if abs(loc.x - 10.0) < tol and abs(loc.y - 11.0) < tol and abs(loc.z - 12.0) < tol:
            results["cube_final_position"] = "PASS"
        else:
            results["cube_final_position"] = (
                f"FAIL: loc={[round(v,3) for v in loc]}, expected ~(10,11,12)"
            )

        y_z_swapped = abs(loc.y - 12.0) < tol and abs(loc.z - 11.0) < tol
        any_negated = loc.y < -tol or loc.z < -tol
        if not y_z_swapped and not any_negated:
            results["cube_no_axis_flip"] = "PASS"
        else:
            results["cube_no_axis_flip"] = (
                f"FAIL: axis flip! loc={[round(v,3) for v in loc]}"
            )

    # --- Check /World/NewCube (auto-tracked, received object — THE KEY TEST) ---
    new_cube = find_by_prim("/World/NewCube")
    if new_cube is None:
        results["newcube_found"] = "FAIL: /World/NewCube not found"
        results["newcube_final_position"] = "SKIP"
        results["newcube_no_axis_flip"] = "SKIP"
        results["newcube_world_consistent"] = "SKIP"
    else:
        results["newcube_found"] = "PASS"
        loc = new_cube.location
        world = new_cube.matrix_world.translation
        print(f"[RoleFlip Verifier] NewCube final local: {[round(v,3) for v in loc]}")
        print(f"[RoleFlip Verifier] NewCube final world: {[round(v,3) for v in world]}")
        print(f"[RoleFlip Verifier] NewCube parent: "
              f"{new_cube.parent.name if new_cube.parent else None}")
        print(f"[RoleFlip Verifier] NewCube MPI identity: "
              f"{new_cube.matrix_parent_inverse == identity}")

        # Phase 2 set NewCube to local (20, 21, 22)
        if abs(loc.x - 20.0) < tol and abs(loc.y - 21.0) < tol and abs(loc.z - 22.0) < tol:
            results["newcube_final_position"] = "PASS"
        else:
            results["newcube_final_position"] = (
                f"FAIL: loc={[round(v,3) for v in loc]}, expected ~(20,21,22)"
            )

        # Axis-flip detection on the received object
        y_z_swapped = abs(loc.y - 22.0) < tol and abs(loc.z - 21.0) < tol
        any_negated = loc.y < -tol or loc.z < -tol
        if not y_z_swapped and not any_negated:
            results["newcube_no_axis_flip"] = "PASS"
        else:
            results["newcube_no_axis_flip"] = (
                f"FAIL: axis flip on received object! "
                f"loc={[round(v,3) for v in loc]}, "
                f"y_z_swapped={y_z_swapped}, negated={any_negated}"
            )

        # World position: local (20,21,22) under World's 90deg X → world (20,-22,21)
        wx, wy, wz = world
        if abs(wx - 20.0) < tol and abs(wy - (-22.0)) < tol and abs(wz - 21.0) < tol:
            results["newcube_world_consistent"] = "PASS"
        else:
            results["newcube_world_consistent"] = (
                f"FAIL: world={[round(v,3) for v in world]}, "
                f"expected ~(20,-22,21)"
            )

    # Write results
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    passed = sum(1 for v in results.values() if "PASS" in v)
    failed = sum(1 for v in results.values() if v.startswith("FAIL"))
    print(f"\n[RoleFlip Verifier] Results: {passed} passed, {failed} failed")
    for k, v in results.items():
        print(f"  {k}: {v}")

    bpy.utils.unregister_class(USD_CONNECT_Hook)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
