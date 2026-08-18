"""Tests for bundled OpenUSD selection and packaging."""

import os
import tomllib
from pathlib import Path

import openusdconnect._runtime as runtime


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

    assert bundled_dependencies == ["usd-core==26.8"]
    assert f"pip install --no-cache-dir {bundled_dependencies[0]}" in dockerfile
    assert "COPY pyproject.toml README.md LICENSE NOTICE ./" in dockerfile
    assert "COPY native/sdf_notice_bridge/ native/sdf_notice_bridge/" in dockerfile
