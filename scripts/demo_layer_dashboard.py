"""Start a dashboard server and populate a small departmental layer stack.

Usage:
    uv run --group bundled-usd --group dashboard python scripts/demo_layer_dashboard.py
"""

from __future__ import annotations

import argparse
import importlib.util
import socket
import sys
import tempfile
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from integrations.server_process import start as start_server_process
from integrations.server_process import stop as stop_process
from integrations.server_process import wait_until_listening
from openusdconnect.cli_common import nonnegative_seconds, port_number, positive_seconds
from openusdconnect.framing import recv_framed
from openusdconnect.protocol import make_hello
from openusdconnect.transport import send_msg

DEFAULT_DEPARTMENTS = "lighting,animation,layout"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start and populate the departmental layer dashboard demo.",
        allow_abbrev=False,
    )
    parser.add_argument("--host", default="127.0.0.1", help="Server host")
    parser.add_argument("--port", type=port_number, default=7200, help="Sync port")
    parser.add_argument(
        "--dashboard-port", type=port_number, default=8080, help="Dashboard HTTP port"
    )
    parser.add_argument(
        "--base",
        type=Path,
        default=PROJECT_ROOT / "test_scene.usda",
        help="Base USD stage",
    )
    parser.add_argument(
        "--event-log",
        type=Path,
        help="Persistent event log; omitted uses a temporary database",
    )
    parser.add_argument(
        "--departments",
        default=DEFAULT_DEPARTMENTS,
        help="Department priority, strongest first",
    )
    parser.add_argument(
        "--plugin-dll-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="Forward a USD plugin dependency directory; repeat as needed",
    )
    parser.add_argument(
        "--startup-timeout",
        type=positive_seconds,
        default=15.0,
        help="Seconds to wait for the server (default: 15)",
    )
    parser.add_argument(
        "--exit-after",
        type=nonnegative_seconds,
        default=0.0,
        help="Stop automatically after N seconds; 0 waits for Ctrl+C",
    )
    return parser.parse_args(argv)


def _require_dashboard_dependency() -> None:
    if importlib.util.find_spec("nicegui") is None:
        raise RuntimeError(
            "dashboard dependency is missing; run with "
            "`uv run --group bundled-usd --group dashboard python "
            "scripts/demo_layer_dashboard.py`"
        )


def _connect_emitter(
    host: str,
    port: int,
    client_id: str,
    department: str,
    session_id: str,
) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=5)
    send_msg(
        sock,
        make_hello(
            "emitter",
            client_id=client_id,
            origin=session_id,
            department=department,
            producer_session_id=session_id,
        ),
    )
    sock.settimeout(5)
    recv_framed(sock)
    return sock


def _send_txn(sock: socket.socket, txn_id: int, events: list[dict]) -> None:
    send_msg(sock, {"type": "txn", "txn_id": txn_id, "events": events})


def _populate_demo(host: str, port: int) -> list[socket.socket]:
    run_id = uuid.uuid4().hex
    connections: list[socket.socket] = []
    specs = (
        ("alice-workstation-blender", "layout", "Alice"),
        ("bob-workstation-maya", "animation", "Bob"),
        ("carol-workstation-houdini", "lighting", "Carol"),
    )
    try:
        for client_id, department, person in specs:
            print(f"Connecting {person} ({department} department)...")
            connections.append(
                _connect_emitter(
                    host, port, client_id, department, f"dashboard-{run_id}-{department}"
                )
            )

        alice, bob, carol = connections
        print("Alice (layout): placing Cube and Sphere...")
        _send_txn(
            alice,
            1,
            [
                {"k": "ensure_prim", "prim": "/World/Cube", "typeName": "Xform"},
                {"k": "ensure_xform_ops", "prim": "/World/Cube"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Cube",
                    "fields": ["t"],
                    "t": [2.0, 0.0, 0.0],
                },
            ],
        )
        _send_txn(
            alice,
            2,
            [
                {"k": "ensure_prim", "prim": "/World/Sphere", "typeName": "Xform"},
                {"k": "ensure_xform_ops", "prim": "/World/Sphere"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Sphere",
                    "fields": ["t"],
                    "t": [-3.0, 0.0, 1.0],
                },
            ],
        )

        print("Bob (animation): moving Cube to its animated position...")
        _send_txn(
            bob,
            1,
            [
                {"k": "ensure_prim", "prim": "/World/Cube", "typeName": "Xform"},
                {"k": "ensure_xform_ops", "prim": "/World/Cube"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Cube",
                    "fields": ["t", "r"],
                    "t": [5.0, 3.0, 0.0],
                    "r": [0.924, 0.0, 0.383, 0.0],
                },
            ],
        )

        print("Carol (lighting): positioning the light rig...")
        _send_txn(
            carol,
            1,
            [
                {"k": "ensure_prim", "prim": "/World/KeyLight", "typeName": "Xform"},
                {"k": "ensure_xform_ops", "prim": "/World/KeyLight"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/KeyLight",
                    "fields": ["t"],
                    "t": [10.0, 8.0, -5.0],
                },
            ],
        )
        _send_txn(
            carol,
            2,
            [
                {"k": "ensure_prim", "prim": "/World/FillLight", "typeName": "Xform"},
                {"k": "ensure_xform_ops", "prim": "/World/FillLight"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/FillLight",
                    "fields": ["t"],
                    "t": [-8.0, 6.0, 3.0],
                },
            ],
        )
        return connections
    except Exception:
        for connection in connections:
            connection.close()
        raise


def _run(args: argparse.Namespace, event_log: Path) -> int:
    base = args.base.expanduser().resolve()
    if not base.is_file():
        raise RuntimeError(f"base stage does not exist: {base}")

    server_args = [
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--base",
        str(base),
        "--event-log",
        str(event_log),
        "--dashboard-port",
        str(args.dashboard_port),
        "--departments",
        args.departments,
    ]
    for directory in args.plugin_dll_dir:
        server_args.extend(("--plugin-dll-dir", directory))

    print(f"Starting server on {args.host}:{args.port}...")
    server = start_server_process(server_args, project_root=PROJECT_ROOT)
    connections: list[socket.socket] = []
    try:
        wait_until_listening(server, args.host, args.port, args.startup_timeout)
        wait_until_listening(server, "127.0.0.1", args.dashboard_port, args.startup_timeout)
        connections = _populate_demo(args.host, args.port)
        dashboard_url = f"http://127.0.0.1:{args.dashboard_port}"
        print(f"\nDashboard ready: {dashboard_url}")
        print("Mute, unmute, and reorder departments to inspect composition.")
        print("Press Ctrl+C to stop.")

        deadline = time.monotonic() + args.exit_after if args.exit_after else None
        while deadline is None or time.monotonic() < deadline:
            if server.poll() is not None:
                raise RuntimeError(f"server exited with code {server.returncode}")
            time.sleep(0.2)
        return 0
    finally:
        for connection in connections:
            connection.close()
        stop_process(server)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        _require_dashboard_dependency()
        if args.event_log:
            event_log = args.event_log.expanduser().resolve()
            event_log.parent.mkdir(parents=True, exist_ok=True)
            return _run(args, event_log)
        with tempfile.TemporaryDirectory(prefix="openusdconnect-dashboard-") as temp_dir:
            return _run(args, Path(temp_dir) / "events.db")
    except KeyboardInterrupt:
        print("\nStopping dashboard demo...")
        return 0
    except (OSError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
