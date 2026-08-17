from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_launcher():
    path = Path(__file__).parents[2] / "scripts" / "start_live_open.py"
    name = "start_live_open"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _Process:
    def __init__(self, pid: int, returncode=None):
        self.pid = pid
        self._returncode = returncode

    def poll(self):
        return self._returncode


def test_wait_for_bridge_requires_complete_running_status(tmp_path):
    launcher = _load_launcher()
    status_file = tmp_path / "status.json"
    status_file.write_text(json.dumps({"state": "running"}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="missing"):
        launcher._wait_for_bridge(status_file, _Process(123, returncode=1), 0.1)

    expected = {
        "state": "running",
        "exposure_kind": "local-directory",
        "root_path": str(tmp_path),
        "file_path": str(tmp_path / "scene.usd"),
    }
    status_file.write_text(json.dumps(expected), encoding="utf-8")

    assert launcher._wait_for_bridge(status_file, _Process(123), 0.1) == expected


def test_start_help_only_shows_windows_exposure_options_on_windows():
    launcher = _load_launcher()

    macos_help = launcher._build_start_parser(is_windows=False).format_help()
    windows_help = launcher._build_start_parser(is_windows=True).format_help()

    for option in ("--drive", "--no-drive", "--force"):
        assert option not in macos_help
        assert option in windows_help


def test_start_parser_rejects_windows_exposure_options_on_other_platforms(tmp_path):
    launcher = _load_launcher()

    with pytest.raises(SystemExit):
        launcher._parse_start_config(
            ["--base", str(tmp_path / "base.usda"), "--drive", "O:"],
            is_windows=False,
        )


def test_start_parser_rejects_force_without_drive_exposure(tmp_path):
    launcher = _load_launcher()

    with pytest.raises(SystemExit) as error:
        launcher._parse_start_config(
            ["--base", str(tmp_path / "base.usda"), "--no-drive", "--force"],
            is_windows=True,
        )

    assert error.value.code == 2


def test_start_parser_builds_typed_platform_exposure_configs(tmp_path):
    launcher = _load_launcher()
    base_args = ["--base", str(tmp_path / "base.usda")]

    directory = launcher._parse_start_config(base_args, is_windows=False)
    drive = launcher._parse_start_config([*base_args, "--force"], is_windows=True)

    assert isinstance(directory.exposure, launcher.DirectoryExposureConfig)
    assert isinstance(drive.exposure, launcher.WindowsDriveExposureConfig)
    assert drive.exposure.drive == "O:"
    assert drive.exposure.force is True


def test_start_parser_uses_canonical_names(tmp_path):
    launcher = _load_launcher()
    base = str(tmp_path / "base.usda")

    canonical = launcher._parse_start_config(
        [
            "--base",
            base,
            "--vfs-host",
            "0.0.0.0",
            "--vfs-share",
            "shots",
            "--vfs-name",
            "live.usd",
            "--vfs-write-mode",
            "drop",
            "--dashboard-port",
            "8080",
            "--startup-timeout",
            "3",
        ],
        is_windows=False,
    )
    assert (canonical.vfs_host, canonical.vfs_share, canonical.vfs_name) == (
        "0.0.0.0",
        "shots",
        "live.usd",
    )
    assert canonical.vfs_write_mode == "drop"
    assert canonical.dashboard_port == 8080
    assert canonical.startup_timeout == 3.0


@pytest.mark.parametrize(
    "alias",
    ["--write-mode", "--bypass-write-validation", "--dashboard", "--wait"],
)
def test_start_parser_rejects_removed_aliases(tmp_path, alias):
    launcher = _load_launcher()
    args = ["--base", str(tmp_path / "base.usda"), alias]
    if alias != "--bypass-write-validation":
        args.append("1")

    with pytest.raises(SystemExit) as error:
        launcher._parse_start_config(args, is_windows=False)

    assert error.value.code == 2


@pytest.mark.parametrize(
    ("is_windows", "no_drive", "expected_drive"),
    [(False, False, ""), (True, False, "O:"), (True, True, "")],
)
def test_start_uses_platform_local_exposure(
    tmp_path,
    monkeypatch,
    is_windows,
    no_drive,
    expected_drive,
):
    launcher = _load_launcher()
    commands = []

    def fake_start_process(command, _log_path, **_kwargs):
        commands.append(command)
        return _Process(1000 + len(commands))

    mirror_dir = tmp_path / "mirror"
    bridge_state = {
        "state": "running",
        "pid": 1002,
        "exposure_kind": "windows-drive" if expected_drive else "local-directory",
        "root_path": "O:\\" if expected_drive else str(mirror_dir),
        "drive": expected_drive,
        "file_path": "O:\\scene.usd" if expected_drive else str(mirror_dir / "scene.usd"),
    }
    monkeypatch.setattr(launcher, "_is_windows", lambda: is_windows)
    monkeypatch.setattr(launcher, "_start_process", fake_start_process)
    monkeypatch.setattr(
        launcher,
        "_wait_for_http",
        lambda _url, _timeout, **_kwargs: {},
    )
    monkeypatch.setattr(
        launcher,
        "_wait_for_bridge",
        lambda _path, _process, _timeout: bridge_state,
    )
    state_file = tmp_path / "session.json"
    args = [
        "--base",
        str(tmp_path / "base.usda"),
        "--mirror-dir",
        str(mirror_dir),
        "--state-file",
        str(state_file),
        "--log-dir",
        str(tmp_path / "logs"),
        "--vfs-host",
        "0.0.0.0",
        "--vfs-port",
        "7391",
        "--vfs-share",
        "shots",
        "--vfs-name",
        "live.usda",
        "--advertise-host",
        "renderbox.local",
    ]
    if no_drive:
        args.append("--no-drive")

    config = launcher._parse_start_config(args, is_windows=is_windows)
    assert launcher._run_start(config) == 0

    bridge_command = commands[1]
    assert bridge_command[1].endswith("local_vfs_bridge.py")
    assert bridge_command[bridge_command.index("--mirror-dir") + 1] == str(mirror_dir)
    assert "--vfs-url" in bridge_command
    server_command = commands[0]
    assert "--event-log" in server_command
    assert server_command[server_command.index("--vfs-host") + 1] == "0.0.0.0"
    assert server_command[server_command.index("--vfs-port") + 1] == "7391"
    assert server_command[server_command.index("--vfs-share") + 1] == "shots"
    assert server_command[server_command.index("--vfs-name") + 1] == "live.usda"
    assert server_command[server_command.index("--advertise-host") + 1] == "renderbox.local"
    expected_url = "http://renderbox.local:7391/shots/live.usda"
    assert bridge_command[bridge_command.index("--vfs-url") + 1] == expected_url
    if expected_drive:
        assert bridge_command[bridge_command.index("--drive") + 1] == expected_drive
    else:
        assert "--drive" not in bridge_command
    assert ("--no-drive" in bridge_command) is (is_windows and no_drive)
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["vfs_url"] == expected_url
    assert state["exposure_kind"] == bridge_state["exposure_kind"]
    assert state["file_path"] == bridge_state["file_path"]


def test_stop_help_only_shows_drive_override_on_windows():
    launcher = _load_launcher()

    assert "--drive" not in launcher._build_stop_parser(is_windows=False).format_help()
    assert "--drive" in launcher._build_stop_parser(is_windows=True).format_help()


def test_wait_for_http_rejects_wrong_scene_identity(monkeypatch):
    launcher = _load_launcher()
    monkeypatch.setattr(
        launcher,
        "_request_live_metadata",
        lambda _url: (200, {"live": True, "scene_id": "other", "port": 7200}),
    )

    with pytest.raises(TimeoutError, match="scene mismatch"):
        launcher._wait_for_http(
            "http://localhost/scene.usd",
            0.01,
            process=_Process(100),
            expected_scene_id="expected",
            expected_sync_port=7200,
        )


def test_wait_for_http_rejects_endpoint_when_child_exits(monkeypatch):
    launcher = _load_launcher()

    class ExitingProcess:
        pid = 100

        def __init__(self):
            self.polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls == 1 else 1

    monkeypatch.setattr(
        launcher,
        "_request_live_metadata",
        lambda _url: (
            200,
            {"live": True, "scene_id": "expected", "port": 7200},
        ),
    )
    monkeypatch.setattr(launcher.time, "sleep", lambda _delay: None)

    with pytest.raises(RuntimeError, match="server exited"):
        launcher._wait_for_http(
            "http://localhost/scene.usd",
            1.0,
            process=ExitingProcess(),
            expected_scene_id="expected",
            expected_sync_port=7200,
        )


def test_active_session_blocks_duplicate_start(tmp_path, monkeypatch):
    launcher = _load_launcher()
    state_file = tmp_path / "session.json"
    state_file.write_text(
        json.dumps({"server_pid": 100, "bridge_pid": 200}),
        encoding="utf-8",
    )
    monkeypatch.setattr(launcher, "_process_exists", lambda pid: pid == 100)

    with pytest.raises(RuntimeError, match="already active"):
        launcher._assert_session_available(state_file)


def test_stop_falls_back_to_recorded_pids(tmp_path, monkeypatch):
    launcher = _load_launcher()
    state_file = tmp_path / "session.json"
    state_file.write_text(
        json.dumps(
            {
                "bridge_pid": 100,
                "server_pid": 200,
                "bridge_status": str(tmp_path / "bridge.json"),
                "drive": "",
            }
        ),
        encoding="utf-8",
    )
    stopped = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(launcher.subprocess, "run", lambda *_args, **_kwargs: Result())
    monkeypatch.setattr(launcher, "_stop_pid", stopped.append)

    assert launcher._run_stop(launcher.StopConfig(state_file=state_file, drive="")) == 0
    assert stopped == [100, 200]
    assert json.loads(state_file.read_text(encoding="utf-8"))["stopped_at"]


def test_stop_failure_is_reported_without_marking_session_stopped(tmp_path, monkeypatch):
    launcher = _load_launcher()
    state_file = tmp_path / "session.json"
    original = {"bridge_pid": 100, "server_pid": 200, "drive": ""}
    state_file.write_text(json.dumps(original), encoding="utf-8")

    class Result:
        returncode = 1
        stdout = ""
        stderr = "helper failed"

    monkeypatch.setattr(launcher.subprocess, "run", lambda *_args, **_kwargs: Result())
    monkeypatch.setattr(
        launcher,
        "_stop_pid",
        lambda pid: (_ for _ in ()).throw(RuntimeError(f"still running {pid}")),
    )

    assert launcher._run_stop(launcher.StopConfig(state_file=state_file, drive="")) == 1
    assert json.loads(state_file.read_text(encoding="utf-8")) == original
