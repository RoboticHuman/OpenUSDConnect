"""Blender side of the emitter outage/reconnect integration test."""

import argparse
import json
import os
import sys
import time
import traceback

import bpy


def _args():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--addon", required=True)
    parser.add_argument("--control-dir", required=True)
    return parser.parse_args(argv)


ARGS = _args()
CONTROL_DIR = os.path.abspath(ARGS.control_dir)
os.makedirs(CONTROL_DIR, exist_ok=True)


def _marker(name, payload=""):
    with open(os.path.join(CONTROL_DIR, name), "w", encoding="utf-8") as stream:
        stream.write(payload)


def _exists(name):
    return os.path.exists(os.path.join(CONTROL_DIR, name))


def _result(**values):
    with open(os.path.join(CONTROL_DIR, "result.json"), "w", encoding="utf-8") as stream:
        json.dump(values, stream, indent=2)


def _find_cube():
    for obj in bpy.data.objects:
        if (
            obj.get("usd_prim_path") == "/World/Cube"
            or obj.get("_usd_xform_prim_path") == "/World/Cube"
        ):
            return obj
    return None


_phase = "setup"
_deadline = time.monotonic() + 50.0
_edit_time = 0.0


def _tick():
    global _phase, _edit_time
    try:
        from usd_connect import capture

        if time.monotonic() > _deadline:
            sender = capture.get_emitter_sender()
            _result(
                status="FAIL",
                reason=f"timeout in {_phase}",
                connected=bool(sender and sender.sock),
                pending=sender.pending_transaction_count if sender else -1,
            )
            bpy.ops.wm.quit_blender()
            return None

        if _phase == "setup":
            bpy.ops.preferences.addon_install(filepath=ARGS.addon, overwrite=True)
            bpy.ops.preferences.addon_enable(module="usd_connect")
            scene = bpy.context.scene
            scene.usd_connect_live_auto_start_emitter = False
            scene.usd_connect_live_auto_start_receiver = False
            assert bpy.ops.usd_connect.import_with_hook(filepath=ARGS.base) == {"FINISHED"}
            scene.usd_connect_emit_host = "127.0.0.1"
            scene.usd_connect_emit_port = ARGS.port
            scene.usd_connect_auto_track = True
            assert bpy.ops.usd_connect.connect_emitter() == {"FINISHED"}
            assert _find_cube() is not None
            _marker("ready")
            _phase = "wait_for_outage"
            return 0.1

        sender = capture.get_emitter_sender()
        if _phase == "wait_for_outage":
            if not _exists("edit-now") or sender is None or sender.sock is not None:
                return 0.1
            cube = _find_cube()
            assert cube is not None
            cube.location.x = 7.5
            bpy.context.view_layer.update()
            _edit_time = time.monotonic()
            _phase = "wait_for_dirty"
            return 0.1

        if _phase == "wait_for_dirty":
            emitter = capture._state.notice_emitter
            author = capture._state.author
            dirty = bool(emitter and (emitter.dirty or emitter.prepared_event_count))
            prim = author.stage.GetPrimAtPath("/World/Cube") if author else None
            authored = bool(prim and prim.GetAttribute("xformOp:translate").Get()[0] == 7.5)
            if not dirty and not authored:
                return 0.1
            _marker("offline-edit-authored")
            _phase = "wait_for_reconnect"
            return 0.1

        if _phase == "wait_for_reconnect":
            emitter = capture._state.notice_emitter
            clean = not emitter or (not emitter.dirty and not emitter.prepared_event_count)
            if (
                sender is None
                or sender.sock is None
                or sender.pending_transaction_count
                or not clean
                or sender.acknowledged_event_count < 1
                or time.monotonic() - _edit_time < 0.5
            ):
                return 0.1
            _result(
                status="PASS",
                connected=True,
                pending=sender.pending_transaction_count,
                acknowledged_events=sender.acknowledged_event_count,
                cube_x=_find_cube().location.x,
            )
            bpy.ops.wm.quit_blender()
            return None
    except Exception as exc:
        traceback.print_exc()
        _result(status="FAIL", reason=str(exc), phase=_phase)
        bpy.ops.wm.quit_blender()
        return None


bpy.app.timers.register(_tick, first_interval=0.1)
