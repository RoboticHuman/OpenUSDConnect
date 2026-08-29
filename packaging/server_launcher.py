"""Relocatable bootstrap copied into standalone packages and container images."""

from __future__ import annotations

import json
import os
import platform
import runpy
import sys
from pathlib import Path

_DLL_HANDLES: list[object] = []
_PLUGIN_DLL_DIRS_ENV = "OPENUSDCONNECT_DLL_DIRS"


def _configure_windows_dll_search() -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    configure = kernel32.SetDefaultDllDirectories
    configure.argtypes = [ctypes.c_uint32]
    configure.restype = ctypes.c_bool
    # USD's native plugin loader uses LoadLibraryW, not Python's LoadLibraryEx.
    # Apply the application/system/explicit-directory policy to both loaders.
    if not configure(0x1000):  # LOAD_LIBRARY_SEARCH_DEFAULT_DIRS
        raise ctypes.WinError(ctypes.get_last_error())


def _inside(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise RuntimeError(f"Runtime manifest path escapes the package: {value}")
    return path


def _requested_plugin_library_dirs() -> list[Path]:
    """Return host plugin dependency directories before the application starts.

    The server normally parses ``--plugin-dll-dir`` itself, but packaged
    processes establish the native loader environment before dispatching to
    that module. Read the configured environment variable and forwarded
    arguments here so external plugin dependencies participate in the initial
    Windows setup or POSIX re-exec. Keep the arguments intact for the server's
    normal validation and logging.
    """
    values = [
        value
        for value in os.environ.get(_PLUGIN_DLL_DIRS_ENV, "").split(os.pathsep)
        if value
    ]
    for index, argument in enumerate(sys.argv):
        if argument == "--plugin-dll-dir" and index + 1 < len(sys.argv):
            values.append(sys.argv[index + 1])
        elif argument.startswith("--plugin-dll-dir="):
            value = argument.partition("=")[2]
            if value:
                values.append(value)
    return list(
        dict.fromkeys(Path(value).expanduser().resolve() for value in values)
    )


def activate() -> dict:
    root = Path(__file__).resolve().parent
    config = json.loads((root / "usd-runtime.json").read_text(encoding="utf-8"))
    if config.get("schema") != 1:
        raise RuntimeError("Unsupported packaged OpenUSD runtime manifest")
    libraries = [_inside(root, p) for p in config.get("library_dirs", [])]
    libraries.extend(path for path in _requested_plugin_library_dirs() if path.is_dir())
    loader = (
        "PATH"
        if os.name == "nt"
        else ("DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH")
    )
    # ELF/Mach-O loaders read their search path before Python starts.
    if os.name != "nt" and os.environ.get("_OUC_PACKAGE_BOOTSTRAP") != str(root):
        env = os.environ.copy()
        env[loader] = os.pathsep.join(map(str, libraries))
        env["_OUC_PACKAGE_BOOTSTRAP"] = str(root)
        os.execve(sys.executable, [sys.executable, "-I", str(Path(__file__)), *sys.argv[1:]], env)
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "OPENUSDCONNECT_USD_ROOT",
        "OPENUSDCONNECT_BUNDLED_USD",
        "PXR_PLUGINPATH_NAME",
        "PXR_USD_WINDOWS_DLL_PATH",
        "PXR_MTLX_STDLIB_SEARCH_PATHS",
        "PXR_MTLX_PLUGIN_SEARCH_PATHS",
    ):
        os.environ.pop(key, None)
    if os.name == "nt":
        _configure_windows_dll_search()
        # Tf's WindowsImportWrapper otherwise adds every directory on PATH again.
        os.environ["PXR_USD_WINDOWS_DLL_PATH"] = os.pathsep.join(map(str, libraries))
        for directory in libraries:
            _DLL_HANDLES.append(os.add_dll_directory(str(directory)))
    python_path = config.get("python_path")
    if python_path:
        sys.path.insert(0, str(_inside(root, python_path)))
        os.environ["OPENUSDCONNECT_USD_ROOT"] = str(_inside(root, "openusd"))
    plugins = [_inside(root, p) for p in config.get("plugin_paths", [])]
    if plugins:
        os.environ["PXR_PLUGINPATH_NAME"] = os.pathsep.join(map(str, plugins))
    return config


def runtime_info(required_plugins: list[str] = ()) -> dict:
    import pxr
    from pxr import Plug, Sdf, Usd

    registry = Plug.Registry()
    for name in required_plugins:
        plugin = registry.GetPluginWithName(name)
        if not plugin or not plugin.Load():
            raise RuntimeError(f"Packaged USD plugin could not be loaded: {name}")
    extensions = sorted(Sdf.FileFormat.FindAllFileFormatExtensions())
    materialx = Sdf.FileFormat.FindByExtension("mtlx") is not None
    return {
        "version": ".".join(map(str, Usd.GetVersion())),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "architecture": platform.machine(),
        "file_formats": extensions,
        "materialx": materialx,
        "plugins": sorted(p.name for p in registry.GetAllPlugins()),
        "validated_plugins": sorted(required_plugins),
        "pxr_path": str(Path(pxr.__file__).resolve()),
    }


def main() -> None:
    config = activate()
    if sys.argv[1:] == ["--runtime-info"]:
        info = runtime_info(config.get("required_plugins", []))
        info["profile"] = config["profile"]
        print(json.dumps(info, sort_keys=True))
        return
    if len(sys.argv) < 2:
        raise SystemExit("Expected a Python module or --runtime-info")
    if sys.argv[1] == "--run-script":
        script = sys.argv[2]
        sys.argv = sys.argv[2:]
        runpy.run_path(script, run_name="__main__")
    else:
        module = sys.argv[1]
        sys.argv = sys.argv[1:]
        runpy.run_module(module, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
