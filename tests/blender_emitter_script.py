"""Blender emitter script for integration test.

Connects to the sync server, creates objects, moves them, sends events, exits.
Run via: blender --background --python tests/blender_emitter_script.py -- --port PORT
"""

import os
import sys
import time

# Add project root to sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import socket

from openusdconnect.protocol import make_hello, make_quit, make_txn
from openusdconnect.transport import send_line


def main():
    # Parse port from args after "--"
    argv = sys.argv
    port = 7200
    if "--" in argv:
        script_args = argv[argv.index("--") + 1 :]
        for i, arg in enumerate(script_args):
            if arg == "--port" and i + 1 < len(script_args):
                port = int(script_args[i + 1])

    print(f"[Emitter] Connecting to 127.0.0.1:{port}")
    sock = socket.create_connection(("127.0.0.1", port))
    send_line(sock, make_hello("emitter"))

    # Send events: create a Sphere, Cube, set transforms, set visibility
    events = [
        # Sphere at (3, 4, 5), visible
        {"k": "ensure_prim", "prim": "/World/TestSphere", "typeName": "Sphere"},
        {"k": "ensure_xform_ops", "prim": "/World/TestSphere"},
        {
            "k": "set_xform_trs",
            "prim": "/World/TestSphere",
            "fields": ["t", "s"],
            "t": [3.0, 4.0, 5.0],
            "s": [2.0, 2.0, 2.0],
        },
        # Cube at (10, 0, 0), hidden
        {"k": "ensure_prim", "prim": "/World/TestCube", "typeName": "Cube"},
        {"k": "ensure_xform_ops", "prim": "/World/TestCube"},
        {"k": "set_xform_trs", "prim": "/World/TestCube", "fields": ["t"], "t": [10.0, 0.0, 0.0]},
        {"k": "set_visibility", "prim": "/World/TestCube", "visible": False},
        # Cone at origin, default transform
        {"k": "ensure_prim", "prim": "/World/TestCone", "typeName": "Cone"},
        {"k": "ensure_xform_ops", "prim": "/World/TestCone"},
        # Empty/Xform
        {"k": "ensure_prim", "prim": "/World/TestXform", "typeName": "Xform"},
    ]

    txn = make_txn("integration-test-emitter", events)
    send_line(sock, txn)
    print(f"[Emitter] Sent {len(events)} events")

    # Small delay to ensure server processes
    time.sleep(0.5)

    send_line(sock, make_quit())
    sock.close()
    print("[Emitter] Done")


if __name__ == "__main__":
    main()
