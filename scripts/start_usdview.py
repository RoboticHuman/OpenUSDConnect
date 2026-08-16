"""Start an OpenUSDConnect server and a pre-connected usdview session.

The event log is temporary unless ``--event-log`` is supplied. Closing usdview
or pressing Ctrl+C also stops the server started by this launcher.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
from pathlib import Path

from openusdconnect.cli_common import nonnegative_int, port_or_zero, positive_seconds
from openusdconnect.defaults import DEFAULT_HOST, DEFAULT_STARTUP_TIMEOUT

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _server_python() -> str:
    """Use the real Windows interpreter, not uv's short-lived venv launcher."""

    if os.name == "nt":
        return str(getattr(sys, "_base_executable", sys.executable))
    return sys.executable


def _server_environment() -> dict[str, str]:
    env = os.environ.copy()
    if os.name != "nt":
        return env
    paths = [str(PROJECT_ROOT)]
    for key in ("purelib", "platlib"):
        path = sysconfig.get_paths().get(key)
        if path and path not in paths:
            paths.append(path)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [*paths, env.get("PYTHONPATH", "")]))
    return env


def _parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description="Start a sync server and open usdview already connected to it.",
        allow_abbrev=False,
    )
    parser.add_argument("stage", help="Base USD stage opened by both server and usdview")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Server bind and connection host")
    parser.add_argument(
        "--port",
        type=port_or_zero,
        default=0,
        help="Sync port; 0 selects a free local port (default: 0)",
    )
    parser.add_argument(
        "--event-log",
        help="Persistent SQLite event log; omitted uses a temporary log",
    )
    parser.add_argument(
        "--layer-mode",
        choices=("managed", "shared_stage"),
        default="managed",
        help="Server layer mode (default: managed)",
    )
    parser.add_argument(
        "--resolver-context",
        action="append",
        default=[],
        metavar="[SCHEME:]CONFIG",
        help="Forward a resolver context to the server; repeat as needed",
    )
    parser.add_argument(
        "--plugin-dll-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="Forward a USD plugin dependency directory to the server",
    )
    parser.add_argument("--export-diff", help="Export the server override layer on shutdown")
    parser.add_argument("--compact", action="store_true", help="Compact the event log on startup")
    parser.add_argument("--token", help="Optional connection token forwarded to usdview")
    parser.add_argument("--usdview", help="Explicit usdview executable; otherwise auto-discovered")
    parser.add_argument(
        "--renderman",
        action="store_true",
        help="Start usdview with the RenderMan delegate environment",
    )
    parser.add_argument("--camera", help="Select this camera after replay catches up")
    parser.add_argument(
        "--expected-seq",
        type=nonnegative_int,
        default=0,
        help="Wait for this replay sequence before applying camera/view settings",
    )
    parser.add_argument(
        "--scene-lights",
        action="store_true",
        help="Use streamed scene lights instead of usdview's defaults",
    )
    parser.add_argument(
        "--startup-timeout",
        type=positive_seconds,
        default=DEFAULT_STARTUP_TIMEOUT,
        help=f"Seconds to wait for the server (default: {DEFAULT_STARTUP_TIMEOUT:g})",
    )
    parser.epilog = (
        "Unknown options are forwarded to usdview. Use -- before usdview-only options "
        "when their names overlap launcher options."
    )
    return parser.parse_known_args(argv)


def _select_port(host: str, requested: int) -> int:
    if requested:
        return requested
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def _server_command(args: argparse.Namespace, port: int, event_log: Path) -> list[str]:
    command = [
        _server_python(),
        "-m",
        "integrations.run_server",
        "--host",
        args.host,
        "--port",
        str(port),
        "--base",
        str(args.stage),
        "--event-log",
        str(event_log),
        "--layer-mode",
        args.layer_mode,
    ]
    for context in args.resolver_context:
        command.extend(("--resolver-context", context))
    for directory in args.plugin_dll_dir:
        command.extend(("--plugin-dll-dir", directory))
    if args.export_diff:
        command.extend(("--export-diff", args.export_diff))
    if args.compact:
        command.append("--compact")
    return command


def _wait_for_server(
    host: str,
    port: int,
    process: subprocess.Popen,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"server exited during startup with code {return_code}")
        try:
            with socket.create_connection((host, port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"server did not start on {host}:{port} within {timeout:g}s")


def _stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


def _run_session(
    args: argparse.Namespace,
    usdview_args: list[str],
    temporary_root: Path,
) -> int:
    stage = Path(args.stage).expanduser().resolve()
    if not stage.is_file():
        raise RuntimeError(f"base stage does not exist: {stage}")
    args.stage = stage
    port = _select_port(args.host, args.port)
    event_log = (
        Path(args.event_log).expanduser().resolve()
        if args.event_log
        else temporary_root / "events.db"
    )
    event_log.parent.mkdir(parents=True, exist_ok=True)

    server = subprocess.Popen(
        _server_command(args, port, event_log),
        cwd=PROJECT_ROOT,
        env=_server_environment(),
    )
    viewer = None
    try:
        _wait_for_server(args.host, port, server, args.startup_timeout)

        from integrations.usdview.launcher import find_usdview, launch_usdview

        usdview_exe = Path(args.usdview).expanduser().resolve() if args.usdview else find_usdview()
        print(f"Server:  {args.host}:{port} (PID {server.pid})", flush=True)
        print(f"Stage:   {stage}", flush=True)
        print(f"usdview: {usdview_exe}", flush=True)
        viewer = launch_usdview(
            stage,
            host=args.host,
            port=port,
            token=args.token,
            extra_args=usdview_args,
            usdview_exe=usdview_exe,
            renderman=args.renderman,
            camera_path=args.camera,
            expected_seq=args.expected_seq,
            scene_lights=args.scene_lights,
        )
        print(f"Connected session started (usdview PID {viewer.pid}).", flush=True)

        while True:
            viewer_code = viewer.poll()
            if viewer_code is not None:
                return viewer_code
            server_code = server.poll()
            if server_code is not None:
                raise RuntimeError(f"server exited with code {server_code}")
            time.sleep(0.2)
    finally:
        _stop_process(viewer)
        _stop_process(server)


def main(argv: list[str] | None = None) -> int:
    args, usdview_args = _parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="openusdconnect-usdview-") as temp_dir:
            return _run_session(args, usdview_args, Path(temp_dir))
    except KeyboardInterrupt:
        print("Stopping usdview session...", flush=True)
        return 130
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
