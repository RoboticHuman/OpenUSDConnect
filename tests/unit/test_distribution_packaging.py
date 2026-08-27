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

    assert extras["runtime"] == ["usd-core==26.8"]
    assert "wsgidav==4.3.3" in extras["vfs"]
    assert "usd-core==26.8" in extras["server"]
    assert "usd-core==26.8" in extras["mcp"]
    assert "mcp>=1.2,<2" in extras["complete"]
    assert scripts["openusdconnect-mcp"] == "integrations.mcp.cli:main"
    assert scripts["openusdconnect-usdview"] == "integrations.usdview.launcher:main"


def test_docker_runtime_is_multistage_non_root_and_health_checked():
    dockerfile = (distribution.REPO_ROOT / "Dockerfile").read_text()
    attributes = (distribution.REPO_ROOT / ".gitattributes").read_text()
    assert "Dockerfile text eol=lf" in attributes.splitlines()
    runtime = dockerfile.split("FROM python:3.13-slim-trixie AS runtime-base", maxsplit=1)[1]

    assert "build-essential" not in runtime
    assert "libxt-dev" not in runtime
    assert "libxt-dev" in dockerfile.split("FROM usd-build-base AS usd-full", 1)[1].split(
        "FROM usd-build-base AS usd-external", 1
    )[0]
    for target, profile in (
        ("server", "runtime"),
        ("live-open", "vfs"),
        ("complete", "complete"),
        ("mcp", "mcp"),
    ):
        assert f"FROM runtime-base AS {target}" in dockerfile
        assert f"--requirement /requirements/{profile}.txt" in dockerfile
    assert "USER openusdconnect" in runtime
    assert "HEALTHCHECK" in runtime
    assert "uv export --frozen" in dockerfile
    assert "--mount=type=bind,from=wheel-builder" in dockerfile
    assert "FROM usd-builder AS release-builder" in dockerfile
    assert "COPY --from=build-base /src/ /src/" in dockerfile
    assert "FROM scratch AS release-packages" in dockerfile
    assert "FROM usd-${USD_PROFILE} AS usd-builder" in dockerfile
    assert '"-I", "/opt/ouc/_launch.py"' in runtime
    assert "--no-emit-package usd-core" in dockerfile


def test_compose_defaults_to_plain_server_and_uses_existing_scene():
    compose = (distribution.REPO_ROOT / "docker-compose.yml").read_text()

    assert "./test_scene.usda:/scenes/scene.usda:ro" in compose
    assert "target: server" in compose
    assert "target: live-open" in compose
    assert "target: complete" in compose
    assert "server-live-open:" in compose
    assert "server-complete:" in compose
    assert "cap_drop:" in compose


def test_docker_context_excludes_local_runtimes_and_assets():
    ignored = (distribution.REPO_ROOT / ".dockerignore").read_text().splitlines()

    for path in (".git/", ".openusd/", ".tmp/", "MVP/", "assets/", "build/", "tests/"):
        assert path in ignored


def test_all_component_expansion_is_stable():
    assert distribution._components(["all"]) == list(distribution.DEFAULT_COMPONENTS)
    assert distribution._components(["all", "linux-packages"]) == [
        *distribution.DEFAULT_COMPONENTS,
        "linux-packages",
    ]
    assert distribution._components(["python", "python", "cpp-sdk"]) == [
        "python",
        "cpp-sdk",
    ]


def test_docker_linux_package_export_is_added_to_release(tmp_path, monkeypatch):
    staging = distribution.REPO_ROOT / "build" / f"distribution-test-{tmp_path.name}"
    exported = staging / "docker-linux-packages"
    existing_sdk = tmp_path / "openusdconnect-client-sdk.zip"
    existing_sdk.write_bytes(b"host-sdk")

    def fake_run(_command, **_kwargs):
        exported.mkdir(parents=True)
        package = exported / "openusdconnect-server-linux.tar.gz"
        package.write_bytes(b"linux-package")
        sdk = exported / existing_sdk.name
        sdk.write_bytes(b"docker-sdk")
        (exported / "release-manifest.json").write_text(
            json.dumps(
                {
                    "artifacts": [
                        {
                            "component": "server",
                            "kind": "self-contained-runtime",
                            "path": package.name,
                            "target": "linux-x64",
                            "metadata": {"openusd": {"profile": "full", "materialx": True}},
                        },
                        {
                            "component": "cpp-sdk",
                            "kind": "source-sdk",
                            "path": sdk.name,
                            "target": None,
                        },
                    ]
                }
            )
        )

    monkeypatch.setattr(distribution, "_checked_run", fake_run)
    try:
        artifacts = distribution.build_linux_packages_with_docker(
            tmp_path,
            staging,
            command="docker",
        )
    finally:
        if staging.exists():
            distribution.shutil.rmtree(staging)

    assert len(artifacts) == 1
    assert artifacts[0].component == "linux-server"
    assert artifacts[0].target == "linux-x64"
    assert artifacts[0].metadata == {
        "built_with": "docker",
        "openusd": {"profile": "full", "materialx": True},
    }
    assert (tmp_path / "openusdconnect-server-linux.tar.gz").read_bytes() == b"linux-package"
    assert existing_sdk.read_bytes() == b"host-sdk"


def test_distribution_defaults_to_full_usd():
    assert distribution._parser().parse_args([]).usd_profile == "full"
    assert distribution._docker_usd_options("core", None, [], False) == [
        "--build-arg",
        "USD_PROFILE=core",
    ]


def test_external_docker_inputs_use_portable_relative_contexts(tmp_path, monkeypatch):
    monkeypatch.setattr(distribution, "REPO_ROOT", tmp_path / "repo")
    source = tmp_path / "usd"
    source.mkdir()
    (source / "runtime-marker").touch()
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugInfo.json").write_text("{}")
    options = distribution._docker_usd_options("external", source, [plugin], True)
    assert "USD_PROFILE=external" in options
    assert "ALLOW_UNPINNED_USD=1" in options
    assert "usd_runtime=build/distribution/docker-inputs/runtime" in options
    assert "usd_plugins=build/distribution/docker-inputs/plugins" in options
    inputs = distribution.REPO_ROOT / "build/distribution/docker-inputs"
    assert (inputs / "runtime/runtime-marker").exists()
    assert (inputs / "plugins/0/plugInfo.json").exists()


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
