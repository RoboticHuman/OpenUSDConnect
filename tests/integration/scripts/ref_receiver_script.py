"""Blender headless receiver for reference duplication test.

Runs inside Blender (--background).  Connects to the sync server,
processes events via BlenderAdapter (including set_reference which
triggers USD import), then inspects the scene for duplicate objects
and writes results JSON.

Run via:
  blender --background --python tests/ref_receiver_script.py \
    -- --port PORT --out RESULTS_FILE [--asset-root DIR]
"""

import json
import os
import sys
import time

import bpy

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(os.path.dirname(_scripts_dir)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
_venv_sp = os.path.join(project_root, ".venv", "Lib", "site-packages")
if os.path.isdir(_venv_sp) and _venv_sp not in sys.path:
    sys.path.append(_venv_sp)
for _k in [k for k in sys.modules if k.startswith("openusdconnect")]:
    del sys.modules[_k]

from integrations.blender.blender_adapter import BlenderAdapter
from openusdconnect.codec import message_to_dict
from openusdconnect.protocol_constants import (
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
    MSG_EVENT,
)
from openusdconnect.receiver import ReceiverThread


def _process_event(adapter, ev):
    """Dispatch a single event dict through the BlenderAdapter."""
    k = ev.get("k")
    prim_path = ev.get("prim", "")

    if k == K_ENSURE_PRIM:
        adapter.ensure_prim(prim_path, ev["typeName"])
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
    else:
        print(f"[RefReceiver] Unknown event kind: {k}")


def main():
    argv = sys.argv
    port = 7200
    out_path = ""
    asset_root = ""

    if "--" in argv:
        script_args = argv[argv.index("--") + 1 :]
        for i, arg in enumerate(script_args):
            if arg == "--port" and i + 1 < len(script_args):
                port = int(script_args[i + 1])
            if arg == "--out" and i + 1 < len(script_args):
                out_path = script_args[i + 1]
            if arg == "--asset-root" and i + 1 < len(script_args):
                asset_root = script_args[i + 1]

    if not out_path:
        print("[RefReceiver] ERROR: --out required")
        sys.exit(1)

    # Set asset root for relative path resolution (best-effort; emitter
    # sends absolute paths so this is a fallback).
    if asset_root:
        try:
            bpy.context.scene.usd_connect_asset_root = asset_root
        except AttributeError:
            bpy.context.scene["usd_connect_asset_root"] = asset_root
        print(f"[RefReceiver] Asset root: {asset_root}")

    print(f"[RefReceiver] Connecting to 127.0.0.1:{port}")
    receiver = ReceiverThread(host="127.0.0.1", port=port, sync_from=1)
    receiver.start()

    # Wait for events to arrive via replay
    time.sleep(2.0)

    adapter = BlenderAdapter()
    lines = receiver.drain_queue()
    print(f"[RefReceiver] Got {len(lines)} messages from queue")

    for raw_buf in lines:
        msg = message_to_dict(raw_buf)
        if msg.get("type") != MSG_EVENT:
            continue
        ev = msg.get("event", {})
        k = ev.get("k")
        prim_path = ev.get("prim", "")
        print(f"[RefReceiver] Processing: {k} {prim_path}")
        _process_event(adapter, ev)

    receiver.stop()
    try:
        receiver.join(timeout=2.0)
    except Exception:
        pass

    # ------------------------------------------------------------------
    # Inspect scene
    # ------------------------------------------------------------------
    print("\n=== Scene Inventory ===")
    prim_path_counts: dict[str, int] = {}
    for obj in bpy.data.objects:
        pp = obj.get("usd_prim_path", "")
        parent_name = obj.parent.name if obj.parent else "(root)"
        print(f"  obj='{obj.name}' prim_path='{pp}' parent='{parent_name}' type={obj.type}")
        if pp:
            prim_path_counts[pp] = prim_path_counts.get(pp, 0) + 1

    print("\n=== Prim Path Counts ===")
    for pp, count in sorted(prim_path_counts.items()):
        status = "OK" if count == 1 else "DUPLICATE"
        print(f"  {pp}: {count} ({status})")

    # Check for Blender dedup suffixes (e.g. "Model.001")
    blender_suffixed = [
        obj.name
        for obj in bpy.data.objects
        if obj.get("usd_prim_path") and "." in obj.name and obj.name.rsplit(".", 1)[-1].isdigit()
    ]
    if blender_suffixed:
        print("\n=== Blender-suffixed names (potential duplicates) ===")
        for name in blender_suffixed:
            print(f"  {name}")

    # ------------------------------------------------------------------
    # TRS verification
    # ------------------------------------------------------------------
    print("\n=== TRS Verification ===")
    chair_obj = None
    for obj in bpy.data.objects:
        if obj.get("usd_prim_path") == "/World/Chair":
            chair_obj = obj
            break
    if chair_obj:
        loc = tuple(round(v, 4) for v in chair_obj.location)
        print(f"  Chair location: {loc}")
    # Check children aren't invisible or zero-scaled
    child_trs_ok = True
    for obj in bpy.data.objects:
        pp = obj.get("usd_prim_path", "")
        if not pp or not pp.startswith("/World/Chair/"):
            continue
        scl = tuple(round(v, 4) for v in obj.scale)
        loc = tuple(round(v, 4) for v in obj.location)
        print(f"  {pp}: loc={loc} scale={scl}")
        if scl == (0.0, 0.0, 0.0):
            print(f"  FAIL: {pp} has zero scale!")
            child_trs_ok = False

    # ------------------------------------------------------------------
    # Build results
    # ------------------------------------------------------------------
    results: dict[str, str] = {}
    results["total_object_count"] = str(len(bpy.data.objects))
    results["object_inventory"] = json.dumps(prim_path_counts)

    has_duplicates = any(c > 1 for c in prim_path_counts.values())
    results["no_duplicates"] = "FAIL: duplicate prim paths found" if has_duplicates else "PASS"

    results["no_blender_suffixes"] = f"FAIL: {blender_suffixed}" if blender_suffixed else "PASS"

    # TRS results
    if chair_obj:
        cx = round(chair_obj.location.x, 2)
        results["chair_trs"] = (
            "PASS" if abs(cx - 3.0) < 0.1 else f"FAIL: chair.location.x={cx}, expected ~3.0"
        )
    else:
        results["chair_trs"] = "FAIL: Chair not found"
    results["children_trs"] = "PASS" if child_trs_ok else "FAIL: zero scale on children"

    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\n[RefReceiver] Results written to {out_path}")
    for k, v in results.items():
        print(f"  {k}: {v}")

    # Always exit 0 so the orchestrator can read the results file
    # and make its own assertions.
    sys.exit(0)


if __name__ == "__main__":
    main()
