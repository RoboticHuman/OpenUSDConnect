"""Shared transport lifecycle operations; no stage or layer policy."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from ._client_utils import resolve_client_token

if TYPE_CHECKING:
    from .receiver import ReceiverThread
    from .sender import EventSender

LOG = logging.getLogger(__name__)


def deadline_after(timeout: float | None) -> float | None:
    return None if timeout is None else time.monotonic() + max(timeout, 0.0)


def remaining_time(deadline: float | None) -> float | None:
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def raise_if_rejected(endpoint, role: str) -> None:
    if endpoint.auth_rejected:
        raise PermissionError(f"{role} authentication rejected")
    if endpoint.hello_rejected:
        raise ConnectionError(endpoint.rejection_reason or f"{role} connection rejected")


def prepare_sender_token(
    sender: EventSender,
    receiver: ReceiverThread | None,
    *,
    host: str,
    port: int,
    persist_token: bool,
) -> None:
    """Fill missing sender credentials; issued tokens are shared by callbacks."""
    if sender.token is not None:
        return
    token = receiver.token if receiver is not None else None
    if token is None:
        token = resolve_client_token(host, port, None, persist_token)
    # A handshake can supply a token while stored credentials are being read.
    if sender.token is None:
        sender.token = token


def stop_receiver(receiver: ReceiverThread) -> None:
    receiver.stop()
    if receiver.is_alive() and receiver is not threading.current_thread():
        receiver.join(timeout=2.0)
        if receiver.is_alive():
            LOG.warning("Receiver thread did not stop within 2 seconds")
