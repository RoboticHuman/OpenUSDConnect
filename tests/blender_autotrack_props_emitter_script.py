"""Blender auto-tracking emitter that verifies deferred custom properties.

Tests that _deferred_set_props correctly sets usd_prim_path and usd_type_name
on auto-tracked objects via bpy.app.timers (by-name lookup, not stale refs).

Since Blender --background mode doesn't run an event loop, we capture the
deferred timer callbacks and execute them manually. This still tests the real
_deferred_set_props code path: timer registration + by-name object lookup.

Run via:
  blender --background --python tests/blender_autotrack_props_emitter_script.py \
    -- --port PORT --out RESULTS_FILE
"""

import sys
import os
import json
import time

import bpy

# Add project root to sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)


def main():
    argv = sys.argv
    port = 7200
    out_path = ""
    if "--" in argv:
        script_args = argv[argv.index("--") + 1:]
        for i, arg in enumerate(script_args):
            if arg == "--port" and i + 1 < len(script_args):
                port = int(script_args[i + 1])
            if arg == "--out" and i + 1 < len(script_args):
                out_path = script_args[i + 1]

    if not out_path:
        print("[Emitter] ERROR: --out required")
        sys.exit(1)

    # Capture deferred timer callbacks so we can execute them manually
    # (Blender --background mode doesn't process timers automatically)
    _captured_timers = []
    _original_register = bpy.app.timers.register

    def _capturing_register(fn, **kw):
        _captured_timers.append(fn)
        return None  # don't actually register (no event loop to fire them)

    bpy.app.timers.register = _capturing_register

    # Clear default scene
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete()

    from integrations.blender.capture import (
        _NetworkEmitter, _depsgraph_handler, _ensure_scene_props,
    )
    import integrations.blender.capture as capture_mod

    _ensure_scene_props()

    emitter = _NetworkEmitter(
        host="127.0.0.1",
        port=port,
        client_id="autotrack-props-test",
        auto_track=True,
    )
    emitter.connect()
    capture_mod._NET_EMITTER = emitter

    bpy.app.handlers.depsgraph_update_post.append(_depsgraph_handler)
    bpy.context.scene.usd_connect_auto_track = True

    # Create World root empty
    bpy.ops.object.empty_add(type='PLAIN_AXES')
    world = bpy.context.active_object
    world.name = "World"
    world["usd_prim_path"] = "/World"
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    _depsgraph_handler(bpy.context.scene, dg)

    # Create a Cube parented under World
    bpy.ops.mesh.primitive_cube_add(location=(5.0, 6.0, 7.0))
    cube = bpy.context.active_object
    cube.name = "AutoCube"
    cube.parent = world
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    _depsgraph_handler(bpy.context.scene, dg)

    # Create a Sphere parented under World
    bpy.ops.mesh.primitive_uv_sphere_add(location=(1.0, 2.0, 3.0))
    sphere = bpy.context.active_object
    sphere.name = "AutoSphere"
    sphere.parent = world
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    _depsgraph_handler(bpy.context.scene, dg)

    # --- Before timers fire: custom props should NOT be set yet ---
    results = {}

    cube_before = bpy.data.objects.get("AutoCube")
    sphere_before = bpy.data.objects.get("AutoSphere")

    results["cube_no_props_before_timer"] = (
        "PASS" if cube_before and "usd_prim_path" not in cube_before
        else "FAIL: custom props already set before timer fired"
    )
    results["sphere_no_props_before_timer"] = (
        "PASS" if sphere_before and "usd_prim_path" not in sphere_before
        else "FAIL: custom props already set before timer fired"
    )

    # --- Execute captured deferred timers (simulates Blender main loop tick) ---
    timers_fired = len(_captured_timers)
    for fn in _captured_timers:
        fn()
    _captured_timers.clear()
    bpy.app.timers.register = _original_register

    results["deferred_timers_fired"] = (
        f"PASS ({timers_fired} timers)"
        if timers_fired >= 2
        else f"FAIL: expected >= 2 timers, got {timers_fired}"
    )

    # --- After timers fire: custom props SHOULD be set ---
    cube_after = bpy.data.objects.get("AutoCube")
    sphere_after = bpy.data.objects.get("AutoSphere")

    # Cube custom properties
    if cube_after is None:
        results["cube_prim_path"] = "FAIL: AutoCube not found"
        results["cube_type_name"] = "FAIL: AutoCube not found"
    else:
        prim = cube_after.get("usd_prim_path", "")
        if prim and prim.startswith("/World/"):
            results["cube_prim_path"] = f"PASS ({prim})"
        else:
            results["cube_prim_path"] = f"FAIL: usd_prim_path={prim!r}"

        type_name = cube_after.get("usd_type_name", "")
        if type_name == "Cube":
            results["cube_type_name"] = "PASS (Cube)"
        else:
            results["cube_type_name"] = f"FAIL: usd_type_name={type_name!r}"

    # Sphere custom properties
    if sphere_after is None:
        results["sphere_prim_path"] = "FAIL: AutoSphere not found"
        results["sphere_type_name"] = "FAIL: AutoSphere not found"
    else:
        prim = sphere_after.get("usd_prim_path", "")
        if prim and prim.startswith("/World/"):
            results["sphere_prim_path"] = f"PASS ({prim})"
        else:
            results["sphere_prim_path"] = f"FAIL: usd_prim_path={prim!r}"

        type_name = sphere_after.get("usd_type_name", "")
        if type_name == "Sphere":
            results["sphere_type_name"] = "PASS (Sphere)"
        else:
            results["sphere_type_name"] = f"FAIL: usd_type_name={type_name!r}"

    # Small delay to ensure events are sent over TCP
    time.sleep(0.5)

    emitter.disconnect()
    capture_mod._NET_EMITTER = None
    bpy.app.handlers.depsgraph_update_post.remove(_depsgraph_handler)

    # Write results
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    passed = sum(1 for v in results.values() if "PASS" in v)
    failed = sum(1 for v in results.values() if v.startswith("FAIL"))
    print(f"\n[Emitter] Results: {passed} passed, {failed} failed")
    for k, v in results.items():
        print(f"  {k}: {v}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
