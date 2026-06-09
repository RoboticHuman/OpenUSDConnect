"""Length-prefixed binary framing for FlatBuffers messages.

Wire format per message:
    [4 bytes: uint32 big-endian payload length][N bytes: payload]

Provides send/recv helpers for both raw sockets and buffered file objects
(socketserver's StreamRequestHandler exposes self.rfile / self.wfile).

Slow-loris mitigation: per-message deadlines are enforced by the server's
socket timeout (``settimeout``).  A client that drip-feeds bytes will
trigger ``TimeoutError`` in ``_recv_exact``, which the server read loop
catches and handles (disconnect or retry).
"""

from __future__ import annotations

import struct

_HEADER = struct.Struct(">I")  # 4-byte big-endian unsigned int
_HEADER_SIZE = _HEADER.size
MAX_MESSAGE_SIZE = 16 * 1024 * 1024  # 16 MiB — same limit as the old _MAX_LINE_SIZE


class MessageTooLarge(Exception):
    """Raised when a message exceeds MAX_MESSAGE_SIZE."""


class IncompleteRead(Exception):
    """Raised when the connection closes mid-message."""


# ---------------------------------------------------------------------------
# Socket-based send / recv
# ---------------------------------------------------------------------------

# Writing the header and payload separately is the write-write-read pattern
# that triggers Nagle + delayed-ACK stalls (tens of ms) on small messages.
# Coalescing into one sendall keeps a small message in a single segment;
# above the limit the concat copy costs more than the second syscall.
_COALESCE_LIMIT = 64 * 1024


def send_framed(sock, payload: bytes) -> None:
    """Send a length-prefixed message over a socket."""
    header = _HEADER.pack(len(payload))
    if len(payload) <= _COALESCE_LIMIT:
        sock.sendall(header + payload)
    else:
        sock.sendall(header)
        sock.sendall(payload)


def recv_framed(sock, *, max_size: int = MAX_MESSAGE_SIZE) -> bytes:
    """Read one length-prefixed message from a socket.

    Returns the payload bytes (without the length header).
    Raises IncompleteRead on clean disconnect or partial header.
    Raises MessageTooLarge if the declared length exceeds *max_size*.
    """
    header = _recv_exact(sock, _HEADER_SIZE)
    length = _HEADER.unpack(header)[0]
    if length > max_size:
        raise MessageTooLarge(f"message length {length} exceeds limit {max_size}")
    return _recv_exact(sock, length)


def _recv_exact(sock, n: int) -> bytes:
    """Read exactly *n* bytes from *sock*, or raise IncompleteRead."""
    buf = bytearray(n)
    view = memoryview(buf)
    pos = 0
    while pos < n:
        nbytes = sock.recv_into(view[pos:])
        if not nbytes:
            raise IncompleteRead(f"expected {n} bytes, got {pos}")
        pos += nbytes
    return bytes(buf)


# ---------------------------------------------------------------------------
# File-object-based send / recv  (for socketserver StreamRequestHandler)
# ---------------------------------------------------------------------------

def send_framed_wfile(wfile, payload: bytes) -> None:
    """Write a length-prefixed message to a buffered wfile."""
    wfile.write(_HEADER.pack(len(payload)))
    wfile.write(payload)
    wfile.flush()


def recv_framed_rfile(rfile, *, max_size: int = MAX_MESSAGE_SIZE) -> bytes:
    """Read one length-prefixed message from a buffered rfile.

    Returns the payload bytes.
    Raises IncompleteRead on short read.
    Raises MessageTooLarge if the declared length exceeds *max_size*.
    """
    header = rfile.read(_HEADER_SIZE)
    if len(header) < _HEADER_SIZE:
        raise IncompleteRead(f"header short read: got {len(header)} bytes")
    length = _HEADER.unpack(header)[0]
    if length > max_size:
        raise MessageTooLarge(f"message length {length} exceeds limit {max_size}")
    data = rfile.read(length)
    if len(data) < length:
        raise IncompleteRead(f"payload short read: expected {length}, got {len(data)}")
    return bytes(data)


# ---------------------------------------------------------------------------
# Batch framing — frame multiple payloads into a single bytes blob
# ---------------------------------------------------------------------------

def frame_batch(payloads: list[bytes]) -> bytes:
    """Frame multiple payloads into a single contiguous bytes object.

    Useful for broadcast: serialize once, send the batch to each receiver
    with a single sendall() call.
    """
    total = sum(len(p) + _HEADER_SIZE for p in payloads)
    buf = bytearray(total)
    offset = 0
    for p in payloads:
        _HEADER.pack_into(buf, offset, len(p))
        offset += _HEADER_SIZE
        buf[offset:offset + len(p)] = p
        offset += len(p)
    return bytes(buf)
