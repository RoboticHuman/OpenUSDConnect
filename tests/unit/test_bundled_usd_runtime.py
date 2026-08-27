"""Tests for bundled OpenUSD selection and packaging."""

import json
import os
import tomllib
from pathlib import Path

import pytest

import openusdconnect._runtime as runtime
from tests.openusd_pin import OPENUSD_CORE_VERSION


@pytest.fixture(autouse=True)
def _clean_runtime_env(monkeypatch, tmp_path):
    """Isolate runtime selection from a shell that already configured OpenUSD.

    The documented setup sources ``openusd_env`` and sets these variables, which
    would otherwise short-circuit ``select_runtime`` and mask the behavior under
    test. Individual tests still set them explicitly when that is the case.
    """
    monkeypatch.delenv(runtime.USD_ROOT_ENV, raising=False)
    monkeypatch.delenv(runtime.BUNDLED_USD_ENV, raising=False)
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    monkeypatch.setenv("PYTHONPATH", os.environ.get("PYTHONPATH", ""))
    monkeypatch.setattr(runtime, "ACTIVE_RUNTIME_FILE", tmp_path / "missing-active.json")
    monkeypatch.setattr(
        runtime, "LEGACY_ACTIVE_RUNTIME_FILE", tmp_path / "missing-legacy-active.json"
    )


def test_bundled_runtime_ignores_only_conflicting_pxr(monkeypatch, tmp_path):
    external = tmp_path / "external-usd"
    site_packages = tmp_path / "site-packages"
    project = tmp_path / "project"
    (external / "pxr").mkdir(parents=True)
    (site_packages / "pxr").mkdir(parents=True)
    project.mkdir()

    class Distribution:
        @staticmethod
        def locate_file(path):
            return site_packages / path

    monkeypatch.setattr(runtime, "_loaded_pxr_path", lambda: None)
    monkeypatch.setattr(runtime.metadata, "distribution", lambda _name: Distribution())
    monkeypatch.setattr(
        runtime.sys,
        "path",
        [str(external), str(site_packages), str(project)],
    )
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join((str(external), str(site_packages), str(project))),
    )

    runtime.select_runtime()

    assert runtime.sys.path == [str(site_packages), str(project)]
    assert runtime.os.environ["PYTHONPATH"] == os.pathsep.join(
        (str(site_packages), str(project))
    )
    assert runtime.os.environ[runtime.BUNDLED_USD_ENV] == "1"


def test_loaded_project_pxr_is_not_reclassified_as_bundled(monkeypatch, tmp_path):
    external = tmp_path / "external-usd" / "pxr"
    bundled = tmp_path / "site-packages" / "pxr"
    external.mkdir(parents=True)
    bundled.mkdir(parents=True)
    original_path = [str(external.parent), str(bundled.parent)]

    class Distribution:
        @staticmethod
        def locate_file(path):
            return bundled.parent / path

    monkeypatch.setattr(runtime, "_loaded_pxr_path", lambda: str(external))
    monkeypatch.setattr(runtime.metadata, "distribution", lambda _name: Distribution())
    monkeypatch.setattr(runtime.sys, "path", original_path.copy())
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(original_path))
    monkeypatch.setenv(runtime.BUNDLED_USD_ENV, "1")

    runtime.select_runtime()

    assert runtime.sys.path == original_path
    assert runtime.os.environ["PYTHONPATH"] == os.pathsep.join(original_path)
    assert runtime.BUNDLED_USD_ENV not in runtime.os.environ


def test_project_runtime_preserves_active_environment(monkeypatch):
    monkeypatch.setattr(runtime, "_bundled_pxr_path", lambda: None)
    monkeypatch.setenv(runtime.BUNDLED_USD_ENV, "1")

    runtime.select_runtime()

    assert runtime.BUNDLED_USD_ENV not in runtime.os.environ


def test_explicit_project_runtime_overrides_installed_bundle(monkeypatch):
    monkeypatch.setenv(runtime.USD_ROOT_ENV, "/project/openusd")
    monkeypatch.setenv(runtime.BUNDLED_USD_ENV, "1")

    runtime.select_runtime()

    assert runtime.BUNDLED_USD_ENV not in runtime.os.environ


def test_managed_project_runtime_is_selected_before_bundle(monkeypatch, tmp_path):
    root = tmp_path / "managed-usd"
    python_path = root / "Lib" / "site-packages"
    (python_path / "pxr").mkdir(parents=True)
    (python_path / "pxr" / "__init__.py").touch()
    config = tmp_path / "active.json"
    config.write_text(
        json.dumps(
            {"schema": 1, "usd_root": str(root), "python_path": str(python_path)}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime, "ACTIVE_RUNTIME_FILE", config)
    monkeypatch.setattr(runtime, "_loaded_pxr_path", lambda: None)
    monkeypatch.setattr(runtime, "_bundled_pxr_path", lambda: str(tmp_path / "bundled"))
    monkeypatch.setattr(runtime.sys, "path", [])

    runtime.select_runtime()

    assert runtime.sys.path[0] == str(python_path)
    assert runtime.os.environ[runtime.USD_ROOT_ENV] == str(root)
    assert runtime.BUNDLED_USD_ENV not in runtime.os.environ


def test_server_entry_point_uses_package_module():
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["scripts"]["openusdconnect-server"] == (
        "openusdconnect.server:main"
    )


def test_docker_uses_bundled_usd_pin():
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    bundled_dependencies = project["dependency-groups"]["bundled-usd"]
    complete = project["project"]["optional-dependencies"]["complete"]

    assert bundled_dependencies == [f"usd-core=={OPENUSD_CORE_VERSION}"]
    assert bundled_dependencies[0] in complete
    assert "--requirement /requirements/complete.txt" in dockerfile
    assert "--only-group bundled-usd" in dockerfile
    assert "--no-emit-package usd-core" in dockerfile
    assert "--requirement /requirements/runtime.txt" in dockerfile
    assert "FROM usd-builder AS release-builder" in dockerfile
