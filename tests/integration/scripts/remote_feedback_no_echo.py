"""Real-Blender regression: received transforms and materials must not echo."""

from __future__ import annotations

import os
import socket
import sys

import bpy

PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", ".."),
)
ADDON_ZIP = os.path.join(PROJECT_ROOT, "dist", "usd_connect_blender.zip")
BASE_USD = os.path.join(PROJECT_ROOT, "test_scene.usda")
SHADER_PATH = "/World/NoEchoLooks/Material/StandardSurface"
PARAMETRIC_PARENT_PATH = "/World/NoEchoParametricParent"
PARAMETRIC_PATH = f"{PARAMETRIC_PARENT_PATH}/Cube"
INITIAL_ROUGHNESS = 0.2
UPDATED_ROUGHNESS = 0.73


def _port() -> int:
    args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    for index, value in enumerate(args):
        if value == "--port" and index + 1 < len(args):
            return int(args[index + 1])
    return 7200


PORT = _port()
_step = 0
_retries = 0
_target_path = ""
_sender = None


def log(message: str) -> None:
    print(f"[REMOTE-NO-ECHO] {message}", flush=True)


def _send_external(events: list[dict]) -> None:
    global _sender
    from openusdconnect.sender import EventSender

    if _sender is None:
        _sender = EventSender(
            host="127.0.0.1",
            port=PORT,
            client_id="remote-feedback-no-echo-external",
            origin="remote-feedback-no-echo-external",
        )
        if not _sender.connect():
            raise RuntimeError("external sender failed to connect")
    if not _sender.send_events(events):
        raise RuntimeError("external transaction was rejected")
    if not _sender.flush(timeout=5.0):
        raise RuntimeError("external transaction was not acknowledged")


def _baseline_roughness() -> float | None:
    from usd_connect.capture import _state

    author = _state.author
    if author is None:
        return None
    values = author._last_shader_values.get(SHADER_PATH)
    if not values or "specular_roughness" not in values:
        return None
    return float(values["specular_roughness"])


def _wait_or_fail(message: str) -> float:
    global _retries
    _retries += 1
    if _retries > 40:
        raise RuntimeError(message)
    return 0.2


