from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_script(name: str):
    path = Path(__file__).parents[2] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_vscode_tasks_invoke_the_maintained_python_launcher():
    setup = _load_script("setup_vscode")

    for task in setup.TASKS_JSON["tasks"]:
        assert task["command"] == "uv"
        assert "scripts/start_usdconnect_debug.py" in task["args"]
        assert not any(argument.endswith(".ps1") for argument in task["args"])
