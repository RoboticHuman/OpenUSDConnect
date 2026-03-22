"""Pytest wrapper for MaterialX Standard Surface mapper tests.

Runs materialx_test_script.py inside Blender's Python via --background.
Skipped if Blender is not configured (see conftest.py for options).
"""

import os
import subprocess

import pytest

from tests.helpers import PROJECT_ROOT

TEST_SCRIPT = os.path.join(
    os.path.dirname(__file__), "scripts", "materialx_test_script.py",
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
