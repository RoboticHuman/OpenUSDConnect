"""E2E test: multi-node reverse path with intent-vfx simpleAsset.

Tests dual MaterialX standard_surface shaders (proxy + render) with
materials in a separate /mtl scope from /geo geometry.

Requires server running externally on port 7202.
Auto-quits Blender when done.
"""

import json
import os
import socket as _socket
import sqlite3
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import bpy

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 7202
DB_PATH = os.path.join(PROJECT_ROOT, "test_mtlx_reverse.db")
BASE_USD = os.path.join(PROJECT_ROOT, "test_scene.usda")
ADDON_ZIP = os.path.join(PROJECT_ROOT, "dist", "usd_connect_blender.zip")
ASSET_PATH = os.path.join(
    PROJECT_ROOT, "assets", "intent-vfx", "assets", "simpleAsset", "simpleAsset.usd"
).replace("\\", "/")


def log(msg):
    print(f"[SIMPLE-E2E] {msg}", flush=True)


def send_cli_events(events):
    from openusdconnect.protocol import make_hello
    from openusdconnect.transport import send_line
    s = _socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=5)
    send_line(s, make_hello("emitter", client_id="e2e_cli"))
    send_line(s, {"type": "txn", "client_id": "e2e_cli", "events": events})
    s.close()


def query_db_events(kind_filter=None):
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("SELECT seq, event FROM events ORDER BY seq").fetchall()
        results = []
        for seq, evt_str in rows:
            evt = json.loads(evt_str)
            inner = evt.get("event", evt)
            if kind_filter and inner.get("k") != kind_filter:
                continue
            results.append((seq, inner))
        return results
    finally:
        conn.close()


_step = 0
_retries = 0
_initial_event_count = 0


