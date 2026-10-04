"""Helpers shared by the high-level clients."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from . import _client_backend
from .client_types import ClientPhase, ClientStatus
from .sender import TransactionRejectedError

if TYPE_CHECKING:
    from .receiver import ReceiverThread

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


class ClientCallbackQueue:
    """Deliver notifications raised on network threads during update() or close()."""

    def __init__(self):
        self._queue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._closed = False

    def wrap(self, callback: Callable) -> Callable:
        def enqueue(value):
            with self._lock:
                if not self._closed:
                    self._queue.put((callback, value))

        return enqueue

    def drain(self) -> None:
        # Only notifications queued before this tick, so a busy receiver cannot
        # starve update().
        for _ in range(self._queue.qsize()):
            try:
                callback, value = self._queue.get_nowait()
            except queue.Empty:
                break
            callback(value)

    def close(self) -> None:
        """Refuse new notifications and deliver the queued ones.

        Every queued notification runs even if one raises; the first error is
        re-raised after the rest have been delivered.
        """
        with self._lock:
            self._closed = True
        error = None
        while True:
            try:
                callback, value = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback(value)
            except Exception as exc:
                if error is not None:
                    LOG.exception("Observer notification failed while closing")
                error = error or exc
        if error is not None:
            raise error


def raise_if_rejected(endpoint, role: str) -> None:
    if endpoint.auth_rejected:
        raise PermissionError(f"{role} authentication rejected")
    if endpoint.hello_rejected:
        raise ConnectionError(endpoint.rejection_reason or f"{role} connection rejected")


def stop_receiver(receiver: ReceiverThread) -> None:
    receiver.stop()
    if receiver.is_alive() and receiver is not threading.current_thread():
        receiver.join(timeout=2.0)
        if receiver.is_alive():
            LOG.warning("Receiver thread did not stop within 2 seconds")
