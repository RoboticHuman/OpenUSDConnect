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
    ARC_KINDS,
    IMPORT_KINDS,
    K_SET_CONNECTABLE_CONNECTION,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_STAGE_METADATA,
    K_SET_VARIANT_SELECTIONS,
    STAGE_SYNC_KINDS,
)

if TYPE_CHECKING:
    from pxr import Usd

    from .adapters import DCCAdapter
    from .emitter import NoticeEmitter
    from .receiver import ReceiverThread

LOG = logging.getLogger(__name__)


# STAGE_SYNC_KINDS / IMPORT_KINDS / ARC_KINDS are derived from the
# EVENT_KIND_INFO table in protocol_constants and re-exported here for
# integrations that import them from the dispatcher. Their semantics are
# documented on EventKindInfo (stage_sync / imports / arc).


def _stage_sync_scope(events: list[dict]) -> list[str] | None:
    """Every prim path a stage-sync batch may author on, for the scoped
    atomic_apply backup. Returns None when the batch writes outside prim
    scopes (stage metadata), forcing the full-layer snapshot.
    """
    paths: list[str] = []
    for ev in events:
        if ev.get("k") == K_SET_STAGE_METADATA:
            return None
        prim = ev.get("prim")
        if not prim:
            return None
        paths.append(prim)
        if ev.get("k") == K_SET_CONNECTABLE_CONNECTION:
            for conn in ev.get("connections", {}).values():
                source = conn.get("source_prim")
                if source:
                    paths.append(source)
    return paths


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

        # Decode geometry to numpy (zero-copy bulk) rather than per-element
        # Python lists — the list path is ~100x slower to decode+apply for
        # heavy meshes. Adapters that need plain sequences normalize at their
        # own boundary.
        result = decode_messages(
            bufs, last_seq=self._last_seq, numpy_arrays=True, clear_on_resync=True
        )
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

            # Stage-backed adapters that share their Usd.Stage with the
            # mirror don't need the separate commit — apply_events on
            # the adapter already covers the same stage.
            adapter_handles_mirror = (
                self.mirror_stage is not None
                and self.adapter.targets_stage() is self.mirror_stage
            )

            # 1. Mirror commit — atomic batch into the dispatcher's
            #    separate USD stage.
            if self.mirror_stage is not None and not adapter_handles_mirror:
                stage_events = [
                    ev for i, ev in enumerate(events)
                    if i not in stage_skip and ev.get("k") in STAGE_SYNC_KINDS
                ]
                if stage_events:
                    scope = _stage_sync_scope(stage_events)
                    with atomic_apply(self.mirror_stage, prim_paths=scope):
                        apply_events(self.mirror_stage, stage_events)

            # 2. Adapter dispatch — every non-skipped event. Reached
            #    after the mirror commit so adapters can rely on the
            #    mirror reflecting the events about to apply.
            non_skipped = [
                ev for i, ev in enumerate(events) if i not in adapter_skip
            ]
            if non_skipped:
                self.adapter.apply_events(non_skipped)

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
