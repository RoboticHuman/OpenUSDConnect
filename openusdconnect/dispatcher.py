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
  8. Remember reference and payload dependencies
  9. Notify ``on_imported`` for newly-imported prim paths

Integrations call ``drain_and_apply()`` from their own tick / idle / event
loop callback.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypedDict

from .codec import decode_messages
from .protocol_constants import (
    ARC_KINDS,
    IMPORT_KINDS,
    K_DELETE_PRIM,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_SET_CONNECTABLE_CONNECTION,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_STAGE_METADATA,
    K_SET_VARIANT_SELECTIONS,
    STAGE_SYNC_KINDS,
)
from .sdf_arc_state import canonical_arc_state, clear_arc_state, read_arc_state

if TYPE_CHECKING:
    from pxr import Sdf, Usd

    from .adapters import DCCAdapter
    from .emitter import NoticeEmitter
    from .receiver import ReceiverThread

LOG = logging.getLogger(__name__)


class AssetDependencyRefreshResult(TypedDict):
    status: Literal["refreshed", "still_missing", "not_tracked", "no_stage"]
    reapplied: int
    affected_prims: list[str]
    pending: list[str]


@dataclass(slots=True)
class _TrackedAssetEvent:
    event: dict
    edit_target: Usd.EditTarget
    dependencies: tuple[tuple[str, str, str], ...]


def _asset_paths(event: dict) -> tuple[str, ...]:
    kind = event.get("k")
    entries = event.get("refs", ()) if kind == K_SET_REFERENCE else event.get("payloads", ())
    explicit = bool(event.get("list_op_explicit", False))
    default_position = "explicit" if explicit else "prepended"
    introduced = {"explicit"} if explicit else {"added", "prepended", "appended"}
    return tuple(
        path
        for entry in entries
        if entry.get("list_position", default_position) in introduced
        if (path := str(entry.get("asset_path", "")))
    )


