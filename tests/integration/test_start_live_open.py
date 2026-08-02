"""End-to-end coverage for the platform-neutral live-open launcher."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from pxr import Usd

pytest.importorskip("wsgidav")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_launcher_starts_and_stops_local_directory_mirror(tmp_path, free_port):
    root = Path(__file__).parents[2]
    script = root / "scripts" / "start_live_open.py"
    state_file = tmp_path / "session.json"
    mirror_dir = tmp_path / "mirror"
    vfs_port = _free_port()
    while vfs_port == free_port:
        vfs_port = _free_port()
    start = [
        sys.executable,
        str(script),
        "--base",
        str(root / "tests" / "fixtures" / "test_asset.usda"),
        "--port",
        str(free_port),
        "--vfs-port",
        str(vfs_port),
        "--mirror-dir",
        str(mirror_dir),
        "--state-file",
        str(state_file),
        "--log-dir",
        str(tmp_path / "logs"),
        "--startup-timeout",
        "10",
    ]
    if os.name == "nt":
        start.append("--no-drive")
    stop = [
        sys.executable,
        str(script),
        "stop",
        "--state-file",
        str(state_file),
    ]
    stopped = False
    try:
        result = subprocess.run(start, cwd=root, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr

        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["exposure_kind"] == "local-directory"
        assert state["drive"] == ""
        assert state["file_path"] == str(mirror_dir / "scene.usd")
        stage = Usd.Stage.Open(state["file_path"])
        assert stage is not None

        result = subprocess.run(stop, cwd=root, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        stopped = True
        assert json.loads(state_file.read_text(encoding="utf-8"))["stopped_at"]
    finally:
        if state_file.exists() and not stopped:
            subprocess.run(stop, cwd=root, capture_output=True, text=True, timeout=20)
