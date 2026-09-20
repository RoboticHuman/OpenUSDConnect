"""Background TCP receiver with replay-aware reconnection."""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections import deque
from collections.abc import Callable

from . import _client_backend
from .codec import (
    HelloRejectionCode,
    PayloadType,
    _decode_stage_metadata_table,
    decode_envelope,
    encode_message,
    message_to_dict,
    payload_type_and_sequence,
    resolve_payload,
)
from .defaults import DEFAULT_HOST, DEFAULT_SYNC_PORT
from .framing import IncompleteRead, MessageTooLarge, recv_framed
from .protocol import make_hello
from .protocol_constants import LayerMode
from .transport import send_msg

LOG = logging.getLogger(__name__)

_RECONNECT_BASE_DELAY = 1.0
_RECONNECT_MAX_DELAY = 30.0
_SOCKET_TIMEOUT = 30.0
_MAX_QUEUE_DEPTH = 50_000
_MAX_CONSECUTIVE_TIMEOUTS = 10

_MESSAGE_KIND_BY_PAYLOAD = {
    PayloadType.BroadcastEvent: _client_backend.ReceiverMessageKind.EVENT,
    PayloadType.LayerGraphState: _client_backend.ReceiverMessageKind.LAYER_GRAPH_STATE,
    PayloadType.Resync: _client_backend.ReceiverMessageKind.RESYNC,
}
_PLAYBACK_PAYLOAD_TYPES = frozenset(
    {
        PayloadType.PlaybackState,
        PayloadType.PlaybackClaimed,
        PayloadType.PlaybackRejected,
    }
)


