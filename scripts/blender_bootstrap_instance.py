"""Bootstrap a Blender instance for OpenUSDConnect debugging.

This script is intended to be passed to Blender with:
  blender --python scripts/blender_bootstrap_instance.py -- [args]

It will:
- install/enable the USD Connect addon from a zip
- apply host/port scene settings for emitter/receiver
- optionally start emitter and/or receiver
- optionally start debugpy and wait for VS Code attach
- register a background timer that watches for addon reload triggers
"""

from __future__ import annotations

import argparse
import os
import sys

import bpy


ADDON_MODULE = "usd_connect"


def _parse_args() -> argparse.Namespace:
    argv = sys.argv
    script_args = []
    if "--" in argv:
        script_args = argv[argv.index("--") + 1 :]

    parser = argparse.ArgumentParser(description="Bootstrap Blender instance for USD Connect")
    parser.add_argument("--addon-zip", required=True, help="Path to dist/usd_connect_blender.zip")
    parser.add_argument("--host", default="127.0.0.1", help="Sync server host")
    parser.add_argument("--port", type=int, default=7200, help="Sync server port")
    parser.add_argument("--role", default="instance", help="Label for logs")
    parser.add_argument("--debug-port", type=int, default=0, help="debugpy listen port; 0 disables")
    parser.add_argument("--wait-for-client", action="store_true", help="Wait for debugger attach")
    parser.add_argument("--start-emitter", action="store_true", help="Auto-start emitter")
    parser.add_argument("--start-receiver", action="store_true", help="Auto-start receiver")
    return parser.parse_args(script_args)


def _ensure_addon(addon_zip: str) -> None:
    if not os.path.isfile(addon_zip):
        raise FileNotFoundError(f"Addon zip not found: {addon_zip}")

    # Install (overwrite) so changes from a fresh build are picked up.
    bpy.ops.preferences.addon_install(filepath=addon_zip, overwrite=True)

    # Enable addon for this Blender session.
    bpy.ops.preferences.addon_enable(module=ADDON_MODULE)

    # Write the installed addon path so setup_vscode.py can generate correct
    # debugpy pathMappings (the addon runs from Blender's addon dir, not the
    # workspace).
    _write_addon_path()


def _write_addon_path() -> None:
    """Detect where Blender installed the addon and save to .blender_addon_path."""
    try:
        import importlib

        mod = importlib.import_module(ADDON_MODULE)
        addon_dir = os.path.dirname(os.path.abspath(mod.__file__))
    except Exception:
        return

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path_file = os.path.join(repo_root, ".blender_addon_path")
    try:
        with open(path_file, "w", encoding="utf-8") as f:
            f.write(addon_dir)
        print(f"[USD Connect Bootstrap] addon installed at: {addon_dir}")
    except OSError as exc:
        print(f"[USD Connect Bootstrap] could not write .blender_addon_path: {exc}")


def _configure_scene(host: str, port: int) -> None:
    scene = bpy.context.scene

    if hasattr(scene, "usd_connect_emit_host"):
        scene.usd_connect_emit_host = host
    if hasattr(scene, "usd_connect_emit_port"):
        scene.usd_connect_emit_port = port

    if hasattr(scene, "usd_connect_recv_host"):
        scene.usd_connect_recv_host = host
    if hasattr(scene, "usd_connect_recv_port"):
        scene.usd_connect_recv_port = port


def _start_network_ops(start_emitter: bool, start_receiver: bool) -> None:
    if start_emitter:
        try:
            bpy.ops.usd_connect.connect_emitter()
            print("[USD Connect Bootstrap] Emitter started")
        except Exception as exc:
            print(f"[USD Connect Bootstrap] Failed to start emitter: {exc}")

    if start_receiver:
        try:
            bpy.ops.usd_connect.start_receiver()
            print("[USD Connect Bootstrap] Receiver started")
        except Exception as exc:
            print(f"[USD Connect Bootstrap] Failed to start receiver: {exc}")


def _start_debugpy(debug_port: int, wait_for_client: bool, role: str) -> None:
    if debug_port <= 0:
        return

    try:
        import debugpy  # type: ignore
    except Exception as exc:
        print(f"[USD Connect Bootstrap:{role}] debugpy import failed: {exc}")
        return

    try:
        debugpy.listen(("127.0.0.1", debug_port))
        print(f"[USD Connect Bootstrap:{role}] debugpy listening on 127.0.0.1:{debug_port}")
    except RuntimeError as exc:
        print(f"[USD Connect Bootstrap:{role}] debugpy already listening: {exc}")

    if wait_for_client:
        print(f"[USD Connect Bootstrap:{role}] waiting for VS Code debugger attach...")
        debugpy.wait_for_client()
        print(f"[USD Connect Bootstrap:{role}] debugger attached")


def _reload_addon(addon_zip: str, role: str) -> None:
    """Disable, reinstall, and re-enable the addon from a fresh zip."""
    print(f"[USD Connect Bootstrap:{role}] reloading addon from {addon_zip}")
    try:
        bpy.ops.preferences.addon_disable(module=ADDON_MODULE)
    except Exception as exc:
        print(f"[USD Connect Bootstrap:{role}] addon_disable warning: {exc}")

    bpy.ops.preferences.addon_install(filepath=addon_zip, overwrite=True)
    bpy.ops.preferences.addon_enable(module=ADDON_MODULE)
    print(f"[USD Connect Bootstrap:{role}] addon reloaded successfully")


def _start_reload_watcher(addon_zip: str, role: str) -> None:
    """Register a bpy.app.timers callback that watches for a reload trigger file.

    The launcher (or any script) can drop a ``.reload_addon`` file in the repo
    root to signal all running Blender instances to rebuild/reinstall the addon.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    trigger_path = os.path.join(repo_root, ".reload_addon")

    def _check_reload() -> float:
        if os.path.isfile(trigger_path):
            # Read optional zip path override from the trigger file, falling
            # back to the original addon_zip passed at launch.
            try:
                content = open(trigger_path, encoding="utf-8-sig").read().strip()
            except OSError:
                content = ""
            zip_path = content if content and os.path.isfile(content) else addon_zip

            # Remove trigger before reloading so other instances also see it
            # (each instance removes independently — first one wins on the
            # filesystem, others just get a FileNotFoundError which is fine).
            try:
                os.remove(trigger_path)
            except FileNotFoundError:
                pass

            _reload_addon(zip_path, role)
        return 2.0  # check again in 2 seconds

    bpy.app.timers.register(_check_reload, first_interval=2.0, persistent=True)
    print(f"[USD Connect Bootstrap:{role}] reload watcher active (trigger: .reload_addon)")


def main() -> None:
    args = _parse_args()
    print(f"[USD Connect Bootstrap:{args.role}] starting")

    _ensure_addon(args.addon_zip)
    _configure_scene(args.host, args.port)
    _start_debugpy(args.debug_port, args.wait_for_client, args.role)
    _start_network_ops(args.start_emitter, args.start_receiver)
    _start_reload_watcher(args.addon_zip, args.role)

    print(
        f"[USD Connect Bootstrap:{args.role}] ready (server={args.host}:{args.port}, "
        f"emitter={args.start_emitter}, receiver={args.start_receiver}, debug_port={args.debug_port})"
    )


if __name__ == "__main__":
    main()
