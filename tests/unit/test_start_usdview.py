"""Tests for the combined server + usdview launcher."""

from pathlib import Path

from scripts import start_usdview


def test_server_command_forwards_project_environment_options(tmp_path):
    stage = tmp_path / "scene.usda"
    stage.write_text("#usda 1.0", encoding="utf-8")
    args, forwarded = start_usdview._parse_args(
        [
            str(stage),
            "--host",
            "localhost",
            "--layer-mode",
            "shared_stage",
            "--resolver-context",
            "asset:config.json",
            "--plugin-dll-dir",
            "C:/plugins/bin",
            "--renderer",
            "Storm",
        ]
    )

    command = start_usdview._server_command(args, 7312, tmp_path / "events.db")

    assert command[-10:] == [
        "--base",
        str(stage),
        "--event-log",
        str(tmp_path / "events.db"),
        "--layer-mode",
        "shared_stage",
        "--resolver-context",
        "asset:config.json",
        "--plugin-dll-dir",
        "C:/plugins/bin",
    ]
    assert forwarded == ["--renderer", "Storm"]


def test_windows_server_uses_base_interpreter_and_current_environment(monkeypatch):
    monkeypatch.setattr(start_usdview.os, "name", "nt")
    monkeypatch.setattr(start_usdview.sys, "_base_executable", "/base/python.exe")
    monkeypatch.setattr(
        start_usdview.sysconfig,
        "get_paths",
        lambda: {"purelib": "/project/site-packages", "platlib": "/project/site-packages"},
    )

    assert start_usdview._server_python() == "/base/python.exe"
    paths = start_usdview._server_environment()["PYTHONPATH"].split(start_usdview.os.pathsep)
    assert paths[:2] == [str(start_usdview.PROJECT_ROOT), "/project/site-packages"]


def test_main_stops_server_when_usdview_exits(monkeypatch, tmp_path):
    stage = tmp_path / "scene.usda"
    stage.write_text("#usda 1.0", encoding="utf-8")
    stopped = []

    class FakeProcess:
        def __init__(self, *, pid, return_codes):
            self.pid = pid
            self._return_codes = iter(return_codes)
            self.returncode = None

        def poll(self):
            try:
                self.returncode = next(self._return_codes)
            except StopIteration:
                pass
            return self.returncode

    server = FakeProcess(pid=100, return_codes=[None, None, None])
    viewer = FakeProcess(pid=200, return_codes=[None, 0])

    monkeypatch.setattr(start_usdview, "_select_port", lambda _host, _port: 7312)
    monkeypatch.setattr(start_usdview, "_wait_for_server", lambda *_args: None)
    monkeypatch.setattr(start_usdview.subprocess, "Popen", lambda *_args, **_kwargs: server)
    monkeypatch.setattr(start_usdview.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(start_usdview, "_stop_process", lambda process: stopped.append(process))
    monkeypatch.setattr("integrations.usdview.launcher.find_usdview", lambda: Path("usdview"))
    monkeypatch.setattr(
        "integrations.usdview.launcher.launch_usdview", lambda *_args, **_kwargs: viewer
    )

    result = start_usdview.main([str(stage)])

    assert result == 0
    assert stopped == [viewer, server]
