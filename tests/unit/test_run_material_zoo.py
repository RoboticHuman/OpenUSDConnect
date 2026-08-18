"""Tests for the live Material Zoo runner helpers."""

import math
import runpy
import sys
from types import ModuleType, SimpleNamespace

import pytest

from scripts import run_material_zoo


def test_stinson_beach_comes_from_selected_usdview_install(tmp_path):
    executable = tmp_path / "OpenUSDInstall" / "bin" / "usdview.cmd"
    texture = (
        executable.parent.parent
        / "lib"
        / "usd"
        / "hdx"
        / "resources"
        / "textures"
        / "StinsonBeach.hdr"
    )
    executable.parent.mkdir(parents=True)
    texture.parent.mkdir(parents=True)
    executable.touch()
    texture.touch()

    assert run_material_zoo._stinson_beach_for_usdview(executable) == str(texture)


def test_viewer_failure_is_reported():
    process = SimpleNamespace(pid=1234, poll=lambda: 7)

    with pytest.raises(RuntimeError, match="viewer process 1234 exited early with code 7"):
        run_material_zoo._raise_if_viewer_failed([process])


def test_unreal_is_an_explicit_material_zoo_viewer():
    args = run_material_zoo._parse_args(["--viewers", "blender", "usdview", "unreal"])

    assert args.viewers == {"blender", "usdview", "unreal"}


def test_show_remains_the_blender_and_usdview_shortcut():
    args = run_material_zoo._parse_args(["--show"])

    assert args.viewers == {"blender", "usdview"}


def test_blender_viewer_configures_and_starts_emitter_and_receiver(tmp_path, monkeypatch):
    scene = SimpleNamespace(name="")
    calls = []

    def record(name):
        def operator(*_args, **_kwargs):
            calls.append(name)
            return {"FINISHED"}

        return operator

    def connect_emitter():
        calls.append(("emitter", scene.usd_connect_emit_host, scene.usd_connect_emit_port))
        return {"FINISHED"}

    def start_receiver():
        calls.append(("receiver", scene.usd_connect_recv_host, scene.usd_connect_recv_port))
        return {"FINISHED"}

    bpy = ModuleType("bpy")
    bpy.context = SimpleNamespace(
        preferences=SimpleNamespace(view=SimpleNamespace(show_splash=True)),
        scene=scene,
    )
    bpy.ops = SimpleNamespace(
        preferences=SimpleNamespace(
            addon_install=record("install"),
            addon_enable=record("enable"),
        ),
        object=SimpleNamespace(
            select_all=record("select"),
            delete=record("delete"),
        ),
        usd_connect=SimpleNamespace(
            import_with_hook=record("import"),
            connect_emitter=connect_emitter,
            start_receiver=start_receiver,
        ),
    )
    bpy.app = SimpleNamespace(timers=SimpleNamespace(register=record("timer")))

    capture = ModuleType("usd_connect.capture")
    capture.get_emitter_sender = lambda: None
    receiver_addon = ModuleType("usd_connect.receiver_addon")
    receiver_addon._LAST_SEQ = 0
    receiver_addon._ADAPTER = None
    receiver_addon._DISPATCHER = None
    receiver_addon._RECEIVER = None
    addon = ModuleType("usd_connect")
    addon.__path__ = []
    addon.capture = capture
    addon.receiver_addon = receiver_addon
    monkeypatch.setitem(sys.modules, "bpy", bpy)
    monkeypatch.setitem(sys.modules, "usd_connect", addon)
    monkeypatch.setitem(sys.modules, "usd_connect.capture", capture)
    monkeypatch.setitem(sys.modules, "usd_connect.receiver_addon", receiver_addon)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "blender",
            "--",
            "--addon-zip",
            str(tmp_path / "addon.zip"),
            "--base",
            str(tmp_path / "base.usda"),
            "--repo",
            str(tmp_path),
            "--port",
            "17420",
            "--expected-seq",
            "1",
        ],
    )

    runpy.run_path(
        str(run_material_zoo.PROJECT_ROOT / "scripts" / "material_zoo_blender_viewer.py")
    )

    assert ("emitter", "127.0.0.1", 17420) in calls
    assert ("receiver", "127.0.0.1", 17420) in calls
    assert calls.index(("emitter", "127.0.0.1", 17420)) < calls.index(
        ("receiver", "127.0.0.1", 17420)
    )


def test_camera_fov_comes_from_streamed_camera_attributes():
    events = [
        {
            "k": "set_gprim_attrs",
            "prim": "/World/_TestCam",
            "attrs": {"focalLength": 35.0, "horizontalAperture": 36.0},
        }
    ]

    fov = run_material_zoo._camera_horizontal_fov(events, "/World/_TestCam")

    assert math.isclose(fov, 54.43222311461495)


def test_dome_intensity_comes_from_streamed_connectable_input():
    events = [
        {
            "k": "set_connectable_input",
            "prim": "/World/_Dome",
            "inputs": {"intensity": 1.0, "texture:file": "StinsonBeach.hdr"},
        }
    ]

    assert run_material_zoo._connectable_input_value(events, "/World/_Dome", "intensity") == 1.0


def test_unreal_uses_project_startup_script_without_execute_popup(tmp_path, monkeypatch):
    project = tmp_path / "project" / "OUCUnrealTest.uproject"
    project.parent.mkdir()
    project.touch()
    launched = {}

    def fake_popen(command, **kwargs):
        launched["command"] = command
        launched["kwargs"] = kwargs
        return SimpleNamespace()

    monkeypatch.setattr(run_material_zoo.subprocess, "Popen", fake_popen)
    engine = SimpleNamespace(editor=tmp_path / "UnrealEditor")

    run_material_zoo._launch_unreal(
        engine,
        project,
        tmp_path,
        17200,
        "/World/_TestCam",
        50.0,
        "/World/_Dome",
        1.0,
        30.0,
    )

    startup_script = project.parent / "Content" / "Python" / "init_unreal.py"
    assert startup_script.read_text(encoding="utf-8") == (
        run_material_zoo.PROJECT_ROOT / "scripts" / "material_zoo_unreal_viewer.py"
    ).read_text(encoding="utf-8")
    assert not any(argument.startswith("-ExecutePythonScript=") for argument in launched["command"])


def test_wait_for_unreal_ready_returns_when_driver_signals(tmp_path):
    ready_path = tmp_path / "ready"
    ready_path.write_text("ready\n", encoding="utf-8")
    process = SimpleNamespace(poll=lambda: None)

    run_material_zoo._wait_for_unreal_ready(
        process,
        ready_path,
        tmp_path / "failure.json",
        tmp_path / "unreal.log",
        1.0,
    )


def test_wait_for_unreal_ready_reports_driver_failure(tmp_path):
    failure_path = tmp_path / "failure.json"
    failure_path.write_text('{"error": "connection rejected"}\n', encoding="utf-8")
    process = SimpleNamespace(poll=lambda: None)

    with pytest.raises(RuntimeError, match="connection rejected"):
        run_material_zoo._wait_for_unreal_ready(
            process,
            tmp_path / "ready",
            failure_path,
            tmp_path / "unreal.log",
            1.0,
        )
