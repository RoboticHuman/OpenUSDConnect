"""Pytest wrapper for BlenderAdapter headless tests.

Runs blender_adapter_test_script.py inside Blender's Python via --background.
Skipped if Blender is not configured (see conftest.py for options).
"""

import os
import subprocess

TEST_SCRIPT = os.path.join(os.path.dirname(__file__), "blender_adapter_test_script.py")


def test_blender_adapter_headless(blender_exe):
    """Run BlenderAdapter tests inside headless Blender."""
    result = subprocess.run(
        [blender_exe, "--background", "--python", TEST_SCRIPT],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)
    assert result.returncode == 0, f"Blender tests failed:\n{result.stdout}\n{result.stderr}"
