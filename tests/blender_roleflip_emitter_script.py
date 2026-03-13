"""Blender role-flip test — Phase 1: Import Y-up USD scene, move + add objects, emit.

Imports test_scene.usda (Y-up) into Blender (Z-up). The importer applies a 90deg X
rotation to the World root prim for coordinate conversion.

1. Moves /World/Cube to local (3,5,7)
2. Creates a NEW cube ("NewCube") under /World via auto-tracking at local (6,8,10)
3. Emits all events

Run via: blender --background --python tests/blender_roleflip_emitter_script.py -- --port PORT --scene PATH
"""

import sys
import os
import time

import bpy

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)


def main():
    argv = sys.argv
    port = 7200
    scene_path = ""
    if "--" in argv:
        script_args = argv[argv.index("--") + 1:]
        for i, arg in enumerate(script_args):
            if arg == "--port" and i + 1 < len(script_args):
                port = int(script_args[i + 1])
            if arg == "--scene" and i + 1 < len(script_args):
                scene_path = script_args[i + 1]

    if not scene_path:
        print("[RoleFlip A] ERROR: --scene required")
        sys.exit(1)

    # Clear default scene
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

    # Import the Y-up USD scene
    print(f"[RoleFlip A] Importing {scene_path}")
    bpy.ops.wm.usd_import(filepath=scene_path)
    bpy.context.view_layer.update()

    import mathutils
    identity = mathutils.Matrix.Identity(4)

    # Find /World/Cube and /World
    cube = None
    world_obj = None
    for obj in bpy.data.objects:
        prim = obj.get("usd_prim_path", "")
        if prim == "/World/Cube":
            cube = obj
        if prim == "/World":
            world_obj = obj

    if cube is None or world_obj is None:
        print("[RoleFlip A] ERROR: /World/Cube or /World not found")
        for obj in bpy.data.objects:
            print(f"  {obj.name}: prim={obj.get('usd_prim_path', '')}")
        sys.exit(1)

    print(f"[RoleFlip A] Found {cube.name} -> /World/Cube")
    print(f"[RoleFlip A] World rotation: {[round(v, 4) for v in world_obj.rotation_euler]}")

    # Connect emitter with auto_track=True
    author = BlenderStageAuthor(base_usd_path=scene_path)
    author.enabled = True
    author.auto_track = True
    author.seed_used_paths()

    emitter_notice = NoticeEmitter(author.stage)
    sender = NetworkSender(host="127.0.0.1", port=port)
    sender.connect()

    capture_mod._state.author = author
    capture_mod._state.notice_emitter = emitter_notice
    capture_mod._state.sender = sender
    bpy.context.scene.usd_connect_auto_track = True
    bpy.app.handlers.depsgraph_update_post.append(_depsgraph_handler)

    # 1. Move existing Cube to (3, 5, 7)
    cube.location = (3.0, 5.0, 7.0)
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    _depsgraph_handler(bpy.context.scene, dg)

    world_pos = [round(v, 4) for v in cube.matrix_world.translation]
    print(f"[RoleFlip A] Cube local=(3,5,7), world={world_pos}")

    # 2. Create a NEW cube under /World — this is the key scenario:
    #    This object doesn't exist in the USD file, it's auto-tracked.
    #    On the receiver it will be created fresh by ensure_prim.
    bpy.ops.mesh.primitive_cube_add(location=(6.0, 8.0, 10.0))
    new_cube = bpy.context.active_object
    new_cube.name = "NewCube"
    new_cube.parent = world_obj
    # MPI set to identity for parenting (standard for new objects)
    new_cube.matrix_parent_inverse = mathutils.Matrix.Identity(4)
    bpy.context.view_layer.update()

    new_world = [round(v, 4) for v in new_cube.matrix_world.translation]
    print(f"[RoleFlip A] NewCube local=(6,8,10), world={new_world}")

    dg = bpy.context.evaluated_depsgraph_get()
    _depsgraph_handler(bpy.context.scene, dg)

    time.sleep(0.5)

    sender.disconnect()
    capture_mod._state.author = None
    capture_mod._state.notice_emitter = None
    capture_mod._state.sender = None
    bpy.app.handlers.depsgraph_update_post.remove(_depsgraph_handler)
    bpy.utils.unregister_class(USD_CONNECT_Hook)
    print("[RoleFlip A] Done")


if __name__ == "__main__":
    main()
