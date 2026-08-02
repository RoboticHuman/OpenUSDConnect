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

    def fake_start_process(command, _log_path):
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
    monkeypatch.setattr(launcher, "_wait_for_http", lambda _url, _timeout: None)
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
    ]
    if no_drive:
        args.append("--no-drive")

    config = launcher._parse_start_config(args, is_windows=is_windows)
    assert launcher._run_start(config) == 0

    bridge_command = commands[1]
    assert bridge_command[1].endswith("local_vfs_bridge.py")
    assert bridge_command[bridge_command.index("--mirror-dir") + 1] == str(mirror_dir)
    if expected_drive:
        assert bridge_command[bridge_command.index("--drive") + 1] == expected_drive
    else:
        assert "--drive" not in bridge_command
    assert ("--no-drive" in bridge_command) is (is_windows and no_drive)
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["exposure_kind"] == bridge_state["exposure_kind"]
    assert state["file_path"] == bridge_state["file_path"]


def test_stop_help_only_shows_drive_override_on_windows():
    launcher = _load_launcher()

    assert "--drive" not in launcher._build_stop_parser(is_windows=False).format_help()
    assert "--drive" in launcher._build_stop_parser(is_windows=True).format_help()
