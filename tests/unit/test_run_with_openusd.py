"""Tests for the project OpenUSD command wrapper."""

import os
from types import SimpleNamespace

import pytest

from scripts import openusd_runtime, run_with_openusd


def _usd_install(tmp_path):
    root = tmp_path / "OpenUSD"
    (root / "lib" / "python" / "pxr").mkdir(parents=True)
    (root / "lib" / "python" / "pxr" / "__init__.py").touch()
    (root / "bin").mkdir()
    return root


def _pxr_package(path):
    (path / "pxr").mkdir(parents=True)
    (path / "pxr" / "__init__.py").touch()
    return path


def test_build_environment_selects_project_runtime(tmp_path, monkeypatch):
    root = _usd_install(tmp_path)
    plugin = tmp_path / "plugins"
    dll = tmp_path / "native"
    rman = tmp_path / "RenderMan"
    for path in (plugin, dll, rman / "bin", rman / "lib"):
        path.mkdir(parents=True)

    monkeypatch.setattr(openusd_runtime, "_loader_path_key", lambda: "PATH")
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


@pytest.mark.parametrize(
    "relative_path",
    [
        ("Lib", "site-packages"),
        ("lib", "python3.13", "site-packages"),
        ("lib64", "python3.13", "dist-packages"),
    ],
)
def test_build_environment_discovers_current_openusd_python_layouts(
    tmp_path, relative_path
):
    root = tmp_path / "OpenUSD"
    root.mkdir()
    expected = _pxr_package(root.joinpath(*relative_path))

    _, python_path = run_with_openusd.build_environment(root, base={})

    assert python_path == expected


def test_build_environment_accepts_python_bindings_outside_prefix(tmp_path):
    root = tmp_path / "OpenUSD"
    root.mkdir()
    python_path = _pxr_package(tmp_path / "venv" / "Lib" / "site-packages")

    env, selected = run_with_openusd.build_environment(
        root, base={}, python_path=python_path
    )

    assert selected == python_path
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(python_path)


def test_build_environment_uses_inherited_rmantree(tmp_path):
    root = _usd_install(tmp_path)
    rman = tmp_path / "RenderMan"
    (rman / "bin").mkdir(parents=True)
    (rman / "lib").mkdir()
    loader_key = openusd_runtime._loader_path_key()
    base = {"RMANTREE": str(rman), "PATH": "existing"}
    if loader_key != "PATH":
        base[loader_key] = "existing-loader"

    env, _ = run_with_openusd.build_environment(root, base=base)

    assert env["RMANTREE"] == str(rman)
    assert str(rman / "bin") in env["PATH"].split(os.pathsep)
    expected_loader_prefix = (
        [str(rman / "lib"), str(rman / "bin")]
        if os.name == "nt"
        else [str(rman / "bin"), str(rman / "lib")]
    )
    assert env[loader_key].split(os.pathsep)[:2] == expected_loader_prefix


def test_build_environment_rejects_invalid_inherited_rmantree(tmp_path):
    root = _usd_install(tmp_path)

    with pytest.raises(RuntimeError, match="RenderMan root does not exist"):
        run_with_openusd.build_environment(
            root, base={"RMANTREE": str(tmp_path / "missing")}
        )


def test_build_environment_selects_python_executable(tmp_path):
    root = _usd_install(tmp_path)
    executable = tmp_path / "OpenUSD" / ".venv" / "Scripts" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()

    env, _ = run_with_openusd.build_environment(
        root, base={"PATH": "existing"}, python_executable=executable
    )

    assert env["OPENUSDCONNECT_PYTHON_EXECUTABLE"] == str(executable)
    assert env["PATH"].split(os.pathsep)[0] == str(executable.parent)


def test_environment_delta_contains_only_changes():
    assert openusd_runtime.environment_delta(
        {"PATH": "before", "UNCHANGED": "value"},
        {"PATH": "after", "UNCHANGED": "value", "ADDED": "new"},
    ) == {"PATH": "after", "ADDED": "new"}
