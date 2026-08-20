"""Resolve an OpenUSD runtime environment for launchers and shell adapters."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

USD_ROOT_ENV = "OPENUSDCONNECT_USD_ROOT"


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
    for candidate in candidates:
        if (candidate / "pxr" / "__init__.py").is_file():
            return candidate
    if python_path is not None:
        raise RuntimeError(f"no pxr Python package found in Python path: {candidates[0]}")
    raise RuntimeError(
        f"no pxr Python package found under OpenUSD install: {usd_root}; "
        "pass --python-path when the bindings were installed outside the prefix"
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


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve an OpenUSD runtime environment.")
    parser.add_argument(
        "--usd-root",
        default=os.environ.get(USD_ROOT_ENV),
        help=f"OpenUSD install prefix (or set {USD_ROOT_ENV}).",
    )
    parser.add_argument("--python-path", help="Directory containing the pxr package.")
    parser.add_argument("--python-executable", help="Python executable to place first on PATH.")
    parser.add_argument("--plugin-path", action="append", default=[])
    parser.add_argument("--dll-dir", action="append", default=[])
    parser.add_argument("--renderman-root")
    parser.add_argument("--format", choices=("json", "posix"), default="json")
    args = parser.parse_args(argv)
    if not args.usd_root:
        parser.error(f"--usd-root is required unless {USD_ROOT_ENV} is set")
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
