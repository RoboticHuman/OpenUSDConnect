"""Demo script: starts server + fake emitters to showcase the layer stack dashboard.

Usage:
    uv run python scripts/demo_layer_dashboard.py

Opens the dashboard at http://localhost:8080 with three emitters in different
departments (animation, layout, lighting) sending transform edits to show
per-department layers, mute/unmute, and strength ordering.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from openusdconnect.codec import message_to_dict
from openusdconnect.framing import recv_framed
from openusdconnect.protocol import make_hello
from openusdconnect.transport import send_msg

SERVER_PORT = 7200
DASHBOARD_PORT = 8080
DEPARTMENTS = "lighting,animation,layout"
DB_PATH = "demo_layer_dashboard.db"


_TXN_IDS = {}


def connect_emitter(port, client_id, origin, department):
    """Connect an emitter socket and send hello."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    send_msg(
        s,
        make_hello(
            "emitter",
            client_id=client_id,
            origin=origin,
            department=department,
            producer_session_id=origin,
        ),
    )
    # Read hello_ok response
    s.settimeout(2)
    try:
        recv_framed(s)
    except Exception:
        pass
    s.settimeout(5)
    _TXN_IDS[s.fileno()] = 0
    return s


def send_txn(sock, client_id, events, proposal_id=None):
    fileno = sock.fileno()
    msg = {"type": "txn", "events": events}
    if proposal_id:
        msg["proposal_id"] = proposal_id
    else:
        _TXN_IDS[fileno] = _TXN_IDS.get(fileno, 0) + 1
        msg["txn_id"] = _TXN_IDS[fileno]
    send_msg(sock, msg)


def _check_port_free(port):
    """Check if a port is available. Exit with message if not."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            print(f"ERROR: Port {port} is already in use. Kill the old server first.")
            sys.exit(1)
    except (ConnectionRefusedError, OSError):
        pass  # Port is free


def _wait_for_server(port, timeout=10):
    """Wait until the server is accepting connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.3)
    return False


