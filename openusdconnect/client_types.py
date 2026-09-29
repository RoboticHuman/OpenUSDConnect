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
    PARKED = "parked"


@dataclass(frozen=True, slots=True)
class ClientStatus:
    """Immutable client state suitable for application and UI polling.

    A directional connection is ``None`` when that role is not present.
    ``acknowledged_events_total`` is cumulative for the lifetime of the
    client instance, including producer-session recovery. Connection and
    replay readiness do not imply that every submitted edit is durable.

    ``has_unsent_changes`` includes noticed edits not yet prepared for sending.
    ``deferred_events`` and ``deferred_layer_keys`` describe incoming shared
    layer edits waiting for asset resolution, independently of replay readiness.
    ``edit_target_is_shared`` describes the current target's publication scope,
    not permission to author while disconnected or recovering; it is ``None``
    for clients that publish whatever the edit target is, or do not publish.
    A pending shared recovery replacement is exposed by ``recovery_stage_pending``.
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
    has_unsent_changes: bool = False
    deferred_events: int = 0
    deferred_layer_keys: tuple[str, ...] = ()
    edit_target_is_shared: bool | None = None
    recovery_stage_pending: bool = False


@dataclass(frozen=True, slots=True)
class SyncUpdate:
    """Work completed by one client update call; read ``client.status`` for state.

    ``acknowledged_events_delta`` is consumed by this update and therefore
    is not a cumulative counter. Directional clients report zero for the
    direction they do not have.
    """

    applied_events: int
    submitted_events: int
    acknowledged_events_delta: int = 0
    pending_events: int = 0
    recovery: RecoveryIncident | None = None


__all__ = ["ClientPhase", "ClientStatus", "SyncUpdate"]
