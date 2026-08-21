"""Resolve an OpenUSD runtime environment for launchers and shell adapters."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import subprocess
import sys
import sysconfig
from collections.abc import Mapping, Sequence
from pathlib import Path

USD_ROOT_ENV = "OPENUSDCONNECT_USD_ROOT"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _platform_key() -> str:
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    return f"{system}-{machine}"


ACTIVE_RUNTIME_FILE = REPO_ROOT / ".openusd" / f"active-{_platform_key()}.json"
LEGACY_ACTIVE_RUNTIME_FILE = REPO_ROOT / ".openusd" / "active.json"


def managed_runtime_config(path: Path | None = None) -> dict[str, str | None] | None:
    """Read the runtime registered by build_openusd.py, if one exists."""
    candidates = (path,) if path is not None else (ACTIVE_RUNTIME_FILE, LEGACY_ACTIVE_RUNTIME_FILE)
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"could not read managed OpenUSD runtime config {candidate}: {exc}"
            ) from exc
        if data.get("schema") != 1:
            raise RuntimeError(
                f"unsupported managed OpenUSD runtime config schema in {candidate}"
            )
        required = ("usd_root", "python_path", "python_executable", "version")
        if any(not isinstance(data.get(key), str) or not data[key] for key in required):
            raise RuntimeError(f"invalid managed OpenUSD runtime config: {candidate}")
        renderman = data.get("renderman_root")
        if renderman is not None and not isinstance(renderman, str):
            raise RuntimeError(f"invalid RenderMan path in managed runtime config: {candidate}")
        root = Path(data["usd_root"]).expanduser().resolve()
        python_path = Path(data["python_path"]).expanduser().resolve()
        if not root.is_dir() or not (python_path / "pxr" / "__init__.py").is_file():
            if path is not None:
                return {key: data.get(key) for key in (*required, "renderman_root")}
            continue
        return {key: data.get(key) for key in (*required, "renderman_root")}
    return None


def _option_is_present(argv: Sequence[str] | None, option: str) -> bool:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--" in arguments:
        arguments = arguments[: arguments.index("--")]
    return option in arguments or any(value.startswith(option + "=") for value in arguments)


def runtime_defaults(
    argv: Sequence[str] | None = None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Resolve explicit environment selection before the managed project build."""
    explicit_root = os.environ.get(USD_ROOT_ENV)
    if explicit_root:
        return explicit_root, None, None, os.environ.get("RMANTREE")
    if _option_is_present(argv, "--usd-root"):
        return None, None, None, os.environ.get("RMANTREE")
    managed = managed_runtime_config()
    if managed is None:
        return None, None, None, os.environ.get("RMANTREE")
    return (
        managed["usd_root"],
        managed["python_path"],
        managed["python_executable"],
        managed["renderman_root"],
    )


def _active_venv_python_paths() -> list[Path]:
    if sys.prefix == sys.base_prefix:
        return []
    paths = sysconfig.get_paths()
    return [Path(paths[name]).resolve() for name in ("purelib", "platlib")]


def _prepend(env: dict[str, str], key: str, paths: Sequence[Path]) -> None:
    values = [str(path.resolve()) for path in paths if path.is_dir()]
    values.extend(filter(None, env.get(key, "").split(os.pathsep)))
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = os.path.normcase(os.path.abspath(value))
        if normalized not in seen:
            seen.add(normalized)
            unique.append(value)
    if unique:
        env[key] = os.pathsep.join(unique)


def _python_path(usd_root: Path, python_path: str | os.PathLike | None = None) -> Path:
    if python_path is not None:
        candidates = [Path(python_path).expanduser().resolve()]
    else:
        candidates = [usd_root / "Lib" / "site-packages"]
        for lib in (usd_root / "lib", usd_root / "lib64"):
            candidates.extend(sorted(lib.glob("python*/site-packages")))
            candidates.extend(sorted(lib.glob("python*/dist-packages")))
        candidates.append(usd_root / "lib" / "python")
        candidates.extend(_active_venv_python_paths())
    for candidate in candidates:
        if (candidate / "pxr" / "__init__.py").is_file():
            return candidate
    if python_path is not None:
        raise RuntimeError(f"no pxr Python package found in Python path: {candidates[0]}")
    raise RuntimeError(
        f"no pxr Python package found under OpenUSD install or active venv: {usd_root}; "
        "pass --python-path when the bindings are in another location"
    )


def _loader_path_key() -> str:
    if os.name == "nt":
        return "PATH"
    if sys.platform == "darwin":
        return "DYLD_LIBRARY_PATH"
    return "LD_LIBRARY_PATH"


def _required_dirs(values: Sequence[str | os.PathLike], label: str) -> list[Path]:
    paths = [Path(value).expanduser().resolve() for value in values]
    missing = [str(path) for path in paths if not path.is_dir()]
    if missing:
        raise RuntimeError(f"{label} does not exist: {', '.join(missing)}")
    return paths


def _required_file(value: str | os.PathLike, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"{label} does not exist: {path}")
    return path


