"""Shared subprocess setup for source-tree server launchers."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import sysconfig
import time
from collections.abc import Iterable, Mapping
from pathlib import Path


def _is_windows() -> bool:
    return os.name == "nt"


def python_executable() -> str:
    """Return the active interpreter, preserving its installed native modules."""

    return sys.executable


def server_environment(
    project_root: str | os.PathLike,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build an inherited environment that can import the active source tree."""

    env = dict(os.environ if base is None else base)
    if not _is_windows():
        return env

    paths = [str(Path(project_root).resolve())]
    for key in ("purelib", "platlib"):
        path = sysconfig.get_paths().get(key)
        if path and path not in paths:
            paths.append(path)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [*paths, env.get("PYTHONPATH", "")]))
    return env


def command(server_args: Iterable[str]) -> list[str]:
    """Build the standard source-tree server command."""

    return [python_executable(), "-m", "integrations.run_server", *server_args]


def start(
    server_args: Iterable[str],
    *,
    project_root: str | os.PathLike,
    env: Mapping[str, str] | None = None,
    **popen_kwargs,
) -> subprocess.Popen:
    """Start a server with project imports and plugin discovery configured."""

    return subprocess.Popen(
        command(server_args),
        cwd=str(Path(project_root).resolve()),
        env=server_environment(project_root, env),
        **popen_kwargs,
    )


def wait_until_listening(
    process: subprocess.Popen,
    host: str,
    port: int,
    timeout: float = 10.0,
) -> None:
    """Wait for the listener or fail immediately if the child exits."""

    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"server exited during startup with code {return_code}")
        try:
            with socket.create_connection((probe_host, port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"server did not start on {host}:{port} within {timeout:g}s")


def stop(process: subprocess.Popen | None, timeout: float = 5.0) -> None:
    """Stop a child process, escalating to kill after the timeout."""

    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)
