"""Send set_payload + load_payload for the test_asset fixture.

Usage:
    uv run python scripts/send_test_payload.py [--port 7200]
"""

import argparse
import json
import os
import socket


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7200)
    args = parser.parse_args()

    asset = os.path.abspath(
        os.path.join("tests", "fixtures", "test_asset.usda"),
    ).replace("\\", "/")

    def send_txn(events):
        s = socket.create_connection(("127.0.0.1", args.port), timeout=5)
        def send(m):
            s.sendall((json.dumps(m) + "\n").encode())
        send({"type": "hello", "role": "emitter", "protocol_version": 1})
        send({"type": "txn", "client_id": "cli", "events": events})
        s.close()

    send_txn([
        {"k": "ensure_prim", "prim": "/World/Asset", "typeName": "Xform"},
        {
            "k": "set_payload",
            "prim": "/World/Asset",
            "payloads": [{"asset_path": asset, "prim_path": "/Model"}],
        },
    ])
    print(f"Sent set_payload for {asset}")

    send_txn([{"k": "load_payload", "prim": "/World/Asset"}])
    print("Sent load_payload")


if __name__ == "__main__":
    main()
