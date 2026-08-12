"""Send set_reference for the MaterialXTest basicTextured asset.

Usage:
    uv run python scripts/send_test_materialx.py [--port 7200]
"""

import argparse
import os
import socket
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from openusdconnect.cli_common import port_number
from openusdconnect.defaults import DEFAULT_SYNC_PORT
from openusdconnect.protocol import make_hello
from openusdconnect.transport import send_msg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=port_number, default=DEFAULT_SYNC_PORT)
    args = parser.parse_args()

    asset = os.path.abspath(
        os.path.join("assets", "test_assets", "MaterialXTest", "basicTextured.usda"),
    ).replace("\\", "/")

    s = socket.create_connection(("127.0.0.1", args.port), timeout=5)

    send_msg(
        s,
        make_hello(
            "emitter", client_id="cli", producer_session_id="materialx-test-cli"
        ),
    )
    send_msg(s, {
        "type": "txn",
        "txn_id": 1,
        "events": [
            {"k": "ensure_prim", "prim": "/World/Teapot", "typeName": "Xform"},
            {
                "k": "set_reference",
                "prim": "/World/Teapot",
                "refs": [
                    {"asset_path": asset, "prim_path": "/Teapot"},
                ],
            },
        ],
    })
    s.close()
    print(f"Sent set_reference for {asset}")


if __name__ == "__main__":
    main()