def build_environment(
    usd_root: str | os.PathLike,
    *,
    base: Mapping[str, str] | None = None,
    plugin_paths: Sequence[str | os.PathLike] = (),
    dll_dirs: Sequence[str | os.PathLike] = (),
    python_path: str | os.PathLike | None = None,
    python_executable: str | os.PathLike | None = None,
    renderman_root: str | os.PathLike | None = None,
) -> tuple[dict[str, str], Path]:
    """Build an environment for an OpenUSD install prefix."""
    root = Path(usd_root).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"OpenUSD root does not exist: {root}")
    env = dict(os.environ if base is None else base)
    selected_python_path = _python_path(root, python_path)
    env[USD_ROOT_ENV] = str(root)

    _prepend(env, "PYTHONPATH", [selected_python_path])
    _prepend(env, "PATH", [root / "bin"])
    native_dirs = [
        root / "lib",
        root / "lib64",
        *_required_dirs(dll_dirs, "native-library directory"),
    ]
    loader_key = _loader_path_key()
    _prepend(env, loader_key, native_dirs)
    if os.name == "nt":
        _prepend(env, "PATH", native_dirs)
    if python_executable is not None:
        executable = _required_file(python_executable, "Python executable")
        _prepend(env, "PATH", [executable.parent])
        env["OPENUSDCONNECT_PYTHON_EXECUTABLE"] = str(executable)
    _prepend(env, "PXR_PLUGINPATH_NAME", _required_dirs(plugin_paths, "plugin path"))

    selected_renderman_root = renderman_root or env.get("RMANTREE")
    if selected_renderman_root:
        rman = Path(selected_renderman_root).expanduser().resolve()
        if not rman.is_dir():
            raise RuntimeError(f"RenderMan root does not exist: {rman}")
        env["RMANTREE"] = str(rman)
        _prepend(env, "PATH", [rman / "bin"])
        _prepend(env, loader_key, [rman / "bin", rman / "lib"])
        if os.name == "nt":
            _prepend(env, "PATH", [rman / "lib"])
        plugins = rman / "lib" / "plugins"
        plugin_usd = root / "plugin" / "usd"
        env.update(
            {
                "RMAN_SHADERPATH": os.pathsep.join(
                    [str(rman / "lib" / "shaders"), str(plugin_usd / "resources" / "shaders")]
                ),
                "RMAN_RIXPLUGINPATH": str(plugins),
                "RMAN_TEXTUREPATH": os.pathsep.join(
                    [str(rman / "lib" / "textures"), str(plugins), str(plugin_usd)]
                ),
                "RMAN_DISPLAYPATH": str(plugins),
                "RMAN_PROCEDURALPATH": str(plugins),
            }
        )
    return env, selected_python_path


def environment_delta(base: Mapping[str, str], configured: Mapping[str, str]) -> dict[str, str]:
    """Return only variables changed by runtime configuration."""
    return {key: value for key, value in configured.items() if base.get(key) != value}


def verify_bindings(
    env: Mapping[str, str],
    python_path: str | os.PathLike,
    python_executable: str | os.PathLike | None = None,
) -> str:
    """Import pxr with the resolved interpreter to confirm the bindings load.

    Finding the ``pxr`` package on disk is not enough: its native modules are
    compiled against one specific Python version, so an interpreter that differs
    from the one OpenUSD was built against fails at import time. Run that import
    in a subprocess under the configured environment and raise a clear error
    when it fails instead of leaving a cryptic DLL/so failure for a later launch.
    """
    interpreter = (
        Path(python_executable).expanduser().resolve()
        if python_executable is not None
        else Path(sys.executable)
    )
    probe = "from pxr import Tf, Usd; print(Usd.GetVersion())"
    result = subprocess.run(
        [str(interpreter), "-c", probe],
        env=dict(env),
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        tail = detail[-1] if detail else "unknown import error"
        raise RuntimeError(
            "could not import OpenUSD (pxr) with the selected interpreter.\n"
            f"  interpreter:  {interpreter}\n"
            f"  bindings:     {python_path}\n"
            f"  import error: {tail}\n"
            "\n"
            "The interpreter's Python version likely differs from the one OpenUSD "
            "was built against.\n"
            "Fix it by activating the matching virtual environment, or by selecting "
            "its interpreter\n"
            "with -PythonExecutable (openusd_env.ps1) or --python-executable "
            "(run_with_openusd.py)."
        )
    return result.stdout.strip()


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    usd_root, python_path, python_executable, renderman_root = runtime_defaults(argv)
    parser = argparse.ArgumentParser(description="Resolve an OpenUSD runtime environment.")
    parser.add_argument(
        "--usd-root",
        default=usd_root,
        help=(
            "OpenUSD install prefix (defaults to the managed project build; "
            f"{USD_ROOT_ENV} overrides it)."
        ),
    )
    parser.add_argument(
        "--python-path", default=python_path, help="Directory containing the pxr package."
    )
    parser.add_argument(
        "--python-executable",
        default=python_executable,
        help="Python executable to place first on PATH.",
    )
    parser.add_argument("--plugin-path", action="append", default=[])
    parser.add_argument("--dll-dir", action="append", default=[])
    parser.add_argument("--renderman-root", default=renderman_root)
    parser.add_argument("--format", choices=("json", "posix"), default="json")
    args = parser.parse_args(argv)
    if not args.usd_root:
        parser.error(
            "no managed OpenUSD runtime is registered; pass --usd-root or set "
            f"{USD_ROOT_ENV}"
        )
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    base = dict(os.environ)
    try:
        env, python_path = build_environment(
            args.usd_root,
            base=base,
            plugin_paths=args.plugin_path,
            dll_dirs=args.dll_dir,
            python_path=args.python_path,
            python_executable=args.python_executable,
            renderman_root=args.renderman_root,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        verify_bindings(env, python_path, args.python_executable)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    delta = environment_delta(base, env)
    if args.format == "json":
        print(
            json.dumps(
                {
                    "environment": delta,
                    "python_path": str(python_path),
                    "renderman_root": env.get("RMANTREE"),
                    "usd_root": env[USD_ROOT_ENV],
                },
                separators=(",", ":"),
            )
        )
    else:
        for key, value in sorted(delta.items()):
            print(f"export {key}={shlex.quote(value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
