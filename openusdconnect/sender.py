"""TCP producer with durable transaction acknowledgement and reconnect replay."""

from __future__ import annotations

import logging
import socket
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from .codec import (
    PayloadType,
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


@dataclass(frozen=True, slots=True)
class _PendingTransaction:
    payload: bytes
    event_count: int
    layer_key: str = ""


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
        self._next_txn_id = 1
        self._pending: OrderedDict[int, _PendingTransaction] = OrderedDict()
        self._acknowledged_transactions = 0
        self._acknowledged_events = 0
        self._acknowledged_events_since_drain = 0
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
        with self._condition:
            return len(self._pending)

    @property
    def pending_event_count(self) -> int:
        with self._condition:
            return sum(item.event_count for item in self._pending.values())

    @property
    def acknowledged_transaction_count(self) -> int:
        with self._condition:
            return self._acknowledged_transactions

    @property
    def acknowledged_event_count(self) -> int:
        with self._condition:
            return self._acknowledged_events

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
            return self._failure is not None

    def connect(self, timeout: float | None = None) -> bool:
        """Handshake, start the result reader, and replay the exact outbox.

        ``timeout`` bounds this attempt and never extends the configured
        handshake timeout.
        """
        with self._connect_lock:
            with self._condition:
                if self.sock is not None:
                    return True
                if self._failure is not None or time.monotonic() < self._retry_after_until:
                    return False

            connect_timeout = self.handshake_timeout
            if timeout is not None:
                connect_timeout = min(connect_timeout, max(timeout, 0.0))
                if connect_timeout <= 0.0:
                    return False

            self.auth_rejected = False
            self.hello_rejected = False
            self.rejection_reason = ""
            sock: socket.socket | None = None
            try:
                sock = socket.create_connection(
                    (self.host, self.port), timeout=connect_timeout
                )
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
            except (OSError, IncompleteRead, MessageTooLarge, ValueError):
                LOG.exception("EventSender: handshake failed")
                self._close_socket_object(sock)
                return False

            if pt == PayloadType.AuthRejected:
                _, rejected = resolve_payload(env)
                self.rejection_reason = self._decode_string(rejected.Reason())
                self.auth_rejected = True
                self._close_socket_object(sock)
                return False
            if pt == PayloadType.HelloRejected:
                _, rejected = resolve_payload(env)
                self.rejection_reason = (
                    self._decode_string(rejected.Reason()) or "connection rejected"
                )
                self.hello_rejected = True
                self._close_socket_object(sock)
                return False
            if pt != PayloadType.HelloOk:
                LOG.error("EventSender: unexpected handshake response %s", pt)
                self._close_socket_object(sock)
                return False

            _, hello_ok = resolve_payload(env)
            self._acknowledge_through(int(hello_ok.CommittedThrough()))
            active_mode = LayerMode("shared_stage" if hello_ok.LayerMode() else "managed")
            if active_mode is not self.layer_mode:
                self.rejection_reason = (
                    f"server negotiated {active_mode.value} instead of {self.layer_mode.value}"
                )
                self._close_socket_object(sock)
                return False
            self.layer_mode_active = active_mode
            issued = self._decode_string(hello_ok.Token())
            if issued:
                self.token = issued
                if self._on_token_issued:
                    self._on_token_issued(issued)
            metadata = hello_ok.StageMetadata()
            if metadata is not None:
                decoded = _decode_stage_metadata_table(metadata)
                if decoded:
                    self.stage_metadata = decoded
                    if self._on_stage_metadata:
                        self._on_stage_metadata(decoded)
            sock.settimeout(None)

            # Serialize publication of the socket with outbox replay. A new
            # send cannot overtake an older pending transaction here.
            try:
                with self._send_lock:
                    with self._condition:
                        self.sock = sock
                        self._socket_generation += 1
                        generation = self._socket_generation
                        pending_payloads = [item.payload for item in self._pending.values()]
                    for payload in pending_payloads:
                        send_raw(sock, payload)
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
                len(pending_payloads),
            )
            return True

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
        try:
            # Transaction identity and wire order are one operation. Without
            # this outer lock, concurrent callers can allocate IDs 1 then 2
            # but acquire the socket lock and transmit them as 2 then 1.
            with self._send_lock:
                with self._condition:
                    if self.sock is None or self._failure is not None:
                        return False
                    if len(self._pending) >= self.max_pending_transactions:
                        return False
                    txn_id = self._next_txn_id
                    payload = encode_message(
                        make_txn(
                            events,
                            layer_key=layer_key,
                            txn_id=txn_id,
                        )
                    )
                    self._pending[txn_id] = _PendingTransaction(
                        payload, len(events), layer_key
                    )
                    self._next_txn_id += 1
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
        payload = encode_message(
            make_txn(events, layer_key=layer_key, txn_id=failure.txn_id)
        )
        repaired = _PendingTransaction(payload, len(events), layer_key)

        with self._send_lock:
            with self._condition:
                if self._failure is not failure:
                    raise RuntimeError("transaction rejection changed during recovery")
                if failure.txn_id not in self._pending:
                    raise RuntimeError("rejected transaction is missing from the outbox")
                self._pending[failure.txn_id] = repaired
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
                self._pending.clear()
                self.session_id = replacement
                self._next_txn_id = 1
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
        with self._condition:
            count = self._acknowledged_events_since_drain
            self._acknowledged_events_since_drain = 0
            return count

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
                if not self._pending:
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
                    self._condition.wait(
                        timeout=0.1 if remaining is None else min(0.1, remaining)
                    )

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
                    self._accept_result(result)
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

    def _accept_result(self, result) -> None:
        txn_id = int(result.TxnId())
        rejected_socket = None
        with self._condition:
            status_value = int(result.Status())
            if status_value == TransactionStatus.Acknowledged:
                self._acknowledge_through_locked(txn_id)
            else:
                code = int(result.RejectionCode())
                reason = self._decode_string(result.Reason())
                self._failure = TransactionFailure(
                    txn_id=txn_id,
                    code=code,
                    reason=reason,
                    expected_txn_id=int(result.ExpectedTxnId()),
                )
                self._recovery_artifact = RecoveryArtifact(
                    producer_session_id=self.session_id,
                    failure=self._failure,
                    transactions=tuple(
                        QuarantinedTransaction(
                            txn_id=pending_txn_id,
                            payload=pending.payload,
                            event_count=pending.event_count,
                            layer_key=pending.layer_key,
                        )
                        for pending_txn_id, pending in self._pending.items()
                    ),
                )
                self._recovery_incident = make_recovery_incident(
                    self._recovery_artifact
                )
                # Later IDs in this session cannot overtake the rejected ID.
                # Close immediately and retain the rejected transaction plus
                # its later suffix as quarantined evidence;
                # reconnect is disabled by _failure until explicit recovery.
                rejected_socket = self.sock
            self._condition.notify_all()
        if rejected_socket is not None:
            self._close(expected=rejected_socket)

    def _acknowledge_through(self, txn_id: int) -> None:
        with self._condition:
            self._acknowledge_through_locked(txn_id)
            self._condition.notify_all()

    def _acknowledge_through_locked(self, txn_id: int) -> None:
        acknowledged_transactions = 0
        acknowledged_events = 0
        while self._pending:
            pending_txn_id = next(iter(self._pending))
            if pending_txn_id > txn_id:
                break
            pending = self._pending.pop(pending_txn_id)
            acknowledged_transactions += 1
            acknowledged_events += pending.event_count
        self._next_txn_id = max(self._next_txn_id, txn_id + 1)
        self._acknowledged_transactions += acknowledged_transactions
        self._acknowledged_events += acknowledged_events
        self._acknowledged_events_since_drain += acknowledged_events

    def _close(self, *, expected: socket.socket | None = None) -> None:
        with self._condition:
            sock = self.sock
            if expected is not None and sock is not expected:
                return
            self.sock = None
            self._condition.notify_all()
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
