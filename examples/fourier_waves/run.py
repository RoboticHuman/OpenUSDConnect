"""All-in-one launcher for the Fourier-wave procedural demo.

Starts the OpenUSDConnect server, launches usdview pre-wired to it, starts
the wave compute client in the background, then animates the procedural's
phases in the foreground. Tears everything down on ``Ctrl+C`` or exit.

Usage:
    uv run python examples/fourier_waves/run.py [author args...]

All knobs from ``author.py --help`` are forwarded as-is, plus:
    --no-usdview     skip launching usdview (headless pipeline only)
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

from examples.fourier_waves import author  # noqa: E402

EMPTY_USDA = _REPO_ROOT / "examples" / "fourier_waves" / "empty.usda"


def _wait_for_port(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _terminate(proc: subprocess.Popen | None, name: str, timeout: float = 5.0) -> None:
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
    for suffix in ("", "-wal", "-shm"):
        try:
            (log_path.parent / (log_path.name + suffix)).unlink()
        except OSError:
            pass


def main() -> int:
    parent = author.build_parser(add_help=False)
    parser = argparse.ArgumentParser(
        parents=[parent],
        description="Start server + usdview + wave client + animated author.",
    )
    parser.add_argument(
        "--no-usdview", action="store_true",
        help="skip launching usdview (headless pipeline only)",
    )
    args = parser.parse_args()
    if args.animate is None:
        args.animate = 0.0

    log_fd, log_path_str = tempfile.mkstemp(prefix="fourier_waves_", suffix=".db")
    os.close(log_fd)
    log_path = Path(log_path_str)

    server_proc: subprocess.Popen | None = None
    usdview_proc: subprocess.Popen | None = None
    wave_proc: subprocess.Popen | None = None

    try:
        print(f"starting server on {args.host}:{args.port}...")
        server_proc = subprocess.Popen(
            [
                sys.executable, "-m", "openusdconnect.server",
                "--host", args.host,
                "--port", str(args.port),
                "--base", str(EMPTY_USDA),
                "--log", str(log_path),
                # Continuous mesh regeneration floods the event log
                # (~2 MB/s at the default resolution); periodic compaction
                # keeps only the latest state per prim, and storage reclaim
                # returns the freed pages to the OS.
                "--compact-interval", "60",
                "--reclaim-interval", "120",
            ],
            cwd=str(_REPO_ROOT),
        )
        if not _wait_for_port(args.host, args.port, timeout=15.0):
            print("server did not become ready in 15s")
            return 1
        print("server ready.")

        print("starting wave compute client...")
        wave_proc = subprocess.Popen(
            [
                sys.executable, "-u",
                str(_REPO_ROOT / "examples" / "fourier_waves" / "wave_client.py"),
                "--host", args.host,
                "--port", str(args.port),
                "--prim", args.prim,
            ],
            cwd=str(_REPO_ROOT),
        )
        time.sleep(1.5)

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
        return author.run_author(args)
    finally:
        print("\nshutting down...")
        _terminate(usdview_proc, "usdview")
        _terminate(wave_proc, "wave client")
        _terminate(server_proc, "server")
        _cleanup_log(log_path)
        print("done.")


if __name__ == "__main__":
    raise SystemExit(main())
