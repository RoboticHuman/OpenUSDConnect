"""Build customer-ready OpenUSDConnect release artifacts.

Each component can be built independently. ``all`` produces the Python wheel
and source archive, a self-contained server bundle, the Blender addon for the
configured host, an Unreal BuildPlugin archive, the native C++ SDK, and a local
Docker image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import runpy
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from integrations.unreal.test_harness import (  # noqa: E402
    package_plugin,
    resolve_engine,
)
from scripts.build_blender_addon import (  # noqa: E402
    build_native_module,
    configured_blender,
    package_addon,
    probe_blender,
    query_python,
)
from scripts.build_blender_addon import (
    smoke_test as smoke_test_blender,
)
from scripts.package_usd_runtime import PROFILES, prepare_runtime, validate_options

DEFAULT_COMPONENTS = ("python", "server", "blender", "unreal", "cpp-sdk", "docker")
OPTIONAL_COMPONENTS = ("linux-packages",)
DOCKER_TARGETS = ("server", "live-open", "complete", "mcp")


@dataclass(frozen=True, slots=True)
class Artifact:
    component: str
    name: str
    kind: str
    path: str | None = None
    sha256: str | None = None
    size: int | None = None
    target: str | None = None
    metadata: dict[str, object] | None = None


def _version() -> str:
    return str(runpy.run_path(REPO_ROOT / "openusdconnect" / "_version.py")["__version__"])


def _checked_run(
    command: list[str],
    *,
    cwd: Path = REPO_ROOT,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    print(f"+ {subprocess.list2cmdline(command)}", flush=True)
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Command timed out after {timeout:g} seconds: {subprocess.list2cmdline(command)}"
        ) from exc
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {subprocess.list2cmdline(command)}\n"
            f"{result.stdout}{result.stderr}"
        )
    if result.stdout.strip():
        print(result.stdout.rstrip())
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_artifact(
    component: str,
    path: Path,
    kind: str,
    *,
    target: str | None = None,
    metadata: dict[str, object] | None = None,
) -> Artifact:
    path = path.resolve()
    return Artifact(
        component=component,
        name=path.name,
        kind=kind,
        path=str(path),
        sha256=_sha256(path),
        size=path.stat().st_size,
        target=target,
        metadata=metadata,
    )


def _zip_tree(source: Path, archive: Path, root_name: str) -> Path:
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as output:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                output.write(path, f"{root_name}/{path.relative_to(source).as_posix()}")
    return archive


def _archive_server_bundle(source: Path, output_dir: Path, root_name: str) -> Path:
    if os.name == "nt":
        return _zip_tree(source, output_dir / f"{root_name}.zip", root_name)
    archive = output_dir / f"{root_name}.tar.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as output:
        output.add(source, arcname=root_name)
    return archive


def _extract_server_bundle(archive: Path, destination: Path) -> None:
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as packaged:
            corrupt = packaged.testzip()
            if corrupt:
                raise RuntimeError(f"Corrupt file in server archive: {corrupt}")
            packaged.extractall(destination)
        return
    with tarfile.open(archive, "r:gz") as packaged:
        packaged.extractall(destination, filter="data")


def _platform_tag() -> str:
    os_name = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(platform.system())
    architecture = {
        "AMD64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(platform.machine())
    if not os_name or not architecture:
        raise RuntimeError(f"Unsupported release host: {platform.system()} {platform.machine()}")
    return f"{os_name}-{architecture}"


def build_python(
    output_dir: Path,
    staging: Path,
    *,
    publish: bool,
) -> tuple[list[Artifact], Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    wheel_dir = staging / "python"
    wheel_dir.mkdir(parents=True, exist_ok=True)
    _checked_run(["uv", "build", "--out-dir", str(wheel_dir)])
    built = sorted(
        path
        for path in wheel_dir.iterdir()
        if path.is_file() and (path.suffix == ".whl" or path.name.endswith(".tar.gz"))
    )
    wheel = next((path for path in built if path.suffix == ".whl"), None)
    if wheel is None:
        raise RuntimeError("uv build did not produce a wheel")
    artifacts = []
    if publish:
        for source in built:
            destination = output_dir / source.name
            shutil.copy2(source, destination)
            artifacts.append(_file_artifact("python", destination, "python-distribution"))
        wheel = output_dir / wheel.name
    return artifacts, wheel


def _managed_python(version: str) -> Path:
    _checked_run(["uv", "python", "install", version])
    result = _checked_run(
        [
            "uv",
            "python",
            "find",
            "--managed-python",
            "--system",
            "--no-project",
            version,
        ]
    )
    return Path(result.stdout.strip().splitlines()[-1]).resolve()


def _python_root(executable: Path) -> Path:
    return executable.parent if os.name == "nt" else executable.parent.parent


def _bundled_python_executable(root: Path, source: Path) -> Path:
    relative = source.relative_to(_python_root(source))
    return root / relative


def _write_server_launchers(bundle: Path) -> None:
    if os.name == "nt":
        (bundle / "openusdconnect-server.cmd").write_text(
            '@echo off\r\n"%~dp0python\\python.exe" -I "%~dp0_launch.py" openusdconnect.server %*\r\n',
            encoding="utf-8",
        )
        (bundle / "openusdconnect-mcp.cmd").write_text(
            '@echo off\r\n"%~dp0python\\python.exe" -I "%~dp0_launch.py" integrations.mcp %*\r\n',
            encoding="utf-8",
        )
    else:
        for name, module in (
            ("openusdconnect-server", "openusdconnect.server"),
            ("openusdconnect-mcp", "integrations.mcp"),
        ):
            path = bundle / name
            path.write_text(
                '#!/bin/sh\nROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
                f'exec "$ROOT/python/bin/python3" -I "$ROOT/_launch.py" {module} "$@"\n',
                encoding="utf-8",
                newline="\n",
            )
            path.chmod(0o755)


def _bundle_launcher_command(bundle: Path, name: str, arguments: list[str]) -> list[str]:
    if os.name == "nt":
        launcher = bundle / f"{name}.cmd"
        invocation = subprocess.list2cmdline([str(launcher), *arguments])
        return ["cmd.exe", "/d", "/s", "/c", invocation]
    return [str(bundle / name), *arguments]


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _free_ports(count: int) -> list[int]:
    ports: set[int] = set()
    while len(ports) < count:
        ports.add(_free_port())
    return list(ports)


def _wait_for_http(url: str, process: subprocess.Popen[str], timeout: float = 30.0) -> bytes:
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Bundled server exited early with code {process.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                return response.read()
        except OSError as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10.0)


@contextmanager
def _temporary_directory(prefix: str):
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        for attempt in range(20):
            try:
                shutil.rmtree(path)
                break
            except FileNotFoundError:
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.25)


def _smoke_test_server_bundle(bundle: Path, bundled_python: Path) -> None:
    sync_port, vfs_port, dashboard_port = _free_ports(3)
    with _temporary_directory(prefix="openusdconnect-server-package-") as work_dir:
        stdout_path = work_dir / "stdout.log"
        stderr_path = work_dir / "stderr.log"
        server_arguments = [
            "--host",
            "127.0.0.1",
            "--port",
            str(sync_port),
            "--base",
            str(REPO_ROOT / "test_scene.usda"),
            "--event-log",
            str(work_dir / "events.db"),
            "--vfs-host",
            "127.0.0.1",
            "--vfs-port",
            str(vfs_port),
            "--dashboard-port",
            str(dashboard_port),
            "--advertise-host",
            "127.0.0.1",
        ]
        command = _bundle_launcher_command(
            bundle,
            "openusdconnect-server",
            server_arguments,
        )
        _checked_run(
            _bundle_launcher_command(bundle, "openusdconnect-mcp", ["--help"]),
            cwd=bundle,
            timeout=30.0,
        )
        with (
            stdout_path.open("w", encoding="utf-8") as stdout,
            stderr_path.open("w", encoding="utf-8") as stderr,
        ):
            process = subprocess.Popen(
                command,
                cwd=bundle,
                stdout=stdout,
                stderr=stderr,
                text=True,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                ),
            )
            try:
                _wait_for_http(f"http://127.0.0.1:{dashboard_port}/api/status", process)
                with socket.create_connection(("127.0.0.1", sync_port), timeout=3.0):
                    pass
                snapshot = _wait_for_http(
                    f"http://127.0.0.1:{vfs_port}/usd/scene.usd",
                    process,
                )
                snapshot_path = work_dir / "scene.usd"
                snapshot_path.write_bytes(snapshot)
                _checked_run(
                    [
                        str(bundled_python),
                        "-I",
                        str(bundle / "_launch.py"),
                        "--run-script",
                        str(REPO_ROOT / "packaging" / "smoke_usd_runtime.py"),
                        "--snapshot",
                        str(snapshot_path),
                    ],
                    cwd=bundle,
                    timeout=30.0,
                )
            finally:
                _terminate_process_tree(process)


def build_server_bundle(
    output_dir: Path,
    staging: Path,
    wheel: Path,
    *,
    python_version: str,
    smoke_test: bool,
    usd_profile: str = "full",
    usd_root: Path | None = None,
    usd_plugin_paths: list[Path] | None = None,
    allow_unpinned_usd: bool = False,
) -> Artifact:
    executable = _managed_python(python_version)
    target = query_python(executable)
    root_name = f"openusdconnect-server-{_version()}-{target.platform_tag}-usd-{usd_profile}"
    bundle = staging / "server" / root_name
    python_destination = bundle / "python"
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)
    shutil.copytree(
        _python_root(executable),
        python_destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    bundled_python = _bundled_python_executable(python_destination, executable)
    bundled_python_relative = bundled_python.relative_to(bundle)
    requirements = staging / "server-requirements.txt"
    _checked_run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--extra",
            "complete",
            "--no-emit-project",
            "--no-hashes",
            "--output-file",
            str(requirements),
            *(["--no-emit-package", "usd-core"] if usd_profile != "core" else []),
        ]
    )
    _checked_run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(bundled_python),
            "--no-cache",
            "--break-system-packages",
            "--no-deps",
            "--requirement",
            str(requirements),
            str(wheel.resolve()),
        ]
    )
    usd = prepare_runtime(
        bundle,
        bundled_python,
        profile=usd_profile,
        usd_root=usd_root,
        plugin_paths=usd_plugin_paths,
        allow_unpinned=allow_unpinned_usd,
    )
    _write_server_launchers(bundle)
    shutil.copy2(REPO_ROOT / "LICENSE", bundle / "LICENSE")
    shutil.copy2(REPO_ROOT / "NOTICE", bundle / "NOTICE")
    (bundle / "README.txt").write_text(
        "OpenUSDConnect self-contained server\n\n"
        "No Python installation is required. Run openusdconnect-server.cmd on Windows "
        "or ./openusdconnect-server on macOS/Linux. Run the adjacent MCP launcher for "
        "the stdio MCP server. Use --help to list options.\n",
        encoding="utf-8",
        newline="\n",
    )
    metadata = {
        "artifact": "server-bundle",
        "version": _version(),
        "platform": target.platform_tag,
        "python_version": target.version_short,
        "python_abi": target.abi_tag,
        "profile": "complete",
        "openusd": usd,
    }
    (bundle / "openusdconnect-build.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if smoke_test:
        _checked_run(
            [
                str(bundled_python),
                "-I",
                str(bundle / "_launch.py"),
                "--runtime-info",
            ],
            cwd=bundle,
            timeout=30.0,
        )
        _checked_run(
            [
                str(bundled_python),
                "-I",
                str(bundle / "_launch.py"),
                "openusdconnect.server",
                "--help",
            ],
            timeout=30.0,
        )
        _checked_run(
            [str(bundled_python), "-I", str(bundle / "_launch.py"), "integrations.mcp", "--help"],
            timeout=30.0,
        )
    archive = _archive_server_bundle(bundle, output_dir, root_name)
    if smoke_test:
        with _temporary_directory(prefix="openusdconnect-extracted-server-package-") as temporary:
            _extract_server_bundle(archive, temporary)
            extracted_bundle = temporary / root_name
            _checked_run(
                [
                    str(extracted_bundle / bundled_python_relative),
                    "-I",
                    str(extracted_bundle / "_launch.py"),
                    "--run-script",
                    str(REPO_ROOT / "packaging" / "smoke_usd_runtime.py"),
                    *(["--materialx"] if usd["materialx"] else []),
                ],
                cwd=temporary,
                timeout=60.0,
            )
            _smoke_test_server_bundle(
                extracted_bundle,
                extracted_bundle / bundled_python_relative,
            )
    return _file_artifact(
        "server", archive, "self-contained-runtime", target=target.platform_tag, metadata=metadata
    )


def build_blender(
    output_dir: Path,
    *,
    blender_executable: Path,
    python_sdk: Path | None,
    generator: str | None,
    smoke_test: bool,
) -> Artifact:
    host = probe_blender(blender_executable)
    if python_sdk:
        target = host.python
    else:
        target = query_python(_managed_python(host.python.version_short))
    native = build_native_module(target, python_sdk=python_sdk, generator=generator)
    archive = package_addon(
        target,
        native,
        output_dir=output_dir,
        compatibility_alias=False,
    )
    if smoke_test:
        smoke_test_blender(host.executable, archive)
    return _file_artifact(
        "blender",
        archive,
        "blender-addon",
        target=f"{target.platform_tag}-{target.abi_tag}",
        metadata={"blender_version": ".".join(str(value) for value in host.version)},
    )


def build_unreal(
    output_dir: Path,
    staging: Path,
    *,
    engine_root: Path | None,
    smoke_test: bool,
) -> Artifact:
    engine = resolve_engine(explicit=engine_root)
    package = package_plugin(engine, force=True)
    root_name = (
        f"openusdconnect-unreal-{_version()}-ue{engine.version}-{engine.target_platform.lower()}"
    )
    staged = staging / "unreal" / root_name / "OpenUSDConnect"
    if staged.parent.exists():
        shutil.rmtree(staged.parent)
    shutil.copytree(
        package,
        staged,
        ignore=shutil.ignore_patterns("Intermediate", "*.pdb", "BuildPlugin.log"),
    )
    marker = staged / ".openusdconnect-package.json"
    build_metadata = {
        "artifact": "unreal-plugin",
        "version": _version(),
        "engine_version": engine.version,
        "target_platform": engine.target_platform,
    }
    if marker.is_file():
        raw_build = json.loads(marker.read_text(encoding="utf-8"))
        build_metadata["build"] = {
            key: raw_build[key] for key in ("engine_fingerprint", "fingerprint") if key in raw_build
        }
        marker.unlink()
    (staged / "openusdconnect-build.json").write_text(
        json.dumps(build_metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if smoke_test:
        smoke_dir = staging / "unreal-smoke"
        if smoke_dir.exists():
            shutil.rmtree(smoke_dir)
        env = os.environ.copy()
        for key in ("PYTHONPATH", "PYTHONHOME", "OPENUSDCONNECT_USD_ROOT", "PXR_PLUGINPATH_NAME"):
            env.pop(key, None)
        _checked_run(
            [
                "uv",
                "run",
                "--isolated",
                "--frozen",
                "--extra",
                "runtime",
                "python",
                str(REPO_ROOT / "scripts/run_unreal_tests.py"),
                "--engine-root",
                str(engine.root),
                "--work-dir",
                str(smoke_dir),
                "--plugin-package",
                str(staged),
                "--timeout",
                "300",
            ],
            env=env,
            timeout=600.0,
        )
    archive = output_dir / f"{root_name}.zip"
    _zip_tree(staged.parent, archive, root_name)
    return _file_artifact(
        "unreal",
        archive,
        "unreal-plugin",
        target=f"UE-{engine.version}-{engine.target_platform}",
        metadata=build_metadata,
    )


def _smoke_test_cpp_sdk(sdk: Path, staging: Path, generator: str | None) -> None:
    source = staging / "cpp-sdk-smoke-source"
    build = staging / "cpp-sdk-smoke-build"
    source.mkdir(parents=True)
    (source / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\n"
        "project(OpenUSDConnectSdkSmoke LANGUAGES CXX)\n"
        f'add_subdirectory("{sdk.as_posix()}" openusdconnect-sdk)\n'
        "add_executable(openusdconnect_sdk_smoke main.cpp)\n"
        "target_link_libraries(openusdconnect_sdk_smoke PRIVATE "
        "OpenUSDConnect::ClientProtocol)\n",
        encoding="utf-8",
        newline="\n",
    )
    (source / "main.cpp").write_text(
        "#include <openusdconnect/client/protocol_codec.h>\n"
        "int main() { flatbuffers::FlatBufferBuilder builder; return builder.GetSize() != 0; }\n",
        encoding="utf-8",
        newline="\n",
    )
    configure = ["cmake", "-S", str(source), "-B", str(build)]
    if generator:
        configure.extend(["-G", generator])
    if os.name == "nt" and generator and generator.startswith("Visual Studio"):
        configure.extend(["-A", "x64"])
    _checked_run(configure, timeout=120.0)
    _checked_run(["cmake", "--build", str(build), "--config", "Release"], timeout=120.0)


def build_cpp_sdk(
    output_dir: Path,
    staging: Path,
    *,
    smoke_test: bool = False,
    generator: str | None = None,
) -> Artifact:
    root_name = f"openusdconnect-client-sdk-{_version()}"
    sdk = staging / "cpp-sdk" / root_name
    if sdk.exists():
        shutil.rmtree(sdk)
    sdk.mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "native" / "client_core", sdk / "client_core")
    flatbuffers = (
        REPO_ROOT
        / "integrations"
        / "unreal"
        / "OpenUSDConnect"
        / "Source"
        / "OpenUSDConnectPXR"
        / "ThirdParty"
        / "flatbuffers"
    )
    if not (flatbuffers / "include" / "flatbuffers" / "flatbuffers.h").is_file():
        _checked_run(
            [
                sys.executable,
                str(
                    REPO_ROOT
                    / "integrations"
                    / "unreal"
                    / "OpenUSDConnect"
                    / "setup_flatbuffers.py"
                ),
            ]
        )
    shutil.copytree(flatbuffers, sdk / "flatbuffers")
    shutil.copy2(REPO_ROOT / "LICENSE", sdk / "LICENSE")
    shutil.copy2(REPO_ROOT / "NOTICE", sdk / "NOTICE")
    (sdk / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\n"
        "project(OpenUSDConnectClientSdk LANGUAGES CXX)\n"
        "if(NOT TARGET flatbuffers::flatbuffers)\n"
        "    add_library(OpenUSDConnectFlatBuffers INTERFACE)\n"
        "    target_include_directories(OpenUSDConnectFlatBuffers INTERFACE "
        "${CMAKE_CURRENT_SOURCE_DIR}/flatbuffers/include)\n"
        "    add_library(flatbuffers::flatbuffers ALIAS OpenUSDConnectFlatBuffers)\n"
        "endif()\n"
        "add_subdirectory(client_core)\n",
        encoding="utf-8",
        newline="\n",
    )
    (sdk / "README.txt").write_text(
        "Add this directory with add_subdirectory(), then link "
        "OpenUSDConnect::ClientCore or OpenUSDConnect::ClientProtocol. The protocol target uses "
        "the included pinned FlatBuffers headers unless the consumer already defines "
        "flatbuffers::flatbuffers.\n",
        encoding="utf-8",
        newline="\n",
    )
    if smoke_test:
        _smoke_test_cpp_sdk(sdk, staging, generator)
    archive = output_dir / f"{root_name}.zip"
    _zip_tree(sdk, archive, root_name)
    return _file_artifact("cpp-sdk", archive, "source-sdk")


def _docker_command_prefix(command: str) -> list[str]:
    prefix = shlex.split(command, posix=os.name != "nt")
    if not prefix:
        raise RuntimeError("Docker command cannot be empty")
    if os.name == "nt":
        prefix = [
            part[1:-1] if part.startswith('"') and part.endswith('"') else part for part in prefix
        ]
    return prefix


def _docker_usd_options(
    profile: str, root: Path | None, plugins: list[Path], allow_unpinned: bool
) -> list[str]:
    validate_options(profile, root, plugins, allow_unpinned)
    options = ["--build-arg", f"USD_PROFILE={profile}"]
    if profile == "external":
        # Relative contexts also work with a Docker CLI invoked through WSL.
        inputs = REPO_ROOT / "build" / "distribution" / "docker-inputs"
        if inputs.exists():
            shutil.rmtree(inputs)
        shutil.copytree(root, inputs / "runtime")
        (inputs / "plugins").mkdir(parents=True)
        for index, plugin in enumerate(plugins):
            shutil.copytree(plugin, inputs / "plugins" / str(index))
        options.extend(
            [
                "--build-context",
                f"usd_runtime={(inputs / 'runtime').relative_to(REPO_ROOT).as_posix()}",
                "--build-context",
                f"usd_plugins={(inputs / 'plugins').relative_to(REPO_ROOT).as_posix()}",
                "--build-arg",
                f"ALLOW_UNPINNED_USD={int(allow_unpinned)}",
            ]
        )
    return options


def build_docker(
    *,
    tag: str,
    target: str,
    smoke_test: bool,
    command: str,
    usd_profile: str = "full",
    usd_root: Path | None = None,
    usd_plugin_paths: list[Path] | None = None,
    allow_unpinned_usd: bool = False,
) -> Artifact:
    prefix = _docker_command_prefix(command)
    options = _docker_usd_options(usd_profile, usd_root, usd_plugin_paths or [], allow_unpinned_usd)
    _checked_run([*prefix, "build", "--target", target, "--tag", tag, *options, "."])
    probe = _checked_run(
        [
            *prefix,
            "run",
            "--rm",
            "--entrypoint",
            "python",
            tag,
            "-I",
            "/opt/ouc/_launch.py",
            "--runtime-info",
        ]
    )
    usd = json.loads(probe.stdout.strip().splitlines()[-1])
    usd.pop("pxr_path", None)
    image = _checked_run(
        [
            *prefix,
            "image",
            "inspect",
            tag,
            "--format",
            "{{.Id}} {{.Os}} {{.Architecture}} {{.Size}}",
        ]
    )
    image_fields = image.stdout.strip().splitlines()[-1].split()
    if len(image_fields) != 4:
        raise RuntimeError(f"Unexpected Docker image metadata: {image.stdout!r}")
    image_id_value, image_os, image_arch, image_size = image_fields
    architecture = {"amd64": "x64", "arm64": "arm64"}.get(image_arch, image_arch)
    if smoke_test:
        _checked_run([*prefix, "run", "--rm", tag, "--help"], timeout=30.0)
        if target == "mcp":
            return Artifact(
                component="docker",
                name=tag,
                kind="docker-image",
                target=f"{image_os}-{architecture}",
                metadata={
                    "docker_target": target,
                    "image_id": image_id_value,
                    "image_size": int(image_size),
                    "openusd": usd,
                },
            )
        container = f"openusdconnect-release-smoke-{os.getpid()}"
        _checked_run(
            [*prefix, "run", "--detach", "--name", container, tag],
            timeout=30.0,
        )
        try:
            deadline = time.monotonic() + 45.0
            status = "starting"
            while time.monotonic() < deadline:
                inspected = subprocess.run(
                    [
                        *prefix,
                        "container",
                        "inspect",
                        container,
                        "--format",
                        "{{.State.Status}} {{.State.Health.Status}}",
                    ],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if inspected.returncode:
                    raise RuntimeError(
                        f"Could not inspect Docker smoke container: {inspected.stderr}"
                    )
                state, status = inspected.stdout.strip().split(maxsplit=1)
                if status == "healthy":
                    break
                if state == "exited":
                    logs = _checked_run([*prefix, "logs", container]).stdout
                    raise RuntimeError(f"Docker smoke container exited early:\n{logs}")
                time.sleep(1.0)
            else:
                logs = _checked_run([*prefix, "logs", container]).stdout
                raise RuntimeError(
                    f"Docker smoke container did not become healthy ({status}):\n{logs}"
                )
            if target in {"live-open", "complete"}:
                _checked_run(
                    [
                        *prefix,
                        "exec",
                        container,
                        "python",
                        "-c",
                        "import urllib.request; "
                        "urllib.request.urlopen('http://127.0.0.1:7280/usd/scene.usd', "
                        "timeout=3).read()",
                    ],
                    timeout=30.0,
                )
            if target == "complete":
                _checked_run(
                    [
                        *prefix,
                        "exec",
                        container,
                        "python",
                        "-c",
                        "import urllib.request; "
                        "urllib.request.urlopen('http://127.0.0.1:8080/api/status', "
                        "timeout=3).read()",
                    ],
                    timeout=30.0,
                )
        finally:
            subprocess.run(
                [*prefix, "rm", "--force", container],
                cwd=REPO_ROOT,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
    return Artifact(
        component="docker",
        name=tag,
        kind="docker-image",
        target=f"{image_os}-{architecture}",
        metadata={
            "docker_target": target,
            "image_id": image_id_value,
            "image_size": int(image_size),
            "openusd": usd,
        },
    )


def _build_commit() -> str:
    commit = os.environ.get("OPENUSDCONNECT_BUILD_COMMIT") or os.environ.get("GITHUB_SHA")
    if commit:
        return commit
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def build_linux_packages_with_docker(
    output_dir: Path,
    staging: Path,
    *,
    command: str,
    usd_profile: str = "full",
    usd_root: Path | None = None,
    usd_plugin_paths: list[Path] | None = None,
    allow_unpinned_usd: bool = False,
) -> list[Artifact]:
    prefix = _docker_command_prefix(command)
    options = _docker_usd_options(usd_profile, usd_root, usd_plugin_paths or [], allow_unpinned_usd)
    exported = staging / "docker-linux-packages"
    if exported.exists():
        shutil.rmtree(exported)
    exported.parent.mkdir(parents=True, exist_ok=True)
    relative_export = exported.relative_to(REPO_ROOT).as_posix()
    _checked_run(
        [
            *prefix,
            "build",
            "--target",
            "release-packages",
            "--build-arg",
            f"OPENUSDCONNECT_BUILD_COMMIT={_build_commit()}",
            *options,
            "--output",
            f"type=local,dest={relative_export}",
            ".",
        ]
    )
    manifest_path = exported / "release-manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("Docker Linux package build did not export a release manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifacts = []
    for record in manifest.get("artifacts", []):
        packaged_path = record.get("path")
        if not packaged_path:
            continue
        source = exported / Path(packaged_path).name
        if not source.is_file():
            raise RuntimeError(f"Docker Linux package build omitted {source.name}")
        destination = output_dir / source.name
        portable_duplicate = record["kind"] == "source-sdk" or (
            record["component"] == "python" and source.name.endswith(".tar.gz")
        )
        if destination.exists():
            if portable_duplicate:
                continue
            raise RuntimeError(f"Docker Linux package collides with existing {destination.name}")
        shutil.copy2(source, destination)
        artifacts.append(
            _file_artifact(
                f"linux-{record['component']}",
                destination,
                record["kind"],
                target=record.get("target") or "linux-x64",
                metadata={**(record.get("metadata") or {}), "built_with": "docker"},
            )
        )
    if not artifacts:
        raise RuntimeError("Docker Linux package build exported no file artifacts")
    return artifacts


def _components(values: list[str]) -> list[str]:
    requested = values or ["all"]
    expanded = (
        [*DEFAULT_COMPONENTS, *(value for value in requested if value != "all")]
        if "all" in requested
        else requested
    )
    return list(dict.fromkeys(expanded))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build OpenUSDConnect distribution artifacts")
    parser.add_argument(
        "--component",
        action="append",
        choices=(*DEFAULT_COMPONENTS, *OPTIONAL_COMPONENTS, "all"),
        default=[],
        help="Artifact to build; repeat as needed. Default: all.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "dist" / "release" / _version(),
    )
    parser.add_argument(
        "--clean-output",
        action="store_true",
        help="Remove an existing output directory before building",
    )
    parser.add_argument("--server-python", default="3.13")
    parser.add_argument(
        "--usd-profile",
        choices=PROFILES,
        default="full",
        help="Server OpenUSD runtime: full includes MaterialX; core uses usd-core",
    )
    parser.add_argument("--usd-root", type=Path, help="External OpenUSD installation to bundle")
    parser.add_argument(
        "--usd-plugin-path",
        type=Path,
        action="append",
        default=[],
        help="Additional relocatable plugin installation (external profile; repeatable)",
    )
    parser.add_argument(
        "--allow-unpinned-usd",
        action="store_true",
        help="Allow an external OpenUSD version different from openusd.lock.json",
    )
    parser.add_argument("--blender", type=Path)
    parser.add_argument("--blender-python-sdk", type=Path)
    parser.add_argument("--unreal-engine", type=Path)
    parser.add_argument(
        "--generator",
        default="Visual Studio 17 2022" if os.name == "nt" else None,
    )
    parser.add_argument("--docker-tag", default=f"openusdconnect-server:{_version()}")
    parser.add_argument(
        "--docker-target",
        choices=DOCKER_TARGETS,
        default="server",
        help="Docker runtime image target. Default: server.",
    )
    parser.add_argument(
        "--docker-command",
        default=os.environ.get("OPENUSDCONNECT_DOCKER_COMMAND", "docker"),
        help="Docker CLI command or command prefix",
    )
    parser.add_argument("--smoke-test", action=argparse.BooleanOptionalAction, default=True)
    return parser


def _write_release_manifest(output_dir: Path, artifacts: list[Artifact]) -> None:
    artifact_records = []
    for artifact in artifacts:
        record = asdict(artifact)
        if artifact.path is not None:
            record["path"] = Path(artifact.path).name
        artifact_records.append(record)
    manifest = {
        "schema": 1,
        "version": _version(),
        "commit": _build_commit(),
        "host": {"platform": _platform_tag(), "python": platform.python_version()},
        "artifacts": artifact_records,
    }
    (output_dir / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    checksum_lines = [
        f"{artifact.sha256}  {artifact.name}"
        for artifact in artifacts
        if artifact.sha256 is not None
    ]
    (output_dir / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n", encoding="ascii", newline="\n"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    components = _components(args.component)
    output_dir = args.output_dir.expanduser().resolve()
    try:
        validate_options(
            args.usd_profile, args.usd_root, args.usd_plugin_path, args.allow_unpinned_usd
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    protected = [REPO_ROOT, Path.home().resolve(), *args.usd_plugin_path]
    if args.usd_root:
        protected.append(args.usd_root.resolve())
    if any(path.resolve().is_relative_to(output_dir) for path in protected):
        print(
            f"error: output directory contains a protected source path: {output_dir}",
            file=sys.stderr,
        )
        return 1
    staging = REPO_ROOT / "build" / "distribution"
    if staging.exists():
        shutil.rmtree(staging)
    if args.clean_output and output_dir.exists():
        unsafe = {Path(output_dir.anchor), Path.home().resolve(), REPO_ROOT}
        if output_dir in unsafe:
            raise RuntimeError(f"Refusing to remove unsafe output directory: {output_dir}")
        shutil.rmtree(output_dir)
    elif (output_dir / "release-manifest.json").exists():
        print(
            f"error: release output already exists: {output_dir}; "
            "pass --clean-output or choose another --output-dir",
            file=sys.stderr,
        )
        return 1
    output_dir.mkdir(parents=True, exist_ok=True)

    artifacts: list[Artifact] = []
    wheel: Path | None = None
    try:
        if "python" in components or "server" in components:
            python_artifacts, wheel = build_python(
                output_dir,
                staging,
                publish="python" in components,
            )
            if "python" in components:
                artifacts.extend(python_artifacts)
        if "server" in components:
            assert wheel is not None
            artifacts.append(
                build_server_bundle(
                    output_dir,
                    staging,
                    wheel,
                    python_version=args.server_python,
                    smoke_test=args.smoke_test,
                    usd_profile=args.usd_profile,
                    usd_root=args.usd_root,
                    usd_plugin_paths=args.usd_plugin_path,
                    allow_unpinned_usd=args.allow_unpinned_usd,
                )
            )
        if "blender" in components:
            blender = args.blender or configured_blender()
            if blender is None:
                raise RuntimeError(
                    "Blender artifact requested; pass --blender or configure BLENDER_EXE/"
                    "blender.test.cfg"
                )
            artifacts.append(
                build_blender(
                    output_dir,
                    blender_executable=blender,
                    python_sdk=args.blender_python_sdk,
                    generator=args.generator,
                    smoke_test=args.smoke_test,
                )
            )
        if "unreal" in components:
            artifacts.append(
                build_unreal(
                    output_dir,
                    staging,
                    engine_root=args.unreal_engine,
                    smoke_test=args.smoke_test,
                )
            )
        if "cpp-sdk" in components:
            artifacts.append(
                build_cpp_sdk(
                    output_dir,
                    staging,
                    smoke_test=args.smoke_test,
                    generator=args.generator,
                )
            )
        if "docker" in components:
            artifacts.append(
                build_docker(
                    tag=args.docker_tag,
                    target=args.docker_target,
                    smoke_test=args.smoke_test,
                    command=args.docker_command,
                    usd_profile=args.usd_profile,
                    usd_root=args.usd_root,
                    usd_plugin_paths=args.usd_plugin_path,
                    allow_unpinned_usd=args.allow_unpinned_usd,
                )
            )
        if "linux-packages" in components:
            artifacts.extend(
                build_linux_packages_with_docker(
                    output_dir,
                    staging,
                    command=args.docker_command,
                    usd_profile=args.usd_profile,
                    usd_root=args.usd_root,
                    usd_plugin_paths=args.usd_plugin_path,
                    allow_unpinned_usd=args.allow_unpinned_usd,
                )
            )
        _write_release_manifest(output_dir, artifacts)
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Release artifacts: {output_dir}")
    for artifact in artifacts:
        print(f"  {artifact.component}: {artifact.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
