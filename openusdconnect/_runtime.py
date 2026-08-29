"""Select the bundled or project-provided OpenUSD bindings."""

from __future__ import annotations

import json
import os
import platform
import sys
import warnings
from importlib import metadata
from pathlib import Path

BUNDLED_USD_ENV = "OPENUSDCONNECT_BUNDLED_USD"
USD_ROOT_ENV = "OPENUSDCONNECT_USD_ROOT"


def _platform_key() -> str:
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    return f"{system}-{machine}"


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
ACTIVE_RUNTIME_FILE = _PROJECT_ROOT / ".openusd" / f"active-{_platform_key()}.json"
LEGACY_ACTIVE_RUNTIME_FILE = _PROJECT_ROOT / ".openusd" / "active.json"
_DLL_DIRECTORY_HANDLES: list[object] = []


def _normalized_path(path: str | os.PathLike) -> str:
    return os.path.normcase(os.path.abspath(path))


def _bundled_pxr_path() -> str | None:
    try:
        distribution = metadata.distribution("usd-core")
    except metadata.PackageNotFoundError:
        return None
    return _normalized_path(distribution.locate_file("pxr"))


def _loaded_pxr_path() -> str | None:
    module = sys.modules.get("pxr")
    if module is None:
        return None
    module_file = getattr(module, "__file__", "")
    return _normalized_path(os.path.dirname(module_file)) if module_file else ""


def _prepend_environment(key: str, paths: list[Path]) -> None:
    values = [str(path) for path in paths if path.is_dir()]
    values.extend(filter(None, os.environ.get(key, "").split(os.pathsep)))
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalized_path(value)
        if normalized not in seen:
            seen.add(normalized)
            unique.append(value)
    if unique:
        os.environ[key] = os.pathsep.join(unique)


def _managed_runtime_configs() -> list[dict[str, object]]:
    configs = []
    for path in (ACTIVE_RUNTIME_FILE, LEGACY_ACTIVE_RUNTIME_FILE):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            warnings.warn(
                f"Ignoring unreadable managed OpenUSD runtime config {path}: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        if data.get("schema") != 1:
            warnings.warn(
                f"Ignoring unsupported managed OpenUSD runtime config {path}",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        configs.append(data)
    return configs


def _activate_managed_runtime(config: dict[str, object]) -> bool:
    try:
        root = Path(str(config["usd_root"])).resolve()
        python_path = Path(str(config["python_path"])).resolve()
    except (KeyError, TypeError, ValueError):
        return False
    if not root.is_dir() or not (python_path / "pxr" / "__init__.py").is_file():
        return False

    sys.path.insert(0, str(python_path))
    _prepend_environment("PYTHONPATH", [python_path])
    native_dirs = [root / "bin", root / "lib", root / "lib64"]
    _prepend_environment("PATH", native_dirs)
    loader_key = (
        "PATH"
        if os.name == "nt"
        else "DYLD_LIBRARY_PATH"
        if sys.platform == "darwin"
        else "LD_LIBRARY_PATH"
    )
    _prepend_environment(loader_key, native_dirs[1:])
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        for directory in native_dirs:
            if directory.is_dir():
                _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(directory))

    os.environ[USD_ROOT_ENV] = str(root)
    os.environ.pop(BUNDLED_USD_ENV, None)
    python_executable = config.get("python_executable")
    if isinstance(python_executable, str) and python_executable:
        os.environ["OPENUSDCONNECT_PYTHON_EXECUTABLE"] = python_executable

    renderman_value = config.get("renderman_root")
    if isinstance(renderman_value, str) and renderman_value:
        renderman = Path(renderman_value).resolve()
        if renderman.is_dir():
            rman_bin = renderman / "bin"
            rman_lib = renderman / "lib"
            _prepend_environment("PATH", [rman_bin, rman_lib])
            _prepend_environment(loader_key, [rman_bin, rman_lib])
            if os.name == "nt" and hasattr(os, "add_dll_directory"):
                for directory in (rman_bin, rman_lib):
                    if directory.is_dir():
                        _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(directory))
            plugin_usd = root / "plugin" / "usd"
            plugins = rman_lib / "plugins"
            os.environ.update(
                {
                    "RMANTREE": str(renderman),
                    "RMAN_SHADERPATH": os.pathsep.join(
                        [
                            str(rman_lib / "shaders"),
                            str(plugin_usd / "resources" / "shaders"),
                        ]
                    ),
                    "RMAN_RIXPLUGINPATH": str(plugins),
                    "RMAN_TEXTUREPATH": os.pathsep.join(
                        [str(rman_lib / "textures"), str(plugins), str(plugin_usd)]
                    ),
                    "RMAN_DISPLAYPATH": str(plugins),
                    "RMAN_PROCEDURALPATH": str(plugins),
                }
            )
    return True


def _prefer_bundled_usd(bundled_pxr: str) -> None:
    configured = os.environ.pop("PYTHONPATH", "")
    if not configured:
        return
    conflicting = set()
    retained = []
    for path in configured.split(os.pathsep):
        resolved = _normalized_path(path or os.curdir)
        candidate_pxr = os.path.join(resolved, "pxr")
        if os.path.isdir(candidate_pxr) and candidate_pxr != bundled_pxr:
            conflicting.add(resolved)
        else:
            retained.append(path)
    if retained:
        os.environ["PYTHONPATH"] = os.pathsep.join(retained)
    sys.path[:] = [
        path
        for path in sys.path
        if _normalized_path(path or os.curdir) not in conflicting
    ]


def select_runtime() -> None:
    if os.environ.get(USD_ROOT_ENV):
        os.environ.pop(BUNDLED_USD_ENV, None)
        return
    loaded_pxr = _loaded_pxr_path()
    if loaded_pxr is None:
        for managed in _managed_runtime_configs():
            if _activate_managed_runtime(managed):
                return
    bundled_pxr = _bundled_pxr_path()
    if bundled_pxr is None:
        os.environ.pop(BUNDLED_USD_ENV, None)
        return
    if loaded_pxr is None:
        _prefer_bundled_usd(bundled_pxr)
    if loaded_pxr in (None, bundled_pxr):
        os.environ[BUNDLED_USD_ENV] = "1"
    else:
        os.environ.pop(BUNDLED_USD_ENV, None)
