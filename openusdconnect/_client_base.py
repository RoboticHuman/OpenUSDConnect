"""Lifecycle shells the high-level clients build on.

``ClientBase`` owns the lifecycle, status, observer hooks, and credential of
every client. ``PublishingClientBase`` adds the sender role: recovery,
playback, and durable completion. ``EmitterClientBase`` adds stage-edit
capture through a ``NoticeEmitter`` with optional transform coalescing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pxr import Usd

from ._client_lifecycle import (
    DEFAULT_WAIT_TIMEOUT_S,
    ClientCallbackQueue,
    _pause_before_poll,
    compute_phase,
    deadline_after,
    raise_if_rejected,
    remaining_time,
    stop_receiver,
    submit_and_wait,
    wait_until_ready,
)
from ._client_utils import ClientCredential
from ._observer_hooks import observer_hooks, stage_metadata_from_message
from .client_observer import ClientObserver, StageMetadata
from .client_types import ClientStatus, SyncUpdate
from .coalescing import TransformCoalescingWindow
from .emitter import NoticeEmitter, PrimChannel
from .receiver import ReceiverThread
from .recovery import RecoveryArtifact, RecoveryError, RejectionDisposition, TransactionFailure
from .sender import EventSender


class ClientBase:
    """Lifecycle, status, observer hooks, and credential shared by every client.

    A subclass builds ``_sender`` and/or ``_receiver`` from ``_credential`` and
    ``_hooks``, implements ``_is_synchronized``, and overrides the other status
    hooks it needs. Call every method from the stage-owning thread.
    """

    _sender: EventSender | None = None
    _receiver: ReceiverThread | None = None
    # Reconnection deliberately suspended by the host (UsdPublisher.disconnect).
    _paused = False

    def __init__(
        self,
        *,
        host: str,
        port: int,
        token: str | None,
        persist_token: bool,
        observer: ClientObserver | None,
    ):
        self._host = host
        self._port = port
        self._started = False
        self._closed = False
        self._callbacks = ClientCallbackQueue()
        self._hooks = observer_hooks(observer, self._callbacks.wrap)
        self._credential = ClientCredential(
            host, port, token, persist_token, self._hooks.on_token_issued,
        )

    @property
    def client_id(self) -> str:
        """Stable identity shared by every connection role."""
        return self._endpoints[0].client_id

    @property
    def stage_metadata(self) -> StageMetadata:
        return stage_metadata_from_message(self._endpoints[0].stage_metadata)

    @property
    def status(self) -> ClientStatus:
        """Current state as one immutable value; read it on the stage-owning thread."""
        receiver, sender = self._receiver, self._sender
        endpoints = self._endpoints
        failure = None if sender is None else sender.transaction_failure
        rebuild_reason = self._rebuild_reason()
        auth_rejected = any(endpoint.auth_rejected for endpoint in endpoints)
        rejected = auth_rejected or any(endpoint.hello_rejected for endpoint in endpoints)
        connected = not self._closed and all(endpoint.connected for endpoint in endpoints)
        synchronized = not self._closed and self._is_synchronized()
        phase = compute_phase(
            closed=self._closed,
            recovery_required=failure is not None or bool(rebuild_reason),
            rejected=rejected,
            parked=self._is_parked(),
            replaying=receiver is not None and receiver.connected and not receiver.synchronized,
            ready=connected and synchronized,
            connecting=self._started and not self._paused,
        )
        if failure is not None:
            reason = str(failure)
        else:
            reasons = [e.rejection_reason for e in (sender, receiver) if e is not None]
            reason = rebuild_reason or next((text for text in reasons if text), "")
        return ClientStatus(
            phase=phase,
            connected=connected,
            synchronized=synchronized,
            receiver_connected=None if receiver is None else receiver.connected,
            sender_connected=None if sender is None else sender.connected,
            prepared_events=self._prepared_events(),
            pending_events=0 if sender is None else sender.pending_event_count,
            acknowledged_events_total=0 if sender is None else sender.acknowledged_event_count,
            failure=failure,
            recovery=None if sender is None else sender.recovery_incident,
            reason=reason,
            auth_rejected=auth_rejected,
            has_unsent_changes=not self._closed and self._has_unsent_changes(),
            **self._role_status(),
        )

    def start(self):
        """Start background networking without blocking and return this client."""
        self._require_open()
        if not self._started:
            if self._receiver is not None:
                self._receiver.start()
            self._started = True
        return self

    def connect(self, timeout: float | None = DEFAULT_WAIT_TIMEOUT_S) -> bool:
        """Start and complete the handshakes within ``timeout``.

        Queued replay still needs :meth:`update` on the stage-owning thread.
        """
        self.start()
        if self._receiver is not None:
            deadline = deadline_after(timeout)
            if not self._receiver.wait_connected(timeout):
                raise_if_rejected(self._receiver, type(self).__name__)
                return False
            timeout = remaining_time(deadline)
        return self._sender is None or self._connect_sender(timeout)

    def wait_until_ready(self, timeout: float | None = DEFAULT_WAIT_TIMEOUT_S) -> bool:
        """Pump updates until ``READY``; ``False`` only on timeout."""
        return wait_until_ready(self, timeout)

    def close(self) -> None:
        """Stop networking and release client-owned resources."""
        if self._closed:
            return
        self._callbacks.close()
        if self._sender is not None:
            self._sender.disconnect()
        if self._receiver is not None:
            stop_receiver(self._receiver)
        self._release()
        self._closed = True

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False

    @property
    def _endpoints(self) -> tuple:
        return tuple(endpoint for endpoint in (self._receiver, self._sender) if endpoint)

    def _is_synchronized(self) -> bool:
        """Status hook: the local state has applied the server's replay."""
        raise NotImplementedError

    def _is_parked(self) -> bool:
        """Status hook: no stage is bound."""
        return False

    def _rebuild_reason(self) -> str:
        """Status hook: why the host must rebuild before continuing, else empty."""
        return ""

    def _prepared_events(self) -> int:
        return 0

    def _has_unsent_changes(self) -> bool:
        return False

    def _role_status(self) -> dict:
        """Status hook: client-specific ``ClientStatus`` fields."""
        return {}

    def _release(self) -> None:
        """Release subclass resources after networking has stopped."""

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError(f"{type(self).__name__} is closed")

    def _require_started(self) -> None:
        self._require_open()
        if not self._started:
            raise RuntimeError(f"{type(self).__name__} has not been started")

    def _begin_update(self) -> bool:
        """Deliver queued notifications; ``False`` when one of them closed the client."""
        self._require_started()
        self._callbacks.drain()
        return not self._closed

    def _connect_sender(self, timeout: float | None = None) -> bool:
        if self._sender.connected:
            return True
        if not self._sender.connect(timeout=timeout):
            raise_if_rejected(self._sender, type(self).__name__)
            return False
        return True

    def _progress(self, applied: int = 0, submitted: int = 0) -> SyncUpdate:
        sender = self._sender
        if sender is None:
            return SyncUpdate(applied_events=applied, submitted_events=submitted)
        return SyncUpdate(
            applied_events=applied,
            submitted_events=submitted,
            acknowledged_events_delta=sender.drain_acknowledged_event_count(),
            pending_events=sender.pending_event_count,
            recovery=sender.recovery_incident,
        )


