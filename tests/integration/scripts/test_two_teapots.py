"""E2E test: two teapots at different paths — check material identity.

Spawns /World/Teapot (under scene root) and /Teapot (bare root).
Both reference the same Teapot.usd asset. Checks that each gets its own
material with correct Base Color connections.

Requires server running externally on port 7202.
"""

import os
import socket as _socket
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import bpy

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 7202
BASE_USD = os.path.join(PROJECT_ROOT, "test_scene.usda")
ADDON_ZIP = os.path.join(PROJECT_ROOT, "dist", "usd_connect_blender.zip")
TEAPOT_ASSET = "./assets/full_assets/Teapot/Teapot.usd"


def log(msg):
    print(f"[2-TEAPOTS] {msg}", flush=True)


def send_cli_events(events):
    global _cli_txn_id
    from openusdconnect.protocol import make_hello
    from openusdconnect.transport import send_line
    s = _socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=5)
    send_line(s, make_hello(
        "emitter", client_id="e2e_cli", producer_session_id="two-teapots-e2e"))
    _cli_txn_id += 1
    send_line(s, {"type": "txn", "events": events, "txn_id": _cli_txn_id})
    s.close()


_cli_txn_id = 0
_step = 0
_retries = 0


def _test_step():
    global _step, _retries

    try:
        if _step == 0:
            log("Step 0: Installing addon...")
            bpy.ops.preferences.addon_install(filepath=ADDON_ZIP, overwrite=True)
            bpy.ops.preferences.addon_enable(module="usd_connect")
            _step = 1
            return 1.0

        elif _step == 1:
            log("Step 1: Connecting emitter + receiver...")
            scene = bpy.context.scene
            scene.usd_connect_base_usd_path = BASE_USD
            scene.usd_connect_auto_track = True
            scene.usd_connect_emit_host = SERVER_HOST
            scene.usd_connect_emit_port = SERVER_PORT
            try:
                bpy.ops.usd_connect.connect_emitter()
            except Exception as e:
                log(f"  Emitter failed: {e}")
            scene.usd_connect_recv_host = SERVER_HOST
            scene.usd_connect_recv_port = SERVER_PORT
            scene.usd_connect_recv_last_seq = 0
            try:
                bpy.ops.usd_connect.start_receiver()
            except Exception as e:
                log(f"  Receiver failed: {e}")
            _step = 2
            return 2.0

        elif _step == 2:
            log("Step 2: Sending first teapot at /World/Teapot...")
            send_cli_events([
                {"k": "ensure_prim", "prim": "/World/Teapot", "typeName": "Xform"},
                {"k": "ensure_xform_ops", "prim": "/World/Teapot"},
                {"k": "set_xform_trs", "prim": "/World/Teapot",
                 "fields": ["t", "r", "s"], "t": [0, 0, 0], "r": [1, 0, 0, 0], "s": [1, 1, 1]},
                {"k": "set_payload", "prim": "/World/Teapot",
                 "payloads": [{"asset_path": TEAPOT_ASSET, "prim_path": "/Teapot"}]},
                {"k": "load_payload", "prim": "/World/Teapot"},
            ])
            _step = 3
            return 5.0

        elif _step == 3:
            log("Step 3: Sending second teapot at /Teapot...")
            send_cli_events([
                {"k": "ensure_prim", "prim": "/Teapot", "typeName": "Xform"},
                {"k": "ensure_xform_ops", "prim": "/Teapot"},
                {"k": "set_xform_trs", "prim": "/Teapot",
                 "fields": ["t", "r", "s"], "t": [3, 0, 0], "r": [1, 0, 0, 0], "s": [1, 1, 1]},
                {"k": "set_payload", "prim": "/Teapot",
                 "payloads": [{"asset_path": TEAPOT_ASSET, "prim_path": "/Teapot"}]},
                {"k": "load_payload", "prim": "/Teapot"},
            ])
            _step = 4
            return 5.0

        elif _step == 4:
            log("Step 4: Checking material state...")

            # List all materials with usd_material_path
            for m in bpy.data.materials:
                mp = m.get("usd_material_path", "")
                node_count = len(m.node_tree.nodes) if m.node_tree else 0
                bc_info = "N/A"
                if m.node_tree:
                    for n in m.node_tree.nodes:
                        if n.type == "BSDF_PRINCIPLED" and "Base Color" in n.inputs:
                            bc = n.inputs["Base Color"]
                            bc_info = f"linked={bc.is_linked} val={bc.default_value[:]}"
                log(f"  Material: {m.name}")
                log(f"    usd_material_path: {mp}")
                log(f"    nodes: {node_count}")
                log(f"    Base Color: {bc_info}")

            # Check objects and their material assignments
            log("")
            for obj in bpy.data.objects:
                if not obj.data or not hasattr(obj.data, "materials"):
                    continue
                mats = [m.name for m in obj.data.materials if m]
                if mats:
                    log(f"  Object: {obj.name} -> materials: {mats}")

            # Verify: each teapot path should have its own Ceramic material
            ceramic_mats = [m for m in bpy.data.materials if "Ceramic" in m.name]
            log("")
            log(f"  Ceramic materials found: {[m.name for m in ceramic_mats]}")
            paths = [m.get("usd_material_path", "") for m in ceramic_mats]
            log(f"  Their paths: {paths}")

            # Check if both have Base Color linked
            all_linked = True
            for m in ceramic_mats:
                if not m.node_tree:
                    all_linked = False
                    continue
                for n in m.node_tree.nodes:
                    if n.type == "BSDF_PRINCIPLED" and "Base Color" in n.inputs:
                        if not n.inputs["Base Color"].is_linked:
                            log(f"  PROBLEM: {m.name} Base Color is NOT linked!")
                            all_linked = False

            if len(ceramic_mats) >= 2 and all_linked:
                log("  SUCCESS: Both teapots have separate Ceramic materials with linked Base Color")
            elif len(ceramic_mats) < 2:
                _retries += 1
                if _retries > 10:
                    log("  FAIL: Not enough Ceramic materials after 10 retries")
                    _step = 99
                    return 1.0
                log(f"  Waiting... (retry {_retries})")
                return 2.0
            else:
                log("  FAIL: Base Color connection lost on one or more materials")

            _step = 99
            return 1.0

        elif _step == 99:
            log("Done. Quitting Blender.")
            bpy.ops.wm.quit_blender()
            return None

    except Exception as e:
        import traceback
        log(f"ERROR in step {_step}: {e}")
        traceback.print_exc()
        _step = 99
        return 1.0


try:
    probe = _socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=1)
    probe.close()
    log(f"Server reachable at {SERVER_HOST}:{SERVER_PORT}")
    bpy.app.timers.register(_test_step, first_interval=2.0)
except OSError:
    log(f"ERROR: Server not reachable at {SERVER_HOST}:{SERVER_PORT}")
