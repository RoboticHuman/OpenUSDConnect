"""Semantics-safe client-side event coalescing helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from time import monotonic

from .protocol_constants import K_SET_XFORM_TRS

_TRS_FIELDS = ("t", "r", "s")


def merge_latest_transform_events(
    pending: list[dict],
    incoming: list[dict],
) -> list[dict]:
    """Append *incoming*, replacing redundant TRS opinions in *pending*.

    Only ``set_xform_trs`` events for the same prim and time code are merged.
    Every other event is an ordering barrier.  This deliberately preserves
    structural operations, distinct animation samples, and edits that may
    change which USD property a later transform targets.

    ``pending`` is updated in place so a :class:`NoticeEmitter` can retain the
    same retry-owned list object until the sender accepts it.
    """
    candidates: dict[tuple[str, float | None], dict] = {}
    for event in pending:
        if event.get("k") != K_SET_XFORM_TRS:
            candidates.clear()
            continue
        candidates[(event["prim"], event.get("time"))] = event

    for event in incoming:
        if event.get("k") != K_SET_XFORM_TRS:
            pending.append(event)
            candidates.clear()
            continue

        key = (event["prim"], event.get("time"))
        previous = candidates.get(key)
        if previous is None:
            pending.append(event)
            candidates[key] = event
            continue

        previous_fields = set(previous.get("fields", ()))
        incoming_fields = set(event.get("fields", ()))
        merged_fields = previous_fields | incoming_fields
        previous["fields"] = [field for field in _TRS_FIELDS if field in merged_fields]
        for field in incoming_fields:
            previous[field] = event[field]

    return pending


def is_transform_only(events: list[dict]) -> bool:
    """Return whether a non-empty batch contains only TRS value events."""
    return bool(events) and all(event.get("k") == K_SET_XFORM_TRS for event in events)


@dataclass(slots=True)
class TransformCoalescingWindow:
    """Opt-in timing policy for an emitter's pre-submission TRS batch."""

    seconds: float = 0.0
    _deadline: float | None = None

    def __post_init__(self) -> None:
        self.seconds = float(self.seconds)
        if not math.isfinite(self.seconds) or self.seconds < 0:
            raise ValueError("transform_coalesce_seconds must be finite and non-negative")

    @property
    def buffering(self) -> bool:
        return self._deadline is not None

    def prepare(self, emitter) -> list[dict]:
        if self.seconds == 0:
            return emitter.prepare_events_for_send()
        events = emitter.prepare_coalesced_transform_events_for_send()
        if not events or not is_transform_only(events):
            self._deadline = None
            return events
        now = monotonic()
        if self._deadline is None:
            self._deadline = now + self.seconds
            return []
        return events if now >= self._deadline else []

    def force(self, emitter) -> list[dict]:
        """Return the currently buffered batch without waiting for its deadline."""
        return emitter.prepare_coalesced_transform_events_for_send()

    def mark_submitted(self) -> None:
        self._deadline = None


__all__ = [
    "TransformCoalescingWindow",
    "is_transform_only",
    "merge_latest_transform_events",
]
