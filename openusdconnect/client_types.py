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
    """Immutable client state for UI polling, taken on the stage-owning thread.

    ``connected`` means every role the client has is connected; a
    directional field is ``None`` when that role is absent.
    ``acknowledged_events_total`` is cumulative for the lifetime of the client
    instance. Connection and replay readiness do not imply that every
    submitted edit is durable. ``edit_target_is_published`` is ``None`` for
    clients that publish from any edit target or do not publish.
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
    auth_rejected: bool = False
    has_unsent_changes: bool = False
    deferred_events: int = 0
    deferred_layer_keys: tuple[str, ...] = ()
    edit_target_is_published: bool | None = None
    recovery_stage_pending: bool = False

    @property
    def can_author(self) -> bool:
        """Whether edits to the current edit target will be published now."""
        return (
            self.phase is ClientPhase.READY
            and self.sender_connected is not None
            and self.edit_target_is_published is not False
        )


@dataclass(frozen=True, slots=True)
class SyncUpdate:
    """Work completed by one client update call; read ``client.status`` for state.

    ``acknowledged_events_delta`` is consumed by this update and therefore
    is not a cumulative counter.
    """

    applied_events: int
    submitted_events: int
    acknowledged_events_delta: int = 0
    pending_events: int = 0
    recovery: RecoveryIncident | None = None


__all__ = ["ClientPhase", "ClientStatus", "SyncUpdate"]
