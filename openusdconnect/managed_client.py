"""Single bidirectional client for server-owned collaboration layers.

``ManagedClient`` composes the emitter, sender, receiver, and dispatcher so a
USD-native application authors and observes one stage. It is the managed-mode
counterpart of ``SharedStageClient`` with the same lifecycle shape
(``start`` / ``wait_connected`` / ``update`` / ``close``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from pxr import Sdf, Usd

from ._client_utils import (
    SyncUpdate,
    client_origin,
    client_token_handlers,
    require_app_name,
    resolve_client_token,
    validate_layered_source,
)
from .adapters import UsdStageAdapter
from .client_id import make_stable_client_id
from .coalescing import TransformCoalescingWindow
from .dispatcher import AssetDependencyRefreshResult, EventDispatcher
from .emitter import NoticeEmitter, PrimChannel
from .receiver import ReceiverThread
from .recovery import RejectionDisposition, TransactionFailure
from .sender import EventSender

LOG = logging.getLogger(__name__)

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7200


class ManagedClient:
    """Bidirectional synchronization with server-owned collaboration layers."""

    def __init__(
        self,
        stage: Usd.Stage,
        *,
        app_name: str,
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
        client_id: str | None = None,
        origin: str | None = None,
        department: str | None = None,
        token: str | None = None,
        persist_token: bool = True,
        reconnect: bool = True,
        on_imported: Callable[[list[str]], None] | None = None,
        on_resync: Callable[[], None] | None = None,
        on_applied: Callable[[list[str]], None] | None = None,
        on_applied_events: Callable[[list[dict]], None] | None = None,
        on_stage_metadata: Callable[[dict], None] | None = None,
        on_playback_state: Callable[[dict], None] | None = None,
        on_playback_claimed: Callable[[dict], None] | None = None,
        on_playback_rejected: Callable[[dict], None] | None = None,
        on_token_issued: Callable[[str], None] | None = None,
        attr_filter: Callable[[str], bool] | None = None,
        replicated_api_schemas: set[str] | None = None,
        extra_channels: Sequence[PrimChannel] | None = None,
        transform_coalesce_seconds: float = 0.0,
    ):
        app_name = require_app_name(app_name)
        if not isinstance(stage, Usd.Stage):
            raise TypeError("ManagedClient requires a Usd.Stage")
        adapter = UsdStageAdapter(stage)
        validate_layered_source(stage)
        stable_client_id = client_id or make_stable_client_id(app_name)
        connection_origin = origin or client_origin(app_name, "sync")
        resolved_token = resolve_client_token(host, port, token, persist_token)
        token_callback = client_token_handlers(host, port, persist_token, on_token_issued)

        self._stage = stage
        self._host = host
        self._port = port
        self._persist_token = persist_token
        self._app_name = app_name
        self._authoring_layer = self._ensure_convergent_edit_target(stage, app_name)
        self._transform_coalescing = TransformCoalescingWindow(transform_coalesce_seconds)

        self._emitter = NoticeEmitter(
            stage,
            attr_filter=attr_filter,
            replicated_api_schemas=replicated_api_schemas,
            extra_channels=extra_channels,
        )
        self._sender = EventSender(
            host,
            port,
            client_id=stable_client_id,
            origin=connection_origin,
            department=department,
            token=resolved_token,
            on_token_issued=token_callback,
        )
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
            layered_replay=True,
        )
        self._dispatcher = EventDispatcher(
            receiver=self._receiver,
            adapter=adapter,
            emitter=self._emitter,
            on_imported=on_imported,
            on_resync=on_resync,
            on_applied=on_applied,
            on_applied_events=on_applied_events,
        )
        self._started = False
        self._closed = False

    @property
    def stage(self) -> Usd.Stage | None:
        """Application-owned stage, or ``None`` while parked."""
        return self._stage

    @property
    def sender(self):
        """The underlying :class:`EventSender`. Read access is safe; mutating
        configuration on this object is at your own risk."""
        return self._sender

    @property
    def receiver(self):
        """The underlying :class:`ReceiverThread`."""
        return self._receiver

    @property
    def dispatcher(self):
        """The underlying :class:`EventDispatcher`."""
        return self._dispatcher

    @property
    def emitter(self):
        """The underlying :class:`NoticeEmitter`."""
        return self._emitter

    @property
    def authoring_layer(self) -> Sdf.Layer | None:
        """Application layer currently intended for local shared edits.

        When the stage initially targets its strong session root, ManagedClient
        creates a transient session sublayer instead. Authoritative managed
        layers compose above that sublayer, so peer edits can converge.
        """
        return self._authoring_layer

    @property
    def connected(self) -> bool:
        return not self._closed and self._receiver.connected and self._sender.connected

    @property
    def synchronized(self) -> bool:
        """Whether the local stage applied replay through the server watermark."""
        return (
            not self._closed and self._receiver.synchronized and not self._sender.recovery_required
        )

    @property
    def recovery_required(self) -> bool:
        """Whether deterministic rejection requires local-state reconciliation."""
        return self._sender.recovery_required

    @property
    def pending_event_count(self) -> int:
        """Number of submitted events not yet durably acknowledged."""
        return self._sender.pending_event_count

    @property
    def transaction_error(self) -> str:
        """Terminal producer rejection, or an empty string."""
        return self._sender.transaction_error

    @property
    def transaction_failure(self) -> TransactionFailure | None:
        """Structured rejection including its recovery disposition, if any."""
        return self._sender.transaction_failure

    @property
    def recovery_disposition(self) -> RejectionDisposition | None:
        """Recovery policy category for the current rejection, if any."""
        return self._sender.recovery_disposition

    def repair_and_resume(self, events: list[dict]) -> int:
        """Replace a recoverable transaction and resume its ordered outbox.

        The application must first reconcile its stage with authoritative
        state and rebuild *events* for that state. The repaired transaction is
        assigned the original rejected ID; later quarantined transactions keep
        their existing IDs and replay after it.
        """
        if self._closed:
            raise RuntimeError("ManagedClient is closed")
        txn_id = self._sender.repair_rejected_transaction(events)
        self._connect_sender()
        return txn_id

    def flush(self, timeout: float | None = None) -> bool:
        """Submit any coalesced transform, then wait for durable acknowledgement."""
        if self._closed:
            raise RuntimeError("ManagedClient is closed")
        if self._transform_coalescing.buffering:
            try:
                self._connect_sender()
            except (PermissionError, ConnectionError):
                return False
            if not self.synchronized:
                return False
            events = self._transform_coalescing.force(self._emitter)
            if events and not self._send(events):
                return False
        return self._sender.flush(timeout)

    @property
    def auth_rejected(self) -> bool:
        return self._receiver.auth_rejected or self._sender.auth_rejected

    @property
    def connection_rejected(self) -> bool:
        return self._receiver.hello_rejected or self._sender.hello_rejected

    @property
    def last_seq(self) -> int:
        return self._dispatcher.last_seq

    @property
    def stage_metadata(self) -> dict:
        return dict(self._receiver.stage_metadata)

    @property
    def prepared_event_count(self) -> int:
        """Number of events retained after an unsuccessful transport write."""
        return self._emitter.prepared_event_count

    @property
    def pending_asset_dependencies(self) -> tuple[str, ...]:
        return self._dispatcher.pending_asset_dependencies

    def start(self) -> ManagedClient:
        """Start the background socket reader and return this client."""
        if self._closed:
            raise RuntimeError("ManagedClient is closed")
        if not self._started:
            if self._receiver.token is None and self._persist_token:
                self._receiver.token = resolve_client_token(self._host, self._port, None, True)
            self._receiver.start()
            self._started = True
        return self

    def wait_connected(self, timeout: float | None = None) -> bool:
        """Wait for the handshake; queued replay still requires ``update``."""
        if not self._started:
            raise RuntimeError("ManagedClient has not been started")
        connected = self._receiver.wait_connected(timeout)
        if connected:
            self._require_layered_replay()
            self._connect_sender()
        elif self._receiver.auth_rejected:
            raise PermissionError("authentication rejected")
        elif self._receiver.hello_rejected:
            raise ConnectionError(self._receiver.rejection_reason or "connection rejected")
        return connected and self.connected

    def _require_layered_replay(self) -> None:
        if self._receiver.connected and not self._receiver.layered_replay_active:
            self.close()
            raise RuntimeError("server did not negotiate required layered replay")

    def _connect_sender(self) -> None:
        if self._sender.connected:
            return
        if self._sender.token is None:
            if self._receiver.token is not None:
                self._sender.token = self._receiver.token
            elif self._persist_token:
                self._sender.token = resolve_client_token(self._host, self._port, None, True)
        if not self._sender.connect():
            if self._sender.auth_rejected:
                raise PermissionError("sender authentication rejected")
            if self._sender.hello_rejected:
                raise ConnectionError(self._sender.rejection_reason or "sender connection rejected")
            raise ConnectionError(f"could not connect sender to {self._host}:{self._port}")

    def _send(self, events: list[dict]) -> int:
        if not events:
            return 0
        if self._sender.send_events(events):
            self._emitter.mark_prepared_events_sent(events)
            self._transform_coalescing.mark_submitted()
            return len(events)
        return 0

    def _prepare_outgoing_events(self) -> list[dict]:
        return self._transform_coalescing.prepare(self._emitter)

    def start_sender(self) -> bool:
        """Connect the sender explicitly for fail-fast error handling.

        Sender connection is otherwise best-effort during :meth:`update`.
        """
        self._connect_sender()
        return self._sender.connected

    def claim_playback(self, time: float | None = None) -> bool:
        """Request the shared-playback leader role."""
        return self._sender.claim_playback(time=time)

    def send_playback_control(
        self,
        action: str,
        *,
        time: float | None = None,
        rate: float | None = None,
    ) -> bool:
        """Drive the shared playhead (leader only)."""
        return self._sender.send_playback_control(action, time=time, rate=rate)

    def update(self) -> SyncUpdate:
        """Freeze local edits, apply the commit stream, then publish them."""
        if self._closed:
            raise RuntimeError("ManagedClient is closed")
        if not self._started:
            raise RuntimeError("ManagedClient has not been started")
        if self._stage is None:
            return SyncUpdate(
                received=0,
                sent=0,
                acknowledged=self._sender.drain_acknowledged_event_count(),
                pending=self._sender.pending_event_count,
            )
        self._require_layered_replay()

        # A queued authoritative record may touch the same field as a newer
        # local opinion. Freeze the local delta before dispatcher invalidation
        # advances emitter baselines, then send that exact batch after the
        # authoritative prefix has applied. SharedStageClient follows the same
        # prepare/apply/restore ordering at the Sdf-layer level.
        self._validate_authoring_target()
        outgoing = self._prepare_outgoing_events()
        received = self._dispatcher.drain_and_apply()

        sent = 0
        try:
            self._connect_sender()
        except (PermissionError, ConnectionError):
            pass  # sender connection is best-effort during update
        if self._sender.connected and self.synchronized:
            sent = self._send(outgoing)

        return SyncUpdate(
            received=received,
            sent=sent,
            acknowledged=self._sender.drain_acknowledged_event_count(),
            pending=self._sender.pending_event_count,
        )

    def publish_current_edit_target(self) -> int:
        """Publish all opinions currently authored in the active edit target.

        An earlier retained batch must be retried with :meth:`update` first.
        This keeps one call from ambiguously mixing two transport transactions.
        """
        if self._closed:
            raise RuntimeError("ManagedClient is closed")
        if self._stage is None:
            return 0
        self._validate_authoring_target()
        if not self._sender.connected:
            try:
                self._connect_sender()
            except (PermissionError, ConnectionError):
                return 0
        if self._emitter.prepared_event_count:
            raise RuntimeError(
                "an earlier publisher batch is still prepared; call update() "
                "before publishing the current edit target"
            )
        return self._send(self._emitter.prepare_snapshot_events_for_send())

    def rebind_stage(self, stage: Usd.Stage | None) -> None:
        """Move sending, receiving, and managed layers to a new stage.

        Pass ``None`` to park: the receiver stays connected and the queue
        continues to fill, but ``update()`` returns zero until a new stage
        is bound.
        """
        if self._closed:
            raise RuntimeError("ManagedClient is closed")
        if self._emitter.prepared_event_count:
            raise RuntimeError("cannot rebind while a prepared publisher batch is pending")
        if stage is None:
            self._dispatcher.unbind_stage()
            self._dispatcher.adapter = None
            self._emitter.cleanup()
            self._stage = None
            self._authoring_layer = None
            return
        adapter = UsdStageAdapter(stage)
        validate_layered_source(stage)
        self._dispatcher.adapter = adapter
        self._dispatcher.bind_layered_stage(stage)
        self._authoring_layer = self._ensure_convergent_edit_target(stage, self._app_name)
        self._emitter.rebind_stage(stage)
        self._stage = stage

    @staticmethod
    def _ensure_convergent_edit_target(stage: Usd.Stage, label: str) -> Sdf.Layer:
        """Return a target weaker than receiver-owned managed layers."""
        edit_layer = stage.GetEditTarget().GetLayer()
        session = stage.GetSessionLayer()
        if edit_layer is not session:
            return edit_layer
        authoring = Sdf.Layer.CreateAnonymous(f"openusdconnect-{label}-authoring")
        with Sdf.ChangeBlock():
            session.subLayerPaths.append(authoring.identifier)
        stage.SetEditTarget(Usd.EditTarget(authoring))
        return authoring

    def _validate_authoring_target(self) -> None:
        stage = self._stage
        if stage is None:
            return
        layer = stage.GetEditTarget().GetLayer()
        if layer is stage.GetSessionLayer():
            raise RuntimeError(
                "ManagedClient cannot publish from the strong session root; "
                "author into client.authoring_layer or another weaker layer"
            )
        router = self._dispatcher.layer_router
        if router is not None and router.key_for(layer) is not None:
            raise RuntimeError("ManagedClient cannot publish from a receiver-owned managed layer")

    def refresh_asset_dependency(
        self,
        asset_path: str | None = None,
    ) -> AssetDependencyRefreshResult:
        """Retry dependencies under the stage's current resolver context."""
        if self._closed:
            raise RuntimeError("ManagedClient is closed")
        return self._dispatcher.refresh_asset_dependency(asset_path)

    def close(self) -> None:
        """Stop networking and release receiver-owned collaboration layers."""
        if self._closed:
            return
        self._sender.disconnect()
        self._receiver.stop()
        if self._receiver.is_alive():
            self._receiver.join(timeout=2.0)
            if self._receiver.is_alive():
                LOG.warning("ManagedClient receiver thread did not stop within 2 seconds")
        self._dispatcher.close()
        self._emitter.cleanup()
        self._closed = True

    def __enter__(self) -> ManagedClient:
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False


__all__ = ["ManagedClient"]
