"""Tests for usdview launcher environment and presentation options."""

from pathlib import Path
from types import SimpleNamespace

from integrations.usdview import _bootstrap, launcher


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


def test_launch_usdview_bootstrap_prefers_selected_install_python(tmp_path, monkeypatch):
    install = tmp_path / "OpenUSDInstall"
    bin_dir = install / "bin"
    python_dir = install / "lib" / "python"
    interpreter = install / ".venv" / "Scripts" / "python.exe"
    bin_dir.mkdir(parents=True)
    python_dir.mkdir(parents=True)
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    script = bin_dir / "usdview"
    script.write_text(f"#!{interpreter}\n", encoding="utf-8")
    launcher_cmd = bin_dir / "usdview.cmd"
    launcher_cmd.touch()

    captured = {}

    def fake_popen(command, *, env):
        captured["command"] = command
        captured["env"] = env
        return object()

    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "conflicting-python"))
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr("integrations.renderman.dll_dirs", lambda: [])

    launcher.launch_usdview("test_scene.usda", usdview_exe=launcher_cmd)

    env = captured["env"]
    assert env["PYTHONPATH"].split(launcher.os.pathsep)[:2] == [
        str(python_dir),
        str(tmp_path / "conflicting-python"),
    ]
    assert env["PATH"].split(launcher.os.pathsep)[:2] == [
        str(bin_dir),
        str(install / "lib"),
    ]
    assert captured["command"][:3] == [
        str(interpreter),
        str(Path(launcher.__file__).with_name("_bootstrap.py")),
        str(script),
    ]


def test_bootstrap_registers_selected_install_dll_dirs(tmp_path, monkeypatch):
    install = tmp_path / "OpenUSDInstall"
    script = install / "bin" / "usdview"
    script.parent.mkdir(parents=True)
    (install / "lib").mkdir()
    script.touch()
    added = []

    monkeypatch.setattr(_bootstrap.os, "name", "nt")
    monkeypatch.setattr(
        _bootstrap.os,
        "add_dll_directory",
        lambda path: added.append(path) or object(),
        raising=False,
    )

    handles = _bootstrap._add_usd_dll_dirs(install)

    assert added == [str(install / "bin"), str(install / "lib")]
    assert len(handles) == 2


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


def test_main_reports_discovery_error_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(
        launcher,
        "launch_usdview",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("usdview unavailable")),
    )

    assert launcher.main(["test_scene.usda"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: usdview unavailable\n"
