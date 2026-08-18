"""Initialize Unreal Editor as a live Material Zoo receiver."""

from __future__ import annotations

import json
import os
import time

import unreal

CONFIG_PATH = os.environ["OUC_MATERIAL_ZOO_UNREAL_CONFIG"]
with open(CONFIG_PATH, encoding="utf-8") as stream:
    CONFIG = json.load(stream)

STATE = {"deadline": time.monotonic() + float(CONFIG["timeout"])}


def _log(message):
    unreal.log(f"OpenUSDConnect Material Zoo: {message}")


def _write(path, payload):
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")


def _touch(path):
    with open(path, "w", encoding="utf-8") as stream:
        stream.write("ready\n")


def _generated_component(prim_path, component_type):
    generated = STATE["actor"].get_generated_component(prim_path)
    if generated is None:
        return None
    candidates = [generated, *generated.get_children_components(True)]
    matches = [item for item in candidates if isinstance(item, component_type)]
    if not matches:
        return None
    if len(matches) > 1:
        raise RuntimeError(
            f"{prim_path} generated multiple {component_type.__name__} components: "
            + ", ".join(item.get_name() for item in matches)
        )
    return matches[0]


def _focus_camera():
    camera_path = CONFIG["camera_path"]
    if not camera_path:
        return {}
    component = _generated_component(camera_path, unreal.CameraComponent)
    if component is None:
        return None

    viewport = STATE["level_editor"]
    viewport_key = viewport.get_active_viewport_config_key()
    location = component.get_world_location()
    rotation = component.get_world_rotation()
    component_field_of_view = float(component.get_editor_property("field_of_view"))
    field_of_view = CONFIG["camera_field_of_view"]
    if field_of_view is None:
        field_of_view = component_field_of_view
    field_of_view = float(field_of_view)
    viewport.set_level_viewport_camera_info(location, rotation, viewport_key)
    viewport.set_level_viewport_fov(field_of_view, viewport_key)
    return {
        "component": component.get_name(),
        "component_field_of_view": component_field_of_view,
        "field_of_view": field_of_view,
        "location": [location.x, location.y, location.z],
        "rotation": [rotation.pitch, rotation.yaw, rotation.roll],
    }


def _activate_dome():
    dome_path = CONFIG["dome_path"]
    if not dome_path:
        return {}
    sky = _generated_component(dome_path, unreal.SkyLightComponent)
    if sky is None:
        return None
    cubemap = sky.get_editor_property("cubemap")
    if cubemap is None:
        return None

    component_intensity = float(sky.get_editor_property("intensity"))
    intensity = CONFIG["dome_intensity"]
    if intensity is None:
        intensity = component_intensity
    intensity = float(intensity)
    sky.set_mobility(unreal.ComponentMobility.MOVABLE)
    sky.set_intensity(intensity)
    sky.set_cubemap(cubemap)
    sky.recapture_sky()
    return {
        "component_intensity": component_intensity,
        "cubemap": cubemap.get_path_name(),
        "intensity": intensity,
    }


def _unregister_callback(name):
    callback = STATE.pop(name, None)
    if callback is not None:
        unreal.unregister_slate_post_tick_callback(callback)


def _fail(exc):
    if STATE.get("failed"):
        return
    STATE["failed"] = True
    _unregister_callback("callback")
    _unregister_callback("startup_callback")
    _write(
        CONFIG["failure_path"],
        {"error": f"{type(exc).__name__}: {exc}"},
    )
    unreal.log_error(f"OpenUSDConnect Material Zoo failed: {exc}")


def _tick(_delta_time):
    try:
        if STATE.get("focused") or STATE.get("failed"):
            return
        if time.monotonic() >= STATE["deadline"]:
            raise RuntimeError("timed out waiting for the receiver replay handshake")
        if not STATE.get("ready"):
            status = STATE["subsystem"].get_status()
            if not (
                status.receiver_connected
                and status.receiver_synchronized
                and status.emitter_connected
            ):
                return
            STATE["ready"] = True
            _touch(CONFIG["ready_path"])
            _log("stage opened; receiver and emitter are connected")

        dome = _activate_dome()
        if dome is None:
            return
        camera = _focus_camera()
        if camera is None:
            return
        STATE["focused"] = True
        camera["dome"] = dome
        camera["render_context"] = str(
            STATE["actor"].get_editor_property("render_context")
        )
        _write(CONFIG["focused_path"], camera)
        if CONFIG["camera_path"]:
            _log(f"viewport focused through {CONFIG['camera_path']}")
        _unregister_callback("callback")
    except Exception as exc:
        _fail(exc)


def _start(_delta_time):
    if STATE.get("started") or STATE.get("failed"):
        return
    STATE["started"] = True
    try:
        level_editor = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        if not level_editor.new_level("/Game/MaterialZoo", False):
            raise RuntimeError("could not create the blank Material Zoo level")
        editor_subsystem = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        world = editor_subsystem.get_editor_world()
        STATE["world"] = world
        STATE["actor_subsystem"] = actor_subsystem

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
        actor.set_render_context("mtlx")
        actor.set_root_layer(CONFIG["base_stage"])
        actor.set_stage_state(unreal.UsdStageState.OPENED_AND_LOADED)
        actor_subsystem.set_selected_level_actors([actor])
        STATE["actor"] = actor
        STATE["level_editor"] = level_editor

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
    finally:
        _unregister_callback("startup_callback")


STATE["startup_callback"] = unreal.register_slate_post_tick_callback(_start)
