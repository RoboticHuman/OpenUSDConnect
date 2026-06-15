"""Blender smoke for live-open through the WebDAV snapshot.

Run via:
  blender --background --python tests/integration/scripts/live_open_smoke.py \
    -- --port PORT --vfs-port VFS_PORT --out RESULTS_FILE
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request

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


def _parse_args():
    port = 7200
    vfs_port = 7280
    out_path = ""
    require_token = False
    argv = sys.argv
    if "--" in argv:
        script_args = argv[argv.index("--") + 1 :]
        for i, arg in enumerate(script_args):
            if arg == "--port" and i + 1 < len(script_args):
                port = int(script_args[i + 1])
            elif arg == "--vfs-port" and i + 1 < len(script_args):
                vfs_port = int(script_args[i + 1])
            elif arg == "--out" and i + 1 < len(script_args):
                out_path = script_args[i + 1]
            elif arg == "--require-token":
                require_token = True
    if not out_path:
        raise RuntimeError("--out is required")
    return port, vfs_port, out_path, require_token


def _find_prim_object(prim_path: str):
    for obj in bpy.data.objects:
        if obj.get("usd_prim_path") == prim_path:
            return obj
    return None


def _object_summary() -> str:
    rows = []
    for obj in bpy.data.objects:
        rows.append(f"{obj.name}:{obj.type}:{obj.get('usd_prim_path', '')}")
    return "; ".join(rows)


def _write_results(out_path: str, results: dict[str, str]) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)


def main():
    port, vfs_port, out_path, require_token = _parse_args()
    results: dict[str, str] = {}

    import integrations.blender as addon
    from integrations.blender import capture, receiver_addon
    from openusdconnect import token_client
    from openusdconnect.sender import EventSender
    from openusdconnect.server import UsdSyncServer
    from openusdconnect.server.connection import ConnectionHandler, ThreadedTCPServer
    from openusdconnect.server.vfs import VirtualStageFile, run_vfs_server

    addon.register()
    token_client._TOKEN_DIR = os.path.dirname(out_path)
    token_client._TOKEN_FILE = os.path.join(os.path.dirname(out_path), "client_tokens.json")
    srv = UsdSyncServer(
        log_path=os.path.join(os.path.dirname(out_path), "live_open.db"),
        require_token=require_token,
        token_db_path=os.path.join(os.path.dirname(out_path), "server_tokens.db"),
    )
    url = f"http://127.0.0.1:{vfs_port}/usd/scene.usd"
    provider = VirtualStageFile(
        srv,
        name="scene.usd",
        advertise_host="127.0.0.1",
        sync_port=port,
        vfs_url=url,
    )
    vfs_handle = run_vfs_server(provider, "127.0.0.1", vfs_port, share="usd")
    tcp_server = ThreadedTCPServer(("127.0.0.1", port), ConnectionHandler, srv)
    tcp_thread = threading.Thread(target=tcp_server.serve_forever, daemon=True)
    tcp_thread.start()
    post_sender = None

    try:
        srv.process_txn(
            [
                {"k": "ensure_prim", "prim": "/World", "typeName": "Xform"},
                {"k": "ensure_prim", "prim": "/World/PreImport", "typeName": "Cube"},
            ],
            client_id="seed",
            origin="seed-origin",
        )

        snapshot_path = os.path.join(os.path.dirname(out_path), "scene.usd")
        urllib.request.urlretrieve(url, snapshot_path)
        result = bpy.ops.usd_connect.import_with_hook(filepath=snapshot_path)
        if "FINISHED" not in result:
            results["import_operator"] = f"FAIL: {result}"
        else:
            results["import_operator"] = "PASS"

        scene = bpy.context.scene
        results["metadata_host"] = (
            "PASS"
            if scene.usd_connect_emit_host == "127.0.0.1"
            and scene.usd_connect_recv_host == "127.0.0.1"
            else f"FAIL: emit={scene.usd_connect_emit_host} recv={scene.usd_connect_recv_host}"
        )
        results["metadata_port"] = (
            "PASS"
            if scene.usd_connect_emit_port == port and scene.usd_connect_recv_port == port
            else f"FAIL: emit={scene.usd_connect_emit_port} recv={scene.usd_connect_recv_port}"
        )
        results["receiver_running"] = "PASS" if scene.usd_connect_recv_running else "FAIL"
        results["emitter_running"] = (
            "PASS" if scene.usd_connect_net_emitter_running and capture._state.sender else "FAIL"
        )
        results["snapshot_seq_seeded"] = (
            "PASS"
            if receiver_addon._LAST_SEQ >= 2 and scene.usd_connect_recv_last_seq >= 2
            else f"FAIL: last={receiver_addon._LAST_SEQ} scene={scene.usd_connect_recv_last_seq}"
        )
        if require_token:
            saved_token = token_client.load_token("127.0.0.1", port)
            results["tofu_token_saved"] = "PASS" if saved_token else "FAIL"
        results["pre_import_object"] = (
            "PASS" if _find_prim_object("/World/PreImport") is not None else "FAIL"
        )
        if results["pre_import_object"] != "PASS":
            results["pre_import_objects"] = _object_summary()

        post_sender = EventSender(
            "127.0.0.1",
            port,
            client_id="live-open-smoke",
            origin="live-open-smoke-origin",
            token=token_client.load_token("127.0.0.1", port),
            on_token_issued=lambda token: token_client.save_token("127.0.0.1", port, token),
        )
        if not post_sender.connect():
            reason = "auth rejected" if post_sender.auth_rejected else "could not connect"
            results["post_sender_connect"] = f"FAIL: {reason}"
        else:
            results["post_sender_connect"] = "PASS"
            sent = post_sender.send_events(
                [{"k": "ensure_prim", "prim": "/World/PostImport", "typeName": "Sphere"}]
            )
            results["post_event_sent"] = "PASS" if sent else "FAIL"

            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline and _find_prim_object("/World/PostImport") is None:
                receiver_addon._drain_and_process()
                try:
                    bpy.context.view_layer.update()
                except RuntimeError:
                    pass
                time.sleep(0.05)
            results["post_import_update"] = (
                "PASS" if _find_prim_object("/World/PostImport") is not None else "FAIL"
            )
    finally:
        try:
            if post_sender is not None:
                post_sender.disconnect()
        except Exception:
            pass
        try:
            if bpy.context.scene.usd_connect_recv_running:
                bpy.ops.usd_connect.stop_receiver()
        except Exception:
            pass
        try:
            if bpy.context.scene.usd_connect_net_emitter_running or capture._state.sender:
                bpy.ops.usd_connect.disconnect_emitter()
        except Exception:
            pass
        try:
            addon.unregister()
        except Exception:
            pass
        tcp_server.shutdown()
        tcp_server.server_close()
        vfs_handle.stop()
        srv.shutdown()
        srv.store.close()

    _write_results(out_path, results)
    failures = {k: v for k, v in results.items() if v != "PASS"}
    for key, value in results.items():
        print(f"{key}: {value}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
