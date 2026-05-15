"""Receive-and-apply pipeline orchestrator.

``EventDispatcher`` composes the receive→apply cycle that any integration
running an emitter alongside a receiver needs:

  1. Drain the receiver queue (raw FlatBuffers frames)
  2. Decode → event dicts; handle resync, sequence dedup, errors
  3. Suppress the emitter (if provided) for the rest of the cycle
  4. Skip-detect arc events whose composed state already matches
  5. Commit stage-affecting events to the mirror stage atomically (if any)
  6. Dispatch every non-skipped event to the adapter
  7. Invalidate emitter diff caches against the mutated stage
  8. Notify ``on_imported`` for newly-imported prim paths

Integrations call ``drain_and_apply()`` from their own tick / idle / event
loop callback.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import nullcontext
from typing import TYPE_CHECKING

from .codec import decode_messages
from .protocol_constants import (
    K_LOAD_PAYLOAD,
    K_SET_MATERIAL_BINDING,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_SHADER_CONNECTION,
    K_SET_SHADER_INPUT,
    K_SET_VARIANT_SELECTIONS,
    K_UNLOAD_PAYLOAD,
)

if TYPE_CHECKING:
    from pxr import Usd

    from .adapters import DCCAdapter
    from .emitter import NoticeEmitter
    from .receiver import ReceiverThread

LOG = logging.getLogger(__name__)


# Events that mutate the USD stage (composition arcs, materials, shaders).
# Transforms, visibility, and gprim attrs go through the adapter only —
# they don't need a separate mirror commit because they target whatever the
# adapter wraps (objects, components, etc.) rather than USD scene description.
STAGE_SYNC_KINDS = frozenset(
    {
        K_SET_REFERENCE,
        K_SET_PAYLOAD,
        K_LOAD_PAYLOAD,
        K_UNLOAD_PAYLOAD,
        K_SET_VARIANT_SELECTIONS,
        K_SET_MATERIAL_BINDING,
        K_SET_SHADER_INPUT,
        K_SET_SHADER_CONNECTION,
    }
)

# Event kinds whose application imports new content into the consumer.
# Used to trigger post-import work (cache warmup, viewport refresh) via the
# ``on_imported`` callback.
IMPORT_KINDS = frozenset({K_LOAD_PAYLOAD, K_SET_REFERENCE})

# Event kinds where the apply path (``ClearReferences()`` + re-add, or
# variant set + select) triggers spurious recomposition even when the
# final state is identical to the incoming event.  Skipping them when
# the arc on the stage already matches avoids unnecessary re-imports
# and composition churn.  The skip check reads pre-commit state — see
# ``_compute_stage_skip`` and ``_arc_changed``.
ARC_KINDS = frozenset({K_SET_REFERENCE, K_SET_PAYLOAD, K_SET_VARIANT_SELECTIONS})


class EventDispatcher:
    """Drives one receive→apply cycle for an integration."""

    def __init__(
        self,
        *,
        receiver: ReceiverThread,
        adapter: DCCAdapter,
        mirror_stage: Usd.Stage | None = None,
        emitter: NoticeEmitter | None = None,
        on_imported: Callable[[list[str]], None] | None = None,
        on_resync: Callable[[], None] | None = None,
    ):
        """
        Args:
            receiver: Background thread holding the inbound message queue.
            adapter: Where to dispatch events.  Use ``UsdStageAdapter``
                when the integration's scene representation IS a
                ``Usd.Stage``; subclass ``DCCAdapter`` otherwise.
            mirror_stage: Optional separate USD stage that should reflect
                stage-affecting events.  Provide one when the adapter's
                scene representation is NOT itself a ``Usd.Stage`` and
                the integration also runs an emitter — the mirror is what
                the emitter watches.  Leave ``None`` when the adapter
                already writes to the stage (no separate mirror needed).
            emitter: Optional ``NoticeEmitter`` to suppress during apply
                and invalidate after.  Required to avoid feedback loops
                whenever the integration also emits.
            on_imported: Called with the list of prim paths that just
                imported new content (load_payload, set_reference).  Use
                for post-import work (seed caches, refresh viewport).
                Fires inside the suppress block, before ``drain_and_apply``
                returns; defer any work that must observe post-tick
                evaluation state to after the call returns.
            on_resync: Called when the server requests a resync.  The
                dispatcher already resets ``last_seq``; the callback is
                where to reset adapter / scene state.
        """
        self.receiver = receiver
        self.adapter = adapter
        self.mirror_stage = mirror_stage
        self.emitter = emitter
        self.on_imported = on_imported
        self.on_resync = on_resync
        self._last_seq = 0

    @property
    def last_seq(self) -> int:
        return self._last_seq

    @last_seq.setter
    def last_seq(self, value: int) -> None:
        self._last_seq = value

    def drain_and_apply(self) -> int:
        """Drain the receiver queue and run the apply pipeline.

        Returns the number of events applied (0 when the queue was empty
        or only contained pings/resync).
        """
        bufs = self.receiver.drain_queue()
        if not bufs:
            return 0

        result = decode_messages(bufs, last_seq=self._last_seq, clear_on_resync=True)
        if result.resync_requested and self.on_resync is not None:
            self.on_resync()
        for exc in result.errors:
            LOG.warning("Decode error: %s", exc)
        self._last_seq = result.last_seq

        events = result.received
        if not events:
            return 0

        return self._apply(events)

    def _apply(self, events: list[dict]) -> int:
        """Run the apply pipeline on a pre-decoded batch.

        Stage-first ordering: skip-detect → mirror commit → adapter
        dispatch → invalidate → on_imported.  The stage commit happens
        BEFORE the adapter dispatch so that if ``atomic_apply`` raises,
        the adapter is never touched — the consumer scene stays
        untouched on a failed batch.  This is the "stage-first"
        guarantee.
        """
        from .event_apply import apply_events, atomic_apply

        suppress_ctx = self.emitter.suppressed() if self.emitter else nullcontext()
        with suppress_ctx:
            # Skip decisions are computed against PRE-commit state so the
            # comparison is meaningful.  Both the mirror commit and the
            # adapter dispatch consume the skip sets — see ARC_KINDS for
            # why we bother.
            stage_skip = self._compute_stage_skip(events)
            adapter_skip = self._compute_adapter_skip(events, stage_skip)

            # 1. Mirror commit — atomic batch into a separate USD stage.
            #    Only stage-affecting kinds (composition arcs, materials,
            #    shaders) need to be reflected on the mirror; transforms
            #    and gprim attrs travel through the adapter only.
            if self.mirror_stage is not None:
                stage_events = [
                    ev for i, ev in enumerate(events)
                    if i not in stage_skip and ev.get("k") in STAGE_SYNC_KINDS
                ]
                if stage_events:
                    with atomic_apply(self.mirror_stage):
                        apply_events(self.mirror_stage, stage_events)

            # 2. Adapter dispatch — every non-skipped event.  Reached
            #    only after the mirror commit succeeded, so adapters can
            #    rely on the mirror reflecting the events about to apply.
            for i, ev in enumerate(events):
                if i in adapter_skip:
                    continue
                self.adapter.apply_event(ev)

            # 3. Emitter cache invalidation — re-syncs the per-prim diff
            #    cache with the just-mutated stage.  Without this the
            #    next emit cycle would compare current stage state to a
            #    stale cache and re-emit a change the server already
            #    knows about (a feedback loop).
            if self.emitter is not None:
                for i, ev in enumerate(events):
                    if i in adapter_skip:
                        continue
                    self.emitter.invalidate_for_event(ev)

            # 4. Post-import callback — fires for events that brought
            #    new content into the consumer (load_payload,
            #    set_reference) so the integration can run post-import
            #    work (cache warmup, viewport refresh, etc.).
            if self.on_imported is not None:
                imported = [
                    ev["prim"]
                    for i, ev in enumerate(events)
                    if i not in adapter_skip
                    and ev.get("k") in IMPORT_KINDS
                    and ev.get("prim")
                ]
                if imported:
                    self.on_imported(imported)

        return len(events)

    def _compute_stage_skip(self, events: list[dict]) -> set[int]:
        """Find arc events whose composed state already matches the stage.

        Reads pre-commit state to avoid unnecessary re-imports.  Unchanged
        arcs are excluded from the mirror commit too — ``ClearReferences()``
        followed by re-adding identical references triggers recomposition
        even when the final state is identical.
        """
        if self.mirror_stage is None:
            return set()
        skip: set[int] = set()
        for i, ev in enumerate(events):
            k = ev.get("k")
            if k in ARC_KINDS and not self._arc_changed(ev, k):
                skip.add(i)
        return skip

    def _compute_adapter_skip(self, events: list[dict], stage_skip: set[int]) -> set[int]:
        """Skip adapter dispatch for arcs already imported on the consumer side.

        Stage idempotence and consumer idempotence are not the same: a
        USD arc can already match before the consumer has imported the
        corresponding scene content.  Only skip adapter dispatch when
        ``adapter.has_imported_children`` confirms the children are
        present consumer-side.  Variant selections still dispatch
        because they may need to rebuild imported children for the
        selection.
        """
        skip: set[int] = set()
        for i in stage_skip:
            ev = events[i]
            if ev.get("k") == K_SET_VARIANT_SELECTIONS:
                continue
            if self.adapter.has_imported_children(ev.get("prim", "")):
                skip.add(i)
        return skip

    def _arc_changed(self, ev: dict, kind: str) -> bool:
        """Return True if an arc event differs from the mirror stage."""
        from .emitter import (
            read_payloads,
            read_references,
            read_variant_selections,
        )

        prim_path = ev.get("prim", "")

        def _norm(arcs):
            return [(p.replace("\\", "/"), r) for p, r in arcs]

        if kind == K_SET_REFERENCE:
            current = _norm(read_references(self.mirror_stage, prim_path))
            incoming = _norm(
                [(e.get("asset_path", ""), e.get("prim_path", "")) for e in ev.get("refs", [])]
            )
            return current != incoming
        if kind == K_SET_PAYLOAD:
            current = _norm(read_payloads(self.mirror_stage, prim_path))
            incoming = _norm(
                [
                    (e.get("asset_path", ""), e.get("prim_path", ""))
                    for e in ev.get("payloads", [])
                ]
            )
            return current != incoming
        if kind == K_SET_VARIANT_SELECTIONS:
            current = dict(read_variant_selections(self.mirror_stage, prim_path))
            incoming = ev.get("selections", {})
            return current != incoming
        return True


__all__ = [
    "ARC_KINDS",
    "EventDispatcher",
    "IMPORT_KINDS",
    "STAGE_SYNC_KINDS",
]
