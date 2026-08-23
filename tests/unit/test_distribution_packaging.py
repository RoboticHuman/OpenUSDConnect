from __future__ import annotations

import json
import tarfile
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace

from scripts import build_blender_addon as blender_packaging
from scripts import build_distribution as distribution


def test_blender_archive_is_tagged_and_contains_exact_native_module(tmp_path):
    target = blender_packaging.HostPython(
        executable=Path("python.exe"),
        version=(3, 11, 13),
        cache_tag="cpython-311",
        extension_suffix=".cp311-win_amd64.pyd",
        platform_tag="windows-x64",
    )
    native = tmp_path / "_native_client.cp311-win_amd64.pyd"
    native.write_bytes(b"native-test-module")

    archive = blender_packaging.package_addon(
        target,
        native,
        output_dir=tmp_path,
        compatibility_alias=False,
    )

    assert archive.name == "usd_connect_blender-0.2.0-windows-x64-cp311.zip"
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        native_names = [name for name in names if "_native_client" in name]
        assert native_names == ["usd_connect/openusdconnect/_native_client.cp311-win_amd64.pyd"]
        metadata = json.loads(bundle.read("usd_connect/openusdconnect-build.json"))
        assert metadata["python_abi"] == "cp311"
        assert metadata["platform"] == "windows-x64"
        manifest = bundle.read("usd_connect/blender_manifest.toml").decode()
        assert 'platforms = ["windows-x64"]' in manifest


def test_cpp_sdk_archive_has_standalone_cmake_entrypoint(tmp_path):
    artifact = distribution.build_cpp_sdk(tmp_path, tmp_path / "staging")

    archive = Path(artifact.path)
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        root = "openusdconnect-client-sdk-0.2.0"
        assert f"{root}/CMakeLists.txt" in names
        assert f"{root}/client_core/include/openusdconnect/client/frame_codec.h" in names
        assert f"{root}/client_core/CMakeLists.txt" in names
        assert f"{root}/flatbuffers/include/flatbuffers/flatbuffers.h" in names
        cmake = bundle.read(f"{root}/CMakeLists.txt").decode()
        assert "flatbuffers::flatbuffers" in cmake


def test_release_manifest_records_checksums(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENUSDCONNECT_BUILD_COMMIT", "release-commit")
    payload = tmp_path / "artifact.zip"
    payload.write_bytes(b"release-payload")
    artifact = distribution._file_artifact("test", payload, "test-archive")

    distribution._write_release_manifest(tmp_path, [artifact])

    manifest = json.loads((tmp_path / "release-manifest.json").read_text())
    assert manifest["version"] == "0.2.0"
    assert manifest["commit"] == "release-commit"
    assert manifest["artifacts"][0]["sha256"] == artifact.sha256
    assert manifest["artifacts"][0]["path"] == "artifact.zip"
    assert (tmp_path / "SHA256SUMS").read_text().strip() == (f"{artifact.sha256}  artifact.zip")


def test_source_distribution_excludes_asset_repository(tmp_path):
    wheel_dir = tmp_path / "distribution"
    artifacts, wheel = distribution.build_python(wheel_dir, tmp_path / "staging", publish=True)
    source = next(Path(item.path) for item in artifacts if item.name.endswith(".tar.gz"))

    with tarfile.open(source) as archive:
        names = archive.getnames()
        assert not any("/assets/full_assets/" in name for name in names)
        assert not any("/assets/intent-vfx/" in name for name in names)
    assert source.stat().st_size < 20 * 1024 * 1024

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "integrations/dashboard/pages.py" in names
        assert "integrations/mcp/cli.py" in names
        assert "integrations/openpbr_translate.py" in names
        assert "integrations/usdview/plugInfo.json" in names
        assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)


def test_customer_install_profiles_match_packaged_entrypoints():
    project = tomllib.loads((distribution.REPO_ROOT / "pyproject.toml").read_text())
    extras = project["project"]["optional-dependencies"]
    scripts = project["project"]["scripts"]

    assert "usd-core==26.8" in extras["server"]
    assert "usd-core==26.8" in extras["mcp"]
    assert "mcp>=1.2,<2" in extras["complete"]
    assert scripts["openusdconnect-mcp"] == "integrations.mcp.cli:main"
    assert scripts["openusdconnect-usdview"] == "integrations.usdview.launcher:main"


def test_docker_runtime_is_multistage_non_root_and_health_checked():
    dockerfile = (distribution.REPO_ROOT / "Dockerfile").read_text()
    runtime = dockerfile.split("FROM python:3.13-slim AS runtime", maxsplit=1)[1]

    assert dockerfile.count("FROM python:3.13-slim") == 2
    assert "build-essential" not in runtime
    assert '"openusdconnect[complete]"' in runtime
    assert "USER openusdconnect" in runtime
    assert "HEALTHCHECK" in runtime


def test_all_component_expansion_is_stable():
    assert distribution._components(["all"]) == list(distribution.DEFAULT_COMPONENTS)
    assert distribution._components(["python", "python", "cpp-sdk"]) == [
        "python",
        "cpp-sdk",
    ]


def test_windows_bundle_smoke_terminates_launcher_process_tree(monkeypatch):
    calls = []

    class Process:
        pid = 4321

        @staticmethod
        def poll():
            return None

        @staticmethod
        def wait(timeout):
            calls.append(("wait", timeout))

    monkeypatch.setattr(distribution, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        distribution.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command),
    )

    distribution._terminate_process_tree(Process())

    assert calls == [["taskkill", "/PID", "4321", "/T", "/F"], ("wait", 10.0)]
