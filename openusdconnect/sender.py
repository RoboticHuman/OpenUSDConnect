"""Durable transaction producer whose connection runs on a native thread."""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable

from . import _client_backend
from .checkpoints import MirrorCheckpoint
from .codec import encode_message
from .protocol import make_claim_playback, make_playback_control, make_txn
from .protocol_constants import LayerMode
from .protocol_validation import validate_events
from .recovery import (
    QuarantinedTransaction,
    RecoveryArtifact,
    RecoveryIncident,
    RejectionDisposition,
    TransactionFailure,
    make_recovery_incident,
)

LOG = logging.getLogger(__name__)

_HANDSHAKE_TIMEOUT_S = 10.0
_MAX_PENDING_TRANSACTIONS = 10_000


class TransactionRejectedError(RuntimeError):
    """Raised by :meth:`EventSender.flush` after a server rejection."""

    def __init__(self, failure: TransactionFailure):
        super().__init__(str(failure))
        self.failure = failure


def _session_id(session_id: str | None) -> str:
    session_id = session_id or uuid.uuid4().hex
    if len(session_id) > 128:
        raise ValueError("session_id must contain 1-128 characters")
    return session_id


def _failure(native) -> TransactionFailure:
    return TransactionFailure(
        txn_id=native.transaction_id,
        code=native.code,
        reason=native.reason,
        expected_txn_id=native.expected_transaction_id,
    )


def _artifact(native) -> RecoveryArtifact:
    return RecoveryArtifact(
        producer_session_id=native.session_id,
        failure=_failure(native.failure),
        transactions=tuple(
            QuarantinedTransaction(
                txn_id=entry.transaction_id,
                payload=entry.payload,
                event_count=entry.event_count,
                layer_key=entry.layer_key,
            )
            for entry in native.transactions
        ),
    )


