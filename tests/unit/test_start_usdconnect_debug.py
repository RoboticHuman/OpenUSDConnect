from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_launcher():
    path = Path(__file__).parents[2] / "scripts" / "start_usdconnect_debug.py"
    name = "start_usdconnect_debug"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_start_server_forwards_host_and_canonical_event_log(monkeypatch):
    launcher = _load_launcher()
    calls = []
    monkeypatch.setattr(
        launcher,
        "start_server_process",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    launcher._start_server("10.0.0.5", 7210, "scene.usda", "events.db")

    command, kwargs = calls[0]
    assert command == [
        "--host",
        "10.0.0.5",
        "--port",
        "7210",
        "--base",
        "scene.usda",
        "--event-log",
        "events.db",
    ]
    assert kwargs["project_root"] == launcher.REPO_ROOT


def test_start_blender_forwards_base_before_network_flags(monkeypatch):
    launcher = _load_launcher()
    calls = []
    monkeypatch.setattr(
        launcher.subprocess,
        "Popen",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    launcher._start_blender(
        "blender",
        "B",
        "10.0.0.5",
        7210,
        "scene.usda",
        0,
        False,
        True,
        True,
    )

    command, kwargs = calls[0]
    assert command == [
        "blender",
        "--python",
        str(launcher.BOOTSTRAP_SCRIPT),
        "--",
        "--addon-zip",
        str(launcher.ADDON_ZIP),
        "--host",
        "10.0.0.5",
        "--port",
        "7210",
        "--base",
        "scene.usda",
        "--role",
        "B",
        "--start-emitter",
        "--start-receiver",
    ]
    assert kwargs["cwd"] == str(launcher.REPO_ROOT)
