"""Helpers shared by the high-level clients."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from . import _client_backend
from .client_types import ClientPhase, ClientStatus
from .sender import TransactionRejectedError

if TYPE_CHECKING:
    from .receiver import EventReceiver
    from .sender import EventSender

LOG = logging.getLogger(__name__)

DEFAULT_WAIT_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.01

_PHASES = {
    _client_backend.ClientPhase.OFFLINE: ClientPhase.OFFLINE,
    _client_backend.ClientPhase.CONNECTING: ClientPhase.CONNECTING,
    _client_backend.ClientPhase.REPLAYING: ClientPhase.REPLAYING,
    _client_backend.ClientPhase.READY: ClientPhase.READY,
    _client_backend.ClientPhase.RECOVERY_REQUIRED: ClientPhase.RECOVERY_REQUIRED,
    _client_backend.ClientPhase.REJECTED: ClientPhase.REJECTED,
    _client_backend.ClientPhase.CLOSED: ClientPhase.CLOSED,
    _client_backend.ClientPhase.PARKED: ClientPhase.PARKED,
}


def deadline_after(timeout: float | None) -> float | None:
    return None if timeout is None else time.monotonic() + max(timeout, 0.0)


def remaining_time(deadline: float | None) -> float | None:
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def _pause_before_poll(deadline: float | None) -> bool:
    remaining = remaining_time(deadline)
    if remaining is not None and remaining <= 0:
        return False
    time.sleep(_POLL_INTERVAL_S if remaining is None else min(_POLL_INTERVAL_S, remaining))
    return True


def compute_phase(
    *,
    closed: bool,
    recovery_required: bool,
    rejected: bool,
    parked: bool,
    replaying: bool,
    ready: bool,
    connecting: bool,
) -> ClientPhase:
    """The one precedence order every client uses for ``ClientStatus.phase``."""
    return _PHASES[
        _client_backend.compute_phase(
            closed=closed,
            recovery_required=recovery_required,
            rejected=rejected,
            parked=parked,
            replaying=replaying,
            ready=ready,
            connecting=connecting,
        )
    ]


def raise_if_blocked(client, status: ClientStatus) -> None:
    """Raise for a state that further updates cannot resolve."""
    if status.failure is not None:
        raise TransactionRejectedError(status.failure)
    name = type(client).__name__
    if status.phase is ClientPhase.CLOSED:
        raise RuntimeError(f"{name} is closed")
    if status.phase is ClientPhase.REJECTED:
        if status.auth_rejected:
            raise PermissionError(status.reason or f"{name} authentication rejected")
        raise ConnectionError(status.reason or f"{name} connection rejected")
    if status.phase is ClientPhase.OFFLINE:
        raise ConnectionError(status.reason or f"{name} is offline and not reconnecting")
    if status.phase is ClientPhase.PARKED:
        raise RuntimeError(f"{name} has no bound stage; call rebind_stage() first")
    if status.phase is ClientPhase.RECOVERY_REQUIRED:
        raise RuntimeError(status.reason or f"{name} requires recovery")


def wait_until_ready(client, timeout: float | None) -> bool:
    """Pump updates on the calling thread until ready; ``False`` only on timeout."""
    client.start()
    deadline = deadline_after(timeout)
    while True:
        client.update()
        status = client.status
        if status.phase is ClientPhase.READY:
            return True
        raise_if_blocked(client, status)
        if not _pause_before_poll(deadline):
            return False


def submit_and_wait(client, timeout: float | None) -> bool:
    """Submit noticed edits and wait until durable; ``False`` only on timeout."""
    client.start()
    deadline = deadline_after(timeout)
    while True:
        client.update()
        status = client.status
        raise_if_blocked(client, status)
        # flush(0) also releases transform coalescing and must not block the pump.
        if status.phase is ClientPhase.READY and client.flush(timeout=0):
            status = client.status
            if not status.has_unsent_changes and not status.pending_events:
                return True
        if not _pause_before_poll(deadline):
            return False


def raise_if_rejected(endpoint, role: str) -> None:
    if endpoint.auth_rejected:
        raise PermissionError(f"{role} authentication rejected")
    if endpoint.hello_rejected:
        raise ConnectionError(endpoint.rejection_reason or f"{role} connection rejected")


def close_endpoint(endpoint: EventReceiver | EventSender) -> None:
    if not endpoint.close(timeout=2.0):
        LOG.warning("%s did not stop within 2 seconds", type(endpoint).__name__)