class ReceiverThread(threading.Thread):
    """Receive wire messages off-thread for a stage-owning consumer to drain.

    Scene events are queued as raw FlatBuffers for consumer-thread decoding
    and USD mutation. Handshake and control messages, including their callbacks,
    are processed on the receiver thread. Overflow closes the connection and
    resumes by replay after the queue drains or the drain wait expires.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_SYNC_PORT,
        sync_from: int = 1,
        reconnect: bool = True,
        max_queue: int = _MAX_QUEUE_DEPTH,
        socket_timeout: float = _SOCKET_TIMEOUT,
        client_id: str | None = None,
        reconnect_base_delay: float = _RECONNECT_BASE_DELAY,
        reconnect_max_delay: float = _RECONNECT_MAX_DELAY,
        origin: str | None = None,
        department: str | None = None,
        token: str | None = None,
        on_token_issued: Callable[[str], None] | None = None,
        on_stage_metadata: Callable[[dict], None] | None = None,
        on_playback_state: Callable[[dict], None] | None = None,
        on_playback_claimed: Callable[[dict], None] | None = None,
        on_playback_rejected: Callable[[dict], None] | None = None,
        layered_replay: bool = True,
        layer_mode: LayerMode | str = LayerMode.MANAGED,
    ):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.sync_from = sync_from
        self.reconnect = reconnect
        self.max_queue = max_queue
        self.socket_timeout = socket_timeout
        self.client_id = client_id
        self.origin = origin
        self.department = department
        self.token = token
        self.layered_replay = bool(layered_replay)
        self.layered_replay_active = False
        self.layer_mode = LayerMode(layer_mode)
        self.layer_mode_active = LayerMode.MANAGED
        self._on_token_issued = on_token_issued
        self._on_stage_metadata = on_stage_metadata
        self._on_playback_state = on_playback_state
        self._on_playback_claimed = on_playback_claimed
        self._on_playback_rejected = on_playback_rejected
        self._reconnect_base_delay = reconnect_base_delay
        self._reconnect_max_delay = reconnect_max_delay
        self._stop_event = threading.Event()
        self.sock: socket.socket | None = None
        self._socket_lock = threading.Lock()
        self._inbox = _client_backend.ReceiverInbox(sync_from, max_queue)
        self._connected_event = threading.Event()
        self._synchronized_event = threading.Event()
        self._handshake_event = threading.Event()
        self._replay_lock = threading.RLock()
        self._received_replay_identity: tuple[str, int] | None = None
        self._initial_replay_identity: tuple[str, int] | None = None
        self._connection_server_instance = ""
        self._replay_identity_supported = False
        self._hello_sent = False
        self._prefix_validation_requested = False
        self._connection_prefix_proven = False
        self._reset_on_hello = False
        self.replay_head_seq = 0
        self.replay_epoch = 0
        self.server_instance = ""
        self.auth_rejected = False
        self.hello_rejected = False
        self.rejection_code = HelloRejectionCode.Unspecified
        self.rejection_reason = ""
        self.connection_error: Exception | None = None
        self.stage_metadata: dict = {}

    @property
    def connected(self) -> bool:
        return self._connected_event.is_set()

    @connected.setter
    def connected(self, value: bool) -> None:
        with self._replay_lock:
            if value:
                self._connected_event.set()
            else:
                self._connected_event.clear()
                self._synchronized_event.clear()
                self._inbox.disconnect(self._inbox.generation)

    @property
    def synchronized(self) -> bool:
        """Whether replay through the server's advertised head was applied."""
        return self.connected and self._synchronized_event.is_set()

    @property
    def last_seq(self) -> int:
        return self._inbox.last_sequence

    @property
    def queued_message_count(self) -> int:
        """Number of received messages waiting for the owning thread to drain."""

        return self._inbox.size

    def wait_connected(self, timeout: float | None = None) -> bool:
        """Wait for the current handshake result, not for replay completion."""
        if self.connected:
            return True
        self._handshake_event.wait(timeout=timeout)
        return self.connected

    def wait_synchronized(self, timeout: float | None = None) -> bool:
        """Wait for replay to be applied by the stage-owning consumer thread."""
        if self.synchronized:
            return True
        self._synchronized_event.wait(timeout=timeout)
        return self.synchronized

    def mark_replay_applied(self) -> bool:
        """Publish READY after a successful drain applied the replay prefix."""
        with self._replay_lock:
            if not self._inbox.mark_replay_applied():
                return False
            self.replay_head_seq = self._inbox.replay_head_sequence
            self.replay_epoch = self._inbox.replay_epoch
            # Publish the applied identity, not a new socket's un-applied Hello.
            self.server_instance = (
                self._received_replay_identity[0] if self._received_replay_identity else ""
            )
            self._synchronized_event.set()
            return True

    def run(self) -> None:
        delay = self._reconnect_base_delay
        while not self._stop_event.is_set():
            self.connection_error = None
            was_connected = False
            try:
                self._connect_and_recv()
            except Exception as exc:
                self.connection_error = exc
                if not self._stop_event.is_set():
                    LOG.exception("ReceiverThread: connection error")
            finally:
                was_connected = self.connected
                self.connected = False
                self._close_socket()

            if self._should_stop_reconnecting():
                # Always release waiters when this thread terminates.
                self._handshake_event.set()
                break

            self._handshake_event.clear()
            if was_connected:
                delay = self._reconnect_base_delay

            if self._inbox.overflowed:
                self._inbox.clear_overflow()
                delay = self._reconnect_base_delay
                self._wait_for_queue_drain()
                continue

            LOG.info("ReceiverThread: reconnecting in %.1fs", delay)
            if self._stop_event.wait(timeout=delay):
                break
            delay = min(delay * 2, self._reconnect_max_delay)

        LOG.info("ReceiverThread stopped")

    def _should_stop_reconnecting(self) -> bool:
        return (
            not self.reconnect
            or self._stop_event.is_set()
            or self.auth_rejected
            or self.hello_rejected
        )

    def _wait_for_queue_drain(self) -> None:
        LOG.info("ReceiverThread: waiting for queue to drain before reconnect")
        deadline = time.monotonic() + self._reconnect_max_delay
        while self._inbox.size and not self._stop_event.wait(timeout=0.1):
            if time.monotonic() >= deadline:
                LOG.warning("ReceiverThread: drain wait timed out, reconnecting anyway")
                return

    def _connect_and_recv(self) -> None:
        """Single connection attempt: connect, handshake, read until EOF/error."""
        LOG.info("ReceiverThread connecting to %s:%s", self.host, self.port)
        sock = socket.create_connection(
            (self.host, self.port),
            timeout=self.socket_timeout,
        )
        with self._socket_lock:
            self.sock = sock
        sock.settimeout(self.socket_timeout)
        # Send small handshake messages promptly.
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        if self._stop_event.is_set():
            self._close_socket(sock)
            return

        with self._replay_lock:
            connection = self._inbox.begin_connection()
            connection_generation = connection.generation
            sync_from = connection.sync_from
            prefix_identity = self._received_replay_identity
            self._synchronized_event.clear()

        hello = make_hello(
            "receiver",
            sync_from=sync_from,
            client_id=self.client_id,
            origin=self.origin,
            department=self.department,
            token=self.token,
            layered_replay=self.layered_replay,
            layer_mode=self.layer_mode,
        )
        # Initial explicit cursors may refer to an externally supplied snapshot.
        # Preserve that contract, but do not claim its unvalidated prefix as proof.
        self._prefix_validation_requested = self._hello_sent
        if self._prefix_validation_requested:
            hello["replay_server_instance"] = prefix_identity[0] if prefix_identity else ""
            if prefix_identity is not None:
                hello["replay_epoch"] = prefix_identity[1]
        self._hello_sent = True
        send_msg(sock, hello)
        self._handshake_event.clear()
        self.auth_rejected = False
        self.hello_rejected = False
        self.rejection_code = HelloRejectionCode.Unspecified
        self.rejection_reason = ""

        consecutive_timeouts = 0
        while not self._stop_event.is_set():
            try:
                buf = recv_framed(sock)
            except TimeoutError:
                consecutive_timeouts += 1
                if consecutive_timeouts >= _MAX_CONSECUTIVE_TIMEOUTS:
                    LOG.warning(
                        "ReceiverThread: %d consecutive timeouts, reconnecting",
                        consecutive_timeouts,
                    )
                    break
                LOG.debug(
                    "ReceiverThread: recv timeout (%d/%d)",
                    consecutive_timeouts,
                    _MAX_CONSECUTIVE_TIMEOUTS,
                )
                continue
            except (IncompleteRead, MessageTooLarge):
                current_generation = connection_generation == self._inbox.generation
                if not self._stop_event.is_set() and current_generation:
                    LOG.warning("ReceiverThread: framing error during read")
                break
            except OSError:
                current_generation = connection_generation == self._inbox.generation
                if not self._stop_event.is_set() and current_generation:
                    LOG.warning("ReceiverThread: socket error during read")
                break

            consecutive_timeouts = 0

            if connection_generation != self._inbox.generation:
                return

            if not self.connected:
                if not self._handle_handshake_message(buf, sync_from, connection_generation):
                    return
                continue

            if not self._handle_data_message(buf, connection_generation):
                return

    @staticmethod
    def _decode_text(value: str | bytes | None) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return value or ""

    @staticmethod
    def _invoke_callback(callback: Callable | None, value, name: str) -> None:
        if callback is None:
            return
        try:
            callback(value)
        except Exception:
            LOG.exception("ReceiverThread: %s callback failed", name)

    def _reject_hello(self, code: int, reason: str) -> None:
        self.hello_rejected = True
        self.rejection_code = code
        self.rejection_reason = reason
        LOG.error("ReceiverThread: connection rejected (%s): %s", code, reason)
        self._handshake_event.set()

    def _handle_handshake_message(
        self, buf: bytes, sync_from: int, connection_generation: int | None = None,
    ) -> bool:
        if connection_generation is None:
            connection_generation = self._inbox.generation
        env = decode_envelope(buf)
        payload_type = env.PayloadType()

        if payload_type == PayloadType.AuthRejected:
            _, rejection = resolve_payload(env)
            reason = self._decode_text(rejection.Reason())
            LOG.error("ReceiverThread: auth rejected %s", reason)
            self.auth_rejected = True
            self.rejection_reason = reason
            self._handshake_event.set()
            return False

        if payload_type == PayloadType.HelloRejected:
            _, rejection = resolve_payload(env)
            self._reject_hello(
                int(rejection.Code()),
                self._decode_text(rejection.Reason()),
            )
            return False

        if payload_type != PayloadType.HelloOk:
            return True

        _, hello = resolve_payload(env)
        self._connection_server_instance = self._decode_text(hello.ServerInstance()) or ""
        self._replay_identity_supported = bool(hello.ReplayIdentity())
        self._connection_prefix_proven = sync_from == 1 or self._prefix_validation_requested
        self.layer_mode_active = LayerMode.SHARED_STAGE if hello.LayerMode() else LayerMode.MANAGED
        if self.layer_mode_active is not self.layer_mode:
            self._reject_hello(
                HelloRejectionCode.LayerModeMismatch,
                "server did not negotiate requested layer mode",
            )
            return False

        self.layered_replay_active = bool(self.layered_replay and hello.LayeredReplay())
        if self.layered_replay and not self.layered_replay_active:
            self._reject_hello(
                HelloRejectionCode.LayeredReplayRequired,
                "server did not negotiate requested layered replay",
            )
            return False

        issued_token = self._decode_text(hello.Token())
        if issued_token:
            self.token = issued_token
            self._invoke_callback(self._on_token_issued, issued_token, "on_token_issued")
            LOG.info("ReceiverThread: token issued by server")

        metadata_table = hello.StageMetadata()
        if metadata_table is not None:
            metadata = _decode_stage_metadata_table(metadata_table)
            if metadata:
                self.stage_metadata = metadata
                self._invoke_callback(self._on_stage_metadata, metadata, "on_stage_metadata")

        with self._replay_lock:
            if connection_generation != self._inbox.generation:
                return False
            epoch = hello.ReplayEpoch()
            identity = (
                (self._connection_server_instance, int(epoch))
                if self._replay_identity_supported and self._connection_server_instance
                and epoch is not None else None
            )
            # This proof belongs only to the handshake's replay, not later live resets.
            self._initial_replay_identity = identity
            if identity is None:
                self._received_replay_identity = None
            if self._reset_on_hello:
                if not self._handle_data_message(
                    encode_message({"type": "resync"}), connection_generation,
                ):
                    return False
                self._reset_on_hello = False
            if identity is not None and (
                sync_from == 1 or (
                    self._prefix_validation_requested
                    and self._received_replay_identity == identity
                )
            ):
                self._received_replay_identity = identity
            # A changed/unknown prefix keeps its old identity until Resync is accepted.
            self.connected = True
        self._handshake_event.set()
        LOG.info("ReceiverThread connected (sync_from=%d)", sync_from)
        return True

    def _handle_control_message(
        self,
        payload_type: int,
        buf: bytes,
        connection_generation: int,
    ) -> bool | None:
        if payload_type == PayloadType.Ping:
            return True

        if payload_type == PayloadType.ReplayComplete:
            complete = message_to_dict(buf)
            head_seq = int(complete["head_seq"])
            epoch = int(complete["epoch"])
            with self._replay_lock:
                result = self._inbox.accept_replay_complete(
                    connection_generation,
                    head_seq,
                    epoch,
                )
                if result == _client_backend.AcceptResult.ACCEPTED:
                    self._initial_replay_identity = None
                    # This covers received/queued frames, even before their consumer
                    # drains them, including resets without a handshake epoch.
                    self._received_replay_identity = (
                        (self._connection_server_instance, epoch)
                        if (
                            self._replay_identity_supported
                            and self._connection_prefix_proven
                            and self._connection_server_instance
                        )
                        else None
                    )
            if result == _client_backend.AcceptResult.STALE_GENERATION:
                return False
            return True

        if payload_type not in _PLAYBACK_PAYLOAD_TYPES:
            return None

        if payload_type == PayloadType.PlaybackState:
            callback = self._on_playback_state
        elif payload_type == PayloadType.PlaybackClaimed:
            callback = self._on_playback_claimed
        else:
            callback = self._on_playback_rejected
        self._invoke_callback(callback, message_to_dict(buf), "playback")
        return True

    def _handle_data_message(self, buf: bytes, connection_generation: int) -> bool:
        payload_type, sequence = payload_type_and_sequence(buf)
        handled = self._handle_control_message(payload_type, buf, connection_generation)
        if handled is not None:
            return handled

        kind = _MESSAGE_KIND_BY_PAYLOAD.get(
            payload_type,
            _client_backend.ReceiverMessageKind.OTHER,
        )
        with self._replay_lock:
            result = self._inbox.accept(connection_generation, kind, sequence, buf)
            if (
                kind == _client_backend.ReceiverMessageKind.RESYNC
                and result == _client_backend.AcceptResult.ACCEPTED
            ):
                self._synchronized_event.clear()
                self._received_replay_identity = self._initial_replay_identity
                self._initial_replay_identity = None
                self._connection_prefix_proven = True
        if result == _client_backend.AcceptResult.STALE_GENERATION:
            return False
        if result == _client_backend.AcceptResult.DUPLICATE:
            return True
        if result == _client_backend.AcceptResult.SEQUENCE_GAP:
            replay_from = self._inbox.last_applied_sequence + 1
            LOG.error(
                "ReceiverThread: sequence gap before %d; replaying from applied %d",
                sequence,
                replay_from,
            )
            self.request_replay_from(replay_from)
            return False
        if result == _client_backend.AcceptResult.QUEUE_FULL:
            LOG.warning(
                "ReceiverThread: queue full (%d), disconnecting to replay from server",
                self.max_queue,
            )
            return False
        return True

    def request_replay_from(self, seq_start: int) -> None:
        """Reconnect and request replay beginning at ``seq_start``.

        Consumers call this after a queued frame fails to decode. Frames queued
        after the failed frame are discarded, and the connection generation
        prevents an in-flight read from adding more stale frames.
        """
        seq_start = int(seq_start)
        if seq_start < 1:
            raise ValueError("replay sequence must be at least 1")

        with self._replay_lock:
            self._inbox.request_replay_from(seq_start)
            self._synchronized_event.clear()
            # Discarded frames may include an unapplied reset. Their received
            # identity cannot prove the consumer's retained prefix, at any cursor.
            self._received_replay_identity = None
            self._initial_replay_identity = None
            if seq_start == 1:
                self._reset_on_hello = True
        self._close_socket()

    def _close_socket(self, sock: socket.socket | None = None) -> None:
        """Detach and close a socket, logging cleanup errors at debug level."""
        with self._socket_lock:
            if sock is None:
                sock = self.sock
            if self.sock is sock:
                self.sock = None

        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            LOG.debug(
                "ReceiverThread: socket shutdown failed during close",
                exc_info=True,
            )
        try:
            sock.close()
        except OSError:
            LOG.debug("ReceiverThread: socket close failed", exc_info=True)

    def drain_queue(self, max_messages: int | None = None) -> deque:
        """Drain queued wire messages, optionally limiting work for this tick."""
        if max_messages is not None and (
            isinstance(max_messages, bool) or not isinstance(max_messages, int) or max_messages < 1
        ):
            raise ValueError("max_messages must be a positive integer or None")
        return deque(self._inbox.drain(max_messages))

    def stop(self) -> None:
        """Request clean shutdown."""
        self._stop_event.set()
        self._handshake_event.set()
        self._close_socket()