class PublishingClientBase(ClientBase):
    """Adds the sender role: recovery, playback, and durable completion."""

    @property
    def recovery_artifact(self) -> RecoveryArtifact | None:
        """Exact quarantined transactions for integration-owned recovery."""
        return self._sender.recovery_artifact

    def flush(self, timeout: float | None = DEFAULT_WAIT_TIMEOUT_S) -> bool:
        """Wait until submitted work is durable; ``False`` on timeout."""
        self._require_open()
        return self._sender.flush(timeout)

    def submit_and_wait(self, timeout: float | None = DEFAULT_WAIT_TIMEOUT_S) -> bool:
        """Publish noticed edits and wait until durable; ``False`` only on timeout."""
        return submit_and_wait(self, timeout)

    def claim_playback(self, time: float | None = None) -> bool:
        """Request the shared-playback leader role."""
        self._require_open()
        return self._sender.claim_playback(time=time)

    def send_playback_control(
        self,
        action: str,
        *,
        time: float | None = None,
        rate: float | None = None,
    ) -> bool:
        """Drive the shared playhead (leader only)."""
        self._require_open()
        return self._sender.send_playback_control(action, time=time, rate=rate)

    def _require_recoverable_failure(self) -> TransactionFailure:
        self._require_open()
        failure = self._sender.transaction_failure
        if failure is None:
            raise RecoveryError("no_incident", "there is no recovery incident to resolve")
        if failure.disposition is not RejectionDisposition.RECOVERABLE_CONFLICT:
            raise RecoveryError(
                "wrong_recovery_kind",
                f"{failure.code_name} is {failure.disposition.value}, not recoverable",
            )
        return failure

    def _repair_and_reconnect(self, events: list[dict], *, layer_key: str = "") -> int:
        """Replace the rejected transaction at its ID, then reconnect the outbox."""
        self._require_recoverable_failure()
        txn_id = self._sender.repair_rejected_transaction(events, layer_key=layer_key)
        if not self._connect_sender():
            raise ConnectionError(
                f"transaction {txn_id} repaired but reconnect to "
                f"{self._host}:{self._port} failed; it remains queued"
            )
        return txn_id

    def _replay_to_fresh_checkpoint(self, timeout: float | None) -> None:
        """Apply replay through a new server watermark before resolving recovery."""
        deadline = deadline_after(timeout)
        receiver = self._receiver
        reconnect = receiver.reconnect
        receiver.reconnect = True
        try:
            receiver.request_replay_from(self.last_seq + 1)
            while True:
                self._apply_queued()
                if receiver.synchronized:
                    return
                if not _pause_before_poll(deadline):
                    raise TimeoutError("authoritative recovery replay timed out")
        finally:
            receiver.reconnect = reconnect

    def _apply_queued(self) -> None:
        """Apply what the receiver has queued; clients with a receiver implement it."""
        raise NotImplementedError

    def _resume_sender_after_recovery(self, timeout: float | None) -> None:
        """Best-effort producer reconnect after recovery has committed."""
        try:
            self._connect_sender(timeout=timeout)
        except (PermissionError, ConnectionError):
            # Recovery already completed and must not look rolled back. Status
            # exposes rejection/offline state; update() retries ordinary loss.
            pass


