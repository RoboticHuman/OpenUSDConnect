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
        # Windows: SO_SNDTIMEO takes a DWORD (4 bytes) in milliseconds
        ms = int(timeout_s * 1000)
        sock.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_SNDTIMEO,
            ms.to_bytes(4, "little"),
        )
    else:
        # Unix/macOS: SO_SNDTIMEO takes struct timeval {long sec, long usec}
        secs = int(timeout_s)
        usecs = int((timeout_s - secs) * 1_000_000)
        sock.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_SNDTIMEO,
            struct.pack("ll", secs, usecs),
        )
