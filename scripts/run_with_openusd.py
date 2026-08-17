"""Run a command with a project-provided OpenUSD installation."""

from __future__ import annotations

import argparse
import os
import subprocess
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


def _python_path(usd_root: Path) -> Path:
    candidates = [usd_root / "lib" / "python"]
    for lib in (usd_root / "lib", usd_root / "lib64"):
        candidates.extend(sorted(lib.glob("python*/site-packages")))
        candidates.extend(sorted(lib.glob("python*/dist-packages")))
    for candidate in candidates:
        if (candidate / "pxr" / "__init__.py").is_file():
            return candidate
    raise RuntimeError(f"no pxr Python package found under OpenUSD install: {usd_root}")


def _loader_path_key() -> str | None:
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


def build_environment(
    usd_root: str | os.PathLike,
    *,
    base: Mapping[str, str] | None = None,
    plugin_paths: Sequence[str | os.PathLike] = (),
    dll_dirs: Sequence[str | os.PathLike] = (),
    renderman_root: str | os.PathLike | None = None,
) -> tuple[dict[str, str], Path]:
    """Build a child environment for the selected OpenUSD installation."""
    root = Path(usd_root).expanduser().resolve()
    python_path = _python_path(root)
    env = dict(os.environ if base is None else base)
    env[USD_ROOT_ENV] = str(root)

    _prepend(env, "PYTHONPATH", [python_path])
    _prepend(env, "PATH", [root / "bin"])
    native_dirs = [root / "lib", *_required_dirs(dll_dirs, "native-library directory")]
    loader_key = _loader_path_key()
    if loader_key:
        _prepend(env, loader_key, native_dirs)
    if os.name == "nt":
        _prepend(env, "PATH", native_dirs)
    _prepend(
        env,
        "PXR_PLUGINPATH_NAME",
        _required_dirs(plugin_paths, "plugin path"),
    )

    if renderman_root:
        rman = Path(renderman_root).expanduser().resolve()
        if not rman.is_dir():
            raise RuntimeError(f"RenderMan root does not exist: {rman}")
        env["RMANTREE"] = str(rman)
        _prepend(env, "PATH", [rman / "bin"])
        if loader_key:
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
    return env, python_path


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a command with a selected project OpenUSD runtime."
    )
    parser.add_argument(
        "--usd-root",
        default=os.environ.get(USD_ROOT_ENV),
        help=f"OpenUSD install prefix (or set {USD_ROOT_ENV}).",
    )
    parser.add_argument(
        "--plugin-path",
        action="append",
        default=[],
        help="Additional USD plugin search path; repeat as needed.",
    )
    parser.add_argument(
        "--dll-dir",
        action="append",
        default=[],
        help="Additional native-library directory; repeat as needed.",
    )
    parser.add_argument(
        "--renderman-root",
        default=None,
        help="Optional RenderManProServer prefix used to configure hdPrman.",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to run after --.")
    args = parser.parse_args(argv)
    if not args.usd_root:
        parser.error(f"--usd-root is required unless {USD_ROOT_ENV} is set")
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("provide a command after --")
    return args


def _resolve_command(command: Sequence[str]) -> list[str]:
    resolved = list(command)
    executable = resolved[0].lower()
    if executable in {"python", "python.exe", "python3", "python3.exe"} or executable.startswith(
        "python3."
    ):
        resolved[0] = sys.executable
    return resolved


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        env, python_path = build_environment(
            args.usd_root,
            plugin_paths=args.plugin_path,
            dll_dirs=args.dll_dir,
            renderman_root=args.renderman_root,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"OpenUSD runtime: {Path(args.usd_root).resolve()} (Python: {python_path})", flush=True)
    command = _resolve_command(args.command)
    try:
        return subprocess.run(command, env=env, check=False).returncode
    except FileNotFoundError:
        print(f"error: command not found: {command[0]}", file=sys.stderr)
        return 127
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