def main():
    _check_port_free(SERVER_PORT)

    # Clean stale DB from previous run
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print(f"Removed stale {DB_PATH}")

    # Start the server
    print(f"Starting server on port {SERVER_PORT} with dashboard on {DASHBOARD_PORT}...")
    print(f"Departments (strongest -> weakest): {DEPARTMENTS}")
    server_proc = subprocess.Popen(
        [
            sys.executable, "-m", "openusdconnect.server",
            "--port", str(SERVER_PORT),
            "--base", "test_scene.usda",
            "--event-log", DB_PATH,
            "--dashboard-port", str(DASHBOARD_PORT),
            "--departments", DEPARTMENTS,
        ],
    )

    # Wait for server to accept connections
    if not _wait_for_server(SERVER_PORT):
        print("Server failed to start within timeout.")
        server_proc.terminate()
        return

    print(f"\n  Dashboard: http://localhost:{DASHBOARD_PORT}")
    print()

    try:
        # -- Connect three emitters in different departments ---------------
        print("Connecting Alice (layout department)...")
        alice = connect_emitter(
            SERVER_PORT, "alice-workstation-blender", "alice-session-001", "layout",
        )

        print("Connecting Bob (animation department)...")
        bob = connect_emitter(SERVER_PORT, "bob-workstation-maya", "bob-session-001", "animation")

        print("Connecting Carol (lighting department)...")
        carol = connect_emitter(
            SERVER_PORT, "carol-workstation-houdini", "carol-session-001", "lighting",
        )

        time.sleep(1)

        # -- Alice creates scene structure (layout) ------------------------
        print("\nAlice (layout): placing Cube and Sphere...")
        send_txn(alice, "alice-workstation-blender", [
            {"k": "ensure_prim", "prim": "/World/Cube", "typeName": "Xform"},
            {"k": "ensure_xform_ops", "prim": "/World/Cube"},
            {"k": "set_xform_trs", "prim": "/World/Cube",
             "fields": ["t"], "t": [2.0, 0.0, 0.0]},
        ])
        time.sleep(0.5)

        send_txn(alice, "alice-workstation-blender", [
            {"k": "ensure_prim", "prim": "/World/Sphere", "typeName": "Xform"},
            {"k": "ensure_xform_ops", "prim": "/World/Sphere"},
            {"k": "set_xform_trs", "prim": "/World/Sphere",
             "fields": ["t"], "t": [-3.0, 0.0, 1.0]},
        ])
        time.sleep(0.5)

        # -- Bob animates (animation -- stronger than layout) ---------------
        print("Bob (animation): moving Cube to animated position...")
        send_txn(bob, "bob-workstation-maya", [
            {"k": "ensure_prim", "prim": "/World/Cube", "typeName": "Xform"},
            {"k": "ensure_xform_ops", "prim": "/World/Cube"},
            {"k": "set_xform_trs", "prim": "/World/Cube",
             "fields": ["t", "r"], "t": [5.0, 3.0, 0.0], "r": [0.924, 0.0, 0.383, 0.0]},
        ])
        time.sleep(0.5)

        # -- Carol sets lighting positions (lighting -- strongest) ----------
        print("Carol (lighting): positioning light rig...")
        send_txn(carol, "carol-workstation-houdini", [
            {"k": "ensure_prim", "prim": "/World/KeyLight", "typeName": "Xform"},
            {"k": "ensure_xform_ops", "prim": "/World/KeyLight"},
            {"k": "set_xform_trs", "prim": "/World/KeyLight",
             "fields": ["t"], "t": [10.0, 8.0, -5.0]},
        ])
        time.sleep(0.5)

        send_txn(carol, "carol-workstation-houdini", [
            {"k": "ensure_prim", "prim": "/World/FillLight", "typeName": "Xform"},
            {"k": "ensure_xform_ops", "prim": "/World/FillLight"},
            {"k": "set_xform_trs", "prim": "/World/FillLight",
             "fields": ["t"], "t": [-8.0, 6.0, 3.0]},
        ])
        time.sleep(0.5)

        # -- Alice proposes a lighting change --------------------------------
        print("Alice (layout): proposing a lighting change...")
        send_msg(alice, {
            "type": "create_proposal",
            "target_department": "lighting",
            "description": "Add rim light for hero shot\n\n"
                "The current lighting setup is too flat on the hero.\n"
                "Adding a rim light at (0, 12, -8) to separate the\n"
                "character from the background in shot 042.",
        })
        time.sleep(0.5)
        # Read the proposal_created response
        try:
            resp_buf = recv_framed(alice)
            proposal_resp = message_to_dict(resp_buf)
            proposal_id = proposal_resp.get("proposal_id", "")
            print(f"  Proposal created: {proposal_id}")

            # Alice sends edits to the proposal
            if proposal_id:
                send_txn(alice, "alice-workstation-blender", [
                    {"k": "ensure_prim", "prim": "/World/RimLight", "typeName": "Xform"},
                    {"k": "ensure_xform_ops", "prim": "/World/RimLight"},
                    {"k": "set_xform_trs", "prim": "/World/RimLight",
                     "fields": ["t"], "t": [0.0, 12.0, -8.0]},
                ], proposal_id=proposal_id)
                print("  Alice sent edits to proposal (muted -- not visible yet)")
        except Exception as e:
            print(f"  (proposal response: {e})")

        time.sleep(0.5)

        print("\n" + "=" * 60)
        print(f"  Dashboard ready at: http://localhost:{DASHBOARD_PORT}")
        print()
        print("  Layer stack (strongest -> weakest):")
        print("    #1  lighting  -- carol-workstation-houdini")
        print("    #2  animation -- bob-workstation-maya")
        print("    #3  layout    -- alice-workstation-blender")
        print()
        print("  Proposal from Alice -> lighting:")
        print("    'Add rim light for hero shot'")
        print()
        print("  Try in the dashboard:")
        print("    - Mute/unmute layers to see composed transforms change")
        print("    - Preview the proposal to see Alice's rim light")
        print("    - Approve it to merge into the lighting layer")
        print("    - Or reject it to discard")
        print()
        print("  Press Ctrl+C to stop.")
        print("=" * 60)

        # Keep connections alive until user stops
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        for s in [alice, bob, carol]:
            try:
                s.close()
            except Exception:
                pass
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
            server_proc.wait()
        time.sleep(0.5)
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = DB_PATH + suffix
            try:
                if os.path.exists(p):
                    os.remove(p)
            except PermissionError:
                pass  # OS still releasing -- cleaned on next run
        print("Done.")


if __name__ == "__main__":
    main()
