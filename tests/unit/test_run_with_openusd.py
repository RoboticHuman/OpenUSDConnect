"""Tests for the project OpenUSD command wrapper."""

import os
from types import SimpleNamespace

import pytest

from scripts import run_with_openusd


def _usd_install(tmp_path):
    root = tmp_path / "OpenUSD"
    (root / "lib" / "python" / "pxr").mkdir(parents=True)
    (root / "lib" / "python" / "pxr" / "__init__.py").touch()
    (root / "bin").mkdir()
    return root


def test_build_environment_selects_project_runtime(tmp_path, monkeypatch):
    root = _usd_install(tmp_path)
    plugin = tmp_path / "plugins"
    dll = tmp_path / "native"
    rman = tmp_path / "RenderMan"
    for path in (plugin, dll, rman / "bin", rman / "lib"):
        path.mkdir(parents=True)

    monkeypatch.setattr(run_with_openusd, "_loader_path_key", lambda: "PATH")
    env, python_path = run_with_openusd.build_environment(
        root,
        base={"PATH": "existing", "PYTHONPATH": "existing-python"},
        plugin_paths=[plugin],
        dll_dirs=[dll],
        renderman_root=rman,
    )

    assert python_path == root / "lib" / "python"
    assert env[run_with_openusd.USD_ROOT_ENV] == str(root)
    assert env["PYTHONPATH"].split(os.pathsep)[:2] == [str(python_path), "existing-python"]
    assert env["PXR_PLUGINPATH_NAME"] == str(plugin)
    assert str(dll) in env["PATH"].split(os.pathsep)
    assert env["RMANTREE"] == str(rman)
    assert str(root / "plugin" / "usd") in env["RMAN_TEXTUREPATH"]


def test_main_forwards_command_and_exit_code(tmp_path, monkeypatch):
    root = _usd_install(tmp_path)
    captured = {}

    def fake_run(command, *, env, check):
        captured.update(command=command, env=env, check=check)
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(run_with_openusd.subprocess, "run", fake_run)

    result = run_with_openusd.main(
        ["--usd-root", str(root), "--", "python", "-c", "print('ok')"]
    )

    assert result == 7
    assert captured["command"] == [
        run_with_openusd.sys.executable,
        "-c",
        "print('ok')",
    ]
    assert captured["env"][run_with_openusd.USD_ROOT_ENV] == str(root)
    assert captured["check"] is False


def test_build_environment_rejects_missing_plugin_path(tmp_path):
    root = _usd_install(tmp_path)

    with pytest.raises(RuntimeError, match="plugin path does not exist"):
        run_with_openusd.build_environment(root, plugin_paths=[tmp_path / "missing"])
