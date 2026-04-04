"""TCP send/recv helpers and connection management.

JSON Lines protocol: one JSON object per line, newline-delimited.
Transport layer is abstracted so WebSocket/UDP could be swapped in later.
"""

from __future__ import annotations

import json
import socket


def send_line(sock: socket.socket, obj: dict) -> None:
    """Serialize obj as JSON + newline and send over socket."""
    sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))
