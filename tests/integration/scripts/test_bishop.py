"""E2E test: Bishop asset multi-node MaterialX with diffuse texture.

Checks that the Bishop's ND_standard_surface shader gets enriched
with proper node network including texture connections.
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
BISHOP_ASSET = os.path.abspath(
    os.path.join(PROJECT_ROOT, "assets", "full_assets", "OpenChessSet", "assets", "Bishop", "Bishop.usd")
).replace("\\", "/")


def log(msg):
    print(f"[BISHOP] {msg}", flush=True)


def send_cli_events(events):
    global _cli_txn_id
    from openusdconnect.protocol import make_hello
    from openusdconnect.transport import send_line
    s = _socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=5)
    send_line(s, make_hello(
        "emitter", client_id="e2e_cli", producer_session_id="bishop-e2e"))
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
            log("Step 1: Connecting...")
            scene = bpy.context.scene
            scene.usd_connect_base_usd_path = BASE_USD
            scene.usd_connect_auto_track = True
            scene.usd_connect_emit_host = SERVER_HOST
            scene.usd_connect_emit_port = SERVER_PORT
            try:
                bpy.ops.usd_connect.connect_emitter()
            except Exception as e:
                log(f"  Emitter: {e}")
            scene.usd_connect_recv_host = SERVER_HOST
            scene.usd_connect_recv_port = SERVER_PORT
            scene.usd_connect_recv_last_seq = 0
            try:
                bpy.ops.usd_connect.start_receiver()
            except Exception as e:
                log(f"  Receiver: {e}")
            _step = 2
            return 2.0

        elif _step == 2:
            log("Step 2: Sending Bishop...")
            send_cli_events([
                {"k": "ensure_prim", "prim": "/World/Bishop", "typeName": "Xform"},
                {"k": "set_reference", "prim": "/World/Bishop",
                 "refs": [{"asset_path": BISHOP_ASSET, "prim_path": "/Bishop"}]},
            ])
            _step = 3
            return 8.0

        elif _step == 3:
            log("Step 3: Checking Bishop materials...")

            # List all objects
            objs = [(o.name, o.type, o.parent.name if o.parent else None)
                    for o in bpy.data.objects if "Bishop" in o.name or "Render" in o.name or "Geom" in o.name]
            log(f"  Objects: {objs}")

            # List materials with shader info
            for m in bpy.data.materials:
                mp = m.get("usd_material_path", "")
                if not mp and "Bishop" not in m.name and "M_Bishop" not in m.name:
                    continue
                log(f"  Material: {m.name} (path={mp})")
                if m.node_tree:
                    for n in m.node_tree.nodes:
                        sid = n.get("usd_info_id", "")
                        node_info = f"    Node: {n.name} type={n.type}"
                        if sid:
                            node_info += f" info_id={sid}"
                        # Check Base Color connection
                        if n.type == "BSDF_PRINCIPLED" and "Base Color" in n.inputs:
                            bc = n.inputs["Base Color"]
                            node_info += f" BaseColor(linked={bc.is_linked})"
                        # Check if texture node has image loaded
                        if n.type == "TEX_IMAGE":
                            img = n.image
                            node_info += f" image={'loaded' if img else 'MISSING'}"
                        log(node_info)

            # Trace the Base Color connection chain for M_Bishop_B
            bishop_mat = bpy.data.materials.get("M_Bishop_B")
            if bishop_mat and bishop_mat.node_tree:
                tree = bishop_mat.node_tree
                bsdf = None
                for n in tree.nodes:
                    if n.type == "BSDF_PRINCIPLED":
                        bsdf = n
                        break
                if bsdf:
                    bc = bsdf.inputs["Base Color"]
                    log("  --- Base Color connection chain ---")
                    log(f"  BSDF Base Color linked={bc.is_linked}")
                    if bc.is_linked:
                        link = bc.links[0]
                        src = link.from_node
                        log(f"  Connected from: {src.name} ({src.type}) output={link.from_socket.name}")
                        # Follow chain deeper
                        visited = set()
                        queue = [src]
                        while queue:
                            node = queue.pop(0)
                            if node.name in visited:
                                continue
                            visited.add(node.name)
                            for inp in node.inputs:
                                if inp.is_linked:
                                    upstream = inp.links[0].from_node
                                    log(f"    {node.name}.{inp.name} <- {upstream.name} ({upstream.type})")
                                    if upstream.type == "TEX_IMAGE":
                                        img = upstream.image
                                        if img:
                                            log(f"      Image: {img.name} file={img.filepath} size={img.size[:]}")
                                        else:
                                            log("      Image: MISSING")
                                    queue.append(upstream)

            # Check emitter state for shader maps
            from usd_connect.capture import _state
            if _state.author:
                maps = [k for k in _state.author._shader_input_maps.keys() if "Bishop" in k]
                log(f"  Shader input maps: {maps}")

            # Check material assignments
            for obj in bpy.data.objects:
                if not obj.data or not hasattr(obj.data, "materials"):
                    continue
                mats = [m.name for m in obj.data.materials if m]
                if mats and ("Bishop" in obj.name or "Render" in obj.name or "Geom" in obj.name):
                    log(f"  {obj.name} -> {mats}")

            _step = 99
            return 1.0

        elif _step == 99:
            log("Done. Quitting.")
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
    log(f"Server OK at {SERVER_HOST}:{SERVER_PORT}")
    bpy.app.timers.register(_test_step, first_interval=2.0)
except OSError:
    log("Server not reachable")
