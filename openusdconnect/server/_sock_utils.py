"""Platform-aware socket helpers (send-only timeout)."""

from __future__ import annotations

import socket
import struct
import sys


def _set_send_timeout(sock: socket.socket, timeout_s: float):
    """Set a send-only timeout on a socket (platform-aware).

    Uses SO_SNDTIMEO so only sends are affected — recv stays blocking.
    settimeout() cannot be used here because it sets both send and recv
    timeouts, causing spurious TimeoutError in the read loop.
    """
    if sys.platform == "win32":
        # Windows: SO_SNDTIMEO takes a DWORD (4 bytes) in milliseconds.
        ms = int(timeout_s * 1000)
        sock.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_SNDTIMEO,
            ms.to_bytes(4, "little"),
        )
        return

    # Unix variants take ``struct timeval`` but the field widths differ:
    #   Linux / *BSD: { long tv_sec; long tv_usec; }              → 16 B on 64-bit
    #   macOS:        { time_t tv_sec; suseconds_t tv_usec; }     → 12 B on 64-bit
    #                 (suseconds_t is int32_t on Darwin)
    secs = int(timeout_s)
    usecs = int((timeout_s - secs) * 1_000_000)
    fmt = "@li" if sys.platform == "darwin" else "@ll"
    sock.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_SNDTIMEO,
        struct.pack(fmt, secs, usecs),
    )
