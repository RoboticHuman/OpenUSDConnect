"""Tests for the PowerShell OpenUSD environment activator."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
SCRIPT = Path(__file__).parents[2] / "scripts" / "openusd_env.ps1"


def _pxr_package(path: Path) -> Path:
    pkg = path / "pxr"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").touch()
    (pkg / "Tf").mkdir()
    (pkg / "Tf" / "__init__.py").touch()
    (pkg / "Usd").mkdir()
    (pkg / "Usd" / "__init__.py").write_text("def GetVersion():\n    return (0, 0, 0)\n")
    return path


def _run_script(root: Path, *, python_path: Path | None = None, rman: Path | None = None):
    if POWERSHELL is None:
        pytest.skip("PowerShell is unavailable")

    env = dict(os.environ)
    env.update(TEST_SCRIPT=str(SCRIPT), TEST_USD_ROOT=str(root))
    env["RMANTREE"] = "" if rman is None else str(rman)
    arguments = ""
    if python_path is not None:
        env["TEST_PYTHON_PATH"] = str(python_path)
        arguments = " -PythonPath $env:TEST_PYTHON_PATH"
    command = (
        f". $env:TEST_SCRIPT $env:TEST_USD_ROOT{arguments}; "
        "[PSCustomObject]@{PythonPath=$env:PYTHONPATH; Path=$env:PATH; "
        "LdLibraryPath=$env:LD_LIBRARY_PATH; DyldLibraryPath=$env:DYLD_LIBRARY_PATH; "
        "RenderMan=$env:RMANTREE; LoaderPython=$env:OPENUSDCONNECT_PYTHON_EXECUTABLE} "
        "| ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command", command],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout.splitlines()[-1])


def test_discovers_current_windows_python_layout(tmp_path):
    root = tmp_path / "OpenUSD"
    root.mkdir()
    python_path = _pxr_package(root / "Lib" / "site-packages")

    result = _run_script(root)

    assert result["PythonPath"].split(os.pathsep)[0] == str(python_path)
    assert Path(result["LoaderPython"]).resolve() == Path(sys.executable).resolve()


def test_accepts_python_bindings_outside_prefix(tmp_path):
    root = tmp_path / "OpenUSD"
    root.mkdir()
    python_path = _pxr_package(tmp_path / "venv" / "Lib" / "site-packages")

    result = _run_script(root, python_path=python_path)

    assert result["PythonPath"].split(os.pathsep)[0] == str(python_path)


def test_configures_inherited_rmantree(tmp_path):
    root = tmp_path / "OpenUSD"
    root.mkdir()
    _pxr_package(root / "Lib" / "site-packages")
    rman = tmp_path / "RenderMan"
    (rman / "bin").mkdir(parents=True)
    (rman / "lib").mkdir()

    result = _run_script(root, rman=rman)

    path = result["Path"].split(os.pathsep)
    if os.name == "nt":
        loader_path = path
    elif sys.platform == "darwin":
        loader_path = result["DyldLibraryPath"].split(os.pathsep)
    else:
        loader_path = result["LdLibraryPath"].split(os.pathsep)
    assert result["RenderMan"] == str(rman)
    assert str(rman / "bin") in path
    assert str(rman / "bin") in loader_path
    assert str(rman / "lib") in loader_path
