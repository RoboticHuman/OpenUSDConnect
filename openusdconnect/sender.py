"""TCP client for sending protocol events to a sync server."""

from __future__ import annotations

import logging
import socket

from .protocol import make_hello, make_quit, make_txn
from .transport import send_line

LOG = logging.getLogger(__name__)


class EventSender:
    """Owns one TCP socket and ``hello`` handshake per connect cycle.

    On socket error during ``send_events`` the sender logs and
    disconnects; the caller is expected to reconnect.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        client_id: str,
        role: str = "emitter",
        origin: str | None = None,
    ):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.role = role
        self.origin = origin
        self.sock: socket.socket | None = None

    @property
    def is_connected(self) -> bool:
        return self.sock is not None

    def connect(self) -> None:
        """Open the socket and send the ``hello`` handshake."""
        self.sock = socket.create_connection((self.host, self.port))
        send_line(
            self.sock,
            make_hello(self.role, client_id=self.client_id, origin=self.origin),
        )
        LOG.info("EventSender connected to %s:%d", self.host, self.port)

    def disconnect(self) -> None:
        """Send ``quit`` and close the socket.  Idempotent."""
        if self.sock is None:
            return
        try:
            send_line(self.sock, make_quit())
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = None

    def send_events(self, events: list) -> None:
        """Wrap *events* in a ``txn`` and send.  No-op if disconnected or empty."""
        if self.sock is None or not events:
            return
        try:
            send_line(self.sock, make_txn(self.client_id, events))
        except OSError:
            LOG.exception("Failed to send events")
            self.disconnect()


__all__ = ["EventSender"]
