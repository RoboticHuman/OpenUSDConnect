"""Real Blender process coverage for emitter recovery after server outage."""

import json
import os
import subprocess
import time

import pytest
from pxr import UsdGeom

from openusdconnect.server import UsdSyncServer
from tests.helpers import PROJECT_ROOT, start_server, stop_server

SCRIPT = os.path.join(
    PROJECT_ROOT,
    "tests",
    "integration",
    "scripts",
    "blender_emitter_reconnect_script.py",
)
BASE = os.path.join(PROJECT_ROOT, "test_scene.usda")
ADDON = os.path.join(PROJECT_ROOT, "dist", "usd_connect_blender.zip")


def _wait_for(path, *, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path.name}")


def test_blender_emitter_reconnects_and_publishes_offline_edit(
    blender_exe,
    tmp_path,
    free_port,
):
    build = subprocess.run(
        [os.sys.executable, "scripts/build_blender_addon.py"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stderr

    port = free_port
    control = tmp_path / "control"
    control.mkdir()
    output_path = tmp_path / "blender-reconnect.log"
    env = os.environ.copy()
    env["BLENDER_USER_RESOURCES"] = os.path.join(PROJECT_ROOT, ".blender", "user_data")
    server = start_server(tmp_path, port, base_path=BASE)
    blender = None
    output = open(output_path, "w", encoding="utf-8")
    try:
        blender = subprocess.Popen(
            [
                blender_exe,
                "--factory-startup",
                "--python",
                SCRIPT,
                "--",
                "--port",
                str(port),
                "--base",
                BASE,
                "--addon",
                ADDON,
                "--control-dir",
                str(control),
            ],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        _wait_for(control / "ready")
        stop_server(server)
        server = None
        (control / "edit-now").write_text("", encoding="utf-8")
        _wait_for(control / "offline-edit-authored")
        server = start_server(tmp_path, port, base_path=BASE)
        _wait_for(control / "result.json", timeout=30.0)
        blender.wait(timeout=10)
        result = json.loads((control / "result.json").read_text(encoding="utf-8"))
        assert result["status"] == "PASS", result
        assert result["pending"] == 0
        assert result["acknowledged_events"] >= 1
    finally:
        if blender is not None and blender.poll() is None:
            blender.terminate()
            try:
                blender.wait(timeout=5)
            except subprocess.TimeoutExpired:
                blender.kill()
        if server is not None:
            stop_server(server)
        output.close()

    restarted = UsdSyncServer(
        base_usd_path=BASE,
        log_path=str(tmp_path / f"events_{port}.db"),
    )
    try:
        translate = (
            UsdGeom.Xformable(restarted.stage.GetPrimAtPath("/World/Cube"))
            .GetOrderedXformOps()[0]
            .Get()
        )
        assert translate[0] == pytest.approx(7.5, abs=1e-5)
    finally:
        restarted.shutdown()
        restarted.store.close()