def _test_step():
    global _step, _retries, _target_path, _sender
    try:
        if _step == 0:
            bpy.ops.preferences.addon_install(filepath=ADDON_ZIP, overwrite=True)
            bpy.ops.preferences.addon_enable(module="usd_connect")
            bpy.ops.object.select_all(action="SELECT")
            bpy.ops.object.delete(use_global=False)

            scene = bpy.context.scene
            scene.usd_connect_base_usd_path = BASE_USD
            scene.usd_connect_auto_track = False
            scene.usd_connect_coalesce_seconds = 0.0
            scene.usd_connect_emit_host = "127.0.0.1"
            scene.usd_connect_emit_port = PORT
            scene.usd_connect_recv_host = "127.0.0.1"
            scene.usd_connect_recv_port = PORT
            scene.usd_connect_recv_last_seq = 0

            result = bpy.ops.usd_connect.import_with_hook(filepath=BASE_USD)
            if "CANCELLED" in result:
                raise RuntimeError("base import was cancelled")
            target = next(
                (obj for obj in bpy.data.objects if obj.get("usd_prim_path")),
                None,
            )
            if target is None:
                raise RuntimeError("base import produced no tagged objects")
            _target_path = target.get("usd_prim_path")

            if "CANCELLED" in bpy.ops.usd_connect.connect_emitter():
                raise RuntimeError("emitter startup was cancelled")
            if "CANCELLED" in bpy.ops.usd_connect.start_receiver():
                raise RuntimeError("receiver startup was cancelled")
            log(
                "emitter and receiver connected with auto-track disabled; "
                f"target={_target_path}",
            )
            _step = 1
            return 1.0

        if _step == 1:
            _send_external(
                [
                    {"k": "ensure_prim", "prim": _target_path, "typeName": "Xform"},
                    {"k": "ensure_xform_ops", "prim": _target_path},
                    {
                        "k": "set_xform_trs",
                        "prim": _target_path,
                        "fields": ["t"],
                        "t": [4.0, 0.0, 0.0],
                    },
                ],
            )
            log("external transform transaction sent")
            _retries = 0
            _step = 2
            return 0.2

        if _step == 2:
            scene = bpy.context.scene
            if int(scene.usd_connect_recv_last_seq) < 3:
                return _wait_or_fail(
                    f"receiver stalled at sequence {scene.usd_connect_recv_last_seq}",
                )
            target = next(
                (
                    obj
                    for obj in bpy.data.objects
                    if obj.get("usd_prim_path") == _target_path
                ),
                None,
            )
            if target is None:
                raise RuntimeError(f"imported {_target_path} object was not found")
            if abs(float(target.location.x) - 4.0) > 1e-4:
                raise RuntimeError(
                    f"remote transform was not applied: {tuple(target.location)}",
                )
            log("remote transform applied; allowing delayed callbacks to drain")
            _step = 3
            return 1.0

        if _step == 3:
            _send_external(
                [
                    {
                        "k": "ensure_prim",
                        "prim": "/World/NoEchoLooks",
                        "typeName": "Scope",
                    },
                    {
                        "k": "ensure_prim",
                        "prim": "/World/NoEchoLooks/Material",
                        "typeName": "Material",
                    },
                    {
                        "k": "ensure_prim",
                        "prim": SHADER_PATH,
                        "typeName": "Shader",
                    },
                    {
                        "k": "set_connectable_input",
                        "prim": SHADER_PATH,
                        "info_id": "ND_standard_surface_surfaceshader",
                        "inputs": {
                            "base": 1.0,
                            "base_color": [0.3, 0.5, 0.7],
                            "specular_roughness": INITIAL_ROUGHNESS,
                        },
                        "input_types": {
                            "base": "float",
                            "base_color": "color3f",
                            "specular_roughness": "float",
                        },
                    },
                ],
            )
            log("initial MaterialX Standard Surface transaction sent")
            _retries = 0
            _step = 4
            return 0.2

        if _step == 4:
            baseline = _baseline_roughness()
            if baseline is None or abs(baseline - INITIAL_ROUGHNESS) > 1e-4:
                return _wait_or_fail(
                    f"initial shader baseline was not seeded: {baseline}",
                )
            _send_external(
                [
                    {
                        "k": "set_connectable_input",
                        "prim": SHADER_PATH,
                        "info_id": "ND_standard_surface_surfaceshader",
                        "inputs": {"specular_roughness": UPDATED_ROUGHNESS},
                        "input_types": {"specular_roughness": "float"},
                    },
                ],
            )
            log(f"shader baseline {baseline}; remote roughness update sent")
            _retries = 0
            _step = 5
            return 0.2

        if _step == 5:
            baseline = _baseline_roughness()
            if baseline is None or abs(baseline - UPDATED_ROUGHNESS) > 1e-4:
                return _wait_or_fail(
                    f"updated shader baseline was not seeded: {baseline}",
                )
            log(
                f"updated shader baseline is {baseline}; "
                "waiting for delayed material callbacks",
            )
            _step = 6
            return 2.0

        if _step == 6:
            _send_external(
                [
                    {
                        "k": "ensure_prim",
                        "prim": PARAMETRIC_PARENT_PATH,
                        "typeName": "Xform",
                    },
                    {"k": "ensure_prim", "prim": PARAMETRIC_PATH, "typeName": "Cube"},
                    {
                        "k": "set_gprim_attrs",
                        "prim": PARAMETRIC_PATH,
                        "attrs": {"size": 1.0},
                    },
                    {"k": "ensure_xform_ops", "prim": PARAMETRIC_PATH},
                    {
                        "k": "set_xform_trs",
                        "prim": PARAMETRIC_PATH,
                        "fields": ["t", "s"],
                        "t": [0.0, 0.0, 0.0],
                        "s": [0.2, 0.4, 0.6],
                    },
                ],
            )
            log("remote parametric Cube transaction sent")
            _retries = 0
            _step = 7
            return 0.2

        if _step == 7:
            scene = bpy.context.scene
            if int(scene.usd_connect_recv_last_seq) < 13:
                return _wait_or_fail(
                    f"parametric Cube replay stalled at {scene.usd_connect_recv_last_seq}",
                )
            obj = next(
                (
                    candidate
                    for candidate in bpy.data.objects
                    if candidate.get("usd_prim_path") == PARAMETRIC_PATH
                ),
                None,
            )
            if obj is None:
                raise RuntimeError("remote parametric Cube was not created")
            expected_display_scale = (0.1, 0.2, 0.3)
            if any(
                abs(float(actual) - expected) > 1e-4
                for actual, expected in zip(
                    obj.scale,
                    expected_display_scale,
                    strict=True,
                )
            ):
                raise RuntimeError(
                    f"unexpected initial parametric scale: {tuple(obj.scale)}",
                )
            log(
                "initial parametric state: "
                f"geometry={obj.get('usd_geom_scale')} "
                f"xform={obj.get('usd_xform_scale')}",
            )
            obj.location.x += 1.0
            bpy.context.view_layer.update()
            log("moved remote parametric Cube locally")
            _retries = 0
            _step = 8
            return 0.2

        if _step == 8:
            scene = bpy.context.scene
            if int(scene.usd_connect_recv_last_seq) < 17:
                return _wait_or_fail(
                    f"local parametric edit stalled at {scene.usd_connect_recv_last_seq}",
                )
            log("same-origin correction applied; waiting for delayed callbacks")
            _step = 9
            return 2.0

        if _step == 9:
            obj = next(
                (
                    candidate
                    for candidate in bpy.data.objects
                    if candidate.get("usd_prim_path") == PARAMETRIC_PATH
                ),
                None,
            )
            if obj is None:
                raise RuntimeError("parametric Cube disappeared after correction")
            expected_display_scale = (0.1, 0.2, 0.3)
            if any(
                abs(float(actual) - expected) > 1e-4
                for actual, expected in zip(
                    obj.scale,
                    expected_display_scale,
                    strict=True,
                )
            ):
                raise RuntimeError(
                    "parametric scale changed after round trip: "
                    f"display={tuple(obj.scale)} "
                    f"geometry={obj.get('usd_geom_scale')} "
                    f"xform={obj.get('usd_xform_scale')}",
                )
            if _sender is not None:
                _sender.disconnect()
                _sender = None
            log("SUCCESS")
            bpy.ops.wm.quit_blender()
            return None
    except Exception as exc:
        import traceback

        log(f"FAIL: {exc}")
        traceback.print_exc()
        if _sender is not None:
            _sender.disconnect()
            _sender = None
        bpy.ops.wm.quit_blender()
        return None


try:
    probe = socket.create_connection(("127.0.0.1", PORT), timeout=1.0)
    probe.close()
    bpy.app.timers.register(_test_step, first_interval=0.1)
except OSError as exc:
    log(f"FAIL: server unavailable: {exc}")
