"""Minimal test: just try importing the MaterialX USD file."""

import os

import bpy

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
ASSET = os.path.join(
    PROJECT_ROOT, "assets", "test_assets", "MaterialXTest", "basicTextured.usda"
)


def _test():
    print(f"[TEST] Asset exists: {os.path.isfile(ASSET)}", flush=True)
    print(f"[TEST] Asset path: {ASSET}", flush=True)

    # Test 1: EXEC_DEFAULT + prim_path_mask (standalone, no override)
    before = set(bpy.data.objects)
    print("[TEST] Test 1: EXEC_DEFAULT + prim_path_mask=/Teapot (no override)", flush=True)
    try:
        result = bpy.ops.wm.usd_import('EXEC_DEFAULT', filepath=ASSET, prim_path_mask="/Teapot")
        print(f"[TEST]   Result: {result}", flush=True)
    except Exception as e:
        print(f"[TEST]   Error: {e}", flush=True)
    new1 = set(bpy.data.objects) - before
    print(f"[TEST]   New objects: {[o.name for o in new1]}", flush=True)

    # Test 2: EXEC_DEFAULT + prim_path_mask + temp_override (like adapter)
    before2 = set(bpy.data.objects)
    print("[TEST] Test 2: EXEC_DEFAULT + prim_path_mask=/Teapot + temp_override", flush=True)
    try:
        window = bpy.context.window
        if window is None:
            windows = list(bpy.context.window_manager.windows)
            window = windows[0] if windows else None
        print(f"[TEST]   Window: {window}", flush=True)
        if window:
            with bpy.context.temp_override(window=window):
                result = bpy.ops.wm.usd_import('EXEC_DEFAULT', filepath=ASSET,
                                                 prim_path_mask="/Teapot",
                                                 import_guide=False,
                                                 import_visible_only=True)
            print(f"[TEST]   Result: {result}", flush=True)
        else:
            print("[TEST]   No window available!", flush=True)
    except Exception as e:
        print(f"[TEST]   Error: {e}", flush=True)
    new2 = set(bpy.data.objects) - before2
    print(f"[TEST]   New objects: {[o.name for o in new2]}", flush=True)

    # Test 3: Without EXEC_DEFAULT + temp_override (original pre-fix behavior)
    before3 = set(bpy.data.objects)
    print("[TEST] Test 3: No EXEC_DEFAULT + temp_override", flush=True)
    try:
        if window:
            with bpy.context.temp_override(window=window):
                result = bpy.ops.wm.usd_import(filepath=ASSET,
                                                 prim_path_mask="/Teapot",
                                                 import_guide=False,
                                                 import_visible_only=True)
            print(f"[TEST]   Result: {result}", flush=True)
    except Exception as e:
        print(f"[TEST]   Error: {e}", flush=True)
    new3 = set(bpy.data.objects) - before3
    print(f"[TEST]   New objects: {[o.name for o in new3]}", flush=True)

    print(f"[TEST] All objects: {[o.name for o in bpy.data.objects]}", flush=True)
    print(f"[TEST] All materials: {[m.name for m in bpy.data.materials]}", flush=True)
    print("[TEST] Done.", flush=True)
    return None


bpy.app.timers.register(_test, first_interval=2.0)