def _test_step():
    global _step, _retries, _initial_event_count

    try:
        if _step == 0:
            log("Step 0: Installing addon...")
            bpy.ops.preferences.addon_install(filepath=ADDON_ZIP, overwrite=True)
            bpy.ops.preferences.addon_enable(module="usd_connect")
            log("  Done")
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
            log("  Done")
            _step = 2
            return 2.0

        elif _step == 2:
            log("Step 2: Sending simpleAsset via CLI...")
            send_cli_events([
                {"k": "ensure_prim", "prim": "/World/Simple", "typeName": "Xform"},
                {
                    "k": "set_reference",
                    "prim": "/World/Simple",
                    "refs": [
                        {"asset_path": ASSET_PATH, "prim_path": "/simpleAsset"},
                    ],
                },
            ])
            log(f"  Sent set_reference for {ASSET_PATH}")
            _step = 3
            return 5.0

        elif _step == 3:
            log("Step 3: Checking import + enrichment...")

            # Objects
            objs = [(o.name, o.type) for o in bpy.data.objects if "imple" in o.name or "Shape" in o.name]
            log(f"  Objects: {objs}")

            # Materials
            mats = []
            for m in bpy.data.materials:
                mp = m.get("usd_material_path", "")
                nodes_info = []
                if m.node_tree:
                    for n in m.node_tree.nodes:
                        sid = n.get("usd_info_id", "")
                        if sid:
                            nodes_info.append(f"{n.name}:{sid}")
                mats.append(f"{m.name} (path={mp}, shaders={nodes_info})")
            log(f"  Materials ({len(mats)}):")
            for m in mats:
                log(f"    {m}")

            # Check for proxy_material with ND_standard_surface
            found_mtlx = False
            for m in bpy.data.materials:
                if "proxy" in m.name.lower() or "render" in m.name.lower():
                    if m.node_tree:
                        for n in m.node_tree.nodes:
                            if n.get("usd_info_id") == "ND_standard_surface_surfaceshader":
                                found_mtlx = True
                                break
                if found_mtlx:
                    break

            if not found_mtlx:
                _retries += 1
                if _retries > 10:
                    log("  FAIL: No MaterialX shader found after 10 retries")
                    _step = 99
                    return 1.0
                log(f"  Waiting... (retry {_retries})")
                return 2.0

            # Check emitter state
            from usd_connect.capture import _state
            if _state.author:
                maps = list(_state.author._shader_input_maps.keys())
                log(f"  _shader_input_maps: {maps}")
                baselines = list(_state.author._last_shader_values.keys())
                log(f"  _last_shader_values: {baselines}")
            else:
                log("  WARNING: author is None!")

            # Check material assignment
            for obj in bpy.data.objects:
                if obj.data and hasattr(obj.data, 'materials') and obj.data.materials:
                    mat_names = [m.name for m in obj.data.materials if m]
                    if mat_names:
                        log(f"  {obj.name} has materials: {mat_names}")

            _initial_event_count = len(query_db_events("set_connectable_input"))
            log(f"  Initial set_shader_input events: {_initial_event_count}")

            _step = 4
            _retries = 0
            return 1.0

        elif _step == 4:
            log("Step 4: Changing shader value via operator...")

            # Find a MaterialX shader node on a material that's assigned
            # to a mesh — orphan materials aren't tracked by depsgraph.
            assigned_mats = set()
            for obj in bpy.data.objects:
                if obj.data and hasattr(obj.data, "materials"):
                    for mat in obj.data.materials:
                        if mat:
                            assigned_mats.add(mat.name)

            target_mat = None
            target_bsdf = None
            for m in bpy.data.materials:
                if m.name not in assigned_mats or not m.node_tree:
                    continue
                for n in m.node_tree.nodes:
                    if n.get("usd_info_id") == "ND_standard_surface_surfaceshader" and n.type == "BSDF_PRINCIPLED":
                        target_mat = m
                        target_bsdf = n
                        break
                if target_bsdf:
                    break

            if not target_bsdf:
                log("  FAIL: No Principled BSDF with MaterialX shader tag found")
                _step = 99
                return 1.0

            log(f"  Target: {target_mat.name} / {target_bsdf.name}")
            old_val = target_bsdf.inputs["Metallic"].default_value
            new_val = 0.77
            log(f"  Metallic: {old_val} -> {new_val}")

            class _ForceUpdate(bpy.types.Operator):
                bl_idname = "test.force_socket_update_simple"
                bl_label = "Test"
                def execute(self_, context):
                    target_bsdf.inputs["Metallic"].default_value = new_val
                    return {'FINISHED'}
            bpy.utils.register_class(_ForceUpdate)
            bpy.ops.test.force_socket_update_simple()
            bpy.utils.unregister_class(_ForceUpdate)

            _step = 5
            return 3.0

        elif _step == 5:
            log("Step 5: Checking for set_shader_input in DB...")
            shader_events = query_db_events("set_connectable_input")
            new_count = len(shader_events) - _initial_event_count
            log(f"  New set_shader_input events: {new_count}")

            if new_count > 0:
                for seq, inner in shader_events[_initial_event_count:]:
                    log(f"    seq={seq}: prim={inner.get('prim')} inputs={inner.get('inputs')}")
                log("  SUCCESS!")
                _step = 99
                return 1.0

            _retries += 1
            if _retries > 10:
                log("  FAIL: No set_shader_input event after 10 retries")
                from usd_connect.capture import _state
                if _state.author:
                    log(f"  Final _shader_input_maps: {list(_state.author._shader_input_maps.keys())}")
                all_events = query_db_events()
                log(f"  Total events in DB: {len(all_events)}")
                for seq, inner in all_events[-5:]:
                    log(f"    seq={seq}: k={inner.get('k')} prim={inner.get('prim')}")
                _step = 99
                return 1.0

            return 2.0

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


# Entry point
try:
    probe = _socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=1)
    probe.close()
    log(f"Server reachable at {SERVER_HOST}:{SERVER_PORT}")
    bpy.app.timers.register(_test_step, first_interval=2.0)
except OSError:
    log("ERROR: Server not reachable. Start it first:")
    log(f"  uv run python -m openusdconnect.server --port {SERVER_PORT} --log test_mtlx_reverse.db")
