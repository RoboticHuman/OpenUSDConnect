"""TCP client for sending protocol events to a sync server.

``EventSender`` owns one socket and one ``hello`` handshake per connect
cycle.  ``connect()`` waits for ``hello_ok`` (or surfaces ``auth_rejected``)
before returning, so callers know the server accepted them before the
first event leaves.  ``send_events`` / ``send_message`` return ``False``
on socket error and put the sender into a disconnected state — callers
reconnect from their own tick loop (the protocol is latest-wins for
most events, so dropping during disconnect is correct: the next
successful tick re-snapshots current state).

Token persistence (TOFU) is opt-in: pass an ``on_token_issued`` callback
to receive a freshly issued token on first connect, and pass the saved
token back via the ``token`` argument on the next session for reconnect.
``token_client.load_token`` / ``save_token`` provide a default disk-backed
implementation.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable

from .codec import (
    PayloadType,
    decode_envelope,
    resolve_payload,
)
from .framing import IncompleteRead, MessageTooLarge, recv_framed
from .protocol import (
    make_claim_playback,
    make_hello,
    make_playback_control,
    make_quit,
    make_txn,
)
from .transport import send_msg

LOG = logging.getLogger(__name__)

_HANDSHAKE_TIMEOUT_S = 10.0


class EventSender:
    """Synchronous TCP sender for the OpenUSDConnect wire protocol."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        client_id: str,
        role: str = "emitter",
        origin: str | None = None,
        department: str | None = None,
        token: str | None = None,
        handshake_timeout: float = _HANDSHAKE_TIMEOUT_S,
        on_token_issued: Callable[[str], None] | None = None,
        on_stage_metadata: Callable[[dict], None] | None = None,
    ):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.role = role
        self.origin = origin
        self.department = department
        self.token = token
        self.handshake_timeout = handshake_timeout
        self._on_token_issued = on_token_issued
        self._on_stage_metadata = on_stage_metadata
        self.sock: socket.socket | None = None
        self.auth_rejected: bool = False
        self.stage_metadata: dict = {}

    @property
    def is_connected(self) -> bool:
        return self.sock is not None

    @property
    def connected(self) -> bool:
        """Alias for :attr:`is_connected` (matches ``ReceiverThread`` API)."""
        return self.sock is not None

    def connect(self) -> bool:
        """Open socket, send hello, wait for hello_ok or auth_rejected.

        Returns ``True`` on successful handshake.  On auth rejection sets
        :attr:`auth_rejected` and returns ``False`` (caller should not
        retry with the same token).  On connection failure returns
        ``False`` and the sender stays disconnected.
        """
        if self.sock is not None:
            return True

        self.auth_rejected = False
        try:
            self.sock = socket.create_connection(
                (self.host, self.port),
                timeout=self.handshake_timeout,
            )
            send_msg(
                self.sock,
                make_hello(
                    self.role,
                    client_id=self.client_id,
                    origin=self.origin,
                    department=self.department,
                    token=self.token,
                ),
            )
            buf = recv_framed(self.sock)
        except (OSError, IncompleteRead, MessageTooLarge):
            LOG.exception("EventSender: handshake failed")
            self._close()
            return False

        env = decode_envelope(buf)
        pt = env.PayloadType()

        if pt == PayloadType.AuthRejected:
            _, ar = resolve_payload(env)
            reason = ar.Reason()
            if isinstance(reason, bytes):
                reason = reason.decode("utf-8")
            LOG.error("EventSender: auth rejected — %s", reason)
            self.auth_rejected = True
            self._close()
            return False

        if pt != PayloadType.HelloOk:
            LOG.error("EventSender: unexpected response type %s", pt)
            self._close()
            return False

        _, ho = resolve_payload(env)
        issued = ho.Token()
        if issued:
            if isinstance(issued, bytes):
                issued = issued.decode("utf-8")
            self.token = issued
            if self._on_token_issued:
                self._on_token_issued(issued)
            LOG.info("EventSender: token issued by server")

        from .codec import _decode_stage_metadata_table

        sm = ho.StageMetadata()
        if sm is not None:
            meta = _decode_stage_metadata_table(sm)
            if meta:
                self.stage_metadata = meta
                if self._on_stage_metadata:
                    self._on_stage_metadata(meta)

        # Handshake complete — clear the timeout so subsequent sends use
        # the OS default (blocking).  Without this the timeout would apply
        # to every send and short-circuit slow networks.
        self.sock.settimeout(None)
        LOG.info("EventSender connected to %s:%d", self.host, self.port)
        return True

    def disconnect(self) -> None:
        """Send ``quit`` and close the socket.  Idempotent."""
        if self.sock is None:
            return
        try:
            send_msg(self.sock, make_quit())
        except OSError:
            pass
        self._close()

    def send_events(self, events: list) -> bool:
        """Wrap *events* in a ``txn`` and send.  Returns success.

        On socket error, marks the sender disconnected and returns
        ``False``; the caller is expected to reconnect from its next tick.
        """
        if self.sock is None or not events:
            return False
        try:
            send_msg(self.sock, make_txn(self.client_id, events))
            return True
        except OSError:
            LOG.exception("EventSender: failed to send events")
            self._close()
            return False

    def send_message(self, msg: dict) -> bool:
        """Send a raw protocol message dict (compact, create_proposal, …).

        Returns success.  Same disconnection semantics as :meth:`send_events`.
        """
        if self.sock is None:
            return False
        try:
            send_msg(self.sock, msg)
            return True
        except OSError:
            LOG.exception("EventSender: failed to send message")
            self._close()
            return False

    def claim_playback(self, time: float | None = None) -> bool:
        """Request the playback-leader role from the server.

        ``time`` (optional) is the claimer's current timecode; the server
        applies it as the shared playhead atomically with the grant.
        """
        return self.send_message(make_claim_playback(self.client_id, time=time))

    def send_playback_control(
        self,
        action: str,
        *,
        time: float | None = None,
        rate: float | None = None,
    ) -> bool:
        """Send a playback control command. Server rejects if not the leader."""
        return self.send_message(make_playback_control(action, time=time, rate=rate))

    def _close(self) -> None:
        if self.sock is None:
            return
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = None


__all__ = ["EventSender"]
