"""Pytest wrappers for MaterialX integration tests.

Skipped if Blender is not configured (see conftest.py for options).
"""

import os
import subprocess
import sys

import pytest

from tests.helpers import PROJECT_ROOT, read_results, run_blender, start_server, stop_server

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "scripts")
TEST_SCRIPT = os.path.join(SCRIPTS_DIR, "materialx_test_script.py")
MTLX_REF_EMITTER = os.path.join(SCRIPTS_DIR, "mtlx_ref_emitter_script.py")
MTLX_REF_RECEIVER = os.path.join(SCRIPTS_DIR, "mtlx_ref_receiver_script.py")
TEAPOT_ASSET = os.path.join(
    PROJECT_ROOT, "assets", "intent-vfx", "assets", "teapot", "teapot.usd",
)


@pytest.mark.materialx
def test_materialx_standard_surface(blender_exe):
    """Run MaterialX mapper tests inside headless Blender."""
    env = os.environ.copy()
    env["BLENDER_USER_RESOURCES"] = os.path.join(
        PROJECT_ROOT, ".blender", "user_data",
    )
    result = subprocess.run(
        [blender_exe, "--background", "--python", TEST_SCRIPT],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)
    assert result.returncode == 0, (
        f"MaterialX tests failed:\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.materialx
def test_materialx_reference_pipeline(blender_exe, tmp_path, free_port):
    """Full pipeline: emitter sends teapot reference → server → receiver.

    Verifies hierarchy collapse (no redundant root) and MaterialX
    enrichment (ActivisionMtlxMapper creates Standard Surface network).
    """
    port = free_port
    server_proc = start_server(tmp_path, port)
    try:
        # Send teapot reference
        emitter_result = subprocess.run(
            [
                sys.executable, MTLX_REF_EMITTER,
                "--port", str(port),
                "--asset-path", TEAPOT_ASSET,
            ],
            cwd=PROJECT_ROOT,
            capture_output=True, text=True, timeout=15,
        )
        print("=== Emitter ===")
        print(emitter_result.stdout)
        assert emitter_result.returncode == 0, emitter_result.stderr

        # Receive and verify in Blender
        results_path = str(tmp_path / "mtlx_ref_results.json")
        blender_result = run_blender(
            blender_exe, MTLX_REF_RECEIVER, port,
            extra_args=["--out", results_path],
            timeout=30,
        )
        print("=== Receiver ===")
        print(blender_result.stdout)
        if blender_result.stderr:
            print(blender_result.stderr)

        results = read_results(results_path, "mtlx_ref")
        failures = {k: v for k, v in results.items() if v.startswith("FAIL")}
        assert not failures, f"MaterialX reference test failures: {failures}"
    finally:
        stop_server(server_proc)