class EmitterClientBase(PublishingClientBase):
    """Adds stage-edit capture through a NoticeEmitter with transform coalescing."""

    def _init_emitter(
        self,
        stage: Usd.Stage,
        *,
        attr_filter: Callable[[str], bool] | None,
        replicated_api_schemas: set[str] | None,
        extra_channels: Sequence[PrimChannel] | None,
        transform_coalesce_seconds: float,
    ) -> None:
        self._transform_coalescing = TransformCoalescingWindow(transform_coalesce_seconds)
        self._emitter = NoticeEmitter(
            stage,
            attr_filter=attr_filter,
            replicated_api_schemas=replicated_api_schemas,
            extra_channels=extra_channels,
        )

    @property
    def sender(self) -> EventSender:
        """The underlying :class:`EventSender`; a diagnostic handle."""
        return self._sender

    @property
    def emitter(self) -> NoticeEmitter:
        """The underlying :class:`NoticeEmitter`; a diagnostic handle."""
        return self._emitter

    def flush(self, timeout: float | None = DEFAULT_WAIT_TIMEOUT_S) -> bool:
        """Submit a coalesced transform, then wait until submitted work is durable."""
        self._require_open()
        deadline = deadline_after(timeout)
        if self._transform_coalescing.buffering:
            if not self._ready_to_publish(remaining_time(deadline)):
                return False
            events = self._transform_coalescing.force(self._emitter)
            if events and not self._send(events):
                return False
        return self._sender.flush(remaining_time(deadline))

    def publish_current_edit_target(self) -> int:
        """Queue every opinion in the edit target; returns events this call submitted."""
        self._require_open()
        if not self._can_capture_edit_target():
            return 0
        if self._emitter.prepared_event_count:
            raise RuntimeError(
                "an earlier publisher batch is still prepared; call update() "
                "before publishing the current edit target"
            )
        self.start()
        self._emitter.prepare_snapshot_events_for_send()
        return self.update().submitted_events

    def _ready_to_publish(self, timeout: float | None) -> bool:
        """Connect the sender within *timeout*; ``False`` while publishing must wait."""
        return self._connect_sender(timeout) and self._is_synchronized()

    def _can_capture_edit_target(self) -> bool:
        return True

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

    def _prepared_events(self) -> int:
        return self._emitter.prepared_event_count

    def _has_unsent_changes(self) -> bool:
        return self._emitter.has_local_changes

    def _release(self) -> None:
        self._emitter.cleanup()
