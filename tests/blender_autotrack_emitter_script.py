"""Blender auto-tracking emitter script for integration test.

Uses BlenderStageAuthor + NoticeEmitter + NetworkSender with auto_track=True,
creates objects via bpy.ops, triggers depsgraph, lets the handler send events.

Run via: blender --background --python tests/blender_autotrack_emitter_script.py -- --port PORT
"""

import os
import sys
import time

import bpy

# Add project root to sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)


def main():
    argv = sys.argv
    port = 7200
    if "--" in argv:
        script_args = argv[argv.index("--") + 1 :]
        for i, arg in enumerate(script_args):
            if arg == "--port" and i + 1 < len(script_args):
                port = int(script_args[i + 1])

    # Clear default scene
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()

    import integrations.blender.capture as capture_mod
    from integrations.blender.capture import (
        BlenderStageAuthor,
        NetworkSender,
        _depsgraph_handler,
        _ensure_scene_props,
    )
    from openusdconnect.emitter import NoticeEmitter

    # Ensure scene properties are registered (auto_track, etc.)
    _ensure_scene_props()

    # Write a minimal temp USD file for the stage author
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".usda", delete=False, mode="w")
    tmp.write('#usda 1.0\ndef Xform "World" {}\n')
    tmp.close()

    author = BlenderStageAuthor(base_usd_path=tmp.name)
    author.enabled = True
    author.auto_track = True
    author.seed_used_paths()

    emitter_notice = NoticeEmitter(author.stage)
    sender = NetworkSender(host="127.0.0.1", port=port)
    sender.connect()

    capture_mod._state.author = author
    capture_mod._state.notice_emitter = emitter_notice
    capture_mod._state.sender = sender

    # Register the depsgraph handler
    bpy.app.handlers.depsgraph_update_post.append(_depsgraph_handler)

    # Enable auto_track on the scene property (depsgraph handler reads this)
    bpy.context.scene.usd_connect_auto_track = True

    # --- Create a World root empty (parent-based auto-tracking requires it) ---
    bpy.ops.object.empty_add(type="PLAIN_AXES")
    world = bpy.context.active_object
    world.name = "World"
    world["usd_prim_path"] = "/World"
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    _depsgraph_handler(bpy.context.scene, dg)

    # --- Create objects and trigger depsgraph ---

    # 1. Create a Cube parented under World
    bpy.ops.mesh.primitive_cube_add(location=(5.0, 6.0, 7.0))
    cube = bpy.context.active_object
    cube.name = "AutoCube"
    cube.parent = world
    # Force depsgraph evaluation
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    _depsgraph_handler(bpy.context.scene, dg)

    # 2. Create a Sphere parented under World
    bpy.ops.mesh.primitive_uv_sphere_add(location=(1.0, 2.0, 3.0))
    sphere = bpy.context.active_object
    sphere.name = "AutoSphere"
    sphere.parent = world
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    _depsgraph_handler(bpy.context.scene, dg)

    # 3. Hide the cube
    cube.hide_set(True)
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    _depsgraph_handler(bpy.context.scene, dg)

    # Small delay to ensure events are sent over TCP
    time.sleep(0.5)

    # Print what we auto-tracked
    for obj in bpy.data.objects:
        prim = obj.get("usd_prim_path", "")
        usd_type = obj.get("usd_type_name", "")
        print(
            f"[AutoTrack Emitter] {obj.name}: "
            f"prim={prim}, type={usd_type}, loc={tuple(obj.location)}"
        )

    sender.disconnect()
    capture_mod._state.author = None
    capture_mod._state.notice_emitter = None
    capture_mod._state.sender = None
    bpy.app.handlers.depsgraph_update_post.remove(_depsgraph_handler)
    print("[AutoTrack Emitter] Done")


if __name__ == "__main__":
    main()
