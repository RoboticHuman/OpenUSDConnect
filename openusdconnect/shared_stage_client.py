"""Bidirectional synchronization for equivalent portable USD stages."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pxr import Sdf, Usd

from ._client_base import PublishingClientBase
from ._client_lifecycle import DEFAULT_WAIT_TIMEOUT_S, deadline_after, remaining_time
from ._client_utils import client_origin, require_app_name
from .client_id import make_stable_client_id
from .client_observer import ClientObserver
from .client_types import SyncUpdate
from .codec import ReceivedEvent, decode_messages
from .defaults import DEFAULT_HOST, DEFAULT_SYNC_PORT
from .event_apply import apply_events, atomic_apply, atomic_apply_prim_paths
from .protocol_constants import (
    K_ERASE_TIME_SAMPLES,
    K_REPLACE_SDF_LAYER_CONTENT,
    K_SET_SDF_SPEC_FIELDS,
    K_SET_SUBLAYERS,
    LayerMode,
)
from .receiver import ReceiverThread
from .recovery import RecoveryArtifact, RecoveryError
from .sdf_layer_tracker import SdfLayerChangeTracker
from .sender import EventSender
from .shared_layer_graph import SharedLayerGraph

LOG = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class SharedRecoveryLayer:
    """One quarantined layer classified against the current graph."""

    rejected_layer_key: str
    source_layer: Sdf.Layer | None
    current_layer_key: str | None
    reachable: bool
    rejected_snapshot: Sdf.Layer | None

    @property
    def source_unavailable(self) -> bool:
        """Whether the rejected layer could not be identified locally."""
        return self.source_layer is None

    @property
    def detached(self) -> bool:
        return not self.source_unavailable and not self.reachable

    @property
    def remapped(self) -> bool:
        return (
            self.reachable
            and self.current_layer_key is not None
            and self.current_layer_key != self.rejected_layer_key
        )


@dataclass(frozen=True, slots=True)
class SharedRecoveryAssessment:
    """Authoritative classification for integration-owned recovery policy."""

    recovery_artifact: RecoveryArtifact
    layers: tuple[SharedRecoveryLayer, ...]
    checkpoint_seq: int
    graph_generation: str
    graph_revision: int

    @property
    def detached_layers(self) -> tuple[SharedRecoveryLayer, ...]:
        return tuple(layer for layer in self.layers if layer.detached)

    @property
    def unchanged_mapping_layers(self) -> tuple[SharedRecoveryLayer, ...]:
        """Layers still reachable through their original protocol keys."""
        return tuple(layer for layer in self.layers if layer.reachable and not layer.remapped)

    @property
    def remapped_layers(self) -> tuple[SharedRecoveryLayer, ...]:
        return tuple(layer for layer in self.layers if layer.remapped)

    @property
    def source_unavailable_layers(self) -> tuple[SharedRecoveryLayer, ...]:
        """Rejected layer keys whose source layers are no longer identifiable."""
        return tuple(layer for layer in self.layers if layer.source_unavailable)

    @property
    def all_layers_detached(self) -> bool:
        return bool(self.layers) and all(layer.detached for layer in self.layers)

    @property
    def rejected_snapshots(self) -> tuple[Sdf.Layer, ...]:
        """Captured rejected state with unavailable entries omitted."""
        return tuple(
            layer.rejected_snapshot
            for layer in self.layers
            if layer.rejected_snapshot is not None
        )


class SharedStageClient(PublishingClientBase):
    """Synchronize authored opinions in a stage's root-layer graph.

    Every process opens its own equivalent stage and resolver context. Opaque
    protocol keys route edits to the corresponding local ``Sdf.Layer``; local
    identifiers and resolved filesystem paths never cross the wire.
    """

    def __init__(
        self,
        stage: Usd.Stage,
        *,
        app_name: str,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_SYNC_PORT,
        client_id: str | None = None,
        origin: str | None = None,
        token: str | None = None,
        persist_token: bool = True,
        reconnect: bool = True,
        background_send: bool = False,
        observer: ClientObserver | None = None,
        delegate_bridge_path: str | Path | None = None,
    ):
        if not isinstance(stage, Usd.Stage):
            raise TypeError("SharedStageClient requires a Usd.Stage")
        app_name = require_app_name(app_name)
        if Sdf.Layer.IsAnonymousLayerIdentifier(stage.GetRootLayer().identifier):
            raise ValueError("shared-stage synchronization requires a portable root layer")
        super().__init__(
            host=host, port=port, token=token, persist_token=persist_token, observer=observer,
        )
        self._stage = stage
        self._graph = SharedLayerGraph(stage)
        self._graph._validate_local_graph()

        from .sdf_delegate_bridge import _find_bridge

        self._delegate_bridge_path = delegate_bridge_path or _find_bridge()
        self._tracker = self._make_tracker(stage, self._graph)
        identity = {
            "client_id": client_id or make_stable_client_id(app_name),
            "origin": origin or client_origin(app_name, "shared"),
        }
        credential = self._credential.endpoint_kwargs()
        self._receiver = ReceiverThread(
            host=host, port=port, sync_from=1, reconnect=reconnect,
            layered_replay=False, layer_mode=LayerMode.SHARED_STAGE,
            **identity, **credential, **self._hooks.receiver_callbacks(),
        )
        self._sender = EventSender(
            host, port, layer_mode=LayerMode.SHARED_STAGE, background_send=background_send,
            **identity, **credential,
        )
        self._last_seq = 0
        self._backlog_marker = 0
        self._set_deferred([])
        self._last_recovery_assessment: SharedRecoveryAssessment | None = None
        self._recovery_rebind_artifact: RecoveryArtifact | None = None

    def _make_tracker(self, stage: Usd.Stage, graph: SharedLayerGraph):
        bridge_path = self._delegate_bridge_path
        if bridge_path is None:
            LOG.info(
                "Sdf delegate bridge not found; using Python fallback. "
                "Build and install with: uv run python -m openusdconnect.build_sdf_notice_bridge"
            )
            return SdfLayerChangeTracker(stage, graph)
        from .sdf_delegate_bridge import NativeSdfLayerChangeTracker

        return NativeSdfLayerChangeTracker(stage, graph, bridge_path)

    @property
    def stage(self) -> Usd.Stage:
        return self._stage

    @property
    def last_seq(self) -> int:
        return self._last_seq

    def is_layer_reachable(self, layer: Sdf.Layer) -> bool:
        """Return whether *layer* is reachable in the synchronized root graph."""
        layer_key = self._graph.key_for(layer)
        return bool(layer_key and layer_key in self._graph.reachable_layer_keys())

    @property
    def _recovery_stage_pending(self) -> bool:
        """Whether a replacement stage is bound but recovery is incomplete."""
        return (
            self._recovery_rebind_artifact is not None
            and self._recovery_rebind_artifact is self._sender.recovery_artifact
        )

    def repair_and_resume(self, events: list[dict], *, layer: Sdf.Layer) -> int:
        """Replace a recoverable layer transaction and resume its outbox.

        The application must first apply authoritative incoming state and
        rebuild *events* against *layer*. The layer must still be mapped by the
        current graph; no semantic merge or layer redirection is inferred.
        """
        self._require_open()
        layer_key = self._graph.key_for(layer)
        if not layer_key or layer_key not in self._graph.reachable_layer_keys():
            raise RecoveryError(
                "invalid_repair_target",
                "repair target layer is not mapped by the current graph",
            )
        self._require_recoverable_artifact()
        txn_id = self._sender.repair_rejected_transaction(events, layer_key=layer_key)
        self._last_recovery_assessment = None
        self._recovery_rebind_artifact = None
        self._reconnect_repaired(txn_id)
        return txn_id

    def refresh_recovery_assessment(
        self,
        *,
        timeout: float | None = DEFAULT_WAIT_TIMEOUT_S,
    ) -> SharedRecoveryAssessment:
        """Replay to a fresh checkpoint and classify every quarantined layer."""
        artifact = self._require_recoverable_artifact()
        previous = self._last_recovery_assessment
        previous_layers = {}
        if previous is not None and previous.recovery_artifact is artifact:
            previous_layers = {
                layer.rejected_layer_key: layer for layer in previous.layers
            }

        captured: list[tuple[str, Sdf.Layer | None, Sdf.Layer | None]] = []
        for layer_key in artifact.layer_keys:
            prior = previous_layers.get(layer_key)
            layer = None
            rejected_snapshot = None
            if prior is not None:
                rejected_snapshot = prior.rejected_snapshot
                layer = prior.source_layer
            if layer is None:
                layer = self._graph.layer_for(layer_key)
            if layer is not None and rejected_snapshot is None:
                rejected_snapshot = Sdf.Layer.CreateAnonymous(
                    "openusdconnect-recovery-shared-layer"
                )
                rejected_snapshot.TransferContent(layer)
            captured.append((layer_key, layer, rejected_snapshot))

        self._replay_to_fresh_checkpoint(timeout)
        assessment = self._build_recovery_assessment(artifact, captured)
        self._last_recovery_assessment = assessment
        return assessment

    def recover_use_server(
        self,
        *,
        clean_stage: Usd.Stage,
        session_id: str | None = None,
        timeout: float | None = DEFAULT_WAIT_TIMEOUT_S,
    ) -> SharedRecoveryAssessment:
        """Select server state by replaying onto a clean equivalent stage.

        Shared stages use application-owned layers. The event log cannot
        safely clear an arbitrary live layer in place, and a currently detached
        layer may be reattached later. The caller therefore supplies a clean
        stage opened from the same collaboration baseline. Rejected work is
        preserved in the returned assessment before the new stage is replayed.
        Producer reconnect is attempted within the same timeout budget; if it
        cannot complete, the normal update loop retries.

        If replay fails after the replacement is bound, it stays bound
        (``recovery_stage_pending``): keep the host on ``client.stage`` and call
        :meth:`resume_recovery`.
        """
        deadline = deadline_after(timeout)
        self._validate_clean_recovery_stage(clean_stage)
        assessment = self.refresh_recovery_assessment(timeout=timeout)
        self._validate_clean_recovery_stage(clean_stage, assessment=assessment)
        self._rebind_stage_for_recovery(clean_stage)
        self._recovery_rebind_artifact = assessment.recovery_artifact
        return self._finish_use_server(assessment, session_id=session_id, deadline=deadline)

    def resume_recovery(
        self,
        *,
        session_id: str | None = None,
        timeout: float | None = DEFAULT_WAIT_TIMEOUT_S,
    ) -> SharedRecoveryAssessment:
        """Continue a Use Server recovery whose replacement replay did not finish.

        Rejected snapshots captured by the original attempt are preserved.
        """
        self._require_recoverable_artifact()
        if not self._recovery_stage_pending:
            raise RecoveryError(
                "no_pending_recovery_stage",
                "no replacement stage is waiting for recovery to complete",
            )
        if self._tracker.has_local_changes:
            raise RecoveryError(
                "local_changes_pending",
                "cannot resume replacement-stage recovery while unsent edits remain",
            )
        return self._finish_use_server(
            self._last_recovery_assessment,
            session_id=session_id,
            deadline=deadline_after(timeout),
        )

    def _finish_use_server(
        self,
        assessment: SharedRecoveryAssessment,
        *,
        session_id: str | None,
        deadline: float | None,
    ) -> SharedRecoveryAssessment:
        remaining = remaining_time(deadline)
        self._replay_to_fresh_checkpoint(remaining)
        assessment = self._build_recovery_assessment(
            assessment.recovery_artifact,
            (
                (layer.rejected_layer_key, layer.source_layer, layer.rejected_snapshot)
                for layer in assessment.layers
            ),
        )
        self._last_recovery_assessment = assessment
        result = self.complete_recovery(
            assessment,
            session_id=session_id,
        )
        remaining = remaining_time(deadline)
        self._resume_sender_after_recovery(remaining)
        return result

    def _validate_clean_recovery_stage(
        self,
        clean_stage: Usd.Stage,
        *,
        assessment: SharedRecoveryAssessment | None = None,
    ) -> None:
        """Reject replacement stages that reuse any rejected live layer."""
        if not isinstance(clean_stage, Usd.Stage):
            raise TypeError("SharedStageClient requires a Usd.Stage")
        if clean_stage is self._stage:
            hint = (
                "; call resume_recovery() to continue the pending replacement"
                if self._recovery_stage_pending
                else ""
            )
            raise RecoveryError(
                "invalid_clean_stage",
                f"Use Server recovery requires a different clean stage{hint}",
            )
        if Sdf.Layer.IsAnonymousLayerIdentifier(clean_stage.GetRootLayer().identifier):
            raise RecoveryError(
                "invalid_clean_stage",
                "Use Server recovery requires a portable root layer",
            )

        rejected_layers = list(
            self._stage.GetLayerStack(includeSessionLayers=False)
        )
        if assessment is not None:
            rejected_layers.extend(
                item.source_layer
                for item in assessment.layers
                if item.source_layer is not None
            )
        replacement_layers = list(
            clean_stage.GetLayerStack(includeSessionLayers=False)
        )
        rejected_identifiers = {layer.identifier for layer in rejected_layers}
        rejected_object_ids = {id(layer) for layer in rejected_layers}
        overlap = {
            replacement.identifier
            for replacement in replacement_layers
            if replacement.identifier in rejected_identifiers
            or id(replacement) in rejected_object_ids
        }
        if overlap:
            raise RecoveryError(
                "shared_loaded_layers",
                "Use Server recovery stage shares loaded layers with the rejected "
                f"stage: {sorted(overlap)!r}"
            )

    def complete_recovery(
        self,
        assessment: SharedRecoveryAssessment,
        *,
        session_id: str | None = None,
    ) -> SharedRecoveryAssessment:
        """Finish after an integration has explicitly reconciled its stage.

        This method does not infer or verify USD merge semantics. The caller
        owns removal, rebase, or export of reachable local opinions. It only
        verifies that *assessment* belongs to the active incident and that the
        receive side is again at a usable authoritative checkpoint.
        """
        self._validate_recovery_assessment(assessment)
        if not self._graph.ready or not self._receiver.synchronized:
            raise RecoveryError(
                "stage_not_synchronized",
                "shared stage must be synchronized before recovery completes",
            )
        return self._complete_recovery(assessment, session_id=session_id)

    def _require_recoverable_artifact(self) -> RecoveryArtifact:
        self._require_started()
        self._require_recoverable_failure()
        return self._sender.recovery_artifact

    def _validate_recovery_assessment(
        self,
        assessment: SharedRecoveryAssessment,
    ) -> None:
        artifact = self._require_recoverable_artifact()
        if assessment.recovery_artifact is not artifact:
            raise RecoveryError(
                "stale_assessment",
                "recovery assessment does not match the active incident",
            )
        if (
            assessment.checkpoint_seq != self._last_seq
            or assessment.graph_generation != self._graph.generation
            or assessment.graph_revision != self._graph.revision
        ):
            raise RecoveryError(
                "stale_assessment",
                "recovery assessment is stale; refresh and reconcile the current graph",
            )

    def _build_recovery_assessment(
        self,
        artifact: RecoveryArtifact,
        captured: Iterable[tuple[str, Sdf.Layer | None, Sdf.Layer | None]],
    ) -> SharedRecoveryAssessment:
        """Classify preserved source layers against the currently bound graph."""
        reachable = set(self._graph.reachable_layer_keys())
        layers = []
        for rejected_key, source, snapshot in captured:
            current_key = self._graph.key_for(source) if source is not None else None
            layers.append(
                SharedRecoveryLayer(
                    rejected_layer_key=rejected_key,
                    source_layer=source,
                    current_layer_key=current_key,
                    reachable=current_key in reachable if current_key is not None else False,
                    rejected_snapshot=snapshot,
                )
            )
        return SharedRecoveryAssessment(
            recovery_artifact=artifact,
            layers=tuple(layers),
            checkpoint_seq=self._last_seq,
            graph_generation=self._graph.generation,
            graph_revision=self._graph.revision,
        )

    def _complete_recovery(
        self,
        assessment: SharedRecoveryAssessment,
        *,
        session_id: str | None,
    ) -> SharedRecoveryAssessment:
        self._validate_recovery_assessment(assessment)
        self._sender.abandon_rejected_session(session_id=session_id)
        self._last_recovery_assessment = None
        self._recovery_rebind_artifact = None
        self._tracker.sync_graph(force=True)
        return assessment

    def update(self, *, max_messages: int | None = None) -> SyncUpdate:
        """Apply queued authoritative records, then publish local layer edits.

        ``max_messages`` bounds one call's receive work; local edits are held
        until the backlog queued before them has been applied.
        """
        if not self._begin_update():
            return self._progress()
        received = self._apply_queued(max_messages)
        sent = 0
        if self._graph.ready and self._receiver.connected and not self._sender.connected:
            self._sender.request_connect()
        if (
            self._sender.connected
            and self._is_synchronized()
            and self._receiver.drained_through(self._backlog_marker)
        ):
            while routed := self._tracker.next_routed_batch():
                batch, layer_key, events = routed
                if not self._sender.send_events(events, layer_key=layer_key):
                    break
                sent += len(events)
                self._tracker.mark_prepared_sent(batch)
        return self._progress(received, sent)

    def _apply_queued(self, max_messages: int | None = None) -> int:
        """Apply queued records while local edits are frozen out of the layers."""
        had_batch = bool(self._tracker.prepared_event_count)
        self._tracker.prepare_local_changes()
        if not had_batch and self._tracker.prepared_event_count:
            self._backlog_marker = self._receiver.freeze_marker()
        try:
            return self._apply_incoming(max_messages)
        finally:
            self._tracker.restore_prepared()

    def _apply_incoming(self, max_messages: int | None = None) -> int:
        generation = self._receiver.generation
        buffers = self._receiver.drain_queue(max_messages)
        if not buffers:
            self._receiver.mark_replay_applied()
            return 0
        result = decode_messages(
            buffers,
            last_seq=self._last_seq,
            numpy_arrays=True,
            clear_on_resync=True,
            preserve_envelopes=True,
            require_contiguous=True,
        )
        if result.resync_requested:
            self._set_deferred([])
        applied_seq = 0 if result.resync_requested else self._last_seq
        applied = 0
        try:
            with self._tracker.suppressed():
                for state in result.layer_graph_states:
                    previous_target = self._stage.GetEditTarget()
                    self._graph.apply_state(state)
                    self._restore_edit_target(previous_target)
                    for layer_state in state["layers"]:
                        layer = self._graph.layer_for(layer_state["layer_key"])
                        if layer is not None:
                            self._tracker.accept_authoritative_sublayers(
                                layer,
                                layer_state["sublayers"],
                            )
                    self._tracker.sync_graph(force=True)
                    applied_seq = max(applied_seq, int(state.get("seq", 0)))
                for record in result.received_records:
                    if self._apply_record(record):
                        applied += 1
                    applied_seq = max(applied_seq, record.seq)
                applied += self._apply_pending()
        except Exception:
            self._last_seq = applied_seq
            self._receiver.request_replay_from(applied_seq + 1)
            raise

        self._last_seq = max(applied_seq, result.last_seq)
        if result.errors:
            self._receiver.request_replay_from(self._last_seq + 1)
            LOG.warning("Shared-stage decode failed: %s", result.errors[0])
        else:
            if result.resync_requested:
                self._receiver.reset_applied_progress()
            self._receiver.mark_applied_through(generation, self._last_seq)
            self._receiver.mark_replay_applied()
        return applied

    def _apply_record(self, record: ReceivedEvent) -> bool:
        layer_key = record.layer_key
        if not layer_key:
            raise ValueError("shared-stage record is missing layer_key")
        event = record.event
        kind = event.get("k")
        if kind == K_SET_SUBLAYERS:
            with self._graph.transaction():
                layer = self._graph.layer_for(layer_key)
                if layer is None:
                    self._graph.apply_sublayers(layer_key, event)
                else:
                    previous_target = self._stage.GetEditTarget()
                    self._stage.SetEditTarget(Usd.EditTarget(layer))
                    try:
                        with atomic_apply(self._stage):
                            self._graph.apply_sublayers(layer_key, event)
                    finally:
                        self._restore_edit_target(previous_target)
                    self._tracker.accept_authoritative_sublayers(
                        layer,
                        event["sublayers"],
                    )
            self._tracker.sync_graph(force=True)
            return True
        if kind not in (K_REPLACE_SDF_LAYER_CONTENT, K_SET_SDF_SPEC_FIELDS, K_ERASE_TIME_SAMPLES):
            raise ValueError(f"unsupported shared-stage event {kind!r}")

        layer = self._graph.layer_for(layer_key)
        if layer is None:
            self._defer(record)
            return False
        self._apply_layer_events(layer, [event])
        return True

    def _apply_layer_events(self, layer: Sdf.Layer, events: list[dict]) -> None:
        """Commit layer content before advancing the tracker's authored baseline."""
        with Usd.EditContext(self._stage, Usd.EditTarget(layer)):
            with atomic_apply(self._stage, prim_paths=atomic_apply_prim_paths(events)):
                apply_events(self._stage, events)
        for event in events:
            self._tracker.accept_authoritative_event(layer, event)

    def _restore_edit_target(self, preferred: Usd.EditTarget) -> None:
        """Restore *preferred* when composed, otherwise select the root layer."""
        preferred_layer = preferred.GetLayer()
        reachable = {
            layer.identifier for layer in self._stage.GetLayerStack(includeSessionLayers=True)
        }
        if preferred_layer.identifier in reachable:
            self._stage.SetEditTarget(preferred)
        else:
            self._stage.SetEditTarget(Usd.EditTarget(self._stage.GetRootLayer()))

    def _defer(self, record: ReceivedEvent) -> None:
        """Hold a record until its layer is mapped by the local graph."""
        self._pending_records.append(record)
        key = record.layer_key or ""
        if key not in self._deferred_layer_keys:
            self._deferred_layer_keys += (key,)

    def _set_deferred(self, records: list[ReceivedEvent]) -> None:
        self._pending_records = records
        self._deferred_layer_keys = tuple(
            dict.fromkeys(record.layer_key or "" for record in records)
        )

    def _apply_pending(self) -> int:
        if not self._pending_records:
            return 0
        retained = []
        applied = 0
        groups: list[tuple[Sdf.Layer, list[ReceivedEvent]]] = []
        for record in self._pending_records:
            layer = self._graph.layer_for(record.layer_key)
            if layer is None:
                retained.append(record)
                continue
            if groups and groups[-1][0] is layer:
                groups[-1][1].append(record)
            else:
                groups.append((layer, [record]))
        for layer, records in groups:
            events = [record.event for record in records]
            self._apply_layer_events(layer, events)
            applied += len(records)
        self._set_deferred(retained)
        return applied

    def refresh_layer_graph(self) -> tuple[str, ...]:
        """Retry unresolved graph edges under this stage's resolver context."""
        self._require_open()
        with self._tracker.suppressed():
            mapped = self._graph.refresh_dependencies()
            self._tracker.sync_graph(force=True)
            for layer_key in self._graph.reachable_layer_keys():
                layer = self._graph.layer_for(layer_key)
                entries = self._graph.sublayers_for(layer_key)
                if layer is not None and entries is not None:
                    self._tracker.accept_authoritative_sublayers(layer, list(entries))
            self._apply_pending()
        self._tracker.restore_prepared()
        return mapped

    def _rebind_stage_for_recovery(self, stage: Usd.Stage) -> None:
        """Bind an equivalent clean portable stage before synchronous replay.

        Rebinding intentionally refuses unsent tracker changes. A rejected
        sender outbox is allowed because its exact bytes and layer snapshots
        are retained by the active recovery incident and assessment.
        """
        self._require_open()
        if not isinstance(stage, Usd.Stage):
            raise TypeError("SharedStageClient requires a Usd.Stage")
        if Sdf.Layer.IsAnonymousLayerIdentifier(stage.GetRootLayer().identifier):
            raise RecoveryError(
                "invalid_clean_stage",
                "shared-stage synchronization requires a portable root layer",
            )
        if self._tracker.has_local_changes:
            raise RecoveryError(
                "local_changes_pending",
                "cannot rebind while unsent shared-stage edits remain",
            )
        if self._sender.pending_transaction_count and not self._sender.recovery_required:
            raise RecoveryError(
                "transactions_pending",
                "cannot rebind while transactions await acknowledgement",
            )

        graph = SharedLayerGraph(stage)
        try:
            graph._validate_local_graph()
        except ValueError as exc:
            raise RecoveryError("invalid_clean_stage", str(exc)) from exc
        tracker = self._make_tracker(stage, graph)
        old_tracker = self._tracker
        self._stage = stage
        self._graph = graph
        self._tracker = tracker
        self._set_deferred([])
        self._last_seq = 0
        old_tracker.close()

    def _is_synchronized(self) -> bool:
        return (
            self._graph.ready
            and self._receiver.synchronized
            and not self._sender.recovery_required
        )

    def _prepared_events(self) -> int:
        return self._tracker.prepared_event_count

    def _has_unsent_changes(self) -> bool:
        return self._tracker.has_local_changes

    def _role_status(self) -> dict:
        target = self._stage.GetEditTarget().GetLayer()
        return {
            "deferred_events": len(self._pending_records),
            "deferred_layer_keys": self._deferred_layer_keys,
            "edit_target_is_published": (
                target in self._stage.GetLayerStack(includeSessionLayers=False)
            ),
            "recovery_stage_pending": self._recovery_stage_pending,
        }

    def _release(self) -> None:
        self._tracker.close()
        self._last_recovery_assessment = None
        self._recovery_rebind_artifact = None


__all__ = [
    "SharedRecoveryAssessment",
    "SharedRecoveryLayer",
    "SharedStageClient",
]
