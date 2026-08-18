"""Start a temporary server, usdview, and the USD-native client demo."""

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
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from examples.usd_native_client import demo  # noqa: E402


def _wait_for_port(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _terminate(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _remove_log(path: Path) -> None:
    pending = [path.with_name(path.name + suffix) for suffix in ("", "-wal", "-shm")]
    deadline = time.monotonic() + 2.0
    while pending:
        remaining = []
        for candidate in pending:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                remaining.append(candidate)
        if not remaining or time.monotonic() >= deadline:
            return
        pending = remaining
        time.sleep(0.02)


def main() -> int:
    parser = argparse.ArgumentParser(
        parents=[demo.build_parser(add_help=False)],
        description="Start a server, two USD clients, and optionally usdview.",
    )
    parser.add_argument("--no-usdview", action="store_true")
    parser.add_argument("--peer-delay", type=float, default=0.75)
    args = parser.parse_args()

    descriptor, log_name = tempfile.mkstemp(prefix="usd_native_client_", suffix=".db")
    os.close(descriptor)
    log_path = Path(log_name)
    server = None
    usdview = None
    peer = None
    try:
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "openusdconnect.server",
                "--host",
                args.host,
                "--port",
                str(args.port),
                "--base",
                str(demo.BASE_USD),
                "--departments",
                "lookdev,layout",
                "--event-log",
                str(log_path),
            ],
            cwd=_REPO_ROOT,
        )
        if not _wait_for_port(args.host, args.port):
            print("server did not become ready", file=sys.stderr)
            return 1

        if not args.no_usdview:
            from integrations.usdview.launcher import launch_usdview

            try:
                usdview = launch_usdview(
                    str(demo.BASE_USD),
                    host=args.host,
                    port=args.port,
                )
            except RuntimeError as exc:
                print(f"usdview unavailable ({exc}); continuing headless")

        peer = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("peer.py")),
                "--host",
                args.host,
                "--port",
                str(args.port),
                "--delay",
                str(args.peer_delay),
            ],
            cwd=_REPO_ROOT,
        )
        result = demo.run(args, expect_peer=True)
        if result:
            return result
        try:
            peer_result = peer.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print("peer did not finish publishing", file=sys.stderr)
            return 1
        if peer_result:
            print(f"peer exited with status {peer_result}", file=sys.stderr)
        return peer_result
    finally:
        _terminate(peer)
        _terminate(usdview)
        _terminate(server)
        _remove_log(log_path)


if __name__ == "__main__":
    raise SystemExit(main())
