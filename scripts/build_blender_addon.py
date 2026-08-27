"""Build a self-contained Blender addon for an exact host Python ABI."""

from __future__ import annotations

import argparse
import ast
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON_NAME = "usd_connect"
PROBE_MARKER = "OPENUSDCONNECT_BLENDER_PROBE="
IMPORT_MARKER = "OPENUSDCONNECT_BLENDER_IMPORT_OK"


@dataclass(frozen=True, slots=True)
class HostPython:
    executable: Path
    version: tuple[int, int, int]
    cache_tag: str
    extension_suffix: str
    platform_tag: str

    @property
    def version_short(self) -> str:
        return f"{self.version[0]}.{self.version[1]}"

    @property
    def abi_tag(self) -> str:
        if self.cache_tag.startswith("cpython-"):
            return "cp" + self.cache_tag.removeprefix("cpython-")
        return self.cache_tag.replace("-", "_")


@dataclass(frozen=True, slots=True)
class BlenderHost:
    executable: Path
    version: tuple[int, int, int]
    python: HostPython


def _checked_run(
    command: list[str],
    *,
    cwd: Path = REPO_ROOT,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {subprocess.list2cmdline(command)}\n"
            f"{result.stdout}{result.stderr}"
        )
    return result


def _platform_tag(system: str, machine: str) -> str:
    os_name = {"windows": "windows", "linux": "linux", "darwin": "macos"}.get(system.lower())
    architecture = {
        "amd64": "x64",
        "x86_64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine.lower())
    if not os_name or not architecture:
        raise RuntimeError(f"Unsupported host platform: {system} {machine}")
    return f"{os_name}-{architecture}"


def _host_python(data: dict[str, object], executable: Path) -> HostPython:
    return HostPython(
        executable=executable.resolve(),
        version=tuple(data["python_version"]),
        cache_tag=str(data["cache_tag"]),
        extension_suffix=str(data["extension_suffix"]),
        platform_tag=_platform_tag(str(data["system"]), str(data["machine"])),
    )


def query_python(executable: Path) -> HostPython:
    expression = (
        "import json,platform,sys,sysconfig;"
        "print(json.dumps({"
        "'python_version':list(sys.version_info[:3]),"
        "'cache_tag':sys.implementation.cache_tag,"
        "'extension_suffix':sysconfig.get_config_var('EXT_SUFFIX'),"
        "'system':platform.system(),"
        "'machine':platform.machine()}))"
    )
    result = _checked_run([str(executable), "-c", expression])
    data = json.loads(result.stdout.strip().splitlines()[-1])
    return _host_python(data, executable)


def probe_blender(executable: Path) -> BlenderHost:
    executable = executable.expanduser().resolve()
    expression = (
        "import bpy,json,platform,sys,sysconfig;"
        f"print('{PROBE_MARKER}'+json.dumps({{"
        "'blender_version':list(bpy.app.version),"
        "'python_executable':sys.executable,"
        "'python_version':list(sys.version_info[:3]),"
        "'cache_tag':sys.implementation.cache_tag,"
        "'extension_suffix':sysconfig.get_config_var('EXT_SUFFIX'),"
        "'system':platform.system(),"
        "'machine':platform.machine()}))"
    )
    result = _checked_run(
        [
            str(executable),
            "--background",
            "--factory-startup",
            "--python-expr",
            expression,
        ]
    )
    line = next(
        (line for line in result.stdout.splitlines() if line.startswith(PROBE_MARKER)), None
    )
    if line is None:
        raise RuntimeError(f"Blender did not report its Python ABI:\n{result.stdout}")
    data = json.loads(line.removeprefix(PROBE_MARKER))
    return BlenderHost(
        executable=executable,
        version=tuple(data["blender_version"]),
        python=_host_python(data, Path(str(data["python_executable"]))),
    )


def configured_blender() -> Path | None:
    configured = os.environ.get("BLENDER_EXE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    config = REPO_ROOT / "blender.test.cfg"
    if config.is_file():
        configured = config.read_text(encoding="utf-8").strip()
        if configured and not configured.startswith("#"):
            return Path(configured).expanduser().resolve()
    return None


def managed_python(version: str) -> Path:
    command = [
        "uv",
        "python",
        "find",
        "--managed-python",
        "--system",
        "--no-project",
        version,
    ]
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        _checked_run(["uv", "python", "install", version])
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    if result.returncode:
        raise RuntimeError(
            f"Could not provision a managed Python {version} development runtime.\n{result.stderr}"
        )
    return Path(result.stdout.strip()).resolve()


def _project_version() -> str:
    values = runpy.run_path(REPO_ROOT / "openusdconnect" / "_version.py")
    return str(values["__version__"])


def _addon_info() -> dict[str, object]:
    path = REPO_ROOT / "integrations" / "blender" / "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "bl_info" for target in node.targets
        ):
            info = ast.literal_eval(node.value)
            expected = tuple(int(part) for part in _project_version().split("."))
            if tuple(info["version"]) != expected:
                raise RuntimeError(
                    f"Blender addon version {info['version']} does not match {expected}"
                )
            return info
    raise RuntimeError(f"bl_info not found in {path}")


def _sdk_paths(root: Path, target: HostPython) -> tuple[Path, Path | None]:
    include = next(
        (
            path
            for path in (root / "include", root / f"include/python{target.version_short}")
            if path.is_dir()
        ),
        None,
    )
    if include is None:
        raise RuntimeError(f"Python headers were not found below {root}")
    library = None
    if os.name == "nt":
        name = f"python{target.version[0]}{target.version[1]}.lib"
        library = next(
            (path for path in (root / "libs" / name, root / "lib" / name) if path.is_file()),
            None,
        )
        if library is None:
            raise RuntimeError(f"{name} was not found below {root}")
    return include, library


def build_native_module(
    target: HostPython,
    *,
    python_sdk: Path | None = None,
    generator: str | None = None,
) -> Path:
    build_dir = REPO_ROOT / "build" / "host-native" / f"{target.platform_tag}-{target.abi_tag}"
    if build_dir.exists():
        shutil.rmtree(build_dir)

    nanobind_dir = _checked_run(
        [
            "uv",
            "run",
            "--frozen",
            "--with",
            "nanobind>=2.11,<3",
            "python",
            "-m",
            "nanobind",
            "--cmake_dir",
        ]
    ).stdout.strip()
    command = [
        "cmake",
        "-S",
        str(REPO_ROOT / "packaging" / "blender_native"),
        "-B",
        str(build_dir),
        f"-DOPENUSDCONNECT_SOURCE_DIR={REPO_ROOT.as_posix()}",
        f"-DPython_EXECUTABLE={target.executable.as_posix()}",
        f"-Dnanobind_DIR={Path(nanobind_dir).as_posix()}",
    ]
    if python_sdk:
        include, library = _sdk_paths(python_sdk.resolve(), target)
        command.append(f"-DPython_INCLUDE_DIR={include.as_posix()}")
        if library:
            command.append(f"-DPython_LIBRARY={library.as_posix()}")
    if generator:
        command.extend(["-G", generator])
    if os.name == "nt" and target.platform_tag == "windows-x64":
        command.extend(["-A", "x64"])
    _checked_run(command)
    _checked_run(["cmake", "--build", str(build_dir), "--config", "Release"])

    candidates = [
        path
        for path in build_dir.rglob("_native_client*")
        if path.is_file() and path.name.endswith(target.extension_suffix)
    ]
    if len(candidates) != 1:
        found = ", ".join(str(path) for path in candidates) or "none"
        raise RuntimeError(
            f"Expected one native module ending with {target.extension_suffix}; found {found}"
        )
    return candidates[0]


def _write_blender_manifest(path: Path, target: HostPython, info: dict[str, object]) -> None:
    minimum = ".".join(str(part) for part in info["blender"])
    path.write_text(
        f'''schema_version = "1.0.0"
id = "usd_connect"
version = "{_project_version()}"
name = "OpenUSDConnect"
tagline = "Synchronize USD edits between connected applications"
maintainer = "OpenUSDConnect contributors"
type = "add-on"
blender_version_min = "{minimum}"
license = ["SPDX:Apache-2.0"]
platforms = ["{target.platform_tag}"]

[permissions]
network = "Connect to an OpenUSDConnect synchronization server"
files = "Open, monitor, and author USD files selected by the user"
''',
        encoding="utf-8",
        newline="\n",
    )


def package_addon(
    target: HostPython,
    native_module: Path,
    *,
    output_dir: Path,
    compatibility_alias: bool = True,
) -> Path:
    info = _addon_info()
    version = _project_version()
    stem = f"usd_connect_blender-{version}-{target.platform_tag}-{target.abi_tag}"
    package_root = REPO_ROOT / "build" / "blender-package" / stem
    addon_dir = package_root / ADDON_NAME
    if package_root.exists():
        shutil.rmtree(package_root)
    addon_dir.mkdir(parents=True)

    for source in (REPO_ROOT / "integrations" / "blender").iterdir():
        if source.suffix == ".py":
            shutil.copy2(source, addon_dir / source.name)
    shutil.copy2(
        REPO_ROOT / "integrations" / "openpbr_to_standard_surface.py",
        addon_dir / "openpbr_to_standard_surface.py",
    )
    core = addon_dir / "openusdconnect"
    shutil.copytree(
        REPO_ROOT / "openusdconnect",
        core,
        ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "*.pyd", "*.so", "*.dylib", "dashboard"
        ),
    )
    shutil.copy2(native_module, core / native_module.name)

    import flatbuffers

    shutil.copytree(
        Path(flatbuffers.__path__[0]),
        addon_dir / "flatbuffers",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    _write_blender_manifest(addon_dir / "blender_manifest.toml", target, info)
    (addon_dir / "openusdconnect-build.json").write_text(
        json.dumps(
            {
                "artifact": "blender-addon",
                "version": version,
                "platform": target.platform_tag,
                "python_version": target.version_short,
                "python_abi": target.abi_tag,
                "native_module": native_module.name,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"{stem}.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package_root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(package_root).as_posix())
    if compatibility_alias:
        shutil.copy2(archive_path, output_dir / "usd_connect_blender.zip")
    shutil.rmtree(package_root)
    return archive_path


def smoke_test(blender: Path, archive: Path) -> None:
    expression = (
        "import bpy;"
        f"bpy.ops.preferences.addon_install(filepath={str(archive)!r},overwrite=True);"
        "bpy.ops.preferences.addon_enable(module='usd_connect');"
        "import openusdconnect._native_client;"
        f"print('{IMPORT_MARKER}')"
    )
    with tempfile.TemporaryDirectory(prefix="openusdconnect-blender-package-") as user_root:
        env = os.environ.copy()
        env["BLENDER_USER_RESOURCES"] = user_root
        result = subprocess.run(
            [
                str(blender),
                "--background",
                "--factory-startup",
                "--python-expr",
                expression,
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    if result.returncode or IMPORT_MARKER not in result.stdout:
        raise RuntimeError(
            f"Blender could not import the packaged native client.\n{result.stdout}{result.stderr}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a platform- and Python-ABI-specific Blender addon."
    )
    parser.add_argument("--blender", type=Path, help="Blender executable for ABI detection/test")
    parser.add_argument("--python-version", help="Target CPython major.minor")
    parser.add_argument("--python-executable", type=Path, help="Matching development Python")
    parser.add_argument("--python-sdk", type=Path, help="SDK root containing include/ and libs/")
    parser.add_argument("--native-module", type=Path, help="Use a prebuilt native module")
    parser.add_argument(
        "--generator",
        default="Visual Studio 17 2022" if os.name == "nt" else None,
    )
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "dist")
    parser.add_argument(
        "--compatibility-alias",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--smoke-test", action=argparse.BooleanOptionalAction, default=True)
    return parser


def _resolve_target(args: argparse.Namespace) -> tuple[HostPython, BlenderHost | None]:
    blender_path = args.blender or configured_blender()
    blender = probe_blender(blender_path) if blender_path else None
    if args.python_executable:
        target = query_python(args.python_executable)
    elif blender and args.python_sdk:
        target = blender.python
    else:
        requested = args.python_version or (blender.python.version_short if blender else None)
        if not requested:
            raise RuntimeError(
                "Pass --blender, --python-version, or configure BLENDER_EXE/blender.test.cfg"
            )
        target = query_python(managed_python(requested))

    if blender:
        if target.version[:2] != blender.python.version[:2]:
            raise RuntimeError(
                f"Blender embeds Python {blender.python.version_short}; target is "
                f"{target.version_short}"
            )
        if target.platform_tag != blender.python.platform_tag:
            raise RuntimeError(
                f"Blender target is {blender.python.platform_tag}; build target is "
                f"{target.platform_tag}"
            )
    return target, blender


def build(args: argparse.Namespace) -> Path:
    target, blender = _resolve_target(args)
    native_module = args.native_module
    if native_module:
        native_module = native_module.expanduser().resolve()
        if not native_module.is_file() or not native_module.name.endswith(target.extension_suffix):
            raise RuntimeError(
                f"Native module must exist and end with {target.extension_suffix}: {native_module}"
            )
    else:
        native_module = build_native_module(
            target,
            python_sdk=args.python_sdk,
            generator=args.generator,
        )
    archive = package_addon(
        target,
        native_module,
        output_dir=args.output_dir.expanduser().resolve(),
        compatibility_alias=args.compatibility_alias,
    )
    print(f"Built: {archive}")
    if blender and args.smoke_test:
        smoke_test(blender.executable, archive)
        print(f"Verified with Blender: {blender.executable}")
    elif args.smoke_test:
        print("Blender smoke test skipped: no Blender executable was supplied")
    return archive


def main(argv: list[str] | None = None) -> int:
    try:
        build(_parser().parse_args(argv))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
