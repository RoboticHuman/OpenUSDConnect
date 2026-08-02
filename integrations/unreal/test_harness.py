"""Provision and run opt-in OpenUSDConnect tests in Unreal Editor."""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from integrations.unreal.test_scenario import UnrealScenario, create_scenario

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SOURCE = Path(__file__).resolve().parent / "OpenUSDConnect"
EDITOR_DRIVER = REPO_ROOT / "tests" / "integration" / "scripts" / "unreal_e2e_driver.py"
ENGINE_CONFIG = REPO_ROOT / "unreal.test.cfg"
TEST_PROJECT_PLUGINS = (
    "USDImporter",
    "PythonScriptPlugin",
    "EditorScriptingUtilities",
    "OpenUSDConnect",
)


class UnrealTestError(RuntimeError):
    """Raised when Unreal test provisioning or execution fails."""


@dataclass(frozen=True, slots=True)
class UnrealEngine:
    root: Path
    editor: Path
    editor_cmd: Path
    run_uat: Path
    target_platform: str
    version: str
    association: str
    source: str
    installed_build: bool


@dataclass(frozen=True, slots=True)
class UnrealRunResult:
    engine: UnrealEngine
    project: Path
    work_dir: Path
    plugin_package: Path | None
    result: dict
    unreal_log: Path
    server_log: Path


def _platform_layout(platform_name: str | None = None) -> tuple[str, Path, Path, Path]:
    platform_name = platform_name or sys.platform
    if platform_name == "darwin":
        return (
            "Mac",
            Path("Engine/Binaries/Mac/UnrealEditor"),
            Path("Engine/Binaries/Mac/UnrealEditor-Cmd"),
            Path("Engine/Build/BatchFiles/RunUAT.sh"),
        )
    if platform_name == "win32":
        return (
            "Win64",
            Path("Engine/Binaries/Win64/UnrealEditor.exe"),
            Path("Engine/Binaries/Win64/UnrealEditor-Cmd.exe"),
            Path("Engine/Build/BatchFiles/RunUAT.bat"),
        )
    if platform_name.startswith("linux"):
        return (
            "Linux",
            Path("Engine/Binaries/Linux/UnrealEditor"),
            Path("Engine/Binaries/Linux/UnrealEditor-Cmd"),
            Path("Engine/Build/BatchFiles/RunUAT.sh"),
        )
    raise UnrealTestError(f"unsupported Unreal host platform {platform_name!r}")


def _engine_root_from_path(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path).expanduser().resolve()
    if candidate.is_file():
        for parent in candidate.parents:
            if parent.name == "Engine":
                return parent.parent
    if candidate.name == "Engine":
        return candidate.parent
    return candidate


