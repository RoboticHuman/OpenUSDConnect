"""Tests for usdview launcher environment and presentation options."""

from pathlib import Path
from types import SimpleNamespace

from integrations.usdview import launcher


def test_resolve_command_bootstraps_shebang_interpreter(tmp_path):
    interpreter = tmp_path / "python.exe"
    interpreter.touch()
    script = tmp_path / "usdview"
    script.write_text(f"#!{interpreter}\n", encoding="utf-8")

    command = launcher._resolve_command(script)

    assert command == [
        str(interpreter),
        str(Path(launcher.__file__).with_name("_bootstrap.py")),
        str(script),
    ]


def test_launch_usdview_forwards_presentation_environment(monkeypatch):
    captured = {}
    process = object()

    def fake_popen(command, *, env):
        captured["command"] = command
        captured["env"] = env
        return process

    monkeypatch.setattr(launcher, "_resolve_command", lambda _exe: ["usdview"])
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr("integrations.renderman.dll_dirs", lambda: [])

    result = launcher.launch_usdview(
        "test_scene.usda",
        host="127.0.0.1",
        port=7312,
        usdview_exe="usdview",
        camera_path="/World/_TestCam",
        expected_seq=262,
        scene_lights=True,
    )

    assert result is process
    assert captured["command"] == ["usdview", "test_scene.usda"]
    env = captured["env"]
    assert env["OPENUSDCONNECT_HOST"] == "127.0.0.1"
    assert env["OPENUSDCONNECT_PORT"] == "7312"
    assert env["OPENUSDCONNECT_CAMERA_PATH"] == "/World/_TestCam"
    assert env["OPENUSDCONNECT_EXPECTED_SEQ"] == "262"
    assert env["OPENUSDCONNECT_SCENE_LIGHTS"] == "1"


def test_main_accepts_presentation_arguments(monkeypatch):
    captured = {}

    def fake_launch(stage, **options):
        captured["stage"] = stage
        captured.update(options)
        return SimpleNamespace(wait=lambda: 0)

    monkeypatch.setattr(launcher, "launch_usdview", fake_launch)

    result = launcher.main(
        [
            "test_scene.usda",
            "--host",
            "localhost",
            "--port",
            "7313",
            "--camera",
            "/World/_TestCam",
            "--expected-seq",
            "262",
            "--scene-lights",
        ]
    )

    assert result == 0
    assert captured["stage"] == "test_scene.usda"
    assert captured["host"] == "localhost"
    assert captured["port"] == 7313
    assert captured["camera_path"] == "/World/_TestCam"
    assert captured["expected_seq"] == 262
    assert captured["scene_lights"] is True
