"""Reproduce user's exact workflow: manual set_reference, then emitter resends it.

1. Receiver processes manual set_reference → imports test_asset.usda → creates objects
2. Emitter sends set_reference AGAIN (from reading stage references) → must NOT re-import
3. Check for Model.001 specifically

Run via:
  blender --background --python tests/ref_manual_then_move_script.py \
    -- --port PORT --out RESULTS_FILE --asset-path PATH
"""

import json
import os
import socket
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
    asset_path = ""

    if "--" in argv:
        script_args = argv[argv.index("--") + 1 :]
        for i, arg in enumerate(script_args):
            if arg == "--port" and i + 1 < len(script_args):
                port = int(script_args[i + 1])
            if arg == "--out" and i + 1 < len(script_args):
                out_path = script_args[i + 1]
            if arg == "--asset-path" and i + 1 < len(script_args):
                asset_path = script_args[i + 1]

    if not out_path or not asset_path:
        print("[ManualThenMove] ERROR: --out and --asset-path required")
        sys.exit(1)

    from integrations.blender.blender_adapter import BlenderAdapter
    from openusdconnect.protocol import (
        K_ENSURE_PRIM,
        K_ENSURE_XFORM_OPS,
        K_SET_REFERENCE,
        K_SET_VISIBILITY,
        K_SET_XFORM_TRS,
        MSG_EVENT,
        make_hello,
        make_quit,
        make_txn,
    )
    from openusdconnect.receiver import ReceiverThread
    from openusdconnect.transport import send_line

    # ==================================================================
    # Phase 1: Send manual set_reference (user's CLI command)
    # ==================================================================
    print(f"[ManualThenMove] Sending manual set_reference for {asset_path}")
    asset_fwd = asset_path.replace("\\", "/")

    emitter_sock = socket.create_connection(("127.0.0.1", port))
    send_line(emitter_sock, make_hello("emitter"))
    send_line(
        emitter_sock,
        make_txn(
            "manual",
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/Chair", "typeName": "Xform"},
                {
                    "k": K_SET_REFERENCE,
                    "prim": "/World/Chair",
                    "refs": [{"asset_path": asset_fwd, "prim_path": "/Model"}],
                },
            ],
        ),
    )
    time.sleep(0.3)
    send_line(emitter_sock, make_quit())
    emitter_sock.close()

    # ==================================================================
    # Phase 2: Start receiver, process the manual events → first import
    # ==================================================================
    print("[ManualThenMove] Starting receiver...")
    adapter = BlenderAdapter()
    receiver = ReceiverThread(host="127.0.0.1", port=port, sync_from=1)
    receiver.start()
    time.sleep(1.5)

    lines = receiver.drain_queue()
    print(f"[ManualThenMove] Phase 1: receiver got {len(lines)} messages")

    for raw_line in lines:
        msg = json.loads(raw_line)
        if msg.get("type") != MSG_EVENT:
            continue
        ev = msg.get("event", {})
        k = ev.get("k")
        prim_path = ev.get("prim", "")
        print(f"  Processing: {k} {prim_path}")

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

    print("\n=== After Phase 1 (initial import) ===")
    for obj in bpy.data.objects:
        pp = obj.get("usd_prim_path", "")
        if pp:
            print(f"  {obj.name:20s} prim='{pp}'")

    # ==================================================================
    # Phase 3: Emitter resends set_reference (simulates what happens
    # when user moves Chair — emitter detects reference on stage and
    # sends it again as a first-encounter event)
    # ==================================================================
    print("\n[ManualThenMove] Sending emitter events (move simulation)...")
    emitter_sock2 = socket.create_connection(("127.0.0.1", port))
    send_line(emitter_sock2, make_hello("emitter"))
    send_line(
        emitter_sock2,
        make_txn(
            "emitter-move",
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/Chair", "typeName": "Xform"},
                {"k": K_ENSURE_XFORM_OPS, "prim": "/World/Chair"},
                {
                    "k": K_SET_REFERENCE,
                    "prim": "/World/Chair",
                    "refs": [{"asset_path": asset_fwd, "prim_path": "/Model"}],
                },
                {
                    "k": K_SET_XFORM_TRS,
                    "prim": "/World/Chair",
                    "fields": ["t"],
                    "t": [3.0, 0.0, 0.0],
                },
                # Children first-encounter
                {"k": K_ENSURE_PRIM, "prim": "/World/Chair/Seat", "typeName": "Mesh"},
                {"k": K_ENSURE_XFORM_OPS, "prim": "/World/Chair/Seat"},
                {"k": K_ENSURE_PRIM, "prim": "/World/Chair/Back", "typeName": "Mesh"},
                {"k": K_ENSURE_XFORM_OPS, "prim": "/World/Chair/Back"},
                {"k": K_ENSURE_PRIM, "prim": "/World/Chair/Leg_0", "typeName": "Mesh"},
                {"k": K_ENSURE_PRIM, "prim": "/World/Chair/Leg_1", "typeName": "Mesh"},
                {"k": K_ENSURE_PRIM, "prim": "/World/Chair/Leg_2", "typeName": "Mesh"},
                {"k": K_ENSURE_PRIM, "prim": "/World/Chair/Leg_3", "typeName": "Mesh"},
            ],
        ),
    )
    time.sleep(0.3)
    send_line(emitter_sock2, make_quit())
    emitter_sock2.close()

    # Process phase 3 events
    time.sleep(1.5)
    lines2 = receiver.drain_queue()
    print(f"[ManualThenMove] Phase 3: receiver got {len(lines2)} messages")

    for raw_line in lines2:
        msg = json.loads(raw_line)
        if msg.get("type") != MSG_EVENT:
            continue
        ev = msg.get("event", {})
        k = ev.get("k")
        prim_path = ev.get("prim", "")
        print(f"  Processing: {k} {prim_path}")

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

    receiver.stop()
    try:
        receiver.join(timeout=2.0)
    except Exception:
        pass

    # ==================================================================
    # Check for duplicates
    # ==================================================================
    print("\n=== Final Scene ===")
    prim_path_counts: dict[str, int] = {}
    for obj in bpy.data.objects:
        pp = obj.get("usd_prim_path", "")
        parent_name = obj.parent.name if obj.parent else "(root)"
        print(
            f"  obj='{obj.name}' prim_path='{pp}' "
            f"parent='{parent_name}' type={obj.type}"
        )
        if pp:
            prim_path_counts[pp] = prim_path_counts.get(pp, 0) + 1

    blender_suffixed = [
        obj.name
        for obj in bpy.data.objects
        if obj.get("usd_prim_path")
        and "." in obj.name
        and obj.name.rsplit(".", 1)[-1].isdigit()
    ]

    chair_obj = None
    for obj in bpy.data.objects:
        if obj.get("usd_prim_path") == "/World/Chair":
            chair_obj = obj
            break

    results: dict[str, str] = {}
    results["total_object_count"] = str(len(bpy.data.objects))
    results["object_inventory"] = json.dumps(prim_path_counts)
    results["no_duplicates"] = (
        "FAIL: duplicate prim paths"
        if any(c > 1 for c in prim_path_counts.values())
        else "PASS"
    )
    results["no_blender_suffixes"] = (
        f"FAIL: {blender_suffixed}" if blender_suffixed else "PASS"
    )
    if chair_obj:
        cx = round(chair_obj.location.x, 2)
        results["chair_trs"] = (
            "PASS" if abs(cx - 3.0) < 0.1 else f"FAIL: x={cx}"
        )
    else:
        results["chair_trs"] = "FAIL: Chair not found"

    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    for k, v in results.items():
        print(f"  {k}: {v}")

    sys.exit(0)


if __name__ == "__main__":
    main()