def _read_build_version(root: Path) -> str:
    build_version = root / "Engine" / "Build" / "Build.version"
    try:
        data = json.loads(build_version.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UnrealTestError(f"invalid Unreal build metadata at {build_version}: {exc}") from exc
    parts = [data.get("MajorVersion"), data.get("MinorVersion"), data.get("PatchVersion")]
    if any(part is None for part in parts):
        raise UnrealTestError(f"Unreal build metadata is missing a version: {build_version}")
    return ".".join(str(part) for part in parts)


def inspect_engine(
    path: str | os.PathLike[str],
    *,
    source: str = "explicit",
    association: str = "",
    platform_name: str | None = None,
) -> UnrealEngine:
    """Validate an installed or source-built Unreal Engine root."""
    root = _engine_root_from_path(path)
    target, editor_rel, editor_cmd_rel, uat_rel = _platform_layout(platform_name)
    editor = root / editor_rel
    editor_cmd = root / editor_cmd_rel
    run_uat = root / uat_rel
    missing = [item for item in (editor, editor_cmd, run_uat) if not item.is_file()]
    if missing:
        joined = ", ".join(str(item) for item in missing)
        raise UnrealTestError(f"{root} is not a usable Unreal Engine root; missing {joined}")
    version = _read_build_version(root)
    if not association:
        major, minor, *_ = version.split(".")
        association = f"{major}.{minor}"
    return UnrealEngine(
        root=root,
        editor=editor,
        editor_cmd=editor_cmd,
        run_uat=run_uat,
        target_platform=target,
        version=version,
        association=association,
        source=source,
        installed_build=(root / "Engine" / "Build" / "InstalledBuild.txt").is_file(),
    )


def _launcher_manifest_dirs(platform_name: str, env: dict[str, str]) -> tuple[Path, ...]:
    home = Path(env.get("HOME", str(Path.home())))
    if platform_name == "darwin":
        return (
            home
            / "Library"
            / "Application Support"
            / "Epic"
            / "EpicGamesLauncher"
            / "Data"
            / "Manifests",
        )
    if platform_name == "win32":
        program_data = Path(env.get("PROGRAMDATA", r"C:\ProgramData"))
        return (program_data / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests",)
    xdg_data = Path(env.get("XDG_DATA_HOME", home / ".local" / "share"))
    return (xdg_data / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests",)


def _manifest_installations(
    manifest_dirs: tuple[Path, ...],
    *,
    platform_name: str,
) -> list[UnrealEngine]:
    engines = []
    for directory in manifest_dirs:
        if not directory.is_dir():
            continue
        for manifest in sorted(directory.glob("*.item")):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            app_name = str(data.get("AppName", ""))
            if not app_name.startswith("UE_") or data.get("DisplayName") != "Unreal Engine":
                continue
            install_location = data.get("InstallLocation")
            if not install_location:
                continue
            try:
                engine = inspect_engine(
                    install_location,
                    source=f"Epic Launcher manifest {manifest.name}",
                    association=app_name.removeprefix("UE_"),
                    platform_name=platform_name,
                )
            except UnrealTestError:
                continue
            engines.append(engine)
    return engines


def _ini_installations(path: Path, *, platform_name: str) -> list[UnrealEngine]:
    if not path.is_file():
        return []
    parser = configparser.ConfigParser()
    parser.optionxform = str
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return []
    if not parser.has_section("Installations"):
        return []
    engines = []
    for association, install_location in parser.items("Installations"):
        try:
            engines.append(
                inspect_engine(
                    install_location,
                    source=f"Unreal registration {path}",
                    association=association,
                    platform_name=platform_name,
                )
            )
        except UnrealTestError:
            continue
    return engines


def _windows_registered_installations() -> list[tuple[str, str]]:
    if sys.platform != "win32":
        return []
    import winreg

    key_path = r"SOFTWARE\Epic Games\Unreal Engine\Builds"
    values = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                values.append((name, value))
                index += 1
    except OSError:
        pass
    return values


def _default_engine_roots(platform_name: str, env: dict[str, str]) -> list[Path]:
    home = Path(env.get("HOME", str(Path.home())))
    if platform_name == "darwin":
        return sorted(Path("/Users/Shared/Epic Games").glob("UE_*"))
    if platform_name == "win32":
        program_files = Path(env.get("PROGRAMFILES", r"C:\Program Files"))
        return sorted((program_files / "Epic Games").glob("UE_*"))
    roots = [home / "UnrealEngine", Path("/opt/UnrealEngine")]
    roots.extend(sorted(home.glob("UnrealEngine-*")))
    return roots


def _project_association(project: Path | None) -> str:
    if project is None or not project.is_file():
        return ""
    try:
        return str(json.loads(project.read_text(encoding="utf-8")).get("EngineAssociation", ""))
    except (OSError, ValueError):
        return ""


def _version_key(engine: UnrealEngine) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in engine.version.split("."))
    except ValueError:
        return (0,)


def discover_engines(
    *,
    explicit: str | os.PathLike[str] | None = None,
    project: str | os.PathLike[str] | None = None,
    config_path: Path = ENGINE_CONFIG,
    env: dict[str, str] | None = None,
    platform_name: str | None = None,
    manifest_dirs: tuple[Path, ...] | None = None,
) -> list[UnrealEngine]:
    """Return validated engines in selection order."""
    env = dict(os.environ if env is None else env)
    platform_name = platform_name or sys.platform
    if explicit:
        return [inspect_engine(explicit, platform_name=platform_name)]

    for variable in ("OUC_UNREAL_ENGINE_ROOT", "UNREAL_ENGINE_ROOT"):
        value = env.get(variable)
        if value:
            return [inspect_engine(value, source=variable, platform_name=platform_name)]

    if config_path.is_file():
        configured = config_path.read_text(encoding="utf-8").strip()
        if configured and not configured.startswith("#"):
            return [
                inspect_engine(
                    configured,
                    source=config_path.name,
                    platform_name=platform_name,
                )
            ]

    candidates: list[UnrealEngine] = []
    directories = (
        _launcher_manifest_dirs(platform_name, env) if manifest_dirs is None else manifest_dirs
    )
    candidates.extend(_manifest_installations(directories, platform_name=platform_name))

    home = Path(env.get("HOME", str(Path.home())))
    install_ini = home / "Library" / "Application Support" / "Epic" / "UnrealEngine" / "Install.ini"
    if platform_name == "darwin":
        candidates.extend(_ini_installations(install_ini, platform_name=platform_name))
    if platform_name == "win32":
        for association, path in _windows_registered_installations():
            try:
                candidates.append(
                    inspect_engine(
                        path,
                        source="Windows Unreal registration",
                        association=association,
                        platform_name=platform_name,
                    )
                )
            except UnrealTestError:
                continue

    for path in _default_engine_roots(platform_name, env):
        try:
            candidates.append(
                inspect_engine(
                    path,
                    source="standard install location",
                    platform_name=platform_name,
                )
            )
        except UnrealTestError:
            continue

    by_root = {}
    for engine in candidates:
        by_root.setdefault(engine.root, engine)
    association = _project_association(Path(project).expanduser() if project else None)
    return sorted(
        by_root.values(),
        key=lambda engine: (engine.association == association, _version_key(engine)),
        reverse=True,
    )


def resolve_engine(**kwargs) -> UnrealEngine:
    engines = discover_engines(**kwargs)
    if not engines:
        raise UnrealTestError(
            "Unreal Engine was not found; pass --engine-root, set "
            "OUC_UNREAL_ENGINE_ROOT, or write the root path to unreal.test.cfg"
        )
    return engines[0]


def _cache_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "OpenUSDConnect" / "Unreal"
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "OpenUSDConnect" / "Unreal"
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "openusdconnect" / "unreal"


def _plugin_fingerprint(plugin_source: Path) -> str:
    digest = hashlib.sha256()
    paths = [plugin_source / "OpenUSDConnect.uplugin"]
    for directory in ("Config", "Content", "Resources", "Shaders", "Source"):
        root = plugin_source / directory
        if root.is_dir():
            paths.extend(path for path in root.rglob("*") if path.is_file())
    for path in sorted(paths):
        if any(part in {"Binaries", "Intermediate"} for part in path.parts):
            continue
        digest.update(path.relative_to(plugin_source).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _engine_fingerprint(engine: UnrealEngine) -> str:
    digest = hashlib.sha256(str(engine.root).encode("utf-8"))
    for path in (
        engine.root / "Engine" / "Build" / "Build.version",
        engine.editor.parent / "UnrealEditor.modules",
    ):
        if path.is_file():
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def package_plugin(
    engine: UnrealEngine,
    *,
    plugin_source: Path = PLUGIN_SOURCE,
    cache_root: Path | None = None,
    force: bool = False,
) -> Path:
    """Build and cache a project-installable plugin package."""
    fingerprint = _plugin_fingerprint(plugin_source)
    engine_fingerprint = _engine_fingerprint(engine)
    cache_root = cache_root or _cache_root()
    package = cache_root / (
        f"{engine.target_platform}-UE-{engine.version}-{engine_fingerprint[:12]}-{fingerprint[:16]}"
    )
    marker = package / ".openusdconnect-package.json"
    if not force and (package / "OpenUSDConnect.uplugin").is_file() and marker.is_file():
        return package

    cache_root.mkdir(parents=True, exist_ok=True)
    build_dir = cache_root / f".{package.name}.building-{os.getpid()}"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir()
    log_path = cache_root / f".{package.name}.BuildPlugin.log"
    command = [
        str(engine.run_uat),
        "BuildPlugin",
        f"-Plugin={plugin_source / 'OpenUSDConnect.uplugin'}",
        f"-Package={build_dir}",
        f"-TargetPlatforms={engine.target_platform}",
        "-Rocket",
    ]
    if sys.platform == "win32":
        command = ["cmd.exe", "/d", "/s", "/c", *command]
    with log_path.open("w", encoding="utf-8") as log:
        completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, text=True)
    if completed.returncode:
        tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-80:])
        raise UnrealTestError(f"Unreal BuildPlugin failed ({log_path}):\n{tail}")
    if not (build_dir / "OpenUSDConnect.uplugin").is_file():
        raise UnrealTestError(f"BuildPlugin did not produce OpenUSDConnect.uplugin in {build_dir}")
    shutil.copy2(log_path, build_dir / "BuildPlugin.log")
    log_path.unlink()
    (build_dir / marker.name).write_text(
        json.dumps(
            {
                "engine_root": str(engine.root),
                "engine_version": engine.version,
                "engine_fingerprint": engine_fingerprint,
                "fingerprint": fingerprint,
                "target_platform": engine.target_platform,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if package.exists():
        shutil.rmtree(package)
    build_dir.rename(package)
    return package


def install_plugin_in_project(
    project: Path,
    plugin_package: Path,
    *,
    replace: bool = False,
) -> Path:
    """Install a packaged plugin into an existing project's Plugins directory."""
    project = project.expanduser().resolve()
    if not project.is_file() or project.suffix != ".uproject":
        raise UnrealTestError(f"not an Unreal project descriptor: {project}")
    plugin_package = plugin_package.expanduser().resolve()
    if not (plugin_package / "OpenUSDConnect.uplugin").is_file():
        raise UnrealTestError(f"invalid Unreal plugin package: {plugin_package}")

    destination = project.parent / "Plugins" / "OpenUSDConnect"
    if destination.exists() and not replace:
        raise UnrealTestError(f"{destination} already exists; pass --replace-plugin to replace it")
    try:
        descriptor = json.loads(project.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UnrealTestError(f"invalid Unreal project descriptor {project}: {exc}") from exc
    if not isinstance(descriptor, dict):
        raise UnrealTestError(f"invalid Unreal project descriptor {project}: expected an object")
    plugins = descriptor.setdefault("Plugins", [])
    if not isinstance(plugins, list) or not all(isinstance(item, dict) for item in plugins):
        raise UnrealTestError(f"invalid Plugins list in {project}")
    by_name = {item.get("Name"): item for item in plugins}
    for name in TEST_PROJECT_PLUGINS:
        entry = by_name.get(name)
        if entry is None:
            plugins.append({"Name": name, "Enabled": True})
        else:
            entry["Enabled"] = True

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.name}.installing-{os.getpid()}")
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(plugin_package, staging)
    if destination.exists():
        shutil.rmtree(destination)
    staging.rename(destination)
    project.write_text(json.dumps(descriptor, indent=2) + "\n", encoding="utf-8")
    return destination


def create_test_project(
    directory: Path,
    engine: UnrealEngine,
    plugin_package: Path,
    *,
    port: int,
) -> Path:
    """Create a content-only project with the packaged plugin installed."""
    directory.mkdir(parents=True, exist_ok=True)
    project_name = "OpenUSDConnectUnrealTest"
    project_path = directory / f"{project_name}.uproject"
    plugins_dir = directory / "Plugins" / "OpenUSDConnect"
    if plugins_dir.exists():
        shutil.rmtree(plugins_dir)
    shutil.copytree(plugin_package, plugins_dir)
    (directory / "Config").mkdir(exist_ok=True)
    (directory / "Content").mkdir(exist_ok=True)
    project_path.write_text(
        json.dumps(
            {
                "FileVersion": 3,
                "EngineAssociation": engine.association,
                "Category": "",
                "Description": "OpenUSDConnect Unreal integration test",
                "Plugins": [{"Name": name, "Enabled": True} for name in TEST_PROJECT_PLUGINS],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (directory / "Config" / "DefaultGame.ini").write_text(
        "\n".join(
            (
                "[/Script/OpenUSDConnect.USDConnectSettings]",
                "ServerHost=127.0.0.1",
                f"ServerPort={port}",
                "Department=",
                "bAutoConnect=False",
                "bUseLiveMetadataFromStage=False",
                "bAutoStartReceiverFromLiveMetadata=False",
                "bAutoStartEmitterFromLiveMetadata=False",
                "bPersistAuthTokens=False",
                "ReconnectDelaySecs=1.0",
                "",
            )
        ),
        encoding="utf-8",
    )
    return project_path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, process: subprocess.Popen, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise UnrealTestError(f"OpenUSDConnect server exited with code {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise UnrealTestError(f"OpenUSDConnect server did not listen on port {port}")


def _stop_process(process: subprocess.Popen, timeout: float = 10.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def _log_tail(path: Path, lines: int = 120) -> str:
    if not path.is_file():
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def _verify_reverse_server_state(scenario: UnrealScenario) -> dict:
    from pxr import Gf

    from openusdconnect.server import UsdSyncServer

    server = UsdSyncServer(
        base_usd_path=str(scenario.base_stage),
        log_path=str(scenario.database_path),
    )
    try:
        translate = (
            server.stage.GetPrimAtPath("/World/PreviewBall").GetAttribute("xformOp:translate").Get()
        )
        roughness = (
            server.stage.GetPrimAtPath("/World/Looks/Preview/Surface")
            .GetAttribute("inputs:roughness")
            .Get()
        )
        expected_translate = Gf.Vec3d(6.0, 2.0, 1.0)
        if translate != expected_translate:
            raise UnrealTestError(f"Unreal reverse transform did not reach the server: {translate}")
        if roughness is None or abs(float(roughness) - 0.11) > 1e-6:
            raise UnrealTestError(
                f"Unreal reverse shader input did not reach the server: {roughness}"
            )
        return {"roughness": float(roughness), "translate": list(translate)}
    finally:
        server.shutdown()
        server.store.close()


def run_unreal_e2e(
    engine: UnrealEngine,
    work_dir: Path,
    *,
    project: Path | None = None,
    plugin_package: Path | None = None,
    rebuild_plugin: bool = False,
    interactive: bool = False,
    timeout: float = 240.0,
) -> UnrealRunResult:
    """Run the server-to-Unreal parity scenario and return structured results."""
    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    scenario = create_scenario(work_dir, port=port, python_executable=Path(sys.executable))

    if project is None:
        plugin_package = plugin_package or package_plugin(engine, force=rebuild_plugin)
        project = create_test_project(work_dir / "UnrealProject", engine, plugin_package, port=port)
    else:
        project = project.expanduser().resolve()
        if not project.is_file():
            raise UnrealTestError(f"Unreal project does not exist: {project}")

    config = json.loads(scenario.config_path.read_text(encoding="utf-8"))
    config["interactive"] = interactive
    config["timeout"] = timeout
    scenario.config_path.write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    server_log = work_dir / "server.log"
    unreal_log = work_dir / "unreal.log"
    with server_log.open("w", encoding="utf-8") as server_output:
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "openusdconnect.server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--base",
                str(scenario.base_stage),
                "--log",
                str(scenario.database_path),
            ],
            cwd=REPO_ROOT,
            stdout=server_output,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_port(port, server)
            executable = engine.editor if interactive else engine.editor_cmd
            command = [
                str(executable),
                str(project),
                f"-ExecutePythonScript={EDITOR_DRIVER}",
                "-ScriptErrorsAreFatal",
                "-nop4",
                "-nosplash",
                "-nosound",
                f"-abslog={unreal_log}",
            ]
            if not interactive:
                command.extend(("-unattended", "-nullrhi"))
            env = os.environ.copy()
            env["OUC_UNREAL_TEST_CONFIG"] = str(scenario.config_path)
            started = time.monotonic()
            with unreal_log.open("a", encoding="utf-8") as unreal_output:
                try:
                    completed = subprocess.run(
                        command,
                        cwd=work_dir,
                        env=env,
                        stdout=unreal_output,
                        stderr=subprocess.STDOUT,
                        timeout=None if interactive else timeout + 90.0,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise UnrealTestError(
                        f"Unreal test exceeded {timeout + 90.0:.0f}s ({unreal_log})"
                    ) from exc
            elapsed = time.monotonic() - started
        finally:
            _stop_process(server)

    if not scenario.result_path.is_file():
        raise UnrealTestError(
            f"Unreal did not write {scenario.result_path}:\n{_log_tail(unreal_log)}"
        )
    result = json.loads(scenario.result_path.read_text(encoding="utf-8"))
    result["editor_elapsed_seconds"] = elapsed
    if not result.get("success"):
        raise UnrealTestError(
            f"Unreal scenario failed: {result.get('error', result)}\n{_log_tail(unreal_log)}"
        )
    if completed.returncode:
        raise UnrealTestError(
            f"Unreal scenario passed but the editor exited with code "
            f"{completed.returncode}:\n{_log_tail(unreal_log)}"
        )
    result["server_reverse_state"] = _verify_reverse_server_state(scenario)
    return UnrealRunResult(
        engine=engine,
        project=project,
        work_dir=work_dir,
        plugin_package=plugin_package,
        result=result,
        unreal_log=unreal_log,
        server_log=server_log,
    )


def temporary_run_directory():
    """Return a TemporaryDirectory suitable for the command-line runner."""
    return tempfile.TemporaryDirectory(prefix="openusdconnect-unreal-")


__all__ = [
    "ENGINE_CONFIG",
    "PLUGIN_SOURCE",
    "UnrealEngine",
    "UnrealRunResult",
    "UnrealTestError",
    "create_scenario",
    "create_test_project",
    "discover_engines",
    "inspect_engine",
    "install_plugin_in_project",
    "package_plugin",
    "resolve_engine",
    "run_unreal_e2e",
    "temporary_run_directory",
]
