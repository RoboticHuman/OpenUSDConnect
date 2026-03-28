"""Check what properties Blender's USD importer sets on objects."""
import os

import bpy

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
ASSET = os.path.join(PROJECT_ROOT, "assets", "intent-vfx", "assets", "simpleAsset", "simpleAsset.usd")

ADDON_ZIP = os.path.join(PROJECT_ROOT, "dist", "usd_connect_blender.zip")

def _test():
    # Install addon first (registers USDHook)
    bpy.ops.preferences.addon_install(filepath=ADDON_ZIP, overwrite=True)
    bpy.ops.preferences.addon_enable(module="usd_connect")
    print("  Addon installed", flush=True)

    before = set(bpy.data.objects)
    bpy.ops.wm.usd_import('EXEC_DEFAULT', filepath=ASSET, prim_path_mask="/simpleAsset")
    new_objs = set(bpy.data.objects) - before

    for obj in sorted(new_objs, key=lambda o: o.name):
        parent = obj.parent.name if obj.parent else None
        props = {k: v for k, v in obj.items() if not k.startswith("_")}
        print(f"  {obj.name} type={obj.type} parent={parent} props={props}", flush=True)

    print("Done.", flush=True)
    bpy.ops.wm.quit_blender()
    return None

bpy.app.timers.register(_test, first_interval=2.0)
