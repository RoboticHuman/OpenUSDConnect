from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_bridge():
    path = Path(__file__).parents[2] / "scripts" / "local_vfs_drive_bridge.py"
    spec = importlib.util.spec_from_file_location("local_vfs_drive_bridge", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_status_file_roundtrip(tmp_path, capsys):
    bridge = _load_bridge()
    status_file = tmp_path / "status.json"

    bridge._write_status(status_file, state="running", drive="O:", etag='"1-2"')
    assert bridge.main(["status", "--status-file", str(status_file)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "running"
    assert payload["drive"] == "O:"
    assert payload["etag"] == '"1-2"'
    assert payload["updated_at"]


def test_default_control_files_live_outside_mount(tmp_path):
    bridge = _load_bridge()
    mount_dir = tmp_path / "usd"

    assert bridge._default_status_file(mount_dir) == (
        tmp_path / "bridge" / "openusdconnect_bridge_status.json"
    )
    assert bridge._default_log_file(mount_dir) == (
        tmp_path / "bridge" / "openusdconnect_bridge.log"
    )


def test_legacy_control_files_are_removed_from_mount(tmp_path):
    bridge = _load_bridge()
    mount_dir = tmp_path / "usd"
    mount_dir.mkdir()
    legacy_status = mount_dir / "openusdconnect_bridge_status.json"
    legacy_log = mount_dir / "openusdconnect_bridge.log"
    scene = mount_dir / "scene.usd"
    legacy_status.write_text("{}", encoding="utf-8")
    legacy_log.write_text("log", encoding="utf-8")
    scene.write_text("#usda 1.0\n", encoding="utf-8")

    bridge._remove_legacy_control_files(mount_dir)

    assert not legacy_status.exists()
    assert not legacy_log.exists()
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

    path.write_bytes(b"#usda 1.0\ndef Xform \"World\" {}\n")
    changed, current_hash = bridge._content_changed(path, initial_hash)

    assert changed is True
    assert current_hash != initial_hash


def test_unmount_invokes_subst_and_optional_process_stop(tmp_path, monkeypatch):
    bridge = _load_bridge()
    status_file = tmp_path / "status.json"
    status_file.write_text(json.dumps({"pid": 12345}), encoding="utf-8")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(bridge.os, "name", "nt")
    monkeypatch.setattr(bridge.subprocess, "run", fake_run)

    assert (
        bridge.main([
            "unmount",
            "--drive",
            "q",
            "--status-file",
            str(status_file),
            "--stop-process",
        ])
        == 0
    )

    assert ["taskkill", "/PID", "12345", "/T", "/F"] in calls
    assert ["subst", "Q:", "/D"] in calls
    assert json.loads(status_file.read_text(encoding="utf-8"))["state"] == "unmounted"
