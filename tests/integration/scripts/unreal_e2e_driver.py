"""Unreal-side driver for the opt-in OpenUSDConnect integration scenario."""

from __future__ import annotations

import difflib
from contextlib import closing
import json
import os
import sqlite3
import subprocess
import time

import unreal
from pxr import Gf, Sdf, UsdUtils

CONFIG_PATH = os.environ["OUC_UNREAL_TEST_CONFIG"]
with open(CONFIG_PATH, encoding="utf-8") as stream:
    CONFIG = json.load(stream)

STATE = {
    "deadline": time.monotonic() + float(CONFIG["timeout"]),
    "phase": "wait_for_connection",
    "started": time.monotonic(),
}


def _log(message):
    unreal.log(f"OpenUSDConnect Unreal test: {message}")


def _write_result(payload):
    payload.setdefault("duration_seconds", time.monotonic() - STATE["started"])
    with open(CONFIG["result_path"], "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")


def _touch(path):
    with open(path, "w", encoding="utf-8") as stream:
        stream.write("ready\n")


def _request_exit():
    unreal.EditorPythonScripting.set_keep_python_script_alive(False)


def _finish(payload):
    if STATE.get("finished"):
        return
    STATE["finished"] = True
    payload.setdefault("success", True)
    payload["status"] = _status()
    _write_result(payload)
    if CONFIG["interactive"] and payload["success"]:
        STATE["phase"] = "interactive"
        _log("scenario passed; the editor and server will remain open until you close Unreal")
        return
    STATE["subsystem"].disconnect()
    _request_exit()


def _fail(exc):
    payload = {
        "error": f"{type(exc).__name__}: {exc}",
        "phase": STATE.get("phase"),
        "success": False,
    }
    try:
        payload["status"] = _status()
    except Exception:
        pass
    _write_result(payload)
    STATE["finished"] = True
    try:
        subsystem = STATE.get("subsystem")
        if subsystem is not None:
            subsystem.disconnect()
    finally:
        if "world" in STATE:
            _request_exit()


def _status():
    status = STATE["subsystem"].get_status()
    return {
        "auth_state": str(status.auth_state),
        "emitter_connected": bool(status.emitter_connected),
        "emitter_started": bool(status.emitter_started),
        "endpoint_host": str(status.endpoint_host),
        "endpoint_port": int(status.endpoint_port),
        "last_message": str(status.last_message),
        "receiver_connected": bool(status.receiver_connected),
        "receiver_started": bool(status.receiver_started),
    }


def _find_stage():
    base_path = os.path.realpath(CONFIG["base_stage"])
    for stage in UsdUtils.StageCache.Get().GetAllStages():
        root = stage.GetRootLayer()
        real_path = root.realPath or root.identifier
        if os.path.realpath(real_path) == base_path:
            return stage
    return None


def _normalized_layer_text(layer):
    return layer.ExportToString().replace("\r\n", "\n")


def _layer_diff(expected_path):
    stage = _find_stage()
    if stage is None:
        return "USD stage is not present in StageCache"
    expected = Sdf.Layer.FindOrOpen(expected_path)
    if expected is None:
        return f"could not open expected layer {expected_path}"
    actual_text = _normalized_layer_text(stage.GetRootLayer())
    expected_text = _normalized_layer_text(expected)
    if actual_text == expected_text:
        return ""
    return "\n".join(
        difflib.unified_diff(
            expected_text.splitlines(),
            actual_text.splitlines(),
            fromfile="expected",
            tofile="unreal",
            lineterm="",
            n=3,
        )
    )