class EventSender:
    """Pipelined producer whose outbox survives reconnects.

    ``send_events`` returns once this object owns the encoded transaction. The
    native outbox removes it only after the server's cumulative durable
    acknowledgement covers it, and replays the same bytes under the same
    Hello-bound producer identity after a reconnect, so an acknowledgement lost
    after commit cannot apply the USD edits twice. A native thread, started by
    the first connection request, writes, reads, and runs the callbacks until
    :meth:`close`; given ``notifications``, the owner drains that queue instead
    and only ``on_token_issued`` runs there.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        client_id: str,
        role: str = "emitter",
        origin: str | None = None,
        department: str | None = None,
        token: str | None = None,
        handshake_timeout: float = _HANDSHAKE_TIMEOUT_S,
        on_token_issued: Callable[[str], None] | None = None,
        on_stage_metadata: Callable[[dict], None] | None = None,
        token_provider: Callable[[], str | None] | None = None,
        layer_mode: LayerMode | str = LayerMode.MANAGED,
        session_id: str | None = None,
        max_pending_transactions: int = _MAX_PENDING_TRANSACTIONS,
        notifications: _client_backend.NotificationQueue | None = None,
    ):
        if role != "emitter":
            raise ValueError("EventSender role must be 'emitter'")
        if max_pending_transactions < 1:
            raise ValueError("max_pending_transactions must be positive")
        self._host = host
        self._port = port
        self._client_id = client_id
        self._origin = origin
        self._department = department
        self._layer_mode = LayerMode(layer_mode)
        self._handshake_timeout = handshake_timeout
        self._max_pending_transactions = max_pending_transactions
        self.token = token
        self._token_provider = token_provider
        self._on_token_issued = on_token_issued
        self._on_stage_metadata = on_stage_metadata

        config = _client_backend.ProducerConfig()
        config.host = host
        config.port = port
        config.client_id = client_id
        config.origin = origin or ""
        config.department = department or ""
        config.layer_mode = _client_backend.NATIVE_LAYER_MODES[self._layer_mode]
        config.session_id = _session_id(session_id)
        config.handshake_timeout = handshake_timeout
        config.max_pending_transactions = max_pending_transactions
        notifications, owns_notifications = _client_backend.notification_queue(
            notifications, on_stage_metadata=on_stage_metadata
        )
        self._endpoint = _client_backend.ProducerEndpoint(config, notifications)
        self._driver = _client_backend.ProducerDriver(
            self._endpoint,
            notifications,
            _client_backend.TcpSocketFactory(),
            **_client_backend.driver_callbacks(self, LOG, sink=owns_notifications),
        )
        self._started = False
        # Pairs each transaction ID with the frame that encodes it.
        self._submit_lock = threading.Lock()
        # Builds the recovery objects once per failure, so their identity holds.
        self._recovery_lock = threading.Lock()
        self._recovery: tuple[RecoveryArtifact, RecoveryIncident] | None = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def role(self) -> str:
        return "emitter"

    @property
    def origin(self) -> str | None:
        return self._origin

    @property
    def department(self) -> str | None:
        return self._department

    @property
    def layer_mode(self) -> LayerMode:
        return self._layer_mode

    @property
    def handshake_timeout(self) -> float:
        return self._handshake_timeout

    @property
    def max_pending_transactions(self) -> int:
        return self._max_pending_transactions

    @property
    def session_id(self) -> str:
        """The producer session; :meth:`abandon_rejected_session` starts a new one."""
        return self._endpoint.status().session_id

    @property
    def is_connected(self) -> bool:
        return self._endpoint.status().connected

    @property
    def connected(self) -> bool:
        return self.is_connected

    @property
    def layer_mode_active(self) -> LayerMode:
        return _client_backend.LAYER_MODES[self._endpoint.status().layer_mode_active]

    @property
    def stage_metadata(self) -> dict:
        """The latest stage metadata the server authored, keyed as on the wire."""
        return _client_backend.stage_metadata_fields(self._endpoint.status().metadata)

    @property
    def auth_rejected(self) -> bool:
        rejection = self._endpoint.status().rejection
        return rejection is not None and rejection.authentication

    @property
    def hello_rejected(self) -> bool:
        rejection = self._endpoint.status().rejection
        return rejection is not None and not rejection.authentication

    @property
    def rejection_reason(self) -> str:
        """Why the latest handshake was refused, or the failure that refuses new ones."""
        rejection = self._endpoint.status().rejection
        if rejection is not None:
            return _client_backend.rejection_reason(rejection)
        failure = self.transaction_failure
        return "" if failure is None else failure.reason

    @property
    def pending_transaction_count(self) -> int:
        return self._endpoint.status().pending_transactions

    @property
    def pending_event_count(self) -> int:
        return self._endpoint.status().pending_events

    @property
    def acknowledged_transaction_count(self) -> int:
        return self._endpoint.status().acknowledged_transactions

    @property
    def acknowledged_event_count(self) -> int:
        return self._endpoint.status().acknowledged_events

    @property
    def acknowledged_checkpoint(self) -> MirrorCheckpoint | None:
        """Post-commit mirror position when all sends are acknowledged.

        None means pending/rejected work or a peer without checkpoint support.
        A Hello highwater alone does not establish a mirror checkpoint.
        """
        checkpoint = self._endpoint.acknowledged_checkpoint()
        if checkpoint is None:
            return None
        return MirrorCheckpoint(
            server_instance=checkpoint.server_instance,
            epoch=checkpoint.epoch,
            head_seq=checkpoint.head_sequence,
        )

    @property
    def transaction_failure(self) -> TransactionFailure | None:
        """Structured terminal result for UI and recovery policy."""
        recovery = self._current_recovery()
        return None if recovery is None else recovery[0].failure

    @property
    def transaction_error(self) -> str:
        failure = self.transaction_failure
        return "" if failure is None else str(failure)

    @property
    def recovery_incident(self) -> RecoveryIncident | None:
        """Immutable summary suitable for status polling and host UI."""
        recovery = self._current_recovery()
        return None if recovery is None else recovery[1]

    @property
    def recovery_artifact(self) -> RecoveryArtifact | None:
        """Exact quarantined bytes for inspection or application-owned export."""
        recovery = self._current_recovery()
        return None if recovery is None else recovery[0]

    @property
    def recovery_disposition(self) -> RejectionDisposition | None:
        """Recommended response category for the current rejection."""
        failure = self.transaction_failure
        return None if failure is None else failure.disposition

    @property
    def recovery_required(self) -> bool:
        """Whether a deterministic rejection quarantined this producer session."""
        return self._current_recovery() is not None

    def snapshot(self) -> _client_backend.ProducerStatus:
        """The native status in one call, for reading several fields together."""
        return self._endpoint.status()

    def request_connect(self, timeout: float | None = 2.0) -> bool:
        """Start one background attempt, returning whether it was scheduled.

        Call again from an update loop to retry transient failures. Attempts
        back off from one to eight seconds; rejection requires explicit connect.
        Callbacks run on the connection thread, just as for synchronous connect.
        """
        if not self._endpoint.request_connect(timeout):
            return False
        self._start()
        self._driver.wake()
        return True

    def cancel_connect(self) -> bool:
        """Invalidate pending handshakes without waiting; report completion.

        An already published connection is left intact. Use disconnect to close
        it as well. A cancelled attempt can no longer publish its connection.
        """
        finished = self._endpoint.cancel_connect()
        self._driver.wake()
        return finished

    def connect(self, timeout: float | None = None) -> bool:
        """Handshake and replay the exact outbox; return whether connected.

        An attempt already in flight is waited for first. ``timeout`` bounds the
        whole call and never extends the configured handshake timeout.
        """
        self._start()
        return self._driver.connect(timeout)

    def disconnect(self) -> None:
        """Close the connection while retaining unacknowledged transactions."""
        self._endpoint.disconnect()
        self._driver.wake()

    def close(self, timeout: float | None = None) -> bool:
        """Stop the connection thread and close its connection; repeated calls are harmless.

        Waits up to ``timeout`` seconds for the thread to exit, without limit
        for ``None``, and returns whether it has exited: ``True`` before the
        first connection request, ``False`` on timeout or from a callback,
        which runs on that thread. Afterwards :meth:`connect` and
        :meth:`request_connect` return ``False``. Closing does not flush; call
        :meth:`flush` first if the outbox matters.
        """
        self._driver.stop()
        return self._driver.join(timeout)

    def __enter__(self) -> EventSender:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def send_events(self, events: list, *, layer_key: str = "") -> bool:
        """Submit a transaction without waiting for its durable result.

        ``True`` means the encoded bytes are owned by the bounded outbox, even
        if the connection fails afterwards. ``False`` means no ownership was
        taken (disconnected, empty input, full outbox, or a terminal rejection).
        """
        if not events:
            return False
        validate_events(events, layer_mode=self._layer_mode)
        with self._submit_lock:
            txn_id = self._endpoint.next_transaction_id()
            payload = encode_message(make_txn(events, layer_key=layer_key, txn_id=txn_id))
            result = self._endpoint.append(txn_id, payload, len(events), layer_key)
        if result != _client_backend.ProducerResult.ACCEPTED:
            return False
        self._driver.wake()
        return True

    def repair_rejected_transaction(self, events: list, *, layer_key: str = "") -> int:
        """Replace a recoverable rejected transaction at the same ordered ID.

        The caller must first reconcile against current authoritative state and
        rebuild the events for that state. This method deliberately performs no
        semantic merge. It only restores the rejected sequence boundary ahead
        of later quarantined transactions and returns the reused transaction ID.
        Call :meth:`connect` afterwards to replay the repaired outbox.
        """
        if not events:
            raise ValueError("repair events must not be empty")
        validate_events(events, layer_mode=self._layer_mode)
        with self._recovery_lock:
            recovery = self._current_recovery_locked()
            if recovery is None:
                raise RuntimeError("there is no rejected transaction to retry")
            failure = recovery[0].failure
            if failure.disposition is not RejectionDisposition.RECOVERABLE_CONFLICT:
                raise RuntimeError(
                    f"{failure.code_name} is {failure.disposition.value}, not recoverable"
                )
            payload = encode_message(make_txn(events, layer_key=layer_key, txn_id=failure.txn_id))
            result = self._endpoint.repair_rejected(payload, len(events), layer_key)
            if result != _client_backend.ProducerResult.ACCEPTED:
                raise RuntimeError(f"native recovery rejected repair: {result}")
            self._recovery = None
        return failure.txn_id

    def abandon_rejected_session(self, *, session_id: str | None = None) -> RecoveryArtifact:
        """Discard a rejected session's outbox and return its preserved evidence.

        This is a transport-level recovery boundary. The caller must reconcile
        its USD stage before reconnecting or submitting rebuilt intent with the
        new producer session.
        """
        replacement = _session_id(session_id)
        with self._recovery_lock:
            recovery = self._current_recovery_locked()
            if recovery is None:
                raise RuntimeError("there is no rejected producer session to abandon")
            artifact = recovery[0]
            if replacement == artifact.producer_session_id:
                raise ValueError("replacement session_id must differ from rejected session")
            # The checks above are the endpoint's preconditions.
            abandoned = self._endpoint.abandon_rejected_session(replacement)
            assert abandoned is not None
            self._recovery = None
        return artifact

    def send_message(self, msg: dict) -> bool:
        """Send a non-transaction protocol message (not retained for replay)."""
        if not self._endpoint.queue_control(encode_message(msg)):
            return False
        self._driver.wake()
        return True

    def drain_acknowledged_event_count(self) -> int:
        """Return successful event acknowledgements received since the last drain."""
        return self._endpoint.drain_acknowledged_event_count()

    def flush(self, timeout: float | None = None) -> bool:
        """Wait for all submitted transactions to reach a terminal result.

        Reconnects and replays while time remains. Returns ``False`` on timeout;
        a deterministic server rejection raises ``TransactionRejectedError``.
        """
        result = self._driver.flush(timeout)
        if result == _client_backend.FlushResult.RECOVERY_REQUIRED:
            failure = self.transaction_failure
            # None only when another thread resolved the failure meanwhile.
            if failure is not None:
                raise TransactionRejectedError(failure)
        return result == _client_backend.FlushResult.FLUSHED

    def claim_playback(self, time: float | None = None) -> bool:
        return self.send_message(make_claim_playback(self._client_id, time=time))

    def send_playback_control(
        self,
        action: str,
        *,
        time: float | None = None,
        rate: float | None = None,
    ) -> bool:
        return self.send_message(make_playback_control(action, time=time, rate=rate))

    def _start(self) -> None:
        """Start the connection thread once; it runs until closed or collected."""
        if not self._started:
            self._driver.start()
            self._started = True

    def _current_recovery(self) -> tuple[RecoveryArtifact, RecoveryIncident] | None:
        with self._recovery_lock:
            return self._current_recovery_locked()

    def _current_recovery_locked(self) -> tuple[RecoveryArtifact, RecoveryIncident] | None:
        # Only repair and abandon clear a failure, and both reset this cache.
        if self._recovery is None:
            native = self._endpoint.artifact()
            if native is not None:
                artifact = _artifact(native)
                self._recovery = (artifact, make_recovery_incident(artifact))
        return self._recovery

    def _connection_token(self) -> str | None:
        """The token for the next handshake; ``None`` abandons the attempt."""
        if self._token_provider is not None:
            try:
                self.token = self._token_provider()
            except Exception:
                LOG.exception("EventSender: token provider failed")
                return None
        return self.token or ""

    def _token_issued(self, token: str) -> None:
        """Adopt a token the server issued, on the connection thread."""
        self.token = token
        self._notify(self._on_token_issued, token, "on_token_issued")

    def _deliver(self, notification) -> None:
        """Run the callback for a notification on the connection thread."""
        if isinstance(notification, _client_backend.StageMetadata):
            self._notify(
                self._on_stage_metadata,
                _client_backend.stage_metadata_fields(notification),
                "on_stage_metadata",
            )

    @staticmethod
    def _notify(callback: Callable | None, value, name: str) -> None:
        if callback is None:
            return
        try:
            callback(value)
        except Exception:
            LOG.exception("EventSender: %s callback failed", name)


__all__ = ["EventSender", "TransactionRejectedError"]
