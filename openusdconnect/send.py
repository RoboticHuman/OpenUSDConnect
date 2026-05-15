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
import sys

from .sender import EventSender


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
        "events",
        nargs="*",
        help=(
            'JSON event dicts (e.g. \'{"k":"ensure_prim","prim":"/World/X","typeName":"Xform"}\')'
        ),
    )
    parser.add_argument("--host", default="127.0.0.1", help="Server host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=7200, help="Server port (default: 7200)")
    parser.add_argument("--client-id", default="cli", help="Client ID (default: cli)")
    parser.add_argument(
        "--msg",
        action="append",
        default=[],
        help="Raw message JSON (for non-event messages like compact, quit). Repeatable.",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read JSON events from stdin, one per line.",
    )
    args = parser.parse_args()

    events: list[dict] = [_parse_json(text) for text in args.events]
    if args.stdin:
        for line in sys.stdin:
            line = line.strip()
            if line:
                events.append(_parse_json(line))
    raw_msgs: list[dict] = [_parse_json(text) for text in args.msg]

    if not events and not raw_msgs:
        parser.print_help()
        sys.exit(1)

    sender = EventSender(args.host, args.port, client_id=args.client_id)
    if not sender.connect():
        print("Failed to connect to server", file=sys.stderr)
        sys.exit(1)

    try:
        if events:
            if not sender.send_events(events):
                print("Failed to send events", file=sys.stderr)
                sys.exit(1)
            print(f"Sent {len(events)} event(s)")

        for msg in raw_msgs:
            if not sender.send_message(msg):
                print(f"Failed to send {msg.get('type', '?')} message", file=sys.stderr)
                sys.exit(1)
            print(f"Sent {msg.get('type', '?')} message")
    finally:
        sender.disconnect()


if __name__ == "__main__":
    main()