def _send(events, client_id):
    payload = "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events)
    completed = subprocess.run(
        [
            CONFIG["python_executable"],
            "-m",
            "openusdconnect.send",
            "--host",
            "127.0.0.1",
            "--port",
            str(CONFIG["port"]),
            "--client-id",
            client_id,
            "--stdin",
        ],
        cwd=CONFIG["repo_root"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    if completed.returncode:
        raise RuntimeError(
            f"sender {client_id} failed: {completed.stdout.strip()} {completed.stderr.strip()}"
        )


def _max_sequence():
    try:
        with closing(sqlite3.connect(CONFIG["database_path"], timeout=0.1)) as connection:
            row = connection.execute("SELECT max(seq) FROM events").fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0


def _material_state(prim_path):
    component = STATE["actor"].get_generated_component(prim_path)
    if component is None:
        return None
    material = component.get_material(0)
    if material is None:
        return None
    library = unreal.MaterialEditingLibrary
    scalars = {}
    vectors = {}
    textures = {}
    if isinstance(material, unreal.MaterialInstanceConstant):
        scalar_names = {"Metallic", "Opacity", "Roughness", "UseBaseColorTexture"}
        for name in library.get_scalar_parameter_names(material):
            if str(name) in scalar_names:
                scalars[str(name)] = library.get_material_instance_scalar_parameter_value(
                    material,
                    name,
                )
        vector_names = {"BaseColor", "EmissiveColor"}
        for name in library.get_vector_parameter_names(material):
            if str(name) in vector_names:
                value = library.get_material_instance_vector_parameter_value(material, name)
                vectors[str(name)] = [value.r, value.g, value.b, value.a]
        for name in library.get_texture_parameter_names(material):
            if str(name) == "BaseColorTexture":
                value = library.get_material_instance_texture_parameter_value(material, name)
                textures[str(name)] = value.get_path_name() if value is not None else None
    return {
        "component_class": component.get_class().get_name(),
        "material_class": material.get_class().get_name(),
        "material_name": material.get_name(),
        "scalar_parameters": scalars,
        "texture_parameters": textures,
        "vector_parameters": vectors,
    }


def _all_materials():
    paths = ("/World/PreviewBall", "/World/TexturedPanel", "/World/MaterialXBall")
    return {path: _material_state(path) for path in paths}


def _near(value, expected, tolerance=1e-4):
    return value is not None and abs(float(value) - expected) <= tolerance


def _preview_matches(state, *, color, metallic, roughness):
    if state is None or state["material_class"] != "MaterialInstanceConstant":
        return False
    scalars = state["scalar_parameters"]
    actual_color = state["vector_parameters"].get("BaseColor")
    return (
        _near(scalars.get("Metallic"), metallic)
        and _near(scalars.get("Roughness"), roughness)
        and actual_color is not None
        and all(
            _near(actual, expected)
            for actual, expected in zip(actual_color[:3], color, strict=True)
        )
    )


def _texture_matches(state, filename=None):
    if state is None or state["material_class"] != "MaterialInstanceConstant":
        return False
    enabled = state["scalar_parameters"].get("UseBaseColorTexture")
    texture = state["texture_parameters"].get("BaseColorTexture")
    return _near(enabled, 1.0) and texture is not None and (filename is None or filename in texture)


def _materials_ready(expectation, *, verify_texture_path=False, require_live_component=False):
    states = _all_materials()
    STATE["last_materials"] = states
    preview = states["/World/PreviewBall"]
    textured = states["/World/TexturedPanel"]
    material_x = states["/World/MaterialXBall"]
    live_component = STATE["actor"].get_generated_component("/World/LiveSphere")
    STATE["last_live_component"] = (
        live_component.get_class().get_name() if live_component is not None else None
    )
    ready = (
        _preview_matches(
            preview,
            color=expectation["color"],
            metallic=expectation["metallic"],
            roughness=expectation["roughness"],
        )
        and _texture_matches(
            textured,
            expectation["texture"] if verify_texture_path else None,
        )
        and material_x is not None
        and not material_x["material_name"].startswith("MI_DisplayColor")
        and (not require_live_component or live_component is not None)
    )
    return states if ready else None


def _author_reverse_edits():
    stage = _find_stage()
    if stage is None:
        raise RuntimeError("USD stage disappeared before reverse edits")
    translate = stage.GetPrimAtPath("/World/PreviewBall").GetAttribute("xformOp:translate")
    roughness = stage.GetPrimAtPath("/World/Looks/Preview/Surface").GetAttribute("inputs:roughness")
    if not translate or not roughness:
        raise RuntimeError("reverse-edit attributes are missing")
    translate.Set(Gf.Vec3d(6.0, 2.0, 1.0))
    roughness.Set(0.11)


def _author_offline_edits():
    stage = _find_stage()
    if stage is None:
        raise RuntimeError("USD stage disappeared during server outage")
    translate = stage.GetPrimAtPath("/World/PreviewBall").GetAttribute("xformOp:translate")
    roughness = stage.GetPrimAtPath("/World/Looks/Preview/Surface").GetAttribute("inputs:roughness")
    if not translate or not roughness:
        raise RuntimeError("offline-edit attributes are missing")
    translate.Set(Gf.Vec3d(9.0, 3.0, 2.0))
    roughness.Set(0.07)


def _tick(_delta_time):
    try:
        if STATE.get("finished"):
            return
        if time.monotonic() >= STATE["deadline"]:
            detail = STATE.get("last_diff", "")
            materials = json.dumps(STATE.get("last_materials"), indent=2, sort_keys=True)
            raise RuntimeError(f"scenario timed out in {STATE['phase']}\n{detail}\n{materials}")

        phase = STATE["phase"]
        if phase == "wait_for_connection":
            status = _status()
            if not status["receiver_connected"] or not status["emitter_connected"]:
                return
            STATE["phase"] = "wait_for_baseline_materials"
            _log("connected; waiting for baseline USD assets")
            return

        if phase == "wait_for_baseline_materials":
            materials = _materials_ready(
                CONFIG["material_expectations"]["baseline"],
                verify_texture_path=True,
            )
            if materials is None:
                return
            _send(CONFIG["initial_events"], "unreal-test-initial")
            STATE["phase"] = "wait_for_initial_parity"
            _log("baseline materials passed; initial transaction sent")
            return

        if phase == "wait_for_initial_parity":
            diff = _layer_diff(CONFIG["expected_initial"])
            STATE["last_diff"] = diff
            if diff:
                return
            materials = _materials_ready(
                CONFIG["material_expectations"]["initial"],
                require_live_component=True,
            )
            if materials is None:
                return
            STATE["initial_materials"] = materials
            _send(CONFIG["update_events"], "unreal-test-update")
            STATE["phase"] = "wait_for_final_parity"
            _log("initial parity passed; update transaction sent")
            return

        if phase == "wait_for_final_parity":
            diff = _layer_diff(CONFIG["expected_final"])
            STATE["last_diff"] = diff
            if diff:
                return
            materials = _materials_ready(
                CONFIG["material_expectations"]["final"],
                require_live_component=True,
            )
            if materials is None:
                return
            STATE["final_materials"] = materials
            STATE["reverse_baseline_seq"] = _max_sequence()
            _author_reverse_edits()
            STATE["phase"] = "wait_for_reverse_emit"
            _log("final parity passed; authored reverse transform and shader edits")
            return

        if phase == "wait_for_reverse_emit":
            max_seq = _max_sequence()
            if max_seq < STATE["reverse_baseline_seq"] + 2:
                return
            STATE["outage_baseline_seq"] = max_seq
            _touch(CONFIG["outage_ready_path"])
            STATE["phase"] = "wait_for_outage_disconnect"
            _log("reverse edits reached the server; requesting outage")
            return

        if phase == "wait_for_outage_disconnect":
            status = _status()
            if status["receiver_connected"]:
                return
            _author_offline_edits()
            _touch(CONFIG["offline_edit_path"])
            STATE["phase"] = "wait_for_outage_reconnect"
            _log("server outage observed; authored queued offline edits")
            return

        if phase == "wait_for_outage_reconnect":
            status = _status()
            if not status["receiver_connected"] or not status["emitter_connected"]:
                return
            max_seq = _max_sequence()
            if max_seq < STATE["outage_baseline_seq"] + 2:
                return
            _finish(
                {
                    "final_materials": STATE["final_materials"],
                    "initial_materials": STATE["initial_materials"],
                    "layer_parity": True,
                    "live_created_component": STATE["last_live_component"],
                    "max_sequence": max_seq,
                    "reverse_baseline_sequence": STATE["reverse_baseline_seq"],
                    "reverse_edits_emitted": True,
                    "outage_baseline_sequence": STATE["outage_baseline_seq"],
                    "offline_edits_emitted": True,
                    "outage_reconnected": True,
                }
            )
    except Exception as exc:
        _fail(exc)


try:
    unreal.EditorPythonScripting.set_keep_python_script_alive(True)
    editor_subsystem = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    world = editor_subsystem.get_editor_world()
    STATE["world"] = world

    settings_class = unreal.load_class(None, "/Script/OpenUSDConnect.USDConnectSettings")
    if settings_class is None:
        raise RuntimeError("OpenUSDConnect settings class is not loaded")
    settings = unreal.get_default_object(settings_class)
    settings.set_editor_property("ServerHost", "127.0.0.1")
    settings.set_editor_property("ServerPort", int(CONFIG["port"]))
    settings.set_editor_property("bAutoConnect", False)

    actor = actor_subsystem.spawn_actor_from_class(
        unreal.UsdStageActor,
        unreal.Vector(0.0, 0.0, 0.0),
        unreal.Rotator(0.0, 0.0, 0.0),
        True,
    )
    actor.set_root_layer(CONFIG["base_stage"])
    actor.set_stage_state(unreal.UsdStageState.OPENED_AND_LOADED)
    STATE["actor"] = actor

    subsystems = [
        item
        for item in unreal.ObjectIterator(unreal.USDConnectSubsystem)
        if item.get_outer() == world
    ]
    if len(subsystems) != 1:
        raise RuntimeError(
            f"expected one USDConnectSubsystem for the editor world, got {subsystems}"
        )
    STATE["subsystem"] = subsystems[0]
    STATE["subsystem"].disconnect()
    STATE["subsystem"].connect()
    STATE["callback"] = unreal.register_slate_post_tick_callback(_tick)
    _log("stage actor created and connection requested")
except Exception as exc:
    _fail(exc)
