"""Open Blender as a live Material Zoo receiver and keep the UI running."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import bpy


def _args() -> argparse.Namespace:
    forwarded = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--addon-zip", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--expected-seq", type=int, required=True)
    parser.add_argument("--camera", default="")
    return parser.parse_args(forwarded)


def _find_object(prim_path: str, object_type: str | None = None):
    return next(
        (
            obj
            for obj in bpy.data.objects
            if obj.get("usd_prim_path", "") == prim_path
            and (object_type is None or obj.type == object_type)
        ),
        None,
    )


def _present(camera_path: str) -> None:
    scene = bpy.context.scene
    if camera_path:
        camera = _find_object(camera_path, "CAMERA")
        if camera is None:
            raise RuntimeError(f"streamed camera not found: {camera_path}")
        scene.camera = camera

    scene.render.resolution_x = 1500
    scene.render.resolution_y = 1000
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.view_settings.view_transform = "Standard"
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            shading = area.spaces.active.shading
            shading.type = "RENDERED"
            for attribute in (
                "use_scene_world",
                "use_scene_world_render",
                "use_scene_lights",
                "use_scene_lights_render",
            ):
                if hasattr(shading, attribute):
                    setattr(shading, attribute, True)
            area.spaces.active.overlay.show_overlays = False
            if camera_path:
                area.spaces.active.region_3d.view_perspective = "CAMERA"


def main() -> None:
    args = _args()
    repo = Path(args.repo).resolve()
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    if hasattr(bpy.context.preferences.view, "show_splash"):
        bpy.context.preferences.view.show_splash = False

    bpy.ops.preferences.addon_install(filepath=str(Path(args.addon_zip).resolve()), overwrite=True)
    bpy.ops.preferences.addon_enable(module="usd_connect")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    scene = bpy.context.scene
    scene.name = "OpenUSDConnect Material Zoo (Live Receiver)"
    scene.usd_connect_live_auto_start_emitter = False
    scene.usd_connect_live_auto_start_receiver = False
    scene.usd_connect_asset_root = str(repo)
    result = bpy.ops.usd_connect.import_with_hook(filepath=str(Path(args.base).resolve()))
    if "FINISHED" not in result:
        raise RuntimeError(f"base USD import failed: {result}")

    scene.usd_connect_recv_host = args.host
    scene.usd_connect_recv_port = args.port
    scene.usd_connect_recv_last_seq = 0

    import usd_connect.receiver_addon as receiver_addon

    receiver_addon._LAST_SEQ = 0
    receiver_addon._ADAPTER = None
    result = bpy.ops.usd_connect.start_receiver()
    if "FINISHED" not in result:
        raise RuntimeError(f"receiver start failed: {result}")

    deadline = time.monotonic() + 120.0

    def poll() -> float | None:
        dispatcher = receiver_addon._DISPATCHER
        last_seq = dispatcher.last_seq if dispatcher is not None else 0
        receiver = receiver_addon._RECEIVER
        if receiver is not None and receiver.auth_rejected:
            print("[Material Zoo Viewer] receiver authentication rejected", flush=True)
            return None
        if last_seq >= args.expected_seq:
            _present(args.camera)
            print(
                f"[Material Zoo Viewer] READY seq={last_seq} camera={args.camera or '<free>'}",
                flush=True,
            )
            return None
        if time.monotonic() >= deadline:
            print(
                f"[Material Zoo Viewer] timeout seq={last_seq}/{args.expected_seq}",
                flush=True,
            )
            return None
        return 0.25

    bpy.app.timers.register(poll, first_interval=0.25)


main()