def _path_is_at_or_below(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _renamed_path(path: str, old_root: str, new_name: str) -> str:
    parent = old_root.rsplit("/", 1)[0]
    new_root = f"{parent}/{new_name}" if parent else f"/{new_name}"
    return new_root + path[len(old_root) :]


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
        on_applied: Callable[[list[str]], None] | None = None,
        on_applied_events: Callable[[list[dict]], None] | None = None,
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
            on_applied: Called with the prim paths of every applied
                (non-skipped) event in the batch.  Use for post-apply
                conditioning scoped to what changed (e.g. receiver-side
                material rewrites).  Fires inside the suppress block.
            on_applied_events: Like ``on_applied`` but receives the applied
                event dicts themselves.  Use when the post-apply work needs
                finer granularity than prim paths (e.g. which inputs an
                event edited).  Fires inside the suppress block, before
                ``on_applied``.
        """
        self.receiver = receiver
        self.adapter = adapter
        self.mirror_stage = mirror_stage
        self.emitter = emitter
        self.on_imported = on_imported
        self.on_resync = on_resync
        self.on_applied = on_applied
        self.on_applied_events = on_applied_events
        self._last_seq = 0
        self._asset_stage = None
        self._asset_events: dict[tuple[str, str], _TrackedAssetEvent] = {}

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
        if result.resync_requested:
            self._clear_asset_dependencies()
            if self.on_resync is not None:
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
                self.mirror_stage is not None and self.adapter.targets_stage() is self.mirror_stage
            )

            # 1. Mirror commit — atomic batch into the dispatcher's
            #    separate USD stage.
            if self.mirror_stage is not None and not adapter_handles_mirror:
                stage_events = [
                    ev
                    for i, ev in enumerate(events)
                    if i not in stage_skip and ev.get("k") in STAGE_SYNC_KINDS
                ]
                if stage_events:
                    scope = _stage_sync_scope(stage_events)
                    with atomic_apply(self.mirror_stage, prim_paths=scope):
                        apply_events(self.mirror_stage, stage_events)

            # 2. Adapter dispatch — every non-skipped event. Reached
            #    after the mirror commit so adapters can rely on the
            #    mirror reflecting the events about to apply.
            non_skipped = [ev for i, ev in enumerate(events) if i not in adapter_skip]
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

            # 4. Remember current composition dependencies. This runs after
            #    both stage and adapter application succeed and retains only
            #    the small reference/payload events needed for explicit
            #    receiver-local refreshes.
            self._observe_asset_dependencies(events)

            # 5. Post-import callback: fires for events that brought
            #    new content into the consumer (load_payload,
            #    set_reference) so the integration can run post-import
            #    work (cache warmup, viewport refresh, etc.).
            if self.on_imported is not None:
                imported = [
                    ev["prim"]
                    for i, ev in enumerate(events)
                    if i not in adapter_skip and ev.get("k") in IMPORT_KINDS and ev.get("prim")
                ]
                if imported:
                    self.on_imported(imported)

            if self.on_applied_events is not None and non_skipped:
                self.on_applied_events(non_skipped)

            if self.on_applied is not None:
                applied = [ev["prim"] for ev in non_skipped if ev.get("prim")]
                if applied:
                    self.on_applied(applied)

        return len(events)

    def _asset_dependency_stage(self) -> Usd.Stage | None:
        stage = self.mirror_stage or self.adapter.targets_stage()
        if stage is not self._asset_stage:
            self._asset_stage = stage
            self._asset_events.clear()
        return stage

    @staticmethod
    def _resolve_asset(
        stage: Usd.Stage,
        anchor_layer: Sdf.Layer,
        authored_path: str,
    ) -> tuple[str, str]:
        """Return ``(identifier, resolved_path)`` in the stage's resolver context."""
        from pxr import Ar, Sdf

        context = stage.GetPathResolverContext()
        try:
            with Ar.ResolverContextBinder(context):
                identifier = Sdf.ComputeAssetPathRelativeToLayer(
                    anchor_layer,
                    authored_path,
                )
                layer_identifier, _arguments = Sdf.Layer.SplitIdentifier(identifier)
                resolved = Ar.GetResolver().Resolve(layer_identifier)
            return identifier, resolved.GetPathString()
        except Exception as exc:  # noqa: BLE001 - resolver plugins define their own errors
            LOG.warning("Asset resolver rejected %r: %s", authored_path, exc)
            return authored_path, ""

    def _clear_asset_dependencies(self) -> None:
        self._asset_events.clear()

    @staticmethod
    def _tracked_asset_event_is_current(
        stage: Usd.Stage,
        tracked: _TrackedAssetEvent,
    ) -> bool:
        from pxr import Sdf

        event = tracked.event
        layer = tracked.edit_target.GetLayer()
        if not stage.HasLocalLayer(layer):
            return False
        spec_path = tracked.edit_target.MapToSpecPath(Sdf.Path(event.get("prim", "")))
        if spec_path.isEmpty:
            return False

        kind = event.get("k")
        key = "refs" if kind == K_SET_REFERENCE else "payloads"
        arc_attr = "referenceList" if kind == K_SET_REFERENCE else "payloadList"
        current = read_arc_state(layer, spec_path, arc_attr)
        expected = canonical_arc_state(
            event.get(key, []),
            authored=event.get("list_op_authored"),
            explicit=bool(event.get("list_op_explicit", False)),
            references=kind == K_SET_REFERENCE,
        )
        return current == expected

    def _discard_stale_asset_events(self, stage: Usd.Stage) -> None:
        for key, tracked in tuple(self._asset_events.items()):
            if not self._tracked_asset_event_is_current(stage, tracked):
                self._asset_events.pop(key)

    def _observe_asset_dependencies(
        self,
        events: list[dict],
        *,
        edit_target: Usd.EditTarget | None = None,
    ) -> None:
        stage = self._asset_dependency_stage()
        if stage is None:
            return

        edit_target = edit_target or stage.GetEditTarget()
        anchor_layer = edit_target.GetLayer()
        resolved_in_batch: dict[str, tuple[str, str]] = {}
        for event in events:
            kind = event.get("k")
            prim_path = event.get("prim", "")

            if kind == K_DELETE_PRIM:
                for key in tuple(self._asset_events):
                    if _path_is_at_or_below(key[1], prim_path):
                        self._asset_events.pop(key)
                continue

            if kind == K_RENAME_PRIM:
                new_name = event.get("new_name", "")
                if not new_name:
                    continue
                moved: list[tuple[tuple[str, str], _TrackedAssetEvent]] = []
                for key, tracked in tuple(self._asset_events.items()):
                    if not _path_is_at_or_below(key[1], prim_path):
                        continue
                    self._asset_events.pop(key)
                    new_prim = _renamed_path(key[1], prim_path, new_name)
                    moved_event = copy.deepcopy(tracked.event)
                    moved_event["prim"] = new_prim
                    moved.append(
                        (
                            (key[0], new_prim),
                            _TrackedAssetEvent(
                                event=moved_event,
                                edit_target=tracked.edit_target,
                                dependencies=tracked.dependencies,
                            ),
                        )
                    )
                self._asset_events.update(moved)
                continue

            if kind not in (K_SET_REFERENCE, K_SET_PAYLOAD):
                continue

            key = (kind, prim_path)
            entries_key = "refs" if kind == K_SET_REFERENCE else "payloads"
            state = canonical_arc_state(
                event.get(entries_key, []),
                authored=event.get("list_op_authored"),
                explicit=bool(event.get("list_op_explicit", False)),
                references=kind == K_SET_REFERENCE,
            )
            tracked_event = copy.deepcopy(event)
            tracked_event[entries_key] = state["entries"]
            tracked_event["list_op_authored"] = state["list_op_authored"]
            tracked_event["list_op_explicit"] = state["list_op_explicit"]
            dependencies: list[tuple[str, str, str]] = []
            for authored_path in _asset_paths(tracked_event):
                resolved = resolved_in_batch.get(authored_path)
                if resolved is None:
                    resolved = self._resolve_asset(
                        stage,
                        anchor_layer,
                        authored_path,
                    )
                    resolved_in_batch[authored_path] = resolved
                identifier, resolved_path = resolved
                dependencies.append((authored_path, identifier, resolved_path))

            if dependencies:
                self._asset_events[key] = _TrackedAssetEvent(
                    event=tracked_event,
                    edit_target=edit_target,
                    dependencies=tuple(dependencies),
                )
            else:
                self._asset_events.pop(key, None)

    @property
    def pending_asset_dependencies(self) -> tuple[str, ...]:
        """Authored paths of unresolved reference/payload dependencies."""
        stage = self._asset_dependency_stage()
        if stage is not None:
            self._discard_stale_asset_events(stage)
        return tuple(
            sorted(
                {
                    authored_path
                    for tracked in self._asset_events.values()
                    for authored_path, _identifier, resolved_path in tracked.dependencies
                    if not resolved_path
                }
            )
        )

    @staticmethod
    def _asset_path_matches(requested: str | None, *candidates: str) -> bool:
        if requested is None:
            return True
        normalized = requested.replace("\\", "/")
        return any(candidate.replace("\\", "/") == normalized for candidate in candidates)

    def refresh_asset_dependency(
        self,
        asset_path: str | None = None,
    ) -> AssetDependencyRefreshResult:
        """Refresh resolver state and retry matching unresolved composition arcs.

        Call this synchronously from the integration's USD/main thread after an
        asset becomes available. The retry is receiver-local: it does not send
        an event to the server or advance ``last_seq``. With ``asset_path=None``,
        every currently pending dependency is considered. Resolver contexts
        refresh as a unit, so any other tracked arc whose resolved path changes
        during the refresh is reapplied as well.
        """
        stage = self._asset_dependency_stage()
        if stage is None:
            return {
                "status": "no_stage",
                "reapplied": 0,
                "affected_prims": [],
                "pending": [],
            }

        self._discard_stale_asset_events(stage)
        tracked = [
            event
            for event in self._asset_events.values()
            if any(
                (
                    not resolved_path
                    if asset_path is None
                    else self._asset_path_matches(
                        asset_path,
                        authored_path,
                        identifier,
                        resolved_path,
                    )
                )
                for authored_path, identifier, resolved_path in event.dependencies
            )
        ]
        if not tracked:
            return {
                "status": "not_tracked",
                "reapplied": 0,
                "affected_prims": [],
                "pending": list(self.pending_asset_dependencies),
            }

        suppress_ctx = self.emitter.suppressed() if self.emitter else nullcontext()
        with suppress_ctx:
            return self._refresh_asset_dependency_suppressed(
                stage,
                asset_path,
                tracked,
            )

    def _refresh_asset_dependency_suppressed(
        self,
        stage: Usd.Stage,
        asset_path: str | None,
        tracked: list[_TrackedAssetEvent],
    ) -> AssetDependencyRefreshResult:
        from pxr import Ar, Usd

        from .event_apply import apply_events, atomic_apply

        # RefreshContext must run while its context is unbound. A conforming
        # custom resolver may emit ResolverChanged here, allowing USD to update
        # dependencies that were already composed successfully. The dispatcher
        # emitter is suppressed around this entire method because that notice
        # can synchronously recompose the stage.
        Ar.GetResolver().RefreshContext(stage.GetPathResolverContext())

        ready: list[_TrackedAssetEvent] = []
        explicitly_selected = {id(event) for event in tracked}
        resolved_in_refresh: dict[tuple[str, str], tuple[str, str]] = {}
        refreshed_dependencies: dict[
            int,
            tuple[tuple[str, str, str], ...],
        ] = {}
        for event in self._asset_events.values():
            event_ready = False
            dependencies: list[tuple[str, str, str]] = []
            for authored_path, old_identifier, old_resolved_path in event.dependencies:
                selected = id(event) in explicitly_selected and (
                    not old_resolved_path
                    if asset_path is None
                    else self._asset_path_matches(
                        asset_path,
                        authored_path,
                        old_identifier,
                        old_resolved_path,
                    )
                )

                anchor_layer = event.edit_target.GetLayer()
                cache_key = (anchor_layer.identifier, authored_path)
                resolved = resolved_in_refresh.get(cache_key)
                if resolved is None:
                    resolved = self._resolve_asset(
                        stage,
                        anchor_layer,
                        authored_path,
                    )
                    resolved_in_refresh[cache_key] = resolved
                identifier, resolved_path = resolved
                dependencies.append((authored_path, identifier, resolved_path))
                if (selected and resolved_path) or resolved_path != old_resolved_path:
                    event_ready = True
            refreshed_dependencies[id(event)] = tuple(dependencies)
            if event_ready:
                ready.append(event)

        if not ready:
            for event in self._asset_events.values():
                event.dependencies = refreshed_dependencies[id(event)]
            return {
                "status": "still_missing",
                "reapplied": 0,
                "affected_prims": [],
                "pending": list(self.pending_asset_dependencies),
            }

        replays: list[tuple[_TrackedAssetEvent, list[dict]]] = []
        adapter_events: list[dict] = []
        for tracked_event in ready:
            event = tracked_event.event
            local_events = [event]
            adapter_events.append(event)
            if event.get("k") == K_SET_PAYLOAD:
                prim = stage.GetPrimAtPath(event.get("prim", ""))
                if prim and prim.IsLoaded():
                    load_event = {"k": K_LOAD_PAYLOAD, "prim": event["prim"]}
                    local_events.append(load_event)
                    adapter_events.append(load_event)
            replays.append((tracked_event, local_events))

        adapter_handles_stage = self.adapter.targets_stage() is stage
        # A dependency can have been received while any local layer or mapped
        # edit target was active. Preserve that authored location during the
        # retry instead of writing a duplicate arc into today's edit target.
        # Snapshot every affected layer before mutating any of them so a stage
        # adapter failure rolls the complete multi-layer replay back.
        with ExitStack() as transaction:
            snapshotted_layers: set[str] = set()
            for tracked_event, _local_events in replays:
                layer = tracked_event.edit_target.GetLayer()
                if layer.identifier in snapshotted_layers:
                    continue
                with Usd.EditContext(stage, tracked_event.edit_target):
                    transaction.enter_context(atomic_apply(stage))
                snapshotted_layers.add(layer.identifier)

            for tracked_event, local_events in replays:
                with Usd.EditContext(stage, tracked_event.edit_target):
                    # Assigning the same SdfListOp is a no-op, so it cannot
                    # make Pcp retry an asset that was missing when the
                    # opinion was first authored. Clear the tracked opinion
                    # immediately before restoring its exact state.
                    arc_event = local_events[0]
                    clear_arc_state(
                        stage,
                        arc_event["prim"],
                        arc_attr=(
                            "referenceList" if arc_event["k"] == K_SET_REFERENCE else "payloadList"
                        ),
                    )
                    if adapter_handles_stage:
                        self.adapter.apply_events(local_events)
                    else:
                        apply_events(stage, local_events)

        # DCC adapters consume the same events only after the mirror stage has
        # committed. They do not expose USD edit targets themselves.
        if not adapter_handles_stage:
            self.adapter.apply_events(adapter_events)

        for tracked_event, local_events in replays:
            if self.emitter is not None:
                with Usd.EditContext(stage, tracked_event.edit_target):
                    for event in local_events:
                        self.emitter.invalidate_for_event(event)
            tracked_event.dependencies = refreshed_dependencies[id(tracked_event)]

        imported = [
            event["prim"]
            for event in adapter_events
            if event.get("k") in IMPORT_KINDS and event.get("prim")
        ]
        if imported and self.on_imported is not None:
            self.on_imported(imported)
        if self.on_applied_events is not None:
            self.on_applied_events(adapter_events)
        affected = sorted({event["prim"] for event in adapter_events if event.get("prim")})
        if affected and self.on_applied is not None:
            self.on_applied(affected)

        return {
            "status": "refreshed",
            "reapplied": len(replays),
            "affected_prims": affected,
            "pending": list(self.pending_asset_dependencies),
        }

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
        from pxr import Sdf

        from .emitter import read_variant_selections

        prim_path = ev.get("prim", "")

        if kind in (K_SET_REFERENCE, K_SET_PAYLOAD):
            references = kind == K_SET_REFERENCE
            entries_key = "refs" if references else "payloads"
            arc_attr = "referenceList" if references else "payloadList"
            edit_target = self.mirror_stage.GetEditTarget()
            spec_path = edit_target.MapToSpecPath(Sdf.Path(prim_path))
            if spec_path.isEmpty:
                return True
            current = read_arc_state(
                edit_target.GetLayer(),
                spec_path,
                arc_attr,
            )
            incoming = canonical_arc_state(
                ev.get(entries_key, []),
                authored=ev.get("list_op_authored"),
                explicit=bool(ev.get("list_op_explicit", False)),
                references=references,
            )
            return current != incoming
        if kind == K_SET_VARIANT_SELECTIONS:
            current = dict(read_variant_selections(self.mirror_stage, prim_path))
            incoming = ev.get("selections", {})
            return current != incoming
        return True


__all__ = [
    "ARC_KINDS",
    "AssetDependencyRefreshResult",
    "EventDispatcher",
    "IMPORT_KINDS",
    "STAGE_SYNC_KINDS",
]
