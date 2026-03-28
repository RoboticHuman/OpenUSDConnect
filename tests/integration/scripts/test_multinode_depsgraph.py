"""E2E test: multi-node shader depsgraph tracking.

Run with:
  BLENDER_USER_RESOURCES=".blender/user_data" \
  .blender/blender-5.0.1-windows-x64/blender.exe \
  --python tests/integration/scripts/test_multinode_depsgraph.py

Installs the addon, starts server connection, sends a Bishop asset,
modifies a multi-node material socket, and checks if the emitter fires.
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
SERVER_PORT = 7200
DB_PATH = os.path.join(PROJECT_ROOT, "usd_events.db")
BASE_USD = os.path.join(PROJECT_ROOT, "test_scene.usda")
ADDON_ZIP = os.path.join(PROJECT_ROOT, "dist", "usd_connect_blender.zip")


def log(msg):
    print(f"[E2E-MULTINODE] {msg}", flush=True)


def send_cli_events(events):
    from openusdconnect.protocol import make_hello
    from openusdconnect.transport import send_line
    s = _socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=5)
    send_line(s, make_hello('emitter', client_id='e2e_cli'))
    send_line(s, {'type': 'txn', 'client_id': 'e2e_cli', 'events': events})
    s.close()


def query_shader_events():
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT seq, event FROM events WHERE event LIKE '%set_shader_input%' ORDER BY seq"
        ).fetchall()
        return [(seq, json.loads(evt)) for seq, evt in rows]
    finally:
        conn.close()


def get_total_event_count():
    if not os.path.exists(DB_PATH):
        return 0
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Timer-based test steps
# ---------------------------------------------------------------------------

_step = 0
_initial_shader_event_count = 0
_retries = 0


def _test_step():
    global _step, _initial_shader_event_count, _retries

    try:
        if _step == 0:
            # Step 0: Install addon
            log("Step 0: Installing addon...")
            bpy.ops.preferences.addon_install(filepath=ADDON_ZIP, overwrite=True)
            bpy.ops.preferences.addon_enable(module="usd_connect")
            log("  Addon installed")
            _step = 1
            return 1.0

        elif _step == 1:
            # Step 1: Set base path and start emitter + receiver
            log("Step 1: Starting emitter + receiver...")
            scene = bpy.context.scene
            scene.usd_connect_base_usd_path = BASE_USD
            scene.usd_connect_auto_track = True

            # Start emitter
            try:
                bpy.ops.usd_connect.connect_emitter()
                log("  Emitter started")
            except Exception as e:
                log(f"  Emitter failed: {e}")

            # Start receiver
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
            # Step 2: Send Bishop via CLI
            log("Step 2: Sending Bishop asset...")
            send_cli_events([
                {'k': 'ensure_prim', 'prim': '/World/Bishop2', 'typeName': 'Xform'},
                {'k': 'ensure_xform_ops', 'prim': '/World/Bishop2'},
                {'k': 'set_xform_trs', 'prim': '/World/Bishop2', 'fields': ['t', 'r', 's'],
                 't': [5, 0, 0], 'r': [1, 0, 0, 0], 's': [1, 1, 1]},
                {'k': 'set_reference', 'prim': '/World/Bishop2',
                 'refs': [{'asset_path': './assets/test_assets/MaterialXTest/basicTextured.usda',
                           'prim_path': '/MaterialXTest'}]},
            ])
            _step = 3
            return 8.0  # Wait for import + enrichment + cache seeding

        elif _step == 3:
            # Step 3: Verify Bishop material exists
            log("Step 3: Checking for Bishop material...")
            mat = None
            for m in bpy.data.materials:
                mp = m.get("usd_material_path", "")
                if "Bishop" in m.name or "Bishop" in mp:
                    mat = m
                    break

            if mat is None:
                log("  Materials found:")
                for m in bpy.data.materials:
                    log(f"    {m.name} usd_material_path={m.get('usd_material_path', 'N/A')}")
                _retries += 1
                if _retries > 10:
                    log("  FAIL: Bishop material never appeared after 10 retries")
                    return None
                log(f"  Waiting... (retry {_retries})")
                return 3.0

            log(f"  Found material: {mat.name}")
            if mat.node_tree:
                for node in mat.node_tree.nodes:
                    tags = {k: v for k, v in node.items() if k.startswith("usd_")}
                    log(f"    Node: {node.name} type={node.type} tags={tags}")

            # Check emitter state
            from usd_connect.capture import _state
            if _state.author:
                log(f"  Emitter enabled: {_state.author.enabled}")
                log(f"  _shader_input_maps: {list(_state.author._shader_input_maps.keys())}")
                log(f"  _last_shader_values: {list(_state.author._last_shader_values.keys())}")
            else:
                log("  WARNING: author is None!")

            _initial_shader_event_count = len(query_shader_events())
            log(f"  Initial shader events in DB: {_initial_shader_event_count}")

            _step = 4
            _retries = 0
            return 1.0

        elif _step == 4:
            # Step 4: First test — does depsgraph fire at all for programmatic
            # socket changes from a timer? Test with a default material.
            log("Step 4: Testing if depsgraph fires for ANY material change from timer...")

            # Create a test material and change its roughness
            test_mat = bpy.data.materials.get("__depsgraph_test__")
            if not test_mat:
                test_mat = bpy.data.materials.new("__depsgraph_test__")
                test_mat.use_nodes = True

            bsdf = None
            for node in test_mat.node_tree.nodes:
                if node.type == "BSDF_PRINCIPLED":
                    bsdf = node
                    break

            if bsdf:
                log(f"  Changing test material Roughness: {bsdf.inputs['Roughness'].default_value} -> 0.777")
                bsdf.inputs["Roughness"].default_value = 0.777
                # Force depsgraph evaluation
                bpy.context.view_layer.update()

            # Install a one-shot depsgraph probe
            _test_probe = {"fired": False, "material_ids": []}

            def _probe_handler(scene, depsgraph):
                for update in depsgraph.updates:
                    if isinstance(update.id, bpy.types.Material):
                        _test_probe["fired"] = True
                        _test_probe["material_ids"].append(update.id.name)

            bpy.app.handlers.depsgraph_update_post.append(_probe_handler)
            # Store probe for next step
            import builtins
            builtins._e2e_probe = _test_probe
            builtins._e2e_probe_handler = _probe_handler

            _step = 40
            return 0.5

        elif _step == 40:
            # Check if the probe fired
            import builtins
            probe = builtins._e2e_probe
            handler = builtins._e2e_probe_handler
            log(f"  Probe result: fired={probe['fired']}, materials={probe['material_ids']}")

            # Now change the Tiled_Brass material
            mat = None
            for m in bpy.data.materials:
                if "Tiled_Brass" in m.name:
                    mat = m
                    break

            if mat and mat.node_tree:
                bsdf = None
                for node in mat.node_tree.nodes:
                    if node.type == "BSDF_PRINCIPLED":
                        bsdf = node
                        break
                if bsdf:
                    probe["fired"] = False
                    probe["material_ids"] = []
                    old_val = bsdf.inputs["Roughness"].default_value
                    log(f"  Changing Tiled_Brass Roughness: {old_val} -> 0.123")
                    bsdf.inputs["Roughness"].default_value = 0.123
                    # Force depsgraph evaluation
                    bpy.context.view_layer.update()

            _step = 41
            return 0.5

        elif _step == 41:
            # Check probe for Tiled_Brass
            import builtins
            probe = builtins._e2e_probe
            handler = builtins._e2e_probe_handler
            log(f"  Tiled_Brass probe: fired={probe['fired']}, materials={probe['material_ids']}")

            # Clean up probe
            try:
                bpy.app.handlers.depsgraph_update_post.remove(handler)
            except ValueError:
                pass

            _step = 5
            return 3.0

        elif _step == 44:
            # Original step 4 — now skipped, we already changed roughness in step 40
            log("Step 4 (original): Changing roughness on multi-node material...")
            mat = None
            for m in bpy.data.materials:
                mp = m.get("usd_material_path", "")
                if "Bishop" in m.name or "Bishop" in mp or "Tiled_Brass" in m.name:
                    mat = m
                    break

            if not mat or not mat.node_tree:
                log("FAIL: Material or node tree not found")
                return None

            bsdf = None
            for node in mat.node_tree.nodes:
                if node.type == "BSDF_PRINCIPLED":
                    bsdf = node
                    break

            if not bsdf:
                log("FAIL: No Principled BSDF")
                for node in mat.node_tree.nodes:
                    log(f"  Node: {node.name} type={node.type}")
                return None

            old_val = bsdf.inputs["Roughness"].default_value
            new_val = 0.123
            log(f"  Changing Roughness: {old_val} -> {new_val}")
            bsdf.inputs["Roughness"].default_value = new_val

            _step = 5
            return 5.0

        elif _step == 5:
            # Step 5: Check if depsgraph is even firing for Material updates
            log("Step 5: Checking depsgraph handler state...")
            from usd_connect.capture import _state

            # Check if handler is registered
            handlers = bpy.app.handlers.depsgraph_update_post
            handler_found = any("_depsgraph_handler" in str(h) for h in handlers)
            log(f"  Depsgraph handler registered: {handler_found}")
            log(f"  Number of handlers: {len(handlers)}")

            # Try triggering depsgraph manually by tagging the material
            mat = None
            for m in bpy.data.materials:
                if "Tiled_Brass" in m.name:
                    mat = m
                    break
            if mat:
                mat.update_tag()
                mat.node_tree.update_tag()
                log("  Tagged material + node_tree for update")

            _step = 6
            return 3.0  # Wait for depsgraph to process

        elif _step == 6:
            # Step 6: Check results
            log("Step 6: Checking for shader events in DB...")
            events = query_shader_events()
            new_events = events[_initial_shader_event_count:]

            if new_events:
                log(f"  *** SUCCESS *** {len(new_events)} new set_shader_input event(s):")
                for seq, evt in new_events:
                    # Just show key info
                    inputs = evt.get('inputs', {})
                    log(f"    seq={seq} prim={evt.get('prim','?')} inputs={inputs}")
            else:
                log("  *** FAIL *** No new shader events found")
                total = get_total_event_count()
                log(f"  Total events in DB: {total}")

                from usd_connect.capture import _state
                if _state.author:
                    log(f"  author.enabled={_state.author.enabled}")
                    maps = _state.author._shader_input_maps
                    log(f"  _shader_input_maps: {list(maps.keys())}")
                    baselines = _state.author._last_shader_values
                    log(f"  _last_shader_values: {list(baselines.keys())}")

                    # Try manual read
                    for sp, im in maps.items():
                        sid_key = sp + ":shader_id"
                        # We know it's ND_standard_surface_surfaceshader
                        mapper = _state.author._shader_registry.get("ND_standard_surface_surfaceshader")
                        if mapper:
                            vals = mapper.read_all_inputs(input_map=im)
                            log(f"  Manual read for {sp}: roughness={vals.get('specular_roughness', 'N/A')}")
                            if sp in baselines:
                                baseline = baselines[sp]
                                log(f"  Baseline roughness: {baseline.get('specular_roughness', 'N/A')}")
                else:
                    log("  author is None!")

            log("Test complete.")
            return None

    except Exception as e:
        log(f"ERROR in step {_step}: {e}")
        import traceback
        traceback.print_exc()
        return None


log("=" * 60)
log("Multi-node shader depsgraph E2E test")
log("=" * 60)
bpy.app.timers.register(_test_step, first_interval=1.0)
