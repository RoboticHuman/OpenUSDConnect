"""Platform-aware socket helpers (send-only timeout, keepalive tuning)."""

from __future__ import annotations

import logging
import socket
import struct
import sys

LOG = logging.getLogger(__name__)


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
    #   macOS:        { time_t tv_sec; suseconds_t tv_usec; }     → 16 B on 64-bit
    #                 (suseconds_t is int32_t on Darwin, but the struct pads to
    #                 16 B for 8-byte alignment of tv_sec, same layout on arm64
    #                 and x86_64. The kernel rejects any optlen != sizeof(struct
    #                 timeval) with EINVAL, so the trailing pad is required.)
    secs = int(timeout_s)
    usecs = int((timeout_s - secs) * 1_000_000)
    fmt = "@li4x" if sys.platform == "darwin" else "@ll"
    sock.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_SNDTIMEO,
        struct.pack(fmt, secs, usecs),
    )


def _set_keepalive(
    sock: socket.socket,
    *,
    idle_s: int = 30,
    interval_s: int = 10,
    count: int = 3,
) -> None:
    """Enable aggressive TCP keepalive on a socket. Defaults give silent-
    disconnect detection in ~60 s instead of the OS default of ~2 hours.

    Per-option setsockopt failures are logged but don't raise — SO_KEEPALIVE
    is the minimum guarantee, the per-platform tuning is best-effort.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        LOG.debug("SO_KEEPALIVE unsupported on this socket", exc_info=True)
        return

    # macOS uses TCP_KEEPALIVE for the idle threshold; Linux + Windows
    # 3.13+ use TCP_KEEPIDLE. Both expose TCP_KEEPINTVL / TCP_KEEPCNT.
    idle_opt = getattr(socket, "TCP_KEEPIDLE", None) or getattr(
        socket, "TCP_KEEPALIVE", None,
    )
    if idle_opt is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, idle_opt, idle_s)
        except OSError:
            LOG.debug("TCP_KEEPIDLE/KEEPALIVE unsupported", exc_info=True)
    if hasattr(socket, "TCP_KEEPINTVL"):
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval_s)
        except OSError:
            LOG.debug("TCP_KEEPINTVL unsupported", exc_info=True)
    if hasattr(socket, "TCP_KEEPCNT"):
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, count)
        except OSError:
            LOG.debug("TCP_KEEPCNT unsupported", exc_info=True)
