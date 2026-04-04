"""Send events to a running OpenUSDConnect server from the command line.

Accepts JSON event dicts as positional arguments, wraps them in a
transaction, and sends via the FlatBuffers wire protocol.

Usage:
    # Single event
    uv run python -m openusdconnect.send \
      '{"k":"ensure_prim","prim":"/World/Sphere","typeName":"Sphere"}'

    # Multiple events in one transaction
    uv run python -m openusdconnect.send \
      '{"k":"ensure_prim","prim":"/World/Sphere","typeName":"Sphere"}' \
      '{"k":"ensure_xform_ops","prim":"/World/Sphere"}'

    # Raw non-event message (compact, quit)
    uv run python -m openusdconnect.send --msg '{"type":"compact"}'

    # Read events from stdin (one JSON object per line)
    cat events.jsonl | uv run python -m openusdconnect.send --stdin

    # Custom host/port and client id
    uv run python -m openusdconnect.send --host 10.0.0.1 --port 7201 --client-id studio-a \
      '{"k":"set_xform_trs","prim":"/World/Cube","fields":["t"],"t":[1,2,3]}'
"""

from __future__ import annotations

import argparse
import json
import socket
import sys


def _connect_and_hello(host: str, port: int, client_id: str) -> socket.socket:
    from .framing import recv_framed
    from .transport import send_msg

    sock = socket.create_connection((host, port), timeout=10)
    send_msg(sock, {
        "type": "hello",
        "role": "emitter",
        "protocol_version": 1,
        "client_id": client_id,
    })
    # Wait for hello_ok before sending events.
    recv_framed(sock)
    return sock


def _send_txn(sock: socket.socket, events: list[dict], client_id: str) -> None:
    from .transport import send_msg

    send_msg(sock, {
        "type": "txn",
        "client_id": client_id,
        "events": events,
    })


def _send_raw_msg(sock: socket.socket, msg: dict) -> None:
    from .transport import send_msg

    send_msg(sock, msg)


def _parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        print(f"  Input: {text[:200]}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        prog="python -m openusdconnect.send",
        description="Send events to a running OpenUSDConnect server.",
    )
    parser.add_argument(
        "events", nargs="*",
        help="JSON event dicts (e.g. '{\"k\":\"ensure_prim\",\"prim\":\"/World/X\"}')",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Server host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=7200, help="Server port (default: 7200)")
    parser.add_argument("--client-id", default="cli", help="Client ID (default: cli)")
    parser.add_argument(
        "--msg", action="append", default=[],
        help="Raw message JSON (for non-event messages like compact, quit). Repeatable.",
    )
    parser.add_argument(
        "--stdin", action="store_true",
        help="Read JSON events from stdin, one per line.",
    )
    args = parser.parse_args()

    events: list[dict] = []
    raw_msgs: list[dict] = []

    # Collect events from positional args
    for text in args.events:
        events.append(_parse_json(text))

    # Collect events from stdin
    if args.stdin:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            events.append(_parse_json(line))

    # Collect raw messages
    for text in args.msg:
        raw_msgs.append(_parse_json(text))

    if not events and not raw_msgs:
        parser.print_help()
        sys.exit(1)

    sock = _connect_and_hello(args.host, args.port, args.client_id)

    try:
        if events:
            _send_txn(sock, events, args.client_id)
            print(f"Sent {len(events)} event(s)")

        for msg in raw_msgs:
            _send_raw_msg(sock, msg)
            print(f"Sent {msg.get('type', '?')} message")

        # Graceful disconnect — tells server we're done.
        _send_raw_msg(sock, {"type": "quit"})
    finally:
        sock.close()


if __name__ == "__main__":
    main()
