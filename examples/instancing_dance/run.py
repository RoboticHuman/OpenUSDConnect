"""All-in-one launcher for the instancing dance demo.

Starts the OpenUSDConnect server, launches usdview pre-wired to it,
runs the sine-wave sender in the foreground, and tears everything down
on ``Ctrl+C`` or normal exit. Useful as a one-command quickstart; the
three-terminal recipe in README.md is the same flow, just open enough
to drive debugging.

Usage:
    uv run python examples/instancing_dance/run.py [dance args...]

All knobs from ``dance.py --help`` are forwarded as-is, plus:
    --no-usdview     skip launching usdview (server + sender only)
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from examples.instancing_dance import dance  # noqa: E402

EMPTY_USDA = _REPO_ROOT / "examples" / "instancing_dance" / "empty.usda"


def _wait_for_port(host: str, port: int, timeout: float = 15.0) -> bool:
    """Poll until a TCP connection to (host, port) succeeds, or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _terminate(proc: subprocess.Popen | None, name: str, timeout: float = 5.0) -> None:
    """Best-effort subprocess shutdown: ``terminate`` then ``kill`` if needed."""
    if proc is None or proc.poll() is not None:
        return
    print(f"  stopping {name}...")
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    print(f"    {name} did not respond, killing")
    proc.kill()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        print(f"    {name} could not be killed")


def _cleanup_log(log_path: Path) -> None:
    """Remove the SQLite log and its WAL/SHM sidecars."""
    base = log_path.parent / log_path.name
    for suffix in ("", "-wal", "-shm"):
        try:
            (base.parent / (base.name + suffix)).unlink()
        except OSError:
            pass


def _sweep_orphans() -> None:
    """Delete stale logs from prior runs that were hard-killed.

    Files still locked by a concurrently-running instance silently fail
    to unlink, so this is safe to call alongside another launcher.
    """
    tmp = Path(tempfile.gettempdir())
    for path in tmp.glob("instancing_dance_*"):
        try:
            path.unlink()
        except OSError:
            pass


def main() -> int:
    parent = dance.build_parser(add_help=False)
    parser = argparse.ArgumentParser(
        parents=[parent],
        description=(
            "Start server + usdview + sender for the instancing dance demo."
        ),
    )
    parser.add_argument(
        "--no-usdview", action="store_true",
        help="skip launching usdview (server + sender only)",
    )
    args = parser.parse_args()

    if not EMPTY_USDA.exists():
        print(f"missing base scene: {EMPTY_USDA}")
        return 1

    _sweep_orphans()

    log_fd, log_path_str = tempfile.mkstemp(prefix="instancing_dance_", suffix=".db")
    os.close(log_fd)
    log_path = Path(log_path_str)

    server_proc: subprocess.Popen | None = None
    usdview_proc: subprocess.Popen | None = None

    try:
        print(f"starting server on {args.host}:{args.port}...")
        server_proc = subprocess.Popen(
            [
                sys.executable, "-m", "openusdconnect.server",
                "--host", args.host,
                "--port", str(args.port),
                "--base", str(EMPTY_USDA),
                "--event-log", str(log_path),
            ],
            cwd=str(_REPO_ROOT),
        )
        if not _wait_for_port(args.host, args.port, timeout=15.0):
            print("server did not become ready in 15s")
            return 1
        print("server ready.")

        if not args.no_usdview:
            from integrations.usdview.launcher import launch_usdview

            print("launching usdview...")
            try:
                usdview_proc = launch_usdview(
                    str(EMPTY_USDA), host=args.host, port=args.port,
                )
            except RuntimeError as exc:
                print(f"  usdview unavailable ({exc}); continuing without it")
                usdview_proc = None
            else:
                time.sleep(3.0)
                print("usdview launched.")
        else:
            print("(usdview skipped per --no-usdview)")

        print()
        return dance.run_dance(args)
    finally:
        print("\nshutting down...")
        _terminate(usdview_proc, "usdview")
        _terminate(server_proc, "server")
        _cleanup_log(log_path)
        print("done.")


if __name__ == "__main__":
    raise SystemExit(main())
