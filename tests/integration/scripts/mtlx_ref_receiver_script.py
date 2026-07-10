"""Blender receiver for MaterialX reference test.

Connects to the server, receives teapot reference events, applies them
via BlenderAdapter (which triggers USD import + MaterialX enrichment),
then verifies hierarchy and MaterialX materials. Writes results JSON.

Run via:
  blender --background --python mtlx_ref_receiver_script.py \
    -- --port PORT --out RESULTS_FILE
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
    MSG_EVENT,
)
from openusdconnect.receiver import ReceiverThread


def _process_event(adapter, ev):
    adapter.apply_event(ev)


def main():
    argv = sys.argv
    port = 7200
    out_path = ""

    if "--" in argv:
        script_args = argv[argv.index("--") + 1 :]
        for i, arg in enumerate(script_args):
            if arg == "--port" and i + 1 < len(script_args):
                port = int(script_args[i + 1])
            if arg == "--out" and i + 1 < len(script_args):
                out_path = script_args[i + 1]

    if not out_path:
        print("[MtlxRefReceiver] ERROR: --out required")
        sys.exit(1)

    print(f"[MtlxRefReceiver] Connecting to 127.0.0.1:{port}")
    receiver = ReceiverThread(host="127.0.0.1", port=port, sync_from=1)
    receiver.start()

    # Poll until we receive events (the emitter may still be sending
    # under heavy CI load).  Stop early once we see a set_reference.
    adapter = BlenderAdapter()
    lines = []
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        time.sleep(0.3)
        batch = receiver.drain_queue()
        if batch:
            lines.extend(batch)
            if any(message_to_dict(b).get("event", {}).get("k") == "set_reference" for b in batch):
                break
    print(f"[MtlxRefReceiver] Got {len(lines)} messages from queue")

    for raw_buf in lines:
        msg = message_to_dict(raw_buf)
        if msg.get("type") != MSG_EVENT:
            continue
        ev = msg.get("event", {})
        k = ev.get("k")
        prim_path = ev.get("prim", "")
        print(f"[MtlxRefReceiver] Processing: {k} {prim_path}")
        _process_event(adapter, ev)

    receiver.stop()
    try:
        receiver.join(timeout=2.0)
    except Exception:
        pass

    # ------------------------------------------------------------------
    # Verify hierarchy
    # ------------------------------------------------------------------
    results = {}

    print("\n=== Scene Inventory ===")
    for obj in bpy.data.objects:
        pp = obj.get("usd_prim_path", "")
        parent_name = obj.parent.name if obj.parent else "(root)"
        print(f"  obj='{obj.name}' prim_path='{pp}' parent='{parent_name}' type={obj.type}")

    # Container should exist
    container = None
    for obj in bpy.data.objects:
        if obj.get("usd_prim_path") == "/World/Teapot":
            container = obj
            break
    results["container_exists"] = "PASS" if container else "FAIL: /World/Teapot not found"

    # Container should have children (imported content parented under it)
    if container:
        child_names = [c.name for c in container.children]
        results["has_children"] = "PASS" if child_names else "FAIL: container has no children"
    else:
        results["has_children"] = "SKIP"

    # Body mesh should exist somewhere in the hierarchy
    body = None
    for obj in bpy.data.objects:
        if "Body" in obj.name and obj.type == "MESH":
            body = obj
            break
    results["body_mesh_exists"] = "PASS" if body else "FAIL: Body mesh not found"

    # ------------------------------------------------------------------
    # Verify MaterialX enrichment
    # ------------------------------------------------------------------
    mat = bpy.data.materials.get("default_material")
    if mat is None:
        results["material_exists"] = "FAIL: default_material not found"
    else:
        results["material_exists"] = "PASS"

    # Check for MaterialX shader (cached input_map proves the shader mapper ran)
    mtlx_path = "/World/Teapot/mtl/default_material/default_shader_mtlx"
    cached = adapter._registry.get_shader(mtlx_path).get("input_map")
    if cached is not None:
        results["materialx_enrichment"] = "PASS"
        # Verify a value was applied
        base_socket = cached.get("base")
        if base_socket and abs(base_socket.default_value - 1.0) < 0.01:
            results["materialx_base_value"] = "PASS"
        else:
            val = base_socket.default_value if base_socket else "missing"
            results["materialx_base_value"] = f"FAIL: base={val}"
    else:
        results["materialx_enrichment"] = "FAIL: no cached input_map"
        results["materialx_base_value"] = "SKIP"

    # Check node tree has more than default Principled BSDF
    if mat and mat.use_nodes and mat.node_tree:
        node_count = len(mat.node_tree.nodes)
        # MaterialX Standard Surface = 5 nodes + Material Output = 6+
        if node_count >= 5:
            results["materialx_node_count"] = "PASS"
        else:
            results["materialx_node_count"] = f"FAIL: only {node_count} nodes"
    else:
        results["materialx_node_count"] = "SKIP"

    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\n[MtlxRefReceiver] Results written to {out_path}")
    for k, v in results.items():
        print(f"  {k}: {v}")

    sys.exit(0)


if __name__ == "__main__":
    main()
