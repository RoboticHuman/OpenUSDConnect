from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

native = pytest.importorskip("openusdconnect._native_client")
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_source_package_finds_native_extension_without_editable_hook(tmp_path):
    site_packages = Path(native.__file__).resolve().parent.parent
    code = (
        "import sys; "
        f"sys.path[:0] = [{str(REPO_ROOT)!r}, {str(site_packages)!r}]; "
        "import openusdconnect._native_client as native; "
        "print(native.__file__)"
    )
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, "-S", "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).resolve() == Path(native.__file__).resolve()


def test_shared_core_and_unreal_module_do_not_enable_cpp_exceptions():
    core = REPO_ROOT / "native" / "client_core"
    sources = [*core.rglob("*.h"), *core.rglob("*.cpp")]

    assert sources
    assert all("throw" not in source.read_text(encoding="utf-8") for source in sources)

    build_rules = (
        REPO_ROOT
        / "integrations"
        / "unreal"
        / "OpenUSDConnect"
        / "Source"
        / "OpenUSDConnect"
        / "OpenUSDConnect.Build.cs"
    ).read_text(encoding="utf-8")
    assert "bEnableExceptions" not in build_rules
