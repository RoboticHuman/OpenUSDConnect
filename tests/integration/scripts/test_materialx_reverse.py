"""E2E test: multi-node MaterialX shader reverse path.

Run with:
  BLENDER_USER_RESOURCES=".blender/user_data" \
  .blender/blender-5.0.1-windows-x64/blender.exe \
  --python tests/integration/scripts/test_materialx_reverse.py

Tests:
  1. Import a MaterialX asset via set_reference
  2. Verify enrichment creates multi-node network
  3. Verify shader input maps are seeded
  4. Change a socket value programmatically
  5. Check if depsgraph fires and event reaches server DB
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
    PROJECT_ROOT, "assets", "test_assets", "MaterialXTest", "basicTextured.usda"
).replace("\\", "/")


def log(msg):
    print(f"[MTLx-REVERSE] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Server management
# ---------------------------------------------------------------------------

_server_proc = None


import glob
import subprocess

_server_proc = None


def _cleanup_db():
    """Remove test DB and WAL/SHM files."""
    for f in glob.glob(os.path.join(PROJECT_ROOT, "test_mtlx_reverse.db*")):
        try:
            os.remove(f)
        except OSError:
            pass


def start_server():
    """Start the server in a detached process with a clean DB."""
    global _server_proc
    _cleanup_db()
    # Use cmd /c to detach from Blender's env so pxr DLLs resolve correctly.
    _server_proc = subprocess.Popen(
        ["cmd", "/c", "uv", "run", "python", "-m", "openusdconnect.server",
         "--host", SERVER_HOST, "--port", str(SERVER_PORT), "--event-log", DB_PATH],
        cwd=PROJECT_ROOT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(20):
        import time
        time.sleep(0.5)
        try:
            probe = _socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=1)
            probe.close()
            log(f"Server started (pid={_server_proc.pid})")
            return True
        except OSError:
            continue
    log("ERROR: Server failed to start")
    return False


def stop_server():
    """Kill the server process and clean up DB files."""
    global _server_proc
    if _server_proc:
        try:
            _server_proc.terminate()
            _server_proc.wait(timeout=5)
        except Exception:
            try:
                _server_proc.kill()
            except Exception:
                pass
        _server_proc = None
        log("Server stopped")
    _cleanup_db()


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
            results.append((seq, evt))
        return results
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Timer-based test steps
# ---------------------------------------------------------------------------

_step = 0
_retries = 0
_initial_event_count = 0


def _test_step():
    global _step, _retries, _initial_event_count

    try:
        if _step == 0:
            # Install addon
            log("Step 0: Installing addon...")
            bpy.ops.preferences.addon_install(filepath=ADDON_ZIP, overwrite=True)
            bpy.ops.preferences.addon_enable(module="usd_connect")
            log("  Addon installed and enabled")
            _step = 1
            return 1.0

        elif _step == 1:
            # Start emitter + receiver
            log("Step 1: Connecting emitter + receiver...")
            scene = bpy.context.scene
            scene.usd_connect_base_usd_path = BASE_USD
            scene.usd_connect_auto_track = True
            scene.usd_connect_emit_host = SERVER_HOST
            scene.usd_connect_emit_port = SERVER_PORT

            try:
                bpy.ops.usd_connect.connect_emitter()
                log("  Emitter started")
            except Exception as e:
                log(f"  Emitter failed: {e}")

            scene.usd_connect_recv_host = SERVER_HOST
            scene.usd_connect_recv_port = SERVER_PORT
            scene.usd_connect_recv_last_seq = 0
            try:
                bpy.ops.usd_connect.start_receiver()
                log("  Receiver started")
            except Exception as e:
                log(f"  Receiver failed: {e}")

            _step = 2
            return 2.0

        elif _step == 2:
            # Enable DEBUG logging for the adapter so we see the full flow
            import logging
            logging.getLogger("usd_connect.blender_adapter").setLevel(logging.DEBUG)
            logging.getLogger("usd_connect.receiver_addon").setLevel(logging.DEBUG)
            logging.getLogger("usd_connect.capture").setLevel(logging.DEBUG)

            # Send MaterialX asset
            log("Step 2: Sending MaterialX asset via CLI...")
            send_cli_events([
                {"k": "ensure_prim", "prim": "/World/Teapot", "typeName": "Xform"},
                {
                    "k": "set_reference",
                    "prim": "/World/Teapot",
                    "refs": [
                        {"asset_path": ASSET_PATH, "prim_path": "/Teapot"},
                    ],
                },
            ])
            log(f"  Sent set_reference for {ASSET_PATH}")
            _step = 3
            return 5.0  # Wait for import + enrichment + seeding

        elif _step == 3:
            # Verify import + enrichment
            log("Step 3: Checking import results...")

            # Check objects
            objs = [o.name for o in bpy.data.objects if "Teapot" in o.name or "teapot" in o.name]
            log(f"  Objects with 'Teapot/teapot': {objs}")

            # Check materials
            mats = []
            for m in bpy.data.materials:
                mp = m.get("usd_material_path", "")
                mats.append(f"{m.name} (usd_material_path={mp})")
            log(f"  Materials: {mats}")

            # Find the Tiled_Brass material
            brass_mat = None
            for m in bpy.data.materials:
                if "Brass" in m.name or "brass" in m.name.lower():
                    brass_mat = m
                    break
                mp = m.get("usd_material_path", "")
                if "Brass" in mp:
                    brass_mat = m
                    break

            if brass_mat is None:
                _retries += 1
                if _retries > 10:
                    log("  FAIL: Tiled_Brass material never appeared")
                    _step = 99
                    return 1.0
                log(f"  Waiting for material... (retry {_retries})")
                return 2.0

            log(f"  Found material: {brass_mat.name}")
            if brass_mat.node_tree:
                for node in brass_mat.node_tree.nodes:
                    tags = {k: v for k, v in node.items() if k.startswith("usd_")}
                    stype = node.type
                    log(f"    Node: {node.name} type={stype} tags={tags}")

            # Check emitter state
            from usd_connect.capture import _state
            if _state.author:
                log(f"  Emitter enabled: {_state.author.enabled}")
                maps = list(_state.author._shader_input_maps.keys())
                log(f"  _shader_input_maps keys: {maps}")
                baselines = list(_state.author._last_shader_values.keys())
                log(f"  _last_shader_values keys: {baselines}")
                for k, v in _state.author._last_shader_values.items():
                    log(f"    {k}: {list(v.keys())}")
            else:
                log("  WARNING: author is None!")

            _initial_event_count = len(query_db_events("set_connectable_input"))
            log(f"  Initial set_shader_input events in DB: {_initial_event_count}")

            _step = 4
            _retries = 0
            return 1.0

        elif _step == 4:
            # Change a socket value on the multi-node material
            log("Step 4: Changing Metallic on multi-node material...")

            brass_mat = None
            for m in bpy.data.materials:
                if "Brass" in m.name or "brass" in m.name.lower():
                    brass_mat = m
                    break

            if not brass_mat or not brass_mat.node_tree:
                log("  FAIL: no brass material to modify")
                _step = 99
                return 1.0

            bsdf = None
            for node in brass_mat.node_tree.nodes:
                if node.type == "BSDF_PRINCIPLED":
                    bsdf = node
                    break

            if not bsdf:
                log("  FAIL: no Principled BSDF in brass material")
                _step = 99
                return 1.0

            # Ensure the material is used by a scene object — depsgraph
            # only tracks materials assigned to visible geometry.
            # The enrichment binding fails because the USD material path
            # is outside the variant scope.  Assign manually for testing.
            for obj in bpy.data.objects:
                if obj.type == 'MESH' and obj.data and not obj.data.materials:
                    obj.data.materials.append(brass_mat)
                    log(f"  Assigned {brass_mat.name} to {obj.name}")
                    break
            else:
                # Fallback: assign to the default Cube
                cube = bpy.data.objects.get("Cube")
                if cube and cube.data:
                    if not cube.data.materials:
                        cube.data.materials.append(brass_mat)
                    else:
                        cube.data.materials[0] = brass_mat
                    log(f"  Assigned {brass_mat.name} to Cube (fallback)")

            old_val = bsdf.inputs["Metallic"].default_value
            new_val = 0.42
            log(f"  Metallic: {old_val} -> {new_val}")

            # Install a one-shot depsgraph probe to see what fires
            def _probe(scene, depsgraph):
                updates = list(depsgraph.updates)
                types = [type(u.id).__name__ for u in updates]
                log(f"  [PROBE] depsgraph fired with {len(updates)} updates: {types}")
                for u in updates:
                    log(f"    {type(u.id).__name__}: {u.id.name}")
                bpy.app.handlers.depsgraph_update_post.remove(_probe)
            bpy.app.handlers.depsgraph_update_post.append(_probe)

            # Change the socket inside an operator — operators trigger the
            # full depsgraph evaluation pipeline after execute() returns,
            # unlike timer callbacks which don't.
            class _ForceUpdate(bpy.types.Operator):
                bl_idname = "test.force_socket_update"
                bl_label = "Test"
                def execute(self_, context):
                    bsdf.inputs["Metallic"].default_value = new_val
                    return {'FINISHED'}
            bpy.utils.register_class(_ForceUpdate)
            bpy.ops.test.force_socket_update()
            bpy.utils.unregister_class(_ForceUpdate)

            _step = 5
            return 3.0  # Wait for depsgraph + emitter + server round-trip

        elif _step == 5:
            # Check if event reached the server
            log("Step 5: Checking for set_shader_input event in DB...")
            shader_events = query_db_events("set_connectable_input")
            new_count = len(shader_events) - _initial_event_count
            log(f"  New set_shader_input events: {new_count}")

            if new_count > 0:
                for seq, evt in shader_events[_initial_event_count:]:
                    inner = evt.get("event", evt)
                    log(f"    seq={seq}: prim={inner.get('prim')} inputs={inner.get('inputs')}")
                log("  SUCCESS: Multi-node reverse path works!")
                _step = 99
                return 1.0

            _retries += 1
            if _retries > 10:
                log("  FAIL: No set_shader_input event after 10 retries")
                # Dump diagnostic info
                from usd_connect.capture import _state
                if _state.author:
                    maps = list(_state.author._shader_input_maps.keys())
                    log(f"  Final _shader_input_maps: {maps}")
                    baselines = list(_state.author._last_shader_values.keys())
                    log(f"  Final _last_shader_values: {baselines}")
                all_events = query_db_events()
                log(f"  Total events in DB: {len(all_events)}")
                for seq, evt in all_events[-5:]:
                    inner = evt.get("event", evt)
                    log(f"    seq={seq}: k={inner.get('k')} prim={inner.get('prim')}")
                _step = 99
                return 1.0

            log(f"  Waiting... (retry {_retries})")
            return 2.0

        elif _step == 99:
            stop_server()
            log("Test complete. Quitting Blender.")
            bpy.ops.wm.quit_blender()
            return None

    except Exception as e:
        import traceback
        log(f"ERROR in step {_step}: {e}")
        traceback.print_exc()
        stop_server()
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# Check if server is already running (started externally).
# If not, try to start it (may fail from Blender's env due to pxr DLL conflicts).
try:
    probe = _socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=1)
    probe.close()
    log(f"Server already running at {SERVER_HOST}:{SERVER_PORT}")
    bpy.app.timers.register(_test_step, first_interval=2.0)
except OSError:
    if start_server():
        bpy.app.timers.register(_test_step, first_interval=2.0)
    else:
        log("Aborting — server failed to start. Start it externally first:")
        log(
            "  uv run python -m openusdconnect.server "
            f"--port {SERVER_PORT} --event-log test_mtlx_reverse.db"
        )
