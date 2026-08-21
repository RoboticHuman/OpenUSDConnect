"""Tests for the Bash/Zsh OpenUSD environment activator."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.python_version import PYTHON_SITE_DIRECTORY

SCRIPT = Path(__file__).parents[2] / "scripts" / "openusd_env.sh"
RUNNER = Path(__file__).parents[2] / "scripts" / "run_with_openusd.py"
WSL = shutil.which("wsl.exe")
BASH = shutil.which("bash")


def _pxr_package(path: Path) -> Path:
    pkg = path / "pxr"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").touch()
    (pkg / "Tf").mkdir()
    (pkg / "Tf" / "__init__.py").touch()
    (pkg / "Usd").mkdir()
    (pkg / "Usd" / "__init__.py").write_text("def GetVersion():\n    return (0, 0, 0)\n")
    return path


def _shell_path(path: Path) -> str:
    if os.name != "nt":
        return str(path)
    if WSL is None:
        pytest.skip("WSL is unavailable")
    result = subprocess.run(
        [WSL, "--distribution", "Ubuntu", "--exec", "wslpath", "-a", str(path)],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _bash(command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    if os.name == "nt":
        if WSL is None:
            pytest.skip("WSL is unavailable")
        invocation = [
            WSL,
            "--distribution",
            "Ubuntu",
            "--exec",
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            command,
        ]
    else:
        if BASH is None:
            pytest.skip("Bash is unavailable")
        invocation = [BASH, "--noprofile", "--norc", "-c", command]
    return subprocess.run(invocation, text=True, capture_output=True, check=check)


def _read_environment(command: str) -> dict[str, str]:
    result = _bash(
        "unset OPENUSDCONNECT_ENV_PYTHON; "
        "EXPECTED_PYTHON=$(python3 -c 'from pathlib import Path; import sys; "
        "print(Path(sys.executable).resolve())'); "
        f"unset PYTHONPATH LD_LIBRARY_PATH RMANTREE; {command}; "
        "printf 'PYTHONPATH=%s\\nLD_LIBRARY_PATH=%s\\nRMANTREE=%s\\nPYTHON=%s\\n"
        "EXPECTED_PYTHON=%s\\n' "
        '"$PYTHONPATH" "$LD_LIBRARY_PATH" "$RMANTREE" '
        '"$OPENUSDCONNECT_PYTHON_EXECUTABLE" "$EXPECTED_PYTHON"'
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


def test_discovers_posix_prefix_with_spaces(tmp_path):
    root = tmp_path / "OpenUSD install"
    python_path = _pxr_package(root / "lib" / PYTHON_SITE_DIRECTORY / "site-packages")
    (root / "bin").mkdir(parents=True)
    (root / "lib64").mkdir()
    command = f"source {shlex.quote(_shell_path(SCRIPT))} {shlex.quote(_shell_path(root))}"

    result = _read_environment(command)

    assert result["PYTHONPATH"].split(":")[0] == _shell_path(python_path)
    assert result["LD_LIBRARY_PATH"].split(":")[:2] == [
        _shell_path(root / "lib"),
        _shell_path(root / "lib64"),
    ]
    assert result["PYTHON"] == result["EXPECTED_PYTHON"]


def test_accepts_external_bindings_and_inherited_rmantree(tmp_path):
    root = tmp_path / "OpenUSDInstall"
    root.mkdir()
    python_path = _pxr_package(
        tmp_path / "OpenUSD" / ".venv" / "lib" / PYTHON_SITE_DIRECTORY / "site-packages"
    )
    rman = tmp_path / "RenderMan Pro Server"
    (rman / "bin").mkdir(parents=True)
    (rman / "lib").mkdir()
    command = (
        f"export RMANTREE={shlex.quote(_shell_path(rman))}; "
        f"source {shlex.quote(_shell_path(SCRIPT))} {shlex.quote(_shell_path(root))} "
        f"--python-path {shlex.quote(_shell_path(python_path))}"
    )

    result = _read_environment(command)

    assert result["PYTHONPATH"].split(":")[0] == _shell_path(python_path)
    assert result["RMANTREE"] == _shell_path(rman)
    assert result["LD_LIBRARY_PATH"].split(":")[:2] == [
        _shell_path(rman / "bin"),
        _shell_path(rman / "lib"),
    ]


def test_invalid_prefix_fails_without_applying_environment(tmp_path):
    missing = _shell_path(tmp_path / "missing")
    script = _shell_path(SCRIPT)

    result = _bash(
        f"unset OPENUSDCONNECT_USD_ROOT; source {shlex.quote(script)} {shlex.quote(missing)}; "
        "status=$?; printf 'status=%s root=%s\\n' \"$status\" "
        '"${OPENUSDCONNECT_USD_ROOT-}"',
        check=False,
    )

    assert "status=2 root=" in result.stdout
    assert "OpenUSD root does not exist" in result.stderr


def test_adapter_rejects_direct_execution(tmp_path):
    root = tmp_path / "OpenUSD"
    _pxr_package(root / "lib" / PYTHON_SITE_DIRECTORY / "site-packages")

    result = _bash(
        f"bash {shlex.quote(_shell_path(SCRIPT))} {shlex.quote(_shell_path(root))}",
        check=False,
    )

    assert result.returncode == 2
    assert "must be sourced" in result.stderr


def test_command_wrapper_runs_with_posix_environment(tmp_path):
    root = tmp_path / "OpenUSD"
    python_path = _pxr_package(root / "lib" / PYTHON_SITE_DIRECTORY / "site-packages")
    (root / "lib64").mkdir()
    code = (
        "import os; "
        "print(os.environ['PYTHONPATH']); "
        "print(os.environ['LD_LIBRARY_PATH'])"
    )
    command = (
        "unset PYTHONPATH LD_LIBRARY_PATH RMANTREE; "
        f"python3 {shlex.quote(_shell_path(RUNNER))} "
        f"--usd-root {shlex.quote(_shell_path(root))} -- "
        f"python3 -c {shlex.quote(code)}"
    )

    result = _bash(command)

    lines = result.stdout.splitlines()
    assert lines[-2] == _shell_path(python_path)
    assert lines[-1].split(":") == [
        _shell_path(root / "lib"),
        _shell_path(root / "lib64"),
    ]
