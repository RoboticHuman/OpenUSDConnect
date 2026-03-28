"""Reproduce exact user workflow for teapot payload + axis conversion.

1. Import test_scene.usda via addon (USDHook tags objects, World gets Rx(90°))
2. Start emitter + receiver (loopback)
3. External CLI sends teapot payload events
4. Move World_Teapot
5. Check for teapot.001 duplicate

Run via:
  blender --background --python tests/integration/scripts/payload_axis_test_script.py \
    -- --port PORT --out RESULTS_FILE
"""

import json
import os
import socket
import sys
import time

import bpy

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(os.path.dirname(_scripts_dir)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
for _k in [k for k in sys.modules if k.startswith("openusdconnect")]:
    del sys.modules[_k]


def _scene_inventory():
    inventory = []
    for obj in bpy.data.objects:
        loc = tuple(round(v, 6) for v in obj.location)
        rot = tuple(round(v, 6) for v in obj.rotation_euler)
        inventory.append({
            "name": obj.name,
            "prim_path": obj.get("usd_prim_path", ""),
            "imported": obj.get("_usd_imported", False),
            "import_root": obj.get("_usd_import_root", False),
            "parent": obj.parent.name if obj.parent else "(root)",
            "obj_type": obj.type,
            "location": loc,
            "rotation_euler": rot,
        })
    return inventory


def _print_inventory(label, inv):
    print(f"\n=== {label} ({len(inv)} objects) ===")
    for o in inv:
        flags = ""
        if o["import_root"]:
            flags += " [import_root]"
        if o["imported"]:
            flags += " [imported]"
        print(
            f"  {o['name']:30s} prim='{o['prim_path']}' "
            f"parent='{o['parent']}' loc={o['location']}{flags}"
        )


def _pump(cycles=50, delay=0.05):
    from integrations.blender import capture as cap_mod
    from integrations.blender import receiver_addon

    for i in range(cycles):
        # Drain receiver
        if receiver_addon._RECEIVER is not None:
            lines = receiver_addon._RECEIVER.drain_queue()
            if lines:
                receiver_addon._set_applying_remote(True)
                try:
                    receiver_addon._drain_and_process(lines)
                    bpy.context.view_layer.update()
                finally:
                    receiver_addon._set_applying_remote(False)

        # Force depsgraph + emitter
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        updates = list(depsgraph.updates)
        if updates and cap_mod._state.author is not None and cap_mod._state.author.enabled:
            if not cap_mod._state.author._applying_remote:
                cap_mod._state.author.on_depsgraph_update(updates)
                cap_mod._try_send_dirty_events(include_matrices=False)

        time.sleep(delay)


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
        print("[Test] ERROR: --out required")
        sys.exit(1)

    base_usd = os.path.join(project_root, "test_scene.usda")

    # ==================================================================
    # Step 1: Import test_scene.usda via addon import (with USDHook)
    # ==================================================================
    from integrations.blender import capture as cap_mod
    from integrations.blender import receiver_addon
    from integrations.blender.capture import (
        NetworkSender,
        USD_CONNECT_Hook,
        _ensure_scene_props,
        _get_stage_author,
    )
    from openusdconnect.emitter import NoticeEmitter

    _ensure_scene_props()
    receiver_addon._ensure_scene_props()
    try:
        bpy.utils.register_class(USD_CONNECT_Hook)
    except Exception:
        pass

    scene = bpy.context.scene
    scene.usd_connect_asset_root = project_root

    # Import the scene (this is what the addon's "Import with tagging" does)
    print("[Test] Step 1: Importing test_scene.usda")
    bpy.ops.wm.usd_import(filepath=base_usd)
    scene.usd_connect_base_usd_path = base_usd

    _print_inventory("After scene import", _scene_inventory())

    # ==================================================================
    # Step 2: Start emitter
    # ==================================================================
    print("\n[Test] Step 2: Starting emitter")
    author = _get_stage_author(bpy.context)
    author.enabled = True
    author.initialize_baseline()
    author.seed_used_paths()
    cap_mod._state.notice_emitter = NoticeEmitter(author.stage)
    cap_mod._state.sender = NetworkSender(host="127.0.0.1", port=port)

    # ==================================================================
    # Step 3: Start receiver
    # ==================================================================
    print("[Test] Step 3: Starting receiver")
    from openusdconnect.receiver import ReceiverThread
    receiver_addon._RECEIVER = ReceiverThread(
        host="127.0.0.1", port=port, sync_from=1,
    )
    receiver_addon._RECEIVER.start()
    receiver_addon._ADAPTER = None
    time.sleep(0.5)

    # ==================================================================
    # Step 4: Send teapot payload from external CLI
    # ==================================================================
    print("[Test] Step 4: Sending teapot payload events")
    from openusdconnect.protocol import make_hello
    from openusdconnect.transport import send_line

    ext_sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    send_line(ext_sock, make_hello("emitter"))
    send_line(ext_sock, {"type": "txn", "client_id": "cli", "events": [
        {"k": "ensure_prim", "prim": "/World/Teapot", "typeName": "Xform"},
        {"k": "ensure_xform_ops", "prim": "/World/Teapot"},
        {"k": "set_xform_trs", "prim": "/World/Teapot",
         "fields": ["t", "r", "s"],
         "t": [0, 3, 0], "r": [1, 0, 0, 0], "s": [1, 1, 1]},
        {"k": "set_payload", "prim": "/World/Teapot",
         "payloads": [{"asset_path": "./assets/intent-vfx/assets/teapot/teapot.usd",
                       "prim_path": "/teapot"}]},
        {"k": "load_payload", "prim": "/World/Teapot"},
    ]})
    ext_sock.close()

    # Let receiver process + emitter react
    _pump(cycles=60, delay=0.1)

    _print_inventory("After payload + pump", _scene_inventory())

    # ==================================================================
    # Step 5: Move World_Teapot (the trigger for teapot.001)
    # ==================================================================
    teapot_obj = None
    for obj in bpy.data.objects:
        if obj.get("usd_prim_path") == "/World/Teapot":
            teapot_obj = obj
            break

    if teapot_obj:
        print("\n[Test] Step 5: Moving World_Teapot")
        teapot_obj.location = (2.0, 0.0, 0.0)
        bpy.context.view_layer.update()
        # Pump aggressively to catch the feedback loop
        _pump(cycles=60, delay=0.1)
    else:
        print("\n[Test] WARNING: World_Teapot not found!")

    inv_final = _scene_inventory()
    _print_inventory("FINAL after move", inv_final)

    # ==================================================================
    # Check results
    # ==================================================================
    suffixed = [o["name"] for o in inv_final if ".001" in o["name"]]
    prim_counts = {}
    for o in inv_final:
        pp = o["prim_path"]
        if pp:
            prim_counts[pp] = prim_counts.get(pp, 0) + 1
    duplicates = {pp: c for pp, c in prim_counts.items() if c > 1}

    results = {
        "total_objects": len(inv_final),
        "suffixed": suffixed,
        "duplicates": duplicates,
        "inventory": inv_final,
    }

    # Cleanup
    if receiver_addon._RECEIVER is not None:
        receiver_addon._RECEIVER.stop()
        receiver_addon._RECEIVER.join(timeout=2)
        receiver_addon._RECEIVER = None

    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    print("\n[Test] === RESULTS ===")
    print(f"  Total: {len(inv_final)}")
    print(f"  Suffixed (.001): {suffixed or 'none'}")
    print(f"  Duplicate paths: {duplicates or 'none'}")
    if suffixed or duplicates:
        print("  STATUS: FAIL")
    else:
        print("  STATUS: PASS")

    sys.exit(0)


if __name__ == "__main__":
    main()
