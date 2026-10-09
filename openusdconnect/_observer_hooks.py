"""Translate a ClientObserver into the callables the low-level endpoints take."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .client_observer import (
    AppliedBatch,
    ClientObserver,
    PlaybackClaim,
    PlaybackState,
    StageMetadata,
)

_STAGE_METADATA_FIELDS = {
    "timeCodesPerSecond": "time_codes_per_second",
    "framesPerSecond": "frames_per_second",
    "startTimeCode": "start_time_code",
    "endTimeCode": "end_time_code",
    "metersPerUnit": "meters_per_unit",
    "upAxis": "up_axis",
}


def stage_metadata_from_message(message: dict) -> StageMetadata:
    return StageMetadata(**{
        field: message[key] for key, field in _STAGE_METADATA_FIELDS.items() if key in message
    })


def _playback_state(message: dict) -> PlaybackState:
    return PlaybackState(
        playing=bool(message["playing"]),
        time=float(message["time"]),
        rate=float(message["rate"]),
        leader_client_id=message["leader_client_id"],
    )


def _claim_granted(message: dict) -> PlaybackClaim:
    return PlaybackClaim(granted=True, leader_client_id=message["leader_client_id"])


def _claim_rejected(message: dict) -> PlaybackClaim:
    return PlaybackClaim(
        granted=False,
        leader_client_id=message["current_leader_client_id"],
        reason=message["reason"],
    )


@dataclass(frozen=True, slots=True)
class ObserverHooks:
    """Callables for the observer methods a subclass overrides, else ``None``."""

    on_applied: Callable[[int, list], None] | None = None
    on_resync: Callable[[], None] | None = None
    on_token_issued: Callable[[str], None] | None = None
    on_stage_metadata: Callable[[dict], None] | None = None
    on_playback_state: Callable[[dict], None] | None = None
    on_playback_claimed: Callable[[dict], None] | None = None
    on_playback_rejected: Callable[[dict], None] | None = None

    def receiver_callbacks(self) -> dict[str, Callable | None]:
        """Keyword arguments for ``EventReceiver``."""
        return {
            "on_stage_metadata": self.on_stage_metadata,
            "on_playback_state": self.on_playback_state,
            "on_playback_claimed": self.on_playback_claimed,
            "on_playback_rejected": self.on_playback_rejected,
        }

    def applied_events_for(self, dispatcher) -> Callable[[list], None] | None:
        """The ``on_applied_events`` callback reporting batches of *dispatcher*."""
        on_applied = self.on_applied
        if on_applied is None:
            return None
        return lambda events: on_applied(dispatcher.applying_seq, events)


def observer_hooks(
    observer: ClientObserver | None,
    notify: Callable[[Callable], Callable],
) -> ObserverHooks:
    """Hooks for *observer*; *notify* defers a notification until ``update()``."""
    if observer is None:
        return ObserverHooks()
    if not isinstance(observer, ClientObserver):
        raise TypeError("observer must be a ClientObserver")

    def overrides(name: str) -> bool:
        return getattr(type(observer), name) is not getattr(ClientObserver, name)

    claims = overrides("on_playback_claim")
    # Delivery methods already run inside update(); notifications arrive on
    # network threads and go through notify.
    return ObserverHooks(
        on_applied=(
            (lambda seq, events: observer.on_applied(AppliedBatch(seq, events)))
            if overrides("on_applied") else None
        ),
        on_resync=observer.on_resync if overrides("on_resync") else None,
        on_token_issued=(
            notify(observer.on_token_issued) if overrides("on_token_issued") else None
        ),
        on_stage_metadata=(
            notify(lambda m: observer.on_stage_metadata(stage_metadata_from_message(m)))
            if overrides("on_stage_metadata") else None
        ),
        on_playback_state=(
            notify(lambda m: observer.on_playback_state(_playback_state(m)))
            if overrides("on_playback_state") else None
        ),
        on_playback_claimed=(
            notify(lambda m: observer.on_playback_claim(_claim_granted(m))) if claims else None
        ),
        on_playback_rejected=(
            notify(lambda m: observer.on_playback_claim(_claim_rejected(m))) if claims else None
        ),
    )
