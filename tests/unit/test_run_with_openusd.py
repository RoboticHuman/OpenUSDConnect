"""Tests for the project OpenUSD command wrapper."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import openusd_runtime, run_with_openusd
from tests.openusd_pin import OPENUSD_VERSION
from tests.python_version import PYTHON_SITE_DIRECTORY


def _write_pxr(pkg):
    """Create an importable pxr stub so the runtime binding check passes."""
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").touch()
    (pkg / "Tf").mkdir()
    (pkg / "Tf" / "__init__.py").touch()
    (pkg / "Usd").mkdir()
    (pkg / "Usd" / "__init__.py").write_text("def GetVersion():\n    return (0, 0, 0)\n")


def _usd_install(tmp_path):
    root = tmp_path / "OpenUSD"
    _write_pxr(root / "lib" / "python" / "pxr")
    (root / "bin").mkdir()
    return root


def _pxr_package(path):
    _write_pxr(path / "pxr")
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
    monkeypatch.setattr(run_with_openusd, "verify_bindings", lambda *a, **k: "0.0.0")

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


def test_managed_build_is_the_default_runtime(tmp_path, monkeypatch):
    root = _usd_install(tmp_path)
    python_path = root / "lib" / "python"
    executable = tmp_path / "venv" / "python"
    executable.parent.mkdir()
    executable.touch()
    config = tmp_path / "active.json"
    config.write_text(
        json.dumps(
            {
                "schema": 1,
                "usd_root": str(root),
                "python_path": str(python_path),
                "python_executable": str(executable),
                "renderman_root": None,
                "version": OPENUSD_VERSION,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv(openusd_runtime.USD_ROOT_ENV, raising=False)
    monkeypatch.setattr(openusd_runtime, "ACTIVE_RUNTIME_FILE", config)

    args = run_with_openusd._parse_args(["--", "python", "-V"])

    assert args.usd_root == str(root)
    assert args.python_path == str(python_path)
    assert args.python_executable == str(executable)


def test_explicit_root_does_not_inherit_managed_build_options(tmp_path, monkeypatch):
    managed = _usd_install(tmp_path / "managed")
    external = _usd_install(tmp_path / "external")
    config = tmp_path / "active.json"
    config.write_text(
        json.dumps(
            {
                "schema": 1,
                "usd_root": str(managed),
                "python_path": str(managed / "lib" / "python"),
                "python_executable": str(tmp_path / "managed-python"),
                "renderman_root": str(tmp_path / "RenderMan"),
                "version": OPENUSD_VERSION,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv(openusd_runtime.USD_ROOT_ENV, raising=False)
    monkeypatch.delenv("RMANTREE", raising=False)
    monkeypatch.setattr(openusd_runtime, "ACTIVE_RUNTIME_FILE", config)

    args = run_with_openusd._parse_args(
        ["--usd-root", str(external), "--", "python", "-V"]
    )

    assert args.usd_root == str(external)
    assert args.python_path is None
    assert args.python_executable is None
    assert args.renderman_root is None


def test_windows_batch_command_uses_cmd(monkeypatch):
    monkeypatch.setattr(run_with_openusd.os, "name", "nt")
    monkeypatch.setattr(
        run_with_openusd.shutil,
        "which",
        lambda command, *, path: r"C:\OpenUSD\bin\usdview.cmd",
    )

    resolved = run_with_openusd._resolve_command(
        ["usdview", "--help"],
        env={"PATH": r"C:\OpenUSD\bin", "COMSPEC": r"C:\Windows\System32\cmd.exe"},
    )

    assert resolved == [
        r"C:\Windows\System32\cmd.exe",
        "/d",
        "/c",
        "call",
        r"C:\OpenUSD\bin\usdview.cmd",
        "--help",
    ]


def test_build_environment_rejects_missing_plugin_path(tmp_path):
    root = _usd_install(tmp_path)

    with pytest.raises(RuntimeError, match="plugin path does not exist"):
        run_with_openusd.build_environment(root, plugin_paths=[tmp_path / "missing"])


@pytest.mark.parametrize(
    "relative_path",
    [
        ("Lib", "site-packages"),
        ("lib", PYTHON_SITE_DIRECTORY, "site-packages"),
        ("lib64", PYTHON_SITE_DIRECTORY, "dist-packages"),
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


def test_build_environment_discovers_bindings_in_active_venv(tmp_path, monkeypatch):
    root = tmp_path / "OpenUSDInstall"
    root.mkdir()
    python_path = _pxr_package(tmp_path / "OpenUSD" / ".venv" / "Lib" / "site-packages")
    monkeypatch.setattr(
        openusd_runtime,
        "_active_venv_python_paths",
        lambda: [python_path],
    )

    env, selected = run_with_openusd.build_environment(root, base={})

    assert selected == python_path
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(python_path)


def test_runtime_cli_discovers_bindings_from_real_active_venv(tmp_path):
    root = tmp_path / "OpenUSDInstall"
    root.mkdir()
    venv = tmp_path / "OpenUSD" / ".venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)],
        check=True,
    )
    executable = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    result = subprocess.run(
        [
            str(executable),
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    python_path = _pxr_package(Path(result.stdout.strip()))
    env = dict(os.environ)
    env.update(PYTHONPATH="", RMANTREE="")

    result = subprocess.run(
        [
            str(executable),
            str(openusd_runtime.__file__),
            "--usd-root",
            str(root),
            "--format",
            "json",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    configuration = json.loads(result.stdout)

    assert configuration["python_path"] == str(python_path)


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


def test_verify_bindings_uses_selected_interpreter_and_environment(monkeypatch):
    captured = {}

    def fake_run(command, *, env, text, capture_output):
        captured.update(command=command, env=env)
        return SimpleNamespace(returncode=0, stdout="(9, 8, 7)\n", stderr="")

    monkeypatch.setattr(openusd_runtime.subprocess, "run", fake_run)

    version = openusd_runtime.verify_bindings(
        {"PYTHONPATH": "bindings"},
        "bindings",
        python_executable=sys.executable,
    )

    assert version == "(9, 8, 7)"
    assert captured["command"][0] == str(Path(sys.executable).resolve())
    assert "from pxr import Tf, Usd" in captured["command"][-1]
    assert captured["env"] == {"PYTHONPATH": "bindings"}


def test_verify_bindings_reports_actionable_error_on_import_failure(monkeypatch):
    def fake_run(command, *, env, text, capture_output):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="ImportError: DLL load failed while importing _tf",
        )

    monkeypatch.setattr(openusd_runtime.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as excinfo:
        openusd_runtime.verify_bindings(
            {}, "bindings", python_executable=sys.executable
        )

    message = str(excinfo.value)
    assert "could not import OpenUSD" in message
    assert "-PythonExecutable" in message
    assert "_tf" in message


def test_main_verifies_bindings_before_emitting_environment(tmp_path, monkeypatch):
    root = _usd_install(tmp_path)
    calls = {}

    def fake_verify(env, python_path, python_executable):
        calls.update(env=env, python_path=python_path)
        return "0.0.0"

    monkeypatch.setattr(openusd_runtime, "verify_bindings", fake_verify)

    assert openusd_runtime.main(["--usd-root", str(root), "--format", "json"]) == 0
    assert calls["python_path"] == root / "lib" / "python"


def test_main_reports_verification_failure(tmp_path, monkeypatch, capsys):
    root = _usd_install(tmp_path)

    def fake_verify(*args, **kwargs):
        raise RuntimeError("bindings could not be imported: boom")

    monkeypatch.setattr(openusd_runtime, "verify_bindings", fake_verify)

    assert openusd_runtime.main(["--usd-root", str(root), "--format", "json"]) == 2
    assert "could not be imported" in capsys.readouterr().err
