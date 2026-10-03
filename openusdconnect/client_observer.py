"""Host notifications for the high-level clients."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .events import Event
from .protocol_constants import IMPORT_KINDS


class AppliedBatch:
    """Authoritative events applied by one delivery.

    ``seq`` is the sequence at the end of the drain that applied the batch;
    every batch of one drain shares it. With an adapter-backed receiver,
    ``events`` are the projected adapter events.
    """

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
    """Override the notifications a host needs; no method runs on a network thread.

    Delivery methods run while the client applies authoritative state: raising
    from one in ``update()`` rolls the batch back and replays it, and stage
    edits made in them are not published. Notification methods only observe
    and arrive in ``update()`` or ``close()``: raising propagates, and later
    notifications wait for the next call. A client calls only the methods it
    supports and a subclass overrides.
    """

    def on_applied(self, batch: AppliedBatch) -> None:
        """Delivery (ManagedClient, UsdReceiver): authoritative events were applied."""

    def on_resync(self) -> None:
        """Delivery (ManagedClient, UsdReceiver): the stream restarted; reset host state."""

    def on_stage_metadata(self, metadata: StageMetadata) -> None:
        """Notification: stage settings received with a handshake."""

    def on_playback_state(self, state: PlaybackState) -> None:
        """Notification (receiving clients): the shared playhead changed."""

    def on_playback_claim(self, result: PlaybackClaim) -> None:
        """Notification (receiving clients): the server answered ``claim_playback()``."""

    def on_token_issued(self, token: str) -> None:
        """Notification: the server issued a credential for host-owned storage."""


__all__ = [
    "AppliedBatch",
    "ClientObserver",
    "PlaybackClaim",
    "PlaybackState",
    "StageMetadata",
]
