"""Blender role-flip test — Phase 2: Receive events, flip to emitter, move again.

1. Import the same Y-up USD scene (World gets 90deg X rotation)
2. Receive Phase 1 events — /World/Cube moved, /World/NewCube created
3. Verify positions match
4. Flip to emitter: move BOTH objects to new positions
5. Emit new transforms for Phase 3 to verify

The critical test: /World/NewCube was created on Instance A and received here.
When we flip to emitter and move it, the emitter must compute the correct local
transform relative to World's rotated coordinate space.

Run via: blender --background --python tests/blender_roleflip_receiver_script.py -- --port PORT --scene PATH --out RESULTS
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
        print("[RoleFlip B] ERROR: --scene and --out required")
        sys.exit(1)

    import mathutils
    identity = mathutils.Matrix.Identity(4)

    # Clear and import
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete()

    from integrations.blender.capture import (
        BlenderStageAuthor, NetworkSender, _depsgraph_handler, _ensure_scene_props,
        USD_CONNECT_Hook,
    )
    from openusdconnect.emitter import NoticeEmitter
    import integrations.blender.capture as capture_mod

    _ensure_scene_props()
    bpy.context.scene.usd_connect_import_skip_leaf_geom = False
    bpy.utils.register_class(USD_CONNECT_Hook)

    print(f"[RoleFlip B] Importing {scene_path}")
    bpy.ops.wm.usd_import(filepath=scene_path)
    bpy.context.view_layer.update()

    # --- Phase 2a: Receive events from Phase 1 ---
    from openusdconnect.receiver import ReceiverThread
    from integrations.blender.blender_adapter import BlenderAdapter

    receiver = ReceiverThread(host="127.0.0.1", port=port, sync_from=1)
    receiver.start()
    time.sleep(2.0)

    adapter = BlenderAdapter()
    lines = receiver.drain_queue()
    print(f"[RoleFlip B] Received {len(lines)} messages")

    events_applied = 0
    for raw_line in lines:
        try:
            msg = json.loads(raw_line)
            if msg.get("type") == "event":
                ev = msg.get("event", {})
                k = ev.get("k")
                prim = ev.get("prim", "")
                print(f"[RoleFlip B] Applying: {k} {prim} "
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
            print(f"[RoleFlip B] Error: {e}")
            import traceback
            traceback.print_exc()

    receiver.stop()
    try:
        receiver.join(timeout=2.0)
    except Exception:
        pass

    bpy.context.view_layer.update()

    results = {}
    results["events_received"] = f"PASS ({events_applied})" if events_applied > 0 else "FAIL: no events"

    # Find objects
    def find_by_prim(prim_path):
        for obj in bpy.data.objects:
            if obj.get("usd_prim_path") == prim_path:
                return obj
        return None

    cube = find_by_prim("/World/Cube")
    new_cube = find_by_prim("/World/NewCube")

    # Verify /World/Cube
    if cube is None:
        results["recv_cube_found"] = "FAIL: /World/Cube not found"
        results["recv_cube_position"] = "SKIP"
    else:
        results["recv_cube_found"] = "PASS"
        loc = cube.location
        tol = 0.5
        if abs(loc.x - 3.0) < tol and abs(loc.y - 5.0) < tol and abs(loc.z - 7.0) < tol:
            results["recv_cube_position"] = "PASS"
        else:
            results["recv_cube_position"] = f"FAIL: loc={[round(v,3) for v in loc]}, expected ~(3,5,7)"
        print(f"[RoleFlip B] Cube local: {[round(v,3) for v in loc]}, "
              f"world: {[round(v,3) for v in cube.matrix_world.translation]}")

    # Verify /World/NewCube — the object created on Instance A and received here
    if new_cube is None:
        results["recv_newcube_found"] = "FAIL: /World/NewCube not found"
        results["recv_newcube_position"] = "SKIP"
        results["recv_newcube_parented"] = "SKIP"
    else:
        results["recv_newcube_found"] = "PASS"
        results["recv_newcube_parented"] = (
            "PASS" if new_cube.parent is not None
            else "FAIL: NewCube has no parent"
        )
        loc = new_cube.location
        tol = 0.5
        if abs(loc.x - 6.0) < tol and abs(loc.y - 8.0) < tol and abs(loc.z - 10.0) < tol:
            results["recv_newcube_position"] = "PASS"
        else:
            results["recv_newcube_position"] = f"FAIL: loc={[round(v,3) for v in loc]}, expected ~(6,8,10)"
        print(f"[RoleFlip B] NewCube local: {[round(v,3) for v in loc]}, "
              f"world: {[round(v,3) for v in new_cube.matrix_world.translation]}, "
              f"MPI_identity: {new_cube.matrix_parent_inverse == identity}")

    # Debug: all objects
    for obj in bpy.data.objects:
        prim = obj.get("usd_prim_path", "")
        if prim:
            print(f"[RoleFlip B] Post-recv {obj.name}: prim={prim}, "
                  f"parent={obj.parent.name if obj.parent else None}, "
                  f"MPI_id={obj.matrix_parent_inverse == identity}")

    # --- Phase 2b: Flip to emitter and move both objects ---
    print("[RoleFlip B] Flipping to emitter role")
    author = BlenderStageAuthor(base_usd_path=scene_path)
    author.enabled = True
    author.auto_track = False
    author.seed_used_paths()

    emitter_notice = NoticeEmitter(author.stage)
    sender = NetworkSender(host="127.0.0.1", port=port)
    sender.connect()

    capture_mod._state.author = author
    capture_mod._state.notice_emitter = emitter_notice
    capture_mod._state.sender = sender
    bpy.app.handlers.depsgraph_update_post.append(_depsgraph_handler)

    # Move existing Cube to new position
    if cube is not None:
        cube.location = (10.0, 11.0, 12.0)
        bpy.context.view_layer.update()
        print(f"[RoleFlip B] Cube moved to local (10,11,12), "
              f"world={[round(v,3) for v in cube.matrix_world.translation]}")
        dg = bpy.context.evaluated_depsgraph_get()
        _depsgraph_handler(bpy.context.scene, dg)

    # Move the RECEIVED NewCube — this is the critical axis-flip scenario
    if new_cube is not None:
        new_cube.location = (20.0, 21.0, 22.0)
        bpy.context.view_layer.update()
        print(f"[RoleFlip B] NewCube moved to local (20,21,22), "
              f"world={[round(v,3) for v in new_cube.matrix_world.translation]}")
        dg = bpy.context.evaluated_depsgraph_get()
        _depsgraph_handler(bpy.context.scene, dg)

    time.sleep(0.5)

    sender.disconnect()
    capture_mod._state.author = None
    capture_mod._state.notice_emitter = None
    capture_mod._state.sender = None
    bpy.app.handlers.depsgraph_update_post.remove(_depsgraph_handler)
    bpy.utils.unregister_class(USD_CONNECT_Hook)

    # Write results
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    passed = sum(1 for v in results.values() if "PASS" in v)
    failed = sum(1 for v in results.values() if v.startswith("FAIL"))
    print(f"\n[RoleFlip B] Results: {passed} passed, {failed} failed")
    for k, v in results.items():
        print(f"  {k}: {v}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
