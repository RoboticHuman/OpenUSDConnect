"""Blender smoke for live-open through the WebDAV snapshot."""

import os
import socket
import subprocess

import pytest

from tests.helpers import PROJECT_ROOT, read_results

pytest.importorskip("wsgidav")

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "scripts")
LIVE_OPEN_SCRIPT = os.path.join(SCRIPTS_DIR, "live_open_smoke.py")


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run_live_open_smoke(
    blender_exe,
    tmp_path,
    sync_port,
    *,
    require_token=False,
    auto_start_emitter=True,
    auto_start_receiver=True,
):
    vfs_port = _free_port()
    results_path = str(tmp_path / "live_open_results.json")

    env = os.environ.copy()
    env["BLENDER_USER_RESOURCES"] = os.path.join(PROJECT_ROOT, ".blender", "user_data")
    cmd = [
        blender_exe,
        "--factory-startup",
        "--background",
        "--python",
        LIVE_OPEN_SCRIPT,
        "--",
        "--port",
        str(sync_port),
        "--vfs-port",
        str(vfs_port),
        "--out",
        results_path,
    ]
    if require_token:
        cmd.append("--require-token")
    if not auto_start_emitter:
        cmd.append("--no-auto-emitter")
    if not auto_start_receiver:
        cmd.append("--no-auto-receiver")
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=PROJECT_ROOT,
    )
    print("=== Blender live-open stdout ===")
    print(result.stdout)
    if result.stderr:
        print("=== Blender live-open stderr ===")
        print(result.stderr)
    assert result.returncode == 0, result.stdout + "\n" + result.stderr

    results = read_results(results_path, "Live Open")
    failures = {k: v for k, v in results.items() if v != "PASS"}
    assert not failures, f"Live-open failures: {failures}"


def test_blender_live_open_auto_connect(blender_exe, tmp_path, free_port):
    _run_live_open_smoke(blender_exe, tmp_path, free_port)


def test_blender_live_open_auto_connect_with_tofu_token(blender_exe, tmp_path, free_port):
    _run_live_open_smoke(blender_exe, tmp_path, free_port, require_token=True)


def test_blender_live_open_can_defer_auto_start(blender_exe, tmp_path, free_port):
    _run_live_open_smoke(
        blender_exe,
        tmp_path,
        free_port,
        auto_start_emitter=False,
        auto_start_receiver=False,
    )
