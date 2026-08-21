"""Build the OpenUSD release supported by OpenUSDConnect.

This is a pinned, non-destructive wrapper around OpenUSD's own build_usd.py.
It owns only the directories selected through this command and never updates or
resets an existing OpenUSD checkout.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import shlex
import shutil
import struct
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Never

REPO_ROOT = Path(__file__).resolve().parents[1]
PIN_FILE = REPO_ROOT / "openusd.lock.json"
PYTHON_VERSION_FILE = REPO_ROOT / ".python-version"
MINIMUM_CMAKE = (3, 27)
SUPPORTED_PLATFORMS = {"win32", "linux", "darwin"}
DEFAULT_JOBS = min(os.cpu_count() or 1, 8)


def _platform_key() -> str:
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    return f"{system}-{machine}"


ACTIVE_RUNTIME_FILE = REPO_ROOT / ".openusd" / f"active-{_platform_key()}.json"
LEGACY_ACTIVE_RUNTIME_FILE = REPO_ROOT / ".openusd" / "active.json"


def load_required_python(path: Path = PYTHON_VERSION_FILE) -> tuple[int, int]:
    value = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+", value):
        raise RuntimeError(f"invalid Python version in {path}: {value!r}")
    major, minor = (int(part) for part in value.split("."))
    return major, minor


REQUIRED_PYTHON = load_required_python()


@dataclass(frozen=True)
class OpenUSDPin:
    repository: str
    tag: str
    commit: str
    version: str
    usd_core: str


@dataclass(frozen=True)
class BuildLayout:
    root: Path
    checkout: Path
    dependency_sources: Path
    build: Path
    install: Path


@dataclass(frozen=True)
class BuildFeatures:
    python: bool
    tools: bool
    materialx: bool
    embree: bool
    usdview: bool
    renderman: Path | None

    @property
    def imaging(self) -> bool:
        return self.usdview or self.materialx or self.embree or self.renderman is not None


@dataclass(frozen=True)
class BuildPlan:
    pin: OpenUSDPin
    layout: BuildLayout
    features: BuildFeatures
    jobs: int
    variant: str
    generator: str | None
    toolset: str | None
    force: tuple[str, ...]
    force_all: bool
    verbose: int


PROFILE_DEFAULTS = {
    "runtime": {
        "python": True,
        "tools": True,
        "materialx": False,
        "embree": False,
        "usdview": False,
    },
    "usdview": {
        "python": True,
        "tools": True,
        "materialx": True,
        "embree": False,
        "usdview": True,
    },
}


def load_pin(path: Path = PIN_FILE) -> OpenUSDPin:
    data = json.loads(path.read_text(encoding="utf-8"))
    pin = OpenUSDPin(**data)
    if not re.fullmatch(r"[0-9a-f]{40}", pin.commit):
        raise RuntimeError(f"invalid OpenUSD commit in {path}: {pin.commit!r}")
    version_parts = pin.version.split(".")
    if len(version_parts) != 3 or version_parts[0] != "0":
        raise RuntimeError(f"invalid OpenUSD version in {path}: {pin.version!r}")
    expected_tag = f"v{version_parts[1]}.{int(version_parts[2]):02d}"
    if pin.tag != expected_tag:
        raise RuntimeError(f"OpenUSD tag and version disagree in {path}")
    if pin.usd_core != pin.version.removeprefix("0."):
        raise RuntimeError(f"OpenUSD source and usd-core versions disagree in {path}")
    return pin


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _default_root(pin: OpenUSDPin) -> Path:
    version_root = REPO_ROOT / ".openusd" / pin.version.removeprefix("0.")
    try:
        legacy = json.loads(LEGACY_ACTIVE_RUNTIME_FILE.read_text(encoding="utf-8"))
        legacy_install = Path(str(legacy["usd_root"])).resolve()
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        legacy_install = None
    if (
        legacy_install is not None
        and legacy_install == (version_root / "install").resolve()
        and legacy_install.is_dir()
    ):
        return version_root
    return version_root / _platform_key()


def _parser(pin: OpenUSDPin) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            f"Clone and build the pinned OpenUSD {pin.version} release using its own build_usd.py."
        )
    )
    parser.add_argument(
        "--profile",
        choices=tuple(PROFILE_DEFAULTS),
        default="runtime",
        help="Feature baseline: headless runtime (default) or usdview.",
    )
    parser.add_argument(
        "--root",
        type=_path,
        default=_default_root(pin),
        help="Root for the managed checkout, dependency sources, build, and install trees.",
    )
    parser.add_argument("--checkout-dir", type=_path, help="Override the OpenUSD checkout path.")
    parser.add_argument(
        "--dependency-source-dir",
        type=_path,
        help="Override the directory used for downloaded third-party sources.",
    )
    parser.add_argument("--build-dir", type=_path, help="Override the build directory.")
    parser.add_argument("--install-dir", type=_path, help="Override the final install prefix.")
    parser.add_argument(
        "--python",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Build OpenUSD Python bindings (profile default: enabled).",
    )
    parser.add_argument(
        "--tools",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Build OpenUSD command-line tools (profile default: enabled).",
    )
    parser.add_argument(
        "--materialx",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Build MaterialX support.",
    )
    parser.add_argument(
        "--embree",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Build the Embree Hydra imaging plugin.",
    )
    parser.add_argument(
        "--usdview",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Build usdview and its required imaging components.",
    )
    parser.add_argument(
        "--renderman",
        type=_path,
        metavar="PATH",
        help="Build hdPrman for usdview against an existing RenderMan installation.",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"Parallel build jobs (default: {DEFAULT_JOBS}).",
    )
    parser.add_argument(
        "--variant",
        choices=("release", "debug", "relwithdebuginfo"),
        default="release",
    )
    parser.add_argument("--generator", help="CMake generator forwarded to build_usd.py.")
    parser.add_argument("--toolset", help="CMake toolset forwarded to build_usd.py.")
    parser.add_argument(
        "--force",
        action="append",
        default=[],
        metavar="DEPENDENCY",
        help="Force one named upstream dependency to rebuild; may be repeated.",
    )
    parser.add_argument(
        "--force-all", action="store_true", help="Force every component to rebuild."
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the pinned checkout and upstream build commands without changing anything.",
    )
    return parser


def _selected(value: bool | None, defaults: dict[str, bool], name: str) -> bool:
    return defaults[name] if value is None else value


def resolve_features(args: argparse.Namespace) -> BuildFeatures:
    defaults = PROFILE_DEFAULTS[args.profile]
    python = _selected(args.python, defaults, "python")
    tools = _selected(args.tools, defaults, "tools")
    materialx = _selected(args.materialx, defaults, "materialx")
    embree = _selected(args.embree, defaults, "embree")
    usdview = _selected(args.usdview, defaults, "usdview")

    if args.renderman is not None:
        if args.usdview is False:
            raise ValueError("--renderman cannot be combined with --no-usdview")
        usdview = True
    if usdview and not python:
        raise ValueError("usdview requires Python bindings; remove --no-python")

    return BuildFeatures(
        python=python,
        tools=tools,
        materialx=materialx,
        embree=embree,
        usdview=usdview,
        renderman=args.renderman,
    )


def resolve_layout(args: argparse.Namespace) -> BuildLayout:
    root = args.root.resolve()
    return BuildLayout(
        root=root,
        checkout=(args.checkout_dir or root / "checkout").resolve(),
        dependency_sources=(args.dependency_source_dir or root / "sources").resolve(),
        build=(args.build_dir or root / "build").resolve(),
        install=(args.install_dir or root / "install").resolve(),
    )


def create_plan(args: argparse.Namespace, pin: OpenUSDPin) -> BuildPlan:
    if args.jobs <= 0:
        raise ValueError("--jobs must be greater than zero")
    return BuildPlan(
        pin=pin,
        layout=resolve_layout(args),
        features=resolve_features(args),
        jobs=args.jobs,
        variant=args.variant,
        generator=args.generator,
        toolset=args.toolset,
        force=tuple(args.force),
        force_all=args.force_all,
        verbose=args.verbose,
    )


def clone_command(plan: BuildPlan) -> list[str]:
    return [
        "git",
        "clone",
        "--branch",
        plan.pin.tag,
        "--depth",
        "1",
        "--single-branch",
        plan.pin.repository,
        str(plan.layout.checkout),
    ]


def upstream_command(plan: BuildPlan) -> list[str]:
    features = plan.features
    command = [
        sys.executable,
        str(plan.layout.checkout / "build_scripts" / "build_usd.py"),
        "--build",
        str(plan.layout.build),
        "--src",
        str(plan.layout.dependency_sources),
        "--build-variant",
        plan.variant,
        "--jobs",
        str(plan.jobs),
        "--no-tests",
        "--no-examples",
        "--no-tutorials",
        "--no-docs",
        "--no-python-docs",
        "--python" if features.python else "--no-python",
        "--tools" if features.tools else "--no-tools",
        "--materialx" if features.materialx else "--no-materialx",
        "--embree" if features.embree else "--no-embree",
        "--usd-imaging" if features.imaging else "--no-imaging",
        "--usdview" if features.usdview else "--no-usdview",
        "--prman" if features.renderman is not None else "--no-prman",
    ]
    if features.renderman is not None:
        command.extend(("--prman-location", str(features.renderman)))
    if plan.generator:
        command.extend(("--generator", plan.generator))
    if plan.toolset:
        command.extend(("--toolset", plan.toolset))
    for dependency in plan.force:
        command.extend(("--force", dependency))
    if plan.force_all:
        command.append("--force-all")
    command.extend("-v" for _ in range(plan.verbose))
    command.append(str(plan.layout.install))
    return command


def _display(command: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(list(command))
    return shlex.join(command)


def _run(
    command: Sequence[str],
    *,
    capture: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if not capture:
        print(f"+ {_display(command)}", flush=True)
    return subprocess.run(
        list(command),
        check=True,
        text=True,
        capture_output=capture,
        env=env,
    )


def _which(executable: str, env: dict[str, str]) -> str | None:
    return shutil.which(executable, path=env.get("PATH"))


def _visual_studio_environment(base: dict[str, str]) -> dict[str, str]:
    if _which("cl", base):
        return base

    system_drive = base.get("SystemDrive", "C:")
    roots = [
        base.get("ProgramFiles(x86)"),
        base.get("ProgramFiles"),
        str(Path(system_drive + os.sep) / "Program Files (x86)"),
        str(Path(system_drive + os.sep) / "Program Files"),
    ]
    candidates = [
        Path(root) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        for root in roots
        if root
    ]
    vswhere = next((path for path in candidates if path.is_file()), None)
    if vswhere is None:
        raise RuntimeError(
            "MSVC cl.exe is not on PATH and vswhere.exe was not found; install the "
            "Visual Studio Desktop development with C++ workload"
        )
    query = subprocess.run(
        (
            str(vswhere),
            "-latest",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property",
            "installationPath",
        ),
        check=True,
        text=True,
        capture_output=True,
        env=base,
    )
    installation = query.stdout.strip()
    if not installation:
        raise RuntimeError("Visual Studio was found, but its C++ x64 toolchain is not installed")
    developer_shell = Path(installation) / "Common7" / "Tools" / "VsDevCmd.bat"
    if not developer_shell.is_file():
        raise RuntimeError(f"Visual Studio developer shell was not found: {developer_shell}")

    comspec = base.get("COMSPEC", "cmd.exe")
    captured = subprocess.run(
        (
            comspec,
            "/d",
            "/c",
            "call",
            str(developer_shell),
            "-no_logo",
            "-arch=x64",
            "-host_arch=x64",
            ">nul",
            "&&",
            "set",
        ),
        text=True,
        capture_output=True,
        env=base,
    )
    if captured.returncode != 0:
        detail = (captured.stderr or captured.stdout).strip()
        raise RuntimeError(f"Visual Studio developer shell failed: {detail}")
    configured = dict(base)
    for line in captured.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key:
            configured[key] = value
    if not _which("cl", configured):
        raise RuntimeError(
            f"Visual Studio developer shell did not configure cl.exe: {developer_shell}"
        )
    print(f"Using Visual Studio toolchain: {installation}")
    return configured


def build_environment() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("OPENUSDCONNECT_USD_ROOT", None)
    env.pop("PXR_PLUGINPATH_NAME", None)
    env.pop("PYTHONPATH", None)
    interpreter_dir = str(Path(sys.executable).resolve().parent)
    env["PATH"] = os.pathsep.join((interpreter_dir, env.get("PATH", ""))).rstrip(os.pathsep)
    if os.name == "nt":
        env = _visual_studio_environment(env)
    return env


def _normalize_repository(url: str) -> str:
    normalized = url.strip().rstrip("/")
    if normalized.startswith("git@github.com:"):
        normalized = "https://github.com/" + normalized.removeprefix("git@github.com:")
    elif normalized.startswith("ssh://git@github.com/"):
        normalized = "https://github.com/" + normalized.removeprefix("ssh://git@github.com/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized.casefold()


def verify_checkout(plan: BuildPlan, env: dict[str, str] | None = None) -> None:
    checkout = plan.layout.checkout
    try:
        inside = _run(
            ("git", "-C", str(checkout), "rev-parse", "--is-inside-work-tree"),
            capture=True,
            env=env,
        ).stdout.strip()
        head = _run(
            ("git", "-C", str(checkout), "rev-parse", "HEAD"), capture=True, env=env
        ).stdout.strip()
        remote = _run(
            ("git", "-C", str(checkout), "remote", "get-url", "origin"),
            capture=True,
            env=env,
        ).stdout.strip()
        dirty = _run(
            ("git", "-C", str(checkout), "status", "--porcelain"), capture=True, env=env
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"existing checkout is not a usable Git repository: {checkout}") from exc

    if inside != "true":
        raise RuntimeError(f"existing checkout is not a Git work tree: {checkout}")
    if _normalize_repository(remote) != _normalize_repository(plan.pin.repository):
        raise RuntimeError(f"existing checkout has an unexpected origin: {remote}")
    if head != plan.pin.commit:
        raise RuntimeError(
            f"existing checkout is at {head}, expected pinned commit {plan.pin.commit}; "
            "choose another --root or update it manually"
        )
    if dirty:
        raise RuntimeError(
            f"existing OpenUSD checkout has local changes: {checkout}; "
            "the bootstrapper will not execute a modified upstream build script"
        )
    if not (checkout / "build_scripts" / "build_usd.py").is_file():
        raise RuntimeError(f"OpenUSD build script is missing from checkout: {checkout}")


def ensure_checkout(plan: BuildPlan, env: dict[str, str] | None = None) -> None:
    checkout = plan.layout.checkout
    if checkout.exists():
        if not checkout.is_dir():
            raise RuntimeError(f"checkout path exists and is not a directory: {checkout}")
        if any(checkout.iterdir()):
            verify_checkout(plan, env)
            print(f"Using pinned OpenUSD checkout: {checkout}")
            return
    checkout.parent.mkdir(parents=True, exist_ok=True)
    _run(clone_command(plan), env=env)
    verify_checkout(plan, env)


def _cmake_version(env: dict[str, str]) -> tuple[int, ...]:
    output = _run(("cmake", "--version"), capture=True, env=env).stdout
    match = re.search(r"cmake version (\d+)\.(\d+)(?:\.(\d+))?", output)
    if not match:
        raise RuntimeError("could not determine the installed CMake version")
    return tuple(int(part) for part in match.groups(default="0"))


def preflight(plan: BuildPlan, env: dict[str, str]) -> None:
    if sys.platform not in SUPPORTED_PLATFORMS:
        raise RuntimeError(
            f"unsupported host platform {sys.platform!r}; this wrapper follows OpenUSD's "
            "Windows, Linux, and macOS desktop build path"
        )
    if sys.version_info[:2] < REQUIRED_PYTHON:
        required = ".".join(str(part) for part in REQUIRED_PYTHON)
        raise RuntimeError(f"OpenUSDConnect requires Python {required} or newer")
    if struct.calcsize("P") != 8:
        raise RuntimeError("OpenUSDConnect and OpenUSD require a 64-bit Python and toolchain")
    for executable in ("git", "cmake"):
        if _which(executable, env) is None:
            raise RuntimeError(f"required executable is not on PATH: {executable}")
    cmake_version = _cmake_version(env)
    if cmake_version < MINIMUM_CMAKE:
        found = ".".join(str(part) for part in cmake_version)
        raise RuntimeError(f"CMake 3.27 or newer is required; found {found}")

    if os.name == "nt":
        if _which("cl", env) is None:
            raise RuntimeError("Visual Studio developer environment did not provide cl.exe")
    elif not any(_which(name, env) for name in ("c++", "g++", "clang++")):
        raise RuntimeError("no supported C++ compiler was found on PATH")

    if plan.features.renderman is not None and not plan.features.renderman.is_dir():
        raise RuntimeError(f"RenderMan installation does not exist: {plan.features.renderman}")
    if plan.features.usdview:
        if not any(_which(name, env) for name in ("pyside6-uic", "pyside2-uic")):
            raise RuntimeError(
                "usdview requires PySide6 or PySide2, including pyside6-uic/pyside2-uic on PATH"
            )
        if importlib.util.find_spec("OpenGL") is None:
            raise RuntimeError("usdview requires PyOpenGL in the active Python environment")


def _python_package(install: Path) -> Path | None:
    candidates = [install / "Lib" / "site-packages", install / "lib" / "python"]
    for lib in (install / "lib", install / "lib64"):
        candidates.extend(sorted(lib.glob("python*/site-packages")))
        candidates.extend(sorted(lib.glob("python*/dist-packages")))
    return next((path for path in candidates if (path / "pxr" / "__init__.py").is_file()), None)


def verify_install(plan: BuildPlan, base_env: dict[str, str] | None = None) -> None:
    install = plan.layout.install
    header = install / "include" / "pxr" / "pxr.h"
    if not header.is_file():
        raise RuntimeError(f"OpenUSD installation is missing {header}")
    if not plan.features.python:
        return
    python_path = _python_package(install)
    if python_path is None:
        raise RuntimeError(f"OpenUSD Python bindings were not found under {install}")

    env = dict(os.environ if base_env is None else base_env)
    env["PYTHONPATH"] = os.pathsep.join((str(python_path), env.get("PYTHONPATH", ""))).rstrip(
        os.pathsep
    )
    native_dirs = [install / "bin", install / "lib", install / "lib64"]
    loader = (
        "PATH"
        if os.name == "nt"
        else "DYLD_LIBRARY_PATH"
        if sys.platform == "darwin"
        else "LD_LIBRARY_PATH"
    )
    env[loader] = os.pathsep.join(
        [str(path) for path in native_dirs if path.is_dir()] + [env.get(loader, "")]
    ).rstrip(os.pathsep)
    probe = "from pxr import Usd; print('.'.join(map(str, Usd.GetVersion())))"
    result = subprocess.run(
        (sys.executable, "-c", probe),
        env=env,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"built pxr bindings could not be imported: {detail}")
    if result.stdout.strip() != plan.pin.version:
        raise RuntimeError(
            f"built pxr reports {result.stdout.strip()}, expected OpenUSD {plan.pin.version}"
        )


def write_manifest(plan: BuildPlan) -> Path:
    path = plan.layout.install / "openusdconnect-build.json"
    data = {
        "pin": asdict(plan.pin),
        "features": {
            **asdict(plan.features),
            "renderman": str(plan.features.renderman) if plan.features.renderman else None,
        },
        "paths": {name: str(value) for name, value in asdict(plan.layout).items()},
        "build": {
            "jobs": plan.jobs,
            "variant": plan.variant,
            "generator": plan.generator,
            "toolset": plan.toolset,
        },
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "command": upstream_command(plan),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def write_runtime_config(plan: BuildPlan, path: Path | None = None) -> Path | None:
    """Register a managed build for automatic project-local runtime selection."""
    if not plan.features.python:
        return None
    path = ACTIVE_RUNTIME_FILE if path is None else path
    python_path = _python_package(plan.layout.install)
    if python_path is None:
        raise RuntimeError(
            f"OpenUSD Python bindings were not found under {plan.layout.install}"
        )
    data = {
        "schema": 1,
        "usd_root": str(plan.layout.install),
        "python_path": str(python_path),
        "python_executable": str(Path(sys.executable).resolve()),
        "renderman_root": (
            str(plan.features.renderman) if plan.features.renderman else None
        ),
        "version": plan.pin.version,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _fail(message: str) -> Never:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        pin = load_pin()
    except (OSError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
        _fail(str(exc))
    parser = _parser(pin)
    args = parser.parse_args(argv)
    try:
        plan = create_plan(args, pin)
    except ValueError as exc:
        parser.error(str(exc))

    print(f"OpenUSD {pin.version} ({pin.tag}, {pin.commit})")
    print(f"Checkout:          {plan.layout.checkout}")
    print(f"Dependency source: {plan.layout.dependency_sources}")
    print(f"Build:             {plan.layout.build}")
    print(f"Install:           {plan.layout.install}")
    if args.dry_run:
        print(f"+ {_display(clone_command(plan))}")
        print(f"+ {_display(upstream_command(plan))}")
        return 0

    try:
        env = build_environment()
        preflight(plan, env)
        ensure_checkout(plan, env)
        _run(upstream_command(plan), env=env)
        verify_install(plan, env)
        manifest = write_manifest(plan)
        runtime_config = write_runtime_config(plan)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Verified OpenUSD {pin.version} installation: {plan.layout.install}")
    print(f"Build manifest: {manifest}")
    if runtime_config is not None:
        print(f"Managed runtime: {runtime_config}")
        print("OpenUSDConnect will select this build automatically.")
    else:
        print("This build has no Python bindings and was not registered as the project runtime.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
