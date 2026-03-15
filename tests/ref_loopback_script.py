"""Loopback reproduction: Blender receiver that ALREADY has the scene imported.

Simulates the real single-instance workflow where the user:
  1. Imports a base scene with /World/Chair referencing test_asset.usda
  2. Starts the receiver — it gets events from the server (or replayed)
  3. Receiver processes set_reference on /World/Chair
  4. But those objects ALREADY EXIST from step 1!
  5. Does bpy.ops.wm.usd_import re-import and create duplicates?

Run via:
  blender --background --python tests/ref_loopback_script.py \
    -- --port PORT --out RESULTS_FILE --scene SCENE_FILE
"""

import json
import os
import sys
import time

import bpy

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)


def main():
    argv = sys.argv
    port = 7200
    out_path = ""
    scene_file = ""

    if "--" in argv:
        script_args = argv[argv.index("--") + 1 :]
        for i, arg in enumerate(script_args):
            if arg == "--port" and i + 1 < len(script_args):
                port = int(script_args[i + 1])
            if arg == "--out" and i + 1 < len(script_args):
                out_path = script_args[i + 1]
            if arg == "--scene" and i + 1 < len(script_args):
                scene_file = script_args[i + 1]

    if not out_path or not scene_file:
        print("[Loopback] ERROR: --out and --scene required")
        sys.exit(1)

    # ==================================================================
    # Step 1: Import the base scene — same as user's normal workflow.
    # This creates objects tagged with usd_prim_path by USDHook.
    # ==================================================================
    print(f"[Loopback] Importing base scene: {scene_file}")

    # Register USDHook so it fires during import
    try:
        from integrations.blender.capture import USD_CONNECT_Hook, _ensure_scene_props
        _ensure_scene_props()
        try:
            bpy.utils.register_class(USD_CONNECT_Hook)
        except Exception:
            pass
    except Exception as e:
        print(f"[Loopback] Warning: could not register USDHook: {e}")

    bpy.context.scene.usd_connect_asset_root = os.path.dirname(scene_file)

    bpy.ops.wm.usd_import(filepath=scene_file)

    print("\n=== Scene after initial import ===")
    for obj in bpy.data.objects:
        pp = obj.get("usd_prim_path", "")
        tn = obj.get("usd_type_name", "")
        parent = obj.parent.name if obj.parent else "(root)"
        print(f"  {obj.name:20s} prim='{pp}' type='{tn}' parent='{parent}'")

    # ==================================================================
    # Step 2: Connect receiver and process events.
    # The emitter (ref_emitter_script.py) already sent events to the
    # server, including set_reference for /World/Chair.  The receiver
    # replays from seq=1 and processes everything.
    # ==================================================================
    print(f"\n[Loopback] Starting receiver (port {port})")

    from integrations.blender.blender_adapter import BlenderAdapter
    from openusdconnect.protocol import (
        K_DEACTIVATE_PRIM,
        K_DELETE_PRIM,
        K_ENSURE_PRIM,
        K_ENSURE_XFORM_OPS,
        K_RENAME_PRIM,
        K_SET_GPRIM_ATTRS,
        K_SET_PAYLOAD,
        K_SET_REFERENCE,
        K_SET_VISIBILITY,
        K_SET_XFORM_MATRICES,
        K_SET_XFORM_TRS,
    )
    from openusdconnect.receiver import ReceiverThread

    adapter = BlenderAdapter()
    receiver = ReceiverThread(host="127.0.0.1", port=port, sync_from=1)
    receiver.start()

    time.sleep(2.0)

    lines = receiver.drain_queue()
    print(f"[Loopback] Receiver got {len(lines)} messages")

    for raw_line in lines:
        msg = json.loads(raw_line)
        if msg.get("type") != "event":
            continue
        ev = msg.get("event", {})
        k = ev.get("k")
        prim_path = ev.get("prim", "")
        extra = ""
        if k == K_SET_REFERENCE:
            extra = f" refs={ev.get('refs')}"
        print(f"  Processing: {k} {prim_path}{extra}")

        if k == K_ENSURE_PRIM:
            adapter.ensure_prim(prim_path, ev.get("typeName", "Xform"))
        elif k == K_ENSURE_XFORM_OPS:
            adapter.ensure_xform_ops(prim_path)
        elif k == K_SET_XFORM_TRS:
            adapter.set_xform_trs(prim_path, ev)
        elif k == K_SET_REFERENCE:
            adapter.set_reference(prim_path, ev.get("refs", []))
        elif k == K_SET_VISIBILITY:
            adapter.set_visibility(prim_path, ev.get("visible", True))
        elif k == K_DELETE_PRIM:
            adapter.delete_prim(prim_path)
        elif k == K_DEACTIVATE_PRIM:
            adapter.deactivate_prim(prim_path, ev.get("active", False))
        elif k == K_SET_XFORM_MATRICES:
            adapter.set_xform_matrices(prim_path, ev)
        elif k == K_SET_GPRIM_ATTRS:
            adapter.set_gprim_attrs(prim_path, ev.get("attrs", {}))
        elif k == K_RENAME_PRIM:
            adapter.rename_prim(prim_path, ev.get("new_name", ""))
        elif k == K_SET_PAYLOAD:
            adapter.set_payload(prim_path, ev.get("payloads", []))

    receiver.stop()
    try:
        receiver.join(timeout=2.0)
    except Exception:
        pass

    # ==================================================================
    # Step 3: Inspect scene for duplicates
    # ==================================================================
    print("\n=== Final Scene Inventory ===")
    prim_path_counts: dict[str, int] = {}
    for obj in bpy.data.objects:
        pp = obj.get("usd_prim_path", "")
        parent_name = obj.parent.name if obj.parent else "(root)"
        loc = tuple(round(v, 4) for v in obj.location)
        print(
            f"  obj='{obj.name}' prim_path='{pp}' "
            f"parent='{parent_name}' type={obj.type} loc={loc}"
        )
        if pp:
            prim_path_counts[pp] = prim_path_counts.get(pp, 0) + 1

    print("\n=== Prim Path Counts ===")
    for pp, count in sorted(prim_path_counts.items()):
        status = "OK" if count == 1 else "DUPLICATE"
        print(f"  {pp}: {count} ({status})")

    blender_suffixed = [
        obj.name
        for obj in bpy.data.objects
        if obj.get("usd_prim_path")
        and "." in obj.name
        and obj.name.rsplit(".", 1)[-1].isdigit()
    ]
    if blender_suffixed:
        print("\n=== Blender-suffixed names (duplicates) ===")
        for name in blender_suffixed:
            print(f"  {name}")

    # ------------------------------------------------------------------
    # TRS verification: check Chair received the final translate
    # ------------------------------------------------------------------
    chair_obj = None
    for obj in bpy.data.objects:
        if obj.get("usd_prim_path") == "/World/Chair":
            chair_obj = obj
            break

    print("\n=== TRS Verification ===")
    if chair_obj:
        loc = tuple(round(v, 4) for v in chair_obj.location)
        print(f"  Chair location: {loc}")
    else:
        print("  Chair not found!")

    # Check that children have valid transforms (not at origin with zero scale)
    child_trs_ok = True
    for obj in bpy.data.objects:
        pp = obj.get("usd_prim_path", "")
        if not pp or not pp.startswith("/World/Chair/"):
            continue
        scl = tuple(round(v, 4) for v in obj.scale)
        if scl == (0.0, 0.0, 0.0):
            print(f"  FAIL: {pp} has zero scale!")
            child_trs_ok = False

    results: dict[str, str] = {}
    results["total_object_count"] = str(len(bpy.data.objects))
    results["object_inventory"] = json.dumps(prim_path_counts)
    results["no_duplicates"] = (
        "FAIL: duplicate prim paths found"
        if any(c > 1 for c in prim_path_counts.values())
        else "PASS"
    )
    results["no_blender_suffixes"] = (
        f"FAIL: {blender_suffixed}" if blender_suffixed else "PASS"
    )

    # TRS results
    if chair_obj:
        # The emitter sends t=[3,0,0] in its last txn (phase 2 movement)
        cx = round(chair_obj.location.x, 2)
        results["chair_trs"] = (
            "PASS" if abs(cx - 3.0) < 0.1
            else f"FAIL: chair.location.x={cx}, expected 3.0"
        )
    else:
        results["chair_trs"] = "FAIL: Chair not found"

    results["children_trs"] = "PASS" if child_trs_ok else "FAIL: zero scale on children"

    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\n[Loopback] Results written to {out_path}")
    for k, v in results.items():
        print(f"  {k}: {v}")

    sys.exit(0)


if __name__ == "__main__":
    main()
