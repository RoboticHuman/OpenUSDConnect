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
