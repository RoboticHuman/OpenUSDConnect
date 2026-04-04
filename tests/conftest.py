"""Root conftest — provides Blender exe path for integration tests.

Resolution order:
1. --blender CLI flag:       uv run pytest --blender /path/to/blender.exe
2. BLENDER_EXE env var:      BLENDER_EXE=/path/to/blender.exe uv run pytest
3. blender.test.cfg file:    single line with the path (in repo root, gitignored)
4. Skip Blender tests if none of the above are set.
"""

import os
import pathlib
import socket

import pytest

_CFG_FILE = pathlib.Path(__file__).parent.parent / "blender.test.cfg"


def _read_cfg() -> str:
    """Read Blender path from config file if it exists."""
    if _CFG_FILE.is_file():
        path = _CFG_FILE.read_text().strip()
        if path and not path.startswith("#"):
            return path
    return ""


def pytest_addoption(parser):
    parser.addoption(
        "--blender",
        action="store",
        default=None,
        help="Path to Blender executable for integration tests",
    )


@pytest.fixture(scope="session")
def blender_exe(request):
    """Resolved Blender executable path, or pytest.skip if unavailable."""
    # 1. CLI flag
    path = request.config.getoption("--blender")
    # 2. Env var
    if not path:
        path = os.environ.get("BLENDER_EXE", "")
    # 3. Config file
    if not path:
        path = _read_cfg()
    # Validate
    if not path or not os.path.isfile(path):
        pytest.skip(f"Blender not found (set --blender, BLENDER_EXE, or {_CFG_FILE.name})")
    return path


@pytest.fixture
def free_port():
    """Return a free TCP port. Binds briefly to let the OS assign one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
