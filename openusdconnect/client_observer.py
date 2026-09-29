"""Host notifications for the high-level clients."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .events import Event
from .protocol_constants import IMPORT_KINDS


class AppliedBatch:
    """Authoritative events applied by one delivery."""

    __slots__ = ("seq", "events", "_prim_paths", "_imported_paths")

    def __init__(self, seq: int, events: Sequence[Event]):
        self.seq = seq
        self.events = events
        self._prim_paths: tuple[str, ...] | None = None
        self._imported_paths: tuple[str, ...] | None = None

    @property
    def prim_paths(self) -> tuple[str, ...]:
        """Sorted unique prim paths the batch touched."""
        if self._prim_paths is None:
            self._prim_paths = tuple(sorted({e["prim"] for e in self.events if e.get("prim")}))
        return self._prim_paths

    @property
    def imported_paths(self) -> tuple[str, ...]:
        """Prims whose references or payloads may have brought in new content."""
        if self._imported_paths is None:
            self._imported_paths = tuple(
                e["prim"] for e in self.events if e.get("k") in IMPORT_KINDS and e.get("prim")
            )
        return self._imported_paths


@dataclass(frozen=True, slots=True)
class StageMetadata:
    """Stage-level settings; ``None`` where the server has no authored opinion."""

    time_codes_per_second: float | None = None
    frames_per_second: float | None = None
    start_time_code: float | None = None
    end_time_code: float | None = None
    meters_per_unit: float | None = None
    up_axis: str | None = None


@dataclass(frozen=True, slots=True)
class PlaybackState:
    playing: bool
    time: float
    rate: float
    leader_client_id: str


@dataclass(frozen=True, slots=True)
class PlaybackClaim:
    """Reply to ``claim_playback()``."""

    granted: bool
    leader_client_id: str
    reason: str = ""


class ClientObserver:
    """Override the notifications a host needs; every method runs inside ``update()``.

    Delivery methods are part of applying authoritative state: raising rolls
    the batch back and replays it. Notification methods only observe: raising
    propagates out of ``update()`` and later notifications wait for the next
    call. The client wires only the methods a subclass overrides.
    """

    def on_applied(self, batch: AppliedBatch) -> None:
        """Delivery: a batch of authoritative events has been applied."""

    def on_resync(self) -> None:
        """Delivery: the server restarted the stream; reset derived host state."""

    def on_stage_metadata(self, metadata: StageMetadata) -> None:
        """Notification: stage settings received with a handshake."""

    def on_playback_state(self, state: PlaybackState) -> None:
        """Notification: the shared playhead changed."""

    def on_playback_claim(self, result: PlaybackClaim) -> None:
        """Notification: the server answered ``claim_playback()``."""

    def on_token_issued(self, token: str) -> None:
        """Notification: the server issued a credential for host-owned storage."""


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


def _overrides(observer: ClientObserver, name: str) -> bool:
    return getattr(type(observer), name) is not getattr(ClientObserver, name)


def observer_callbacks(
    observer: ClientObserver | None,
    notify: Callable[[Callable], Callable],
    applying_seq: Callable[[], int],
) -> dict[str, Callable]:
    """Low-level callables for the methods *observer* overrides.

    *notify* queues a notification for delivery during ``update()``.
    """
    if observer is None:
        return {}
    if not isinstance(observer, ClientObserver):
        raise TypeError("observer must be a ClientObserver")
    callbacks: dict[str, Callable] = {}
    if _overrides(observer, "on_applied"):
        callbacks["on_applied_events"] = lambda events: observer.on_applied(
            AppliedBatch(applying_seq(), events)
        )
    if _overrides(observer, "on_resync"):
        callbacks["on_resync"] = observer.on_resync
    if _overrides(observer, "on_stage_metadata"):
        callbacks["on_stage_metadata"] = notify(
            lambda message: observer.on_stage_metadata(stage_metadata_from_message(message))
        )
    if _overrides(observer, "on_playback_state"):
        callbacks["on_playback_state"] = notify(
            lambda message: observer.on_playback_state(_playback_state(message))
        )
    if _overrides(observer, "on_playback_claim"):
        callbacks["on_playback_claimed"] = notify(
            lambda message: observer.on_playback_claim(_claim_granted(message))
        )
        callbacks["on_playback_rejected"] = notify(
            lambda message: observer.on_playback_claim(_claim_rejected(message))
        )
    if _overrides(observer, "on_token_issued"):
        callbacks["on_token_issued"] = notify(observer.on_token_issued)
    return callbacks


__all__ = [
    "AppliedBatch",
    "ClientObserver",
    "PlaybackClaim",
    "PlaybackState",
    "StageMetadata",
]
