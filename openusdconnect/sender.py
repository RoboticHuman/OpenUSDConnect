"""TCP producer with durable transaction acknowledgement and reconnect replay."""

from __future__ import annotations

import logging
import socket
import threading
import time
import uuid
from collections.abc import Callable

from . import _client_backend
from .codec import (
    PayloadType,
    TransactionRejectionCode,
    TransactionStatus,
    _decode_stage_metadata_table,
    decode_envelope,
    encode_message,
    resolve_payload,
)
from .framing import IncompleteRead, MessageTooLarge, recv_framed
from .protocol import (
    make_claim_playback,
    make_hello,
    make_playback_control,
    make_quit,
    make_txn,
)
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
from .transport import send_msg, send_raw

LOG = logging.getLogger(__name__)

_HANDSHAKE_TIMEOUT_S = 10.0
_MAX_PENDING_TRANSACTIONS = 10_000


class TransactionRejectedError(RuntimeError):
    """Raised by :meth:`EventSender.flush` after a server rejection."""

    def __init__(self, failure: TransactionFailure):
        super().__init__(str(failure))
        self.failure = failure


class EventSender:
    """Pipelined producer whose outbox survives socket reconnects.

    ``send_events`` returns once this object owns the encoded transaction. A
    background reader removes it only after the server's cumulative durable
    acknowledgement covers it. The same encoded bytes and Hello-bound producer
    identity are replayed after reconnect, so an ACK lost after commit cannot
    apply the USD edits twice.
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
        layer_mode: LayerMode | str = LayerMode.MANAGED,
        session_id: str | None = None,
        max_pending_transactions: int = _MAX_PENDING_TRANSACTIONS,
    ):
        if role != "emitter":
            raise ValueError("EventSender role must be 'emitter'")
        if max_pending_transactions < 1:
            raise ValueError("max_pending_transactions must be positive")
        self.host = host
        self.port = port
        self.client_id = client_id
        self.role = role
        self.origin = origin
        self.department = department
        self.token = token
        self.layer_mode = LayerMode(layer_mode)
        self.layer_mode_active = LayerMode.MANAGED
        self.handshake_timeout = handshake_timeout
        self.session_id = session_id or uuid.uuid4().hex
        if not self.session_id or len(self.session_id) > 128:
            raise ValueError("session_id must contain 1-128 characters")
        self.max_pending_transactions = max_pending_transactions
        self._on_token_issued = on_token_issued
        self._on_stage_metadata = on_stage_metadata

        self.sock: socket.socket | None = None
        self.auth_rejected = False
        self.hello_rejected = False
        self.rejection_reason = ""
        self.stage_metadata: dict = {}

        self._condition = threading.Condition(threading.RLock())
        self._connect_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        self._socket_generation = 0
        self._session = _client_backend.ProducerSession(max_pending_transactions)
        self._failure: TransactionFailure | None = None
        self._recovery_artifact: RecoveryArtifact | None = None
        self._recovery_incident: RecoveryIncident | None = None
        self._retry_after_until = 0.0

    @property
    def is_connected(self) -> bool:
        with self._condition:
            return self.sock is not None

    @property
    def connected(self) -> bool:
        return self.is_connected

    @property
    def pending_transaction_count(self) -> int:
        return self._session.pending_transaction_count

    @property
    def pending_event_count(self) -> int:
        return self._session.pending_event_count

    @property
    def acknowledged_transaction_count(self) -> int:
        return self._session.acknowledged_transaction_count

    @property
    def acknowledged_event_count(self) -> int:
        return self._session.acknowledged_event_count

    @property
    def _next_txn_id(self) -> int:
        """Compatibility view of the native outbox sequence cursor."""
        return self._session.next_transaction_id

    @property
    def transaction_error(self) -> str:
        with self._condition:
            return str(self._failure) if self._failure is not None else ""

    @property
    def transaction_failure(self) -> TransactionFailure | None:
        """Structured terminal result for UI and recovery policy."""
        with self._condition:
            return self._failure

    @property
    def recovery_incident(self) -> RecoveryIncident | None:
        """Immutable summary suitable for status polling and host UI."""
        with self._condition:
            return self._recovery_incident

    @property
    def recovery_artifact(self) -> RecoveryArtifact | None:
        """Exact quarantined bytes for inspection or application-owned export."""
        with self._condition:
            return self._recovery_artifact

    @property
    def recovery_disposition(self) -> RejectionDisposition | None:
        """Recommended response category for the current rejection."""
        with self._condition:
            return self._failure.disposition if self._failure is not None else None

    @property
    def recovery_required(self) -> bool:
        """Whether a deterministic rejection quarantined this producer session."""
        with self._condition:
            return self._session.recovery_required

    def connect(self, timeout: float | None = None) -> bool:
        """Handshake, start the result reader, and replay the exact outbox.

        ``timeout`` bounds this attempt and never extends the configured
        handshake timeout.
        """
        with self._connect_lock:
            with self._condition:
                if self.sock is not None:
                    return True
                if self._session.recovery_required or time.monotonic() < self._retry_after_until:
                    return False

            connect_timeout = self.handshake_timeout
            if timeout is not None:
                connect_timeout = min(connect_timeout, max(timeout, 0.0))
                if connect_timeout <= 0.0:
                    return False

            connection = self._session.begin_connection()
            if connection is None:
                return False
            generation = connection.generation
            self._socket_generation = generation

            self.auth_rejected = False
            self.hello_rejected = False
            self.rejection_reason = ""
            sock: socket.socket | None = None
            try:
                sock = socket.create_connection((self.host, self.port), timeout=connect_timeout)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                send_msg(
                    sock,
                    make_hello(
                        self.role,
                        client_id=self.client_id,
                        origin=self.origin,
                        department=self.department,
                        token=self.token,
                        layer_mode=self.layer_mode,
                        producer_session_id=self.session_id,
                    ),
                )
                buf = recv_framed(sock)
                env = decode_envelope(buf)
                pt = env.PayloadType()
                if not self._accept_handshake_response(sock, env, pt, generation):
                    self._session.disconnect(generation)
                    self._close_socket_object(sock)
                    return False
            except (OSError, IncompleteRead, MessageTooLarge, ValueError):
                LOG.exception("EventSender: handshake failed")
                self._session.disconnect(generation)
                self._close_socket_object(sock)
                return False
            except Exception:
                # Resolver/plugin callbacks and malformed FlatBuffer accessors
                # are outside the narrow transport exception family above, but
                # they must still leave this sender disconnected.
                LOG.exception("EventSender: unexpected handshake failure")
                self._session.disconnect(generation)
                self._close_socket_object(sock)
                return False

            # Serialize publication of the socket with outbox replay. A new
            # send cannot overtake an older pending transaction here.
            try:
                with self._send_lock:
                    with self._condition:
                        self.sock = sock
                    replayed = 0
                    while pending := self._session.claim_next_unsent(generation):
                        send_raw(sock, pending[1])
                        replayed += 1
            except OSError:
                LOG.info("EventSender: reconnect replay failed", exc_info=True)
                self._close(expected=sock)
                return False

            reader = threading.Thread(
                target=self._read_results,
                args=(sock, generation),
                name=f"openusdconnect-ack-{self.client_id}",
                daemon=True,
            )
            with self._condition:
                self._reader_thread = reader
            reader.start()
            LOG.info(
                "EventSender connected to %s:%d (session=%s, pending=%d)",
                self.host,
                self.port,
                self.session_id,
                replayed,
            )
            return True

    def _accept_handshake_response(
        self, sock: socket.socket, env, payload_type: int, generation: int
    ) -> bool:
        """Validate one server hello and initialize connection metadata.

        Application callbacks are observers. Their failure is logged but does
        not invalidate an otherwise completed protocol handshake.
        """
        if payload_type == PayloadType.AuthRejected:
            _, rejected = resolve_payload(env)
            self.rejection_reason = self._decode_string(rejected.Reason())
            self.auth_rejected = True
            return False
        if payload_type == PayloadType.HelloRejected:
            _, rejected = resolve_payload(env)
            self.rejection_reason = self._decode_string(rejected.Reason()) or "connection rejected"
            self.hello_rejected = True
            return False
        if payload_type != PayloadType.HelloOk:
            LOG.error("EventSender: unexpected handshake response %s", payload_type)
            return False

        _, hello_ok = resolve_payload(env)
        active_mode = LayerMode("shared_stage" if hello_ok.LayerMode() else "managed")
        if active_mode is not self.layer_mode:
            self.rejection_reason = (
                f"server negotiated {active_mode.value} instead of {self.layer_mode.value}"
            )
            return False
        self.layer_mode_active = active_mode

        committed_through = int(hello_ok.CommittedThrough())
        result = self._session.accept_hello(generation, committed_through)
        if result != _client_backend.ProducerResult.ACCEPTED:
            self.rejection_reason = self._highwater_failure_reason(result, committed_through)
            self._record_session_failure(
                txn_id=committed_through,
                code=int(TransactionRejectionCode.UnexpectedId),
                reason=self.rejection_reason,
            )
            return False

        issued = self._decode_string(hello_ok.Token())
        if issued:
            self.token = issued
            self._notify_handshake_callback(
                self._on_token_issued,
                issued,
                name="on_token_issued",
            )

        metadata = hello_ok.StageMetadata()
        if metadata is not None:
            decoded = _decode_stage_metadata_table(metadata)
            if decoded:
                self.stage_metadata = decoded
                self._notify_handshake_callback(
                    self._on_stage_metadata,
                    decoded,
                    name="on_stage_metadata",
                )
        sock.settimeout(None)
        return True

    @staticmethod
    def _notify_handshake_callback(callback, value, *, name: str) -> None:
        if callback is None:
            return
        try:
            callback(value)
        except Exception:
            LOG.exception("EventSender: %s callback failed", name)

    def disconnect(self) -> None:
        """Close the socket while retaining unacknowledged transactions."""
        with self._send_lock:
            with self._condition:
                sock = self.sock
            if sock is not None:
                try:
                    send_msg(sock, make_quit())
                except OSError:
                    pass
        self._close(expected=sock)

    def send_events(self, events: list, *, layer_key: str = "") -> bool:
        """Submit a transaction without waiting for its durable result.

        ``True`` means the encoded bytes are owned by the bounded outbox, even
        if the socket fails during this call. ``False`` means no ownership was
        taken (disconnected before submission, empty input, full outbox, or a
        terminal rejection).
        """
        if not events:
            return False
        validate_events(events, layer_mode=self.layer_mode)
        try:
            # Transaction identity and wire order are one operation. Without
            # this outer lock, concurrent callers can allocate IDs 1 then 2
            # but acquire the socket lock and transmit them as 2 then 1.
            with self._send_lock:
                with self._condition:
                    if self.sock is None or self._failure is not None:
                        return False
                    if not self._session.can_append:
                        return False
                    generation = self._socket_generation
                    txn_id = self._session.next_transaction_id
                    payload = encode_message(
                        make_txn(
                            events,
                            layer_key=layer_key,
                            txn_id=txn_id,
                        )
                    )
                    result = self._session.append(
                        generation, txn_id, payload, len(events), layer_key
                    )
                    if result != _client_backend.ProducerResult.ACCEPTED:
                        return False
                    sock = self.sock
                if sock is not None:
                    send_raw(sock, payload)
        except OSError:
            LOG.info(
                "EventSender: send became ambiguous; retaining transaction",
                exc_info=True,
            )
            self._close(expected=sock)
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
        validate_events(events, layer_mode=self.layer_mode)
        with self._condition:
            failure = self._failure
            if failure is None:
                raise RuntimeError("there is no rejected transaction to retry")
            if failure.disposition is not RejectionDisposition.RECOVERABLE_CONFLICT:
                raise RuntimeError(
                    f"{failure.code_name} is {failure.disposition.value}, not recoverable"
                )

        # A rejection normally already closes this socket. Make the boundary
        # explicit so a racing reader cannot leave a repaired outbox attached to
        # the connection that delivered the rejection.
        self.disconnect()
        payload = encode_message(make_txn(events, layer_key=layer_key, txn_id=failure.txn_id))
        with self._send_lock:
            with self._condition:
                if self._failure is not failure:
                    raise RuntimeError("transaction rejection changed during recovery")
                result = self._session.repair_rejected(payload, len(events), layer_key)
                if result != _client_backend.ProducerResult.ACCEPTED:
                    raise RuntimeError(f"native recovery rejected repair: {result}")
                self._failure = None
                self._recovery_artifact = None
                self._recovery_incident = None
                self._retry_after_until = 0.0
                self._condition.notify_all()
        return failure.txn_id

    def abandon_rejected_session(self, *, session_id: str | None = None) -> RecoveryArtifact:
        """Discard a rejected session's outbox and return its preserved evidence.

        This is a transport-level recovery boundary. The caller must reconcile
        its USD stage before reconnecting or submitting rebuilt intent with the
        new producer session.
        """
        replacement = session_id or uuid.uuid4().hex
        if not replacement or len(replacement) > 128:
            raise ValueError("session_id must contain 1-128 characters")

        with self._condition:
            failure = self._failure
            artifact = self._recovery_artifact
            previous_session_id = self.session_id
            if failure is None or artifact is None:
                raise RuntimeError("there is no rejected producer session to abandon")
            if replacement == previous_session_id:
                raise ValueError("replacement session_id must differ from rejected session")

        self.disconnect()
        with self._send_lock:
            with self._condition:
                if self._failure is not failure or self._recovery_artifact is not artifact:
                    raise RuntimeError("transaction rejection changed during recovery")
                self._session.reset_session()
                self.session_id = replacement
                self._failure = None
                self._recovery_artifact = None
                self._recovery_incident = None
                self._retry_after_until = 0.0
                self.rejection_reason = ""
                self._condition.notify_all()
        return artifact

    def send_message(self, msg: dict) -> bool:
        """Send a non-transaction protocol message (not retained for replay)."""
        with self._condition:
            sock = self.sock
        if sock is None:
            return False
        try:
            with self._send_lock:
                with self._condition:
                    if self.sock is not sock:
                        return False
                send_msg(sock, msg)
            return True
        except OSError:
            self._close(expected=sock)
            return False

    def drain_acknowledged_event_count(self) -> int:
        """Return successful event acknowledgements received since the last drain."""
        return self._session.drain_acknowledged_event_count()

    def flush(self, timeout: float | None = None) -> bool:
        """Wait for all submitted transactions to reach a terminal result.

        Reconnects and replays while time remains. Returns ``False`` on timeout;
        a deterministic server rejection raises ``TransactionRejectedError``.
        """
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        while True:
            with self._condition:
                if self._failure is not None:
                    raise TransactionRejectedError(self._failure)
                if self._session.empty:
                    return True
                connected = self.sock is not None
                retry_at = self._retry_after_until
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                if connected:
                    self._condition.wait(
                        timeout=0.25 if remaining is None else min(0.25, remaining)
                    )
                    continue
            delay = max(0.0, retry_at - time.monotonic())
            if delay:
                if deadline is not None and time.monotonic() + delay >= deadline:
                    return False
                time.sleep(min(delay, 0.25))
                continue
            if not self.connect():
                with self._condition:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return False
                    self._condition.wait(timeout=0.1 if remaining is None else min(0.1, remaining))

    def claim_playback(self, time: float | None = None) -> bool:
        return self.send_message(make_claim_playback(self.client_id, time=time))

    def send_playback_control(
        self,
        action: str,
        *,
        time: float | None = None,
        rate: float | None = None,
    ) -> bool:
        return self.send_message(make_playback_control(action, time=time, rate=rate))

    def _read_results(self, sock: socket.socket, generation: int) -> None:
        try:
            while True:
                buf = recv_framed(sock)
                env = decode_envelope(buf)
                if env.PayloadType() == PayloadType.TransactionResult:
                    _, result = resolve_payload(env)
                    self._accept_result(result, generation)
                elif env.PayloadType() == PayloadType.RateLimited:
                    _, limited = resolve_payload(env)
                    with self._condition:
                        self._retry_after_until = max(
                            self._retry_after_until,
                            time.monotonic() + float(limited.RetryAfter()),
                        )
                    break
        except (OSError, IncompleteRead, MessageTooLarge, ValueError):
            pass
        finally:
            with self._condition:
                is_current = generation == self._socket_generation and self.sock is sock
            if is_current:
                self._close(expected=sock)

    def _accept_result(self, result, generation: int) -> None:
        txn_id = int(result.TxnId())
        rejected_socket = None
        with self._condition:
            status_value = int(result.Status())
            if status_value == TransactionStatus.Acknowledged:
                accepted = self._session.acknowledge_through(generation, txn_id)
                if accepted == _client_backend.ProducerResult.STALE_GENERATION:
                    return
                if accepted != _client_backend.ProducerResult.ACCEPTED:
                    failure = TransactionFailure(
                        txn_id=txn_id,
                        code=int(TransactionRejectionCode.UnexpectedId),
                        reason=self._highwater_failure_reason(accepted, txn_id),
                    )
                    self._record_session_failure_locked(failure)
                    rejected_socket = self.sock
            else:
                code = int(result.RejectionCode())
                reason = self._decode_string(result.Reason())
                failure = TransactionFailure(
                    txn_id=txn_id,
                    code=code,
                    reason=reason,
                    expected_txn_id=int(result.ExpectedTxnId()),
                )
                native_disposition = {
                    RejectionDisposition.RECOVERABLE_CONFLICT: (
                        _client_backend.ProducerRecoveryDisposition.RECOVERABLE_CONFLICT
                    ),
                    RejectionDisposition.INVALID_OPERATION: (
                        _client_backend.ProducerRecoveryDisposition.INVALID_OPERATION
                    ),
                    RejectionDisposition.SESSION_FATAL: (
                        _client_backend.ProducerRecoveryDisposition.SESSION_FATAL
                    ),
                }[failure.disposition]
                accepted = self._session.reject(generation, txn_id, native_disposition)
                if accepted == _client_backend.ProducerResult.STALE_GENERATION:
                    return
                if accepted == _client_backend.ProducerResult.TRANSACTION_MISSING:
                    failure = TransactionFailure(
                        txn_id=txn_id,
                        code=int(TransactionRejectionCode.UnexpectedId),
                        reason=f"server rejected unknown transaction {txn_id}",
                    )
                self._record_session_failure_locked(failure)
                rejected_socket = self.sock
            self._condition.notify_all()
        if rejected_socket is not None:
            self._close(expected=rejected_socket)

    def _record_session_failure(
        self, *, txn_id: int, code: int, reason: str, expected_txn_id: int = 0
    ) -> None:
        with self._condition:
            self._record_session_failure_locked(
                TransactionFailure(
                    txn_id=txn_id,
                    code=code,
                    reason=reason,
                    expected_txn_id=expected_txn_id,
                )
            )
            self._condition.notify_all()

    def _record_session_failure_locked(self, failure: TransactionFailure) -> None:
        self._failure = failure
        self._recovery_artifact = RecoveryArtifact(
            producer_session_id=self.session_id,
            failure=failure,
            transactions=tuple(
                QuarantinedTransaction(
                    txn_id=pending_txn_id,
                    payload=payload,
                    event_count=event_count,
                    layer_key=layer_key,
                )
                for pending_txn_id, payload, event_count, layer_key in self._session.entries()
            ),
        )
        self._recovery_incident = make_recovery_incident(self._recovery_artifact)

    def _highwater_failure_reason(self, result, transaction_id: int) -> str:
        if result == _client_backend.ProducerResult.HIGHWATER_AHEAD:
            return (
                f"server producer highwater {transaction_id} is ahead of local "
                f"transaction {self._session.next_transaction_id - 1}"
            )
        if result == _client_backend.ProducerResult.HIGHWATER_REGRESSED:
            return (
                f"server producer highwater regressed from "
                f"{self._session.last_acknowledged_transaction_id} to {transaction_id}"
            )
        return f"invalid producer result for transaction {transaction_id}: {result}"

    def _close(self, *, expected: socket.socket | None = None) -> None:
        with self._condition:
            sock = self.sock
            if expected is not None and sock is not expected:
                return
            self.sock = None
            generation = self._socket_generation
            self._condition.notify_all()
        self._session.disconnect(generation)
        self._close_socket_object(sock)

    @staticmethod
    def _close_socket_object(sock: socket.socket | None) -> None:
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    @staticmethod
    def _decode_string(value) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return value or ""


__all__ = ["EventSender", "TransactionRejectedError"]
