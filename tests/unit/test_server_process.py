from __future__ import annotations

from integrations import server_process


def test_command_uses_renderer_aware_wrapper(monkeypatch):
    monkeypatch.setattr(server_process, "python_executable", lambda: "python-real")

    assert server_process.command(["--port", "7312"]) == [
        "python-real",
        "-m",
        "integrations.run_server",
        "--port",
        "7312",
    ]


def test_start_passes_project_environment_to_real_process(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(server_process, "command", lambda args: ["server", *args])
    monkeypatch.setattr(
        server_process,
        "server_environment",
        lambda root, base: {"PROJECT_ROOT": str(root), **base},
    )
    monkeypatch.setattr(
        server_process.subprocess,
        "Popen",
        lambda command, **kwargs: captured.update(command=command, kwargs=kwargs) or object(),
    )

    server_process.start(
        ["--port", "7312"],
        project_root=tmp_path,
        env={"CUSTOM": "yes"},
        stdout=-1,
    )

    assert captured["command"] == ["server", "--port", "7312"]
    assert captured["kwargs"]["cwd"] == str(tmp_path.resolve())
    assert captured["kwargs"]["env"]["CUSTOM"] == "yes"
    assert captured["kwargs"]["stdout"] == -1


def test_wait_reports_child_exit_without_waiting():
    class Process:
        def poll(self):
            return 2

    try:
        server_process.wait_until_listening(Process(), "127.0.0.1", 7312, timeout=10)
    except RuntimeError as error:
        assert "code 2" in str(error)
    else:
        raise AssertionError("expected child exit to fail startup")
