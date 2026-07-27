"""Send a teapot reference with MaterialX materials to the server.

Pure Python (no Blender). Connects to the server, sends ensure_prim +
set_reference for the teapot asset, then disconnects.
"""

import argparse
import os
import socket
import sys
import time

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(os.path.dirname(_scripts_dir)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from openusdconnect.protocol import make_hello
from openusdconnect.transport import send_msg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7200)
    parser.add_argument("--asset-path", required=True)
    args = parser.parse_args()

    s = socket.create_connection(("127.0.0.1", args.port), timeout=5)

    send_msg(s, make_hello("emitter"))
    send_msg(s, {
        "type": "txn",
        "client_id": "mtlx_ref_emitter",
        "events": [
            {"k": "ensure_prim", "prim": "/World/Teapot", "typeName": "Xform"},
            {"k": "ensure_xform_ops", "prim": "/World/Teapot"},
            {
                "k": "set_reference",
                "prim": "/World/Teapot",
                "refs": [{
                    "asset_path": args.asset_path,
                    "prim_path": "/teapot",
                }],
            },
        ],
    })

    print(f"[MtlxRefEmitter] Sent teapot reference: {args.asset_path}")
    send_msg(s, {"type": "quit"})
    time.sleep(0.5)
    s.close()


if __name__ == "__main__":
    main()
