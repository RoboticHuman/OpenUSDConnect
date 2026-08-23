"""Start a temporary server and the Qt native-scene projection example."""

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

from examples.qt_native_viewer import viewer  # noqa: E402

BASE_USD = Path(__file__).with_name("empty.usda")


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
    for suffix in ("", "-wal", "-shm"):
        try:
            path.with_name(path.name + suffix).unlink(missing_ok=True)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7340)
    parser.add_argument(
        "--exit-after",
        type=float,
        default=0.0,
        help="close the viewer after this many seconds; zero waits for the window to close",
    )
    args = parser.parse_args()

    descriptor, log_name = tempfile.mkstemp(prefix="qt_native_viewer_", suffix=".db")
    os.close(descriptor)
    log_path = Path(log_name)
    server = None
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
                str(BASE_USD),
                "--departments",
                "animation,layout",
                "--event-log",
                str(log_path),
            ],
            cwd=_REPO_ROOT,
        )
        if not _wait_for_port(args.host, args.port):
            print("server did not become ready", file=sys.stderr)
            return 1
        viewer_args = ["--host", args.host, "--port", str(args.port), "--demo"]
        if args.exit_after > 0:
            viewer_args.extend(("--exit-after", str(args.exit_after)))
        return viewer.main(viewer_args)
    finally:
        _terminate(server)
        _remove_log(log_path)


if __name__ == "__main__":
    raise SystemExit(main())
