"""Asset integration tests — pytest wrappers that launch Blender.

Each test starts a server, runs a Blender script, and checks for SUCCESS
in the output. Skipped by default; enable with --asset-tests flag.

Usage:
    uv run pytest tests/integration/asset_tests/ --asset-tests -v
"""

import os
import subprocess
import sys

import pytest

from tests.helpers import run_blender, start_server, stop_server

SCRIPTS_DIR = os.path.dirname(__file__)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPTS_DIR)))


@pytest.fixture(scope="session", autouse=True)
def _build_addon():
    """Rebuild the addon zip before running asset tests."""
    result = subprocess.run(
        [sys.executable, "scripts/build_blender_addon.py"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Addon build failed:\n{result.stderr}"


def _run_asset_test(blender_exe, tmp_path, script_name, port, timeout=90):
    """Run an asset test script in Blender and assert SUCCESS."""
    script = os.path.join(SCRIPTS_DIR, script_name)
    server = start_server(tmp_path, port)
    try:
        r = run_blender(blender_exe, script, port, timeout=timeout,
                        background=False)
        print(f"\n=== {script_name} stdout ===")
        print(r.stdout[-2000:] if len(r.stdout) > 2000 else r.stdout)
        if r.stderr:
            print("=== stderr ===")
            print(r.stderr[-500:])
        assert "SUCCESS" in r.stdout, (
            f"{script_name} did not print SUCCESS.\n"
            f"Last output: {r.stdout[-500:]}"
        )
    finally:
        stop_server(server)


def test_bishop_materialx(blender_exe, tmp_path):
    """Bishop: MaterialX multi-node network with texture connections."""
    _run_asset_test(blender_exe, tmp_path, "test_bishop.py", 7210)


def test_teapot_variants(blender_exe, tmp_path):
    """Teapot: variant switching with interleaved live editing."""
    _run_asset_test(blender_exe, tmp_path, "test_teapot_variants.py", 7211,
                    timeout=120)


def test_two_teapots_identity(blender_exe, tmp_path):
    """Two Teapots: path-based material identity separation."""
    _run_asset_test(blender_exe, tmp_path, "test_two_teapots.py", 7212)


def test_vehicles_multi_binding(blender_exe, tmp_path):
    """Vehicles 4WD: multiple material bindings per asset."""
    _run_asset_test(blender_exe, tmp_path, "test_vehicles.py", 7213)






