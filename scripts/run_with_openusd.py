"""Run a command with a project-provided OpenUSD installation."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

if __package__:
    from .openusd_runtime import USD_ROOT_ENV, build_environment
else:
    from openusd_runtime import USD_ROOT_ENV, build_environment

__all__ = ["USD_ROOT_ENV", "build_environment"]


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
        "--python-path",
        default=None,
        help="Directory containing pxr when bindings are installed outside --usd-root.",
    )
    parser.add_argument(
        "--python-executable",
        default=None,
        help="Python executable used for a command beginning with 'python'.",
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


def _resolve_command(command: Sequence[str], python_executable: str | None = None) -> list[str]:
    resolved = list(command)
    executable = resolved[0].lower()
    if executable in {"python", "python.exe", "python3", "python3.exe"} or executable.startswith(
        "python3."
    ):
        resolved[0] = str(Path(python_executable).resolve()) if python_executable else sys.executable
    return resolved


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        env, python_path = build_environment(
            args.usd_root,
            plugin_paths=args.plugin_path,
            dll_dirs=args.dll_dir,
            python_path=args.python_path,
            python_executable=args.python_executable,
            renderman_root=args.renderman_root,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"OpenUSD runtime: {Path(args.usd_root).resolve()} (Python: {python_path})", flush=True)
    command = _resolve_command(args.command, args.python_executable)
    try:
        return subprocess.run(command, env=env, check=False).returncode
    except FileNotFoundError:
        print(f"error: command not found: {command[0]}", file=sys.stderr)
        return 127
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
