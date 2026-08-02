from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_bridge():
    path = Path(__file__).parents[2] / "scripts" / "local_vfs_bridge.py"
    name = "local_vfs_bridge"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_status_file_roundtrip(tmp_path, capsys):
    bridge = _load_bridge()
    status_file = tmp_path / "status.json"

    bridge._write_status(
        status_file,
        state="running",
        exposure_kind="local-directory",
        root_path=str(tmp_path),
        file_path=str(tmp_path / "scene.usd"),
        etag='"1-2"',
    )
    assert bridge.main(["status", "--status-file", str(status_file)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "running"
    assert payload["exposure_kind"] == "local-directory"
    assert payload["file_path"] == str(tmp_path / "scene.usd")
    assert payload["etag"] == '"1-2"'
    assert payload["updated_at"]


def test_default_control_files_live_outside_mirror(tmp_path):
    bridge = _load_bridge()
    mount_dir = tmp_path / "usd"

    assert bridge._default_status_file(mount_dir) == (
        tmp_path / "bridge" / "openusdconnect_bridge_status.json"
    )
    assert bridge._default_log_file(mount_dir) == (
        tmp_path / "bridge" / "openusdconnect_bridge.log"
    )


def test_control_files_are_removed_from_mirror(tmp_path):
    bridge = _load_bridge()
    mount_dir = tmp_path / "usd"
    mount_dir.mkdir()
    status = mount_dir / "openusdconnect_bridge_status.json"
    log = mount_dir / "openusdconnect_bridge.log"
    scene = mount_dir / "scene.usd"
    status.write_text("{}", encoding="utf-8")
    log.write_text("log", encoding="utf-8")
    scene.write_text("#usda 1.0\n", encoding="utf-8")

    bridge._remove_control_files_from_mirror(mount_dir)

    assert not status.exists()
    assert not log.exists()
    assert scene.exists()


def test_content_changed_ignores_same_bytes(tmp_path):
    bridge = _load_bridge()
    path = tmp_path / "scene.usd"
    path.write_bytes(b"#usda 1.0\n")
    initial_hash = bridge._hash_file(path)

    path.write_bytes(b"#usda 1.0\n")
    changed, current_hash = bridge._content_changed(path, initial_hash)

    assert changed is False
    assert current_hash == initial_hash


def test_content_changed_detects_new_bytes(tmp_path):
    bridge = _load_bridge()
    path = tmp_path / "scene.usd"
    path.write_bytes(b"#usda 1.0\n")
    initial_hash = bridge._hash_file(path)

    path.write_bytes(b'#usda 1.0\ndef Xform "World" {}\n')
    changed, current_hash = bridge._content_changed(path, initial_hash)

    assert changed is True
    assert current_hash != initial_hash


def test_upload_sends_if_match_header(tmp_path, monkeypatch):
    bridge = _load_bridge()
    path = tmp_path / "scene.usd"
    path.write_bytes(b"#usda 1.0\n")
    calls = []

    def fake_request(method, url, body=None, headers=None):
        calls.append((method, url, body, headers))
        return 200, {}, b""

    monkeypatch.setattr(bridge, "_request", fake_request)

    bridge._upload("http://127.0.0.1:7280/usd/scene.usd", path, '"1-2"')

    assert calls == [
        (
            "PUT",
            "http://127.0.0.1:7280/usd/scene.usd",
            b"#usda 1.0\n",
            {"If-Match": '"1-2"'},
        )
    ]


def test_directory_exposure_uses_local_paths(tmp_path):
    bridge = _load_bridge()

    exposure = bridge._prepare_exposure(
        tmp_path,
        "scene.usd",
        config=bridge.DirectoryExposureConfig(),
    )

    assert exposure.kind == "local-directory"
    assert exposure.root_path == str(tmp_path)
    assert exposure.file_path == str(tmp_path / "scene.usd")
    assert exposure.drive == ""


def test_bridge_help_only_shows_platform_exposure_options():
    bridge = _load_bridge()

    macos_help = bridge._build_run_parser(is_windows=False).format_help()
    windows_help = bridge._build_run_parser(is_windows=True).format_help()

    for option in ("--drive", "--no-drive", "--force", "--release-on-exit"):
        assert option not in macos_help
        assert option in windows_help


def test_bridge_parser_rejects_windows_exposure_options_on_other_platforms():
    bridge = _load_bridge()

    with pytest.raises(SystemExit):
        bridge._parse_bridge_config(["--drive", "O:"], is_windows=False)


@pytest.mark.parametrize("modifier", ["--force", "--release-on-exit"])
def test_bridge_parser_rejects_drive_modifiers_for_directory_exposure(modifier):
    bridge = _load_bridge()

    with pytest.raises(SystemExit) as error:
        bridge._parse_bridge_config(["--no-drive", modifier], is_windows=True)

    assert error.value.code == 2


def test_bridge_parser_builds_typed_platform_exposure_configs(tmp_path):
    bridge = _load_bridge()

    directory = bridge._parse_bridge_config(
        ["--mirror-dir", str(tmp_path)],
        is_windows=False,
    )
    drive = bridge._parse_bridge_config(
        ["--mirror-dir", str(tmp_path), "--force", "--release-on-exit"],
        is_windows=True,
    )

    assert isinstance(directory.exposure, bridge.DirectoryExposureConfig)
    assert isinstance(drive.exposure, bridge.WindowsDriveExposureConfig)
    assert drive.exposure.drive == "O:"
    assert drive.exposure.force is True
    assert drive.exposure.release_on_exit is True


def test_windows_exposure_uses_subst(tmp_path, monkeypatch):
    bridge = _load_bridge()
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(bridge, "_is_windows", lambda: True)
    monkeypatch.setattr(bridge.subprocess, "run", fake_run)

    exposure = bridge._prepare_exposure(
        tmp_path,
        "scene.usd",
        config=bridge.WindowsDriveExposureConfig(
            drive="O:",
            force=True,
            release_on_exit=False,
        ),
    )
    bridge._release_exposure(exposure)

    assert exposure.kind == "windows-drive"
    assert exposure.root_path == "O:\\"
    assert exposure.file_path == "O:\\scene.usd"
    assert calls == [
        ["subst", "O:", "/D"],
        ["subst", "O:", str(tmp_path)],
        ["subst", "O:", "/D"],
    ]


def test_stop_releases_windows_drive_and_process(tmp_path, monkeypatch):
    bridge = _load_bridge()
    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps(
            {
                "pid": 12345,
                "drive": "Q:",
                "exposure_kind": "windows-drive",
                "root_path": "Q:\\",
                "file_path": "Q:\\scene.usd",
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(bridge, "_is_windows", lambda: True)
    monkeypatch.setattr(bridge.subprocess, "run", fake_run)

    assert (
        bridge.main(
            [
                "stop",
                "--status-file",
                str(status_file),
                "--stop-process",
            ]
        )
        == 0
    )

    assert ["taskkill", "/PID", "12345", "/T", "/F"] in calls
    assert ["subst", "Q:", "/D"] in calls
    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert payload["state"] == "stopped"
    assert payload["pid"] == 0
    assert payload["file_path"] == "Q:\\scene.usd"


def test_stop_directory_exposure_does_not_release_drive(tmp_path, monkeypatch):
    bridge = _load_bridge()
    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps(
            {
                "pid": 0,
                "drive": "",
                "exposure_kind": "local-directory",
                "root_path": str(tmp_path),
                "file_path": str(tmp_path / "scene.usd"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bridge, "_is_windows", lambda: False)
    monkeypatch.setattr(
        bridge,
        "_unsubst",
        lambda _drive: pytest.fail("directory exposure must not call subst"),
    )

    assert bridge.main(["stop", "--status-file", str(status_file)]) == 0
    assert json.loads(status_file.read_text(encoding="utf-8"))["state"] == "stopped"


def test_stop_uses_explicit_pid_when_status_is_missing(monkeypatch):
    bridge = _load_bridge()
    calls = []
    monkeypatch.setattr(bridge, "_is_windows", lambda: False)
    monkeypatch.setattr(bridge.os, "kill", lambda pid, signal: calls.append((pid, signal)))

    assert bridge.main(["stop", "--pid", "12345", "--stop-process"]) == 0

    assert calls == [(12345, bridge.signal.SIGTERM)]


def test_stop_help_only_shows_drive_override_on_windows():
    bridge = _load_bridge()

    assert "--drive" not in bridge._build_stop_parser(is_windows=False).format_help()
    assert "--drive" in bridge._build_stop_parser(is_windows=True).format_help()


def test_open_uses_macos_open(monkeypatch):
    bridge = _load_bridge()
    calls = []
    monkeypatch.setattr(bridge, "_is_windows", lambda: False)
    monkeypatch.setattr(bridge, "_is_macos", lambda: True)
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda command: calls.append(command))

    bridge._maybe_open("/tmp/usd")

    assert calls == [["/usr/bin/open", "/tmp/usd"]]
