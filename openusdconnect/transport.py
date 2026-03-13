"""TCP send/recv helpers and connection management.

JSON Lines protocol: one JSON object per line, newline-delimited.
Transport layer is abstracted so WebSocket/UDP could be swapped in later.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Generator


def send_line(sock: socket.socket, obj: dict) -> None:
    """Serialize obj as JSON + newline and send over socket."""
    sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))


def recv_lines(sock: socket.socket, bufsize: int = 4096) -> Generator[dict, None, None]:
    """Generator yielding parsed JSON objects from a TCP stream.

    Reads from socket in chunks, splits on newlines, yields parsed dicts.
    Stops when the connection is closed (recv returns empty bytes).
    """
    buf = b""
    while True:
        data = sock.recv(bufsize)
        if not data:
            break
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if line:
                yield json.loads(line.decode("utf-8"))


def recv_lines_from_file(fileobj) -> Generator[dict, None, None]:
    """Generator yielding parsed JSON objects from a file-like object (socket.makefile).

    Useful for threaded receivers that prefer readline() over raw recv().
    """
    while True:
        line = fileobj.readline()
        if not line:
            break
        line = line.strip()
        if line:
            yield json.loads(line)
