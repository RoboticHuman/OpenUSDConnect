"""TCP send/recv helpers for the FlatBuffers wire protocol.

Messages are length-prefixed FlatBuffers binaries.  The ``send_msg`` helper
takes a plain dict (the same format the rest of the codebase produces),
encodes it via the codec, and sends with framing.
"""

from __future__ import annotations

import socket

from .codec import encode_message
from .framing import recv_framed, send_framed


def send_msg(sock: socket.socket, obj: dict) -> None:
    """Encode *obj* as FlatBuffers and send length-prefixed over *sock*."""
    send_framed(sock, encode_message(obj))


def send_raw(sock: socket.socket, payload: bytes) -> None:
    """Send a pre-encoded FlatBuffers payload with length-prefix framing."""
    send_framed(sock, payload)


def recv_msg(sock: socket.socket) -> bytes:
    """Read one length-prefixed message from *sock*.  Returns raw FB bytes."""
    return recv_framed(sock)


# Backward-compatible alias will be removed once all callers migrate.
send_line = send_msg
