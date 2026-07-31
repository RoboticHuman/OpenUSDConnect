"""Conftest for asset integration tests.

These tests are heavy (each launches Blender) so they're skipped by default.
Run with: uv run pytest tests/integration/asset_tests/ --asset-tests -v
"""

import pytest

# Only collect the pytest wrapper (test_assets.py), not the Blender scripts
# (test_bishop.py, etc.) which import bpy and can't run under pytest directly.
collect_ignore_glob = ["test_bishop.py", "test_teapot_*.py", "test_two_*.py",
                       "test_vehicles.py", "test_camera_scene.py",
                       "test_playback_*.py", "test_chair_replay.py",
                       "test_material_zoo_replay.py",
                       "test_headless_time_samples_to_blender.py",
                       "diag_*.py"]


def pytest_addoption(parser):
    try:
        parser.addoption(
            "--asset-tests",
            action="store_true",
            default=False,
            help="Run heavy asset integration tests (requires Blender + ~5min)",
        )
    except ValueError:
        pass  # already registered by parent conftest


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--asset-tests"):
        skip = pytest.mark.skip(reason="asset tests disabled (use --asset-tests)")
        for item in items:
            if "asset_tests" in str(item.fspath):
                item.add_marker(skip)
