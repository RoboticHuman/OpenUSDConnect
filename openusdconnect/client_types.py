"""Public lifecycle and progress values shared by the client APIs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .recovery import RecoveryIncident, TransactionFailure


class ClientPhase(StrEnum):
    """High-level lifecycle state shared by every client role."""

    OFFLINE = "offline"
    CONNECTING = "connecting"
    REPLAYING = "replaying"
    READY = "ready"
    RECOVERY_REQUIRED = "recovery_required"
    REJECTED = "rejected"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class ClientStatus:
    """Immutable client state suitable for application and UI polling.

    A directional connection is ``None`` when that role is not present.
    ``acknowledged_events_total`` is cumulative for the lifetime of the
    client instance, including producer-session recovery. Connection and
    replay readiness do not imply that every submitted edit is durable.
    """

    phase: ClientPhase
    connected: bool
    synchronized: bool
    receiver_connected: bool | None = None
    sender_connected: bool | None = None
    prepared_events: int = 0
    pending_events: int = 0
    acknowledged_events_total: int = 0
    failure: TransactionFailure | None = None
    recovery: RecoveryIncident | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SyncUpdate:
    """Work completed by one bidirectional client update call.

    ``acknowledged_events_delta`` is consumed by this update and therefore
    is not a cumulative counter.
    """

    applied_events: int
    submitted_events: int
    acknowledged_events_delta: int = 0
    pending_events: int = 0
    recovery: RecoveryIncident | None = None


__all__ = ["ClientPhase", "ClientStatus", "SyncUpdate"]
