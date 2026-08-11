"""Bidirectional synchronization for equivalent file-backed USD stages."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pxr import Sdf, Usd

from ._client_utils import (
    ClientPhase,
    ClientStatus,
    SyncUpdate,
    client_origin,
    client_token_handlers,
    require_app_name,
    resolve_client_token,
)
from .client_id import make_stable_client_id
from .codec import ReceivedEvent, decode_messages
from .event_apply import apply_events, atomic_apply
from .protocol_constants import (
    K_REPLACE_SDF_LAYER_CONTENT,
    K_SET_SDF_SPEC_FIELDS,
    K_SET_SUBLAYERS,
    LayerMode,
)
from .receiver import ReceiverThread
from .recovery import (
    RecoveryArtifact,
    RecoveryError,
    RejectionDisposition,
)
from .sdf_layer_tracker import SdfLayerChangeTracker
from .sender import EventSender
from .shared_layer_graph import SharedLayerGraph
from .token_client import load_token

LOG = logging.getLogger(__name__)

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7200


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


class SharedStageClient:
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
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
        client_id: str | None = None,
        origin: str | None = None,
        token: str | None = None,
        persist_token: bool = True,
        reconnect: bool = True,
        on_stage_metadata: Callable[[dict], None] | None = None,
        on_playback_state: Callable[[dict], None] | None = None,
        on_playback_claimed: Callable[[dict], None] | None = None,
        on_playback_rejected: Callable[[dict], None] | None = None,
        on_token_issued: Callable[[str], None] | None = None,
        delegate_bridge_path: str | Path | None = None,
    ):
        if not isinstance(stage, Usd.Stage):
            raise TypeError("SharedStageClient requires a Usd.Stage")
        app_name = require_app_name(app_name)
        if Sdf.Layer.IsAnonymousLayerIdentifier(stage.GetRootLayer().identifier):
            raise ValueError("shared-stage synchronization requires a portable root layer")
        stable_client_id = client_id or make_stable_client_id(app_name)
        connection_origin = origin or client_origin(app_name, "shared")
        resolved_token = resolve_client_token(host, port, token, persist_token)
        token_callback = client_token_handlers(host, port, persist_token, on_token_issued)

        self._stage = stage
        self._host = host
        self._port = port
        self._persist_token = persist_token
        self._graph = SharedLayerGraph(stage)
        self._graph._validate_local_graph()

        from .sdf_delegate_bridge import _find_bridge

        self._delegate_bridge_path = delegate_bridge_path or _find_bridge()
        self._tracker = self._make_tracker(stage, self._graph)
        self._receiver = ReceiverThread(
            host=host,
            port=port,
            sync_from=1,
            reconnect=reconnect,
            client_id=stable_client_id,
            origin=connection_origin,
            token=resolved_token,
            on_token_issued=token_callback,
            on_stage_metadata=on_stage_metadata,
            on_playback_state=on_playback_state,
            on_playback_claimed=on_playback_claimed,
            on_playback_rejected=on_playback_rejected,
            layered_replay=False,
            layer_mode=LayerMode.SHARED_STAGE,
        )
        self._sender = EventSender(
            host,
            port,
            client_id=stable_client_id,
            origin=connection_origin,
            token=resolved_token,
            on_token_issued=token_callback,
            layer_mode=LayerMode.SHARED_STAGE,
        )
        self._last_seq = 0
        self._pending_records: list[ReceivedEvent] = []
        self._last_recovery_assessment: SharedRecoveryAssessment | None = None
        self._started = False
        self._closed = False

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
    def status(self) -> ClientStatus:
        """Current transport, replay, durability, and recovery state."""
        failure = self._sender.transaction_failure
        reason = str(failure) if failure is not None else (
            self._sender.rejection_reason or self._receiver.rejection_reason
        )
        if self._closed:
            phase = ClientPhase.CLOSED
        elif failure is not None:
            phase = ClientPhase.RECOVERY_REQUIRED
        elif (
            self._receiver.auth_rejected
            or self._sender.auth_rejected
            or self._receiver.hello_rejected
            or self._sender.hello_rejected
        ):
            phase = ClientPhase.REJECTED
        elif self._receiver.connected and not self._receiver.synchronized:
            phase = ClientPhase.REPLAYING
        elif self.connected and self.synchronized:
            phase = ClientPhase.READY
        elif self._started:
            phase = ClientPhase.CONNECTING
        else:
            phase = ClientPhase.OFFLINE
        return ClientStatus(
            phase=phase,
            connected=self.connected,
            synchronized=self.synchronized,
            receiver_connected=self._receiver.connected,
            sender_connected=self._sender.connected,
            prepared_events=self.prepared_event_count,
            pending_events=self.pending_event_count,
            acknowledged_events_total=self._sender.acknowledged_event_count,
            failure=failure,
            recovery=self._sender.recovery_incident,
            reason=reason,
        )

    @property
    def connected(self) -> bool:
        return not self._closed and self._receiver.connected and self._sender.connected

    @property
    def synchronized(self) -> bool:
        """Whether the local layer graph applied the server replay watermark."""
        return (
            not self._closed
            and self._graph.ready
            and self._receiver.synchronized
            and not self._sender.recovery_required
        )

    @property
    def pending_event_count(self) -> int:
        """Number of submitted events not yet durably acknowledged."""
        return self._sender.pending_event_count

    @property
    def recovery_artifact(self) -> RecoveryArtifact | None:
        """Exact quarantined transactions for integration-owned recovery."""
        return self._sender.recovery_artifact

    def repair_and_resume(self, events: list[dict], *, layer: Sdf.Layer) -> int:
        """Replace a recoverable layer transaction and resume its outbox.

        The application must first apply authoritative incoming state and
        rebuild *events* against *layer*. The layer must still be mapped by the
        current graph; no semantic merge or layer redirection is inferred.
        """
        if self._closed:
            raise RuntimeError("SharedStageClient is closed")
        layer_key = self._graph.key_for(layer)
        if not layer_key or layer_key not in self._graph.reachable_layer_keys():
            raise RecoveryError(
                "invalid_repair_target",
                "repair target layer is not mapped by the current graph",
            )
        self._require_recoverable_artifact()
        txn_id = self._sender.repair_rejected_transaction(events, layer_key=layer_key)
        self._last_recovery_assessment = None
        if not self._connect_sender():
            raise ConnectionError(
                f"transaction {txn_id} repaired but reconnect to "
                f"{self._host}:{self._port} failed; it remains queued"
            )
        return txn_id

    def refresh_recovery_assessment(
        self,
        *,
        timeout: float | None = 10.0,
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

        self._refresh_recovery_checkpoint(timeout)
        reachable = set(self._graph.reachable_layer_keys())
        layers = []
        for rejected_key, layer, rejected_snapshot in captured:
            current_key = self._graph.key_for(layer) if layer is not None else None
            layers.append(
                SharedRecoveryLayer(
                    rejected_layer_key=rejected_key,
                    source_layer=layer,
                    current_layer_key=current_key,
                    reachable=current_key in reachable if current_key is not None else False,
                    rejected_snapshot=rejected_snapshot,
                )
            )
        assessment = SharedRecoveryAssessment(
            recovery_artifact=artifact,
            layers=tuple(layers),
            checkpoint_seq=self._last_seq,
            graph_generation=self._graph.generation,
            graph_revision=self._graph.revision,
        )
        self._last_recovery_assessment = assessment
        return assessment

    def recover_use_server(
        self,
        *,
        clean_stage: Usd.Stage,
        session_id: str | None = None,
        timeout: float | None = 10.0,
    ) -> SharedRecoveryAssessment:
        """Select server state by replaying onto a clean equivalent stage.

        Shared stages use application-owned file layers. The event log cannot
        safely clear an arbitrary live layer in place, and a currently detached
        layer may be reattached later. The caller therefore supplies a clean
        stage opened from the same collaboration baseline. Rejected work is
        preserved in the returned assessment before the new stage is replayed.
        Producer reconnect is attempted within the same timeout budget; if it
        cannot complete, the normal update loop retries.
        """
        self._validate_clean_recovery_stage(clean_stage)
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        assessment = self.refresh_recovery_assessment(timeout=timeout)
        self._validate_clean_recovery_stage(clean_stage, assessment=assessment)
        self._rebind_stage_for_recovery(clean_stage)
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        self._refresh_recovery_checkpoint(remaining)
        assessment = self._reclassify_recovery_assessment(assessment)
        self._last_recovery_assessment = assessment
        result = self.complete_recovery(
            assessment,
            session_id=session_id,
        )
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
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
            raise RecoveryError(
                "invalid_clean_stage",
                "Use Server recovery requires a different clean stage",
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
        if self._closed:
            raise RuntimeError("SharedStageClient is closed")
        if not self._started:
            raise RuntimeError("SharedStageClient has not been started")
        artifact = self._sender.recovery_artifact
        failure = self._sender.transaction_failure
        if artifact is None or failure is None:
            raise RecoveryError(
                "no_incident",
                "there is no recovery incident to resolve",
            )
        if failure.disposition is not RejectionDisposition.RECOVERABLE_CONFLICT:
            raise RecoveryError(
                "wrong_recovery_kind",
                f"{failure.code_name} is {failure.disposition.value}, not a "
                "recoverable shared-stage conflict",
            )
        return artifact

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

    def _reclassify_recovery_assessment(
        self,
        assessment: SharedRecoveryAssessment,
    ) -> SharedRecoveryAssessment:
        """Classify preserved source layers against the currently bound graph."""
        reachable = set(self._graph.reachable_layer_keys())
        layers = []
        for prior in assessment.layers:
            source = prior.source_layer
            current_key = self._graph.key_for(source) if source is not None else None
            layers.append(
                SharedRecoveryLayer(
                    rejected_layer_key=prior.rejected_layer_key,
                    source_layer=source,
                    current_layer_key=current_key,
                    reachable=current_key in reachable if current_key is not None else False,
                    rejected_snapshot=prior.rejected_snapshot,
                )
            )
        return SharedRecoveryAssessment(
            recovery_artifact=assessment.recovery_artifact,
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
        self._tracker.sync_graph(force=True)
        return assessment

    def _resume_sender_after_recovery(self, timeout: float | None) -> None:
        """Best-effort producer reconnect after state recovery has committed."""
        try:
            self._connect_sender(timeout=timeout)
        except (PermissionError, ConnectionError):
            # Recovery already completed and must not look rolled back. Status
            # exposes rejection/offline state; update() retries ordinary loss.
            pass

    def _refresh_recovery_checkpoint(self, timeout: float | None) -> None:
        """Apply through a fresh shared-stage replay watermark."""
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        reconnect = self._receiver.reconnect
        self._receiver.reconnect = True
        try:
            self._receiver.request_replay_from(self._last_seq + 1)
            while True:
                self._tracker.prepare_local_changes()
                try:
                    self._apply_incoming()
                finally:
                    self._tracker.restore_prepared()
                if self._receiver.synchronized:
                    return
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("authoritative shared-stage recovery replay timed out")
                time.sleep(0.01)
        finally:
            self._receiver.reconnect = reconnect

    def flush(self, timeout: float | None = None) -> bool:
        """Wait for every submitted layer edit to be durably committed."""
        if self._closed:
            raise RuntimeError("SharedStageClient is closed")
        return self._sender.flush(timeout)

    @property
    def stage_metadata(self) -> dict:
        return dict(self._receiver.stage_metadata)

    @property
    def last_seq(self) -> int:
        return self._last_seq

    @property
    def prepared_event_count(self) -> int:
        return self._tracker.prepared_event_count

    @property
    def deferred_event_count(self) -> int:
        return len(self._pending_records)

    @property
    def deferred_layer_keys(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(record.layer_key or "" for record in self._pending_records))

    def is_layer_reachable(self, layer: Sdf.Layer) -> bool:
        """Return whether *layer* is reachable in the synchronized root graph."""
        layer_key = self._graph.key_for(layer)
        return bool(layer_key and layer_key in self._graph.reachable_layer_keys())

    def start(self) -> SharedStageClient:
        """Start the background receiver; connect the sender after handshake."""
        if self._closed:
            raise RuntimeError("SharedStageClient is closed")
        if not self._started:
            if self._receiver.token is None and self._persist_token:
                self._receiver.token = load_token(self._host, self._port)
            self._receiver.start()
            self._started = True
        return self

    def connect(self, timeout: float | None = None) -> bool:
        """Start and complete both shared-stage handshakes within ``timeout``."""
        self.start()
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        if not self._receiver.wait_connected(timeout):
            if self._receiver.auth_rejected:
                raise PermissionError("shared-stage receiver authentication rejected")
            if self._receiver.hello_rejected:
                raise ConnectionError(
                    self._receiver.rejection_reason or "shared-stage receiver rejected"
                )
            return False
        if self._receiver.layer_mode_active is not LayerMode.SHARED_STAGE:
            raise RuntimeError("server did not negotiate shared-stage mode")
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        return self._connect_sender(timeout=remaining)

    def _connect_sender(self, timeout: float | None = None) -> bool:
        if self._sender.connected:
            return True
        self._sender.token = self._receiver.token
        if not self._sender.connect(timeout=timeout):
            if self._sender.auth_rejected:
                raise PermissionError("shared-stage sender authentication rejected")
            if self._sender.hello_rejected:
                raise ConnectionError(
                    self._sender.rejection_reason or "shared-stage sender rejected"
                )
            return False
        return True

    def update(self) -> SyncUpdate:
        """Apply queued authoritative records, then publish local layer edits."""
        if self._closed:
            raise RuntimeError("SharedStageClient is closed")
        if not self._started:
            raise RuntimeError("SharedStageClient has not been started")

        self._tracker.prepare_local_changes()
        try:
            received = self._apply_incoming()
        finally:
            self._tracker.restore_prepared()
        sent = 0
        if self._graph.ready:
            try:
                self._connect_sender()
            except (PermissionError, ConnectionError):
                pass  # best-effort during update
        if self._sender.connected and self._graph.ready and self.synchronized:
            while routed := self._tracker.next_routed_batch():
                batch, layer_key, events = routed
                if not self._sender.send_events(events, layer_key=layer_key):
                    break
                sent += len(events)
                self._tracker.mark_prepared_sent(batch)
        return SyncUpdate(
            applied_events=received,
            submitted_events=sent,
            acknowledged_events_delta=self._sender.drain_acknowledged_event_count(),
            pending_events=self._sender.pending_event_count,
            recovery=self._sender.recovery_incident,
        )

    def _apply_incoming(self) -> int:
        buffers = self._receiver.drain_queue()
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
            self._pending_records.clear()
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
        if kind not in (K_REPLACE_SDF_LAYER_CONTENT, K_SET_SDF_SPEC_FIELDS):
            raise ValueError(f"unsupported shared-stage event {kind!r}")

        layer = self._graph.layer_for(layer_key)
        if layer is None:
            self._pending_records.append(record)
            return False
        with Usd.EditContext(self._stage, Usd.EditTarget(layer)):
            with atomic_apply(self._stage):
                apply_events(self._stage, [event])
        self._tracker.accept_authoritative_event(layer, event)
        return True

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
            with Usd.EditContext(self._stage, Usd.EditTarget(layer)):
                with atomic_apply(self._stage):
                    apply_events(self._stage, [record.event for record in records])
            for record in records:
                self._tracker.accept_authoritative_event(layer, record.event)
            applied += len(records)
        self._pending_records = retained
        return applied

    def refresh_layer_graph(self) -> tuple[str, ...]:
        """Retry unresolved graph edges under this stage's resolver context."""
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
        """Bind an equivalent clean file-backed stage before synchronous replay.

        Rebinding intentionally refuses unsent tracker changes. A rejected
        sender outbox is allowed because its exact bytes and layer snapshots
        are retained by the active recovery incident and assessment.
        """
        if self._closed:
            raise RuntimeError("SharedStageClient is closed")
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
        self._pending_records.clear()
        self._last_seq = 0
        old_tracker.close()

    def close(self) -> None:
        if self._closed:
            return
        self._sender.disconnect()
        self._receiver.stop()
        if self._receiver.is_alive():
            self._receiver.join(timeout=2.0)
            if self._receiver.is_alive():
                LOG.warning("SharedStageClient receiver did not stop within 2 seconds")
        self._tracker.close()
        self._last_recovery_assessment = None
        self._closed = True

    def __enter__(self) -> SharedStageClient:
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False


__all__ = [
    "SharedRecoveryAssessment",
    "SharedRecoveryLayer",
    "SharedStageClient",
]
