"""Background receiver thread connects to server, queues incoming events.

ReceiverThread is DCC-agnostic. It connects to the server as a receiver,
reads length-prefixed FlatBuffers messages in a background thread, and
provides a thread-safe queue for the main thread to drain. DCC-specific
timer/callback registration is the plugin's responsibility.

Features:
- Automatic reconnection with exponential backoff on connection loss
- Socket timeout to detect hung connections
- Bounded queue on overflow, disconnects, waits for drain, then reconnects for replay
"""

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

# Reconnection defaults
_RECONNECT_BASE_DELAY = 1.0  # seconds
_RECONNECT_MAX_DELAY = 30.0  # seconds
_SOCKET_TIMEOUT = 30.0  # seconds detect hung connections
_MAX_QUEUE_DEPTH = 50_000  # max queued messages before overflow (disconnect + replay)
_MAX_CONSECUTIVE_TIMEOUTS = 10  # 10 x 30s = 5 min max idle before reconnect


class ReceiverThread(threading.Thread):
    """Background TCP client that connects to server and queues incoming events.

    Automatically reconnects on connection loss with exponential backoff.
    Uses socket timeouts to detect hung servers. Queue depth is bounded
    to prevent unbounded memory growth.

    The queue stores raw FlatBuffers bytes.  Consumers use the codec to
    decode them (zero-copy via ``decode_envelope`` / ``resolve_payload``).

    Usage:
        rt = ReceiverThread(host="127.0.0.1", port=7200)
        rt.start()
        # ... periodically on main thread:
        for raw_buf in rt.drain_queue():
            env = decode_envelope(raw_buf)
            msg_type, obj = resolve_payload(env)
            # process typed FB object
        # ... when done:
        rt.stop()
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
        on_token_issued: Callable | None = None,
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
        self._received_replay_complete: tuple[int, int, int, int] | None = None
        self.replay_head_seq = 0
        self.replay_epoch = 0
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
    def connected(self, value: bool):
        if value:
            self._connected_event.set()
        else:
            self._connected_event.clear()
            self._synchronized_event.clear()
            self._inbox.disconnect(self._inbox.generation)
            self._received_replay_complete = None

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
        if not self._inbox.mark_replay_applied():
            return False
        self.replay_head_seq = self._inbox.replay_head_sequence
        self.replay_epoch = self._inbox.replay_epoch
        self._synchronized_event.set()
        return True

    def run(self):
        delay = self._reconnect_base_delay
        while not self._stop_event.is_set():
            self.connection_error = None
            try:
                self._connect_and_recv()
            except Exception as exc:
                self.connection_error = exc
                if not self._stop_event.is_set():
                    LOG.exception("ReceiverThread: connection error")
            finally:
                self.connected = False
                self._close_socket()

            if (
                not self.reconnect
                or self._stop_event.is_set()
                or self.auth_rejected
                or self.hello_rejected
            ):
                # Authentication/negotiation paths signal this themselves.
                # Transport and callback failures must do so here, otherwise
                # wait_connected(None) can outlive a terminated thread.
                self._handshake_event.set()
                break

            self._handshake_event.clear()

            if self._inbox.overflowed:
                # Intentional disconnect wait for main thread to drain
                # before reconnecting, otherwise we'll overflow again.
                self._inbox.clear_overflow()
                delay = self._reconnect_base_delay
                LOG.info("ReceiverThread: waiting for queue to drain before reconnect")
                drain_start = time.monotonic()
                while not self._stop_event.is_set():
                    if self._inbox.size == 0:
                        break
                    if time.monotonic() - drain_start > self._reconnect_max_delay:
                        LOG.warning("ReceiverThread: drain wait timed out, reconnecting anyway")
                        break
                    if self._stop_event.wait(timeout=0.1):
                        break
                continue

            LOG.info("ReceiverThread: reconnecting in %.1fs", delay)
            if self._stop_event.wait(timeout=delay):
                break  # stop requested during backoff
            delay = min(delay * 2, self._reconnect_max_delay)

        LOG.info("ReceiverThread stopped")

    def _connect_and_recv(self):
        """Single connection attempt: connect, handshake, read until EOF/error."""
        LOG.info("ReceiverThread connecting to %s:%s", self.host, self.port)
        sock = socket.create_connection(
            (self.host, self.port),
            timeout=self.socket_timeout,
        )
        with self._socket_lock:
            self.sock = sock
        sock.settimeout(self.socket_timeout)
        # Hello/acks are small; Nagle would delay them. Matches the server's
        # accepted-socket setting.
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        if self._stop_event.is_set():
            self._close_socket(sock)
            return

        connection = self._inbox.begin_connection()
        connection_generation = connection.generation
        sync_from = connection.sync_from
        self._synchronized_event.clear()
        self._received_replay_complete = None

        # Send hello as receiver, normally resuming after the latest queued
        # sequence. A decode failure can override this with the last sequence
        # the consumer applied successfully.
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

            # Pre-handshake: check for auth/hello_ok messages
            if not self.connected:
                env = decode_envelope(buf)
                pt = env.PayloadType()

                if pt == PayloadType.AuthRejected:
                    _, ar = resolve_payload(env)
                    reason = ar.Reason()
                    if isinstance(reason, bytes):
                        reason = reason.decode("utf-8")
                    LOG.error("ReceiverThread: auth rejected %s", reason)
                    self.auth_rejected = True
                    self._handshake_event.set()
                    return

                if pt == PayloadType.HelloRejected:
                    _, rejection = resolve_payload(env)
                    code = int(rejection.Code())
                    reason = rejection.Reason()
                    if isinstance(reason, bytes):
                        reason = reason.decode("utf-8")
                    self.hello_rejected = True
                    self.rejection_code = code
                    self.rejection_reason = reason or ""
                    LOG.error(
                        "ReceiverThread: connection rejected (%s): %s",
                        self.rejection_code,
                        self.rejection_reason,
                    )
                    self._handshake_event.set()
                    return

                if pt == PayloadType.HelloOk:
                    _, ho = resolve_payload(env)
                    self.layer_mode_active = LayerMode(
                        "shared_stage" if ho.LayerMode() else "managed"
                    )
                    if self.layer_mode_active is not self.layer_mode:
                        self.hello_rejected = True
                        self.rejection_code = HelloRejectionCode.LayerModeMismatch
                        self.rejection_reason = "server did not negotiate requested layer mode"
                        LOG.error("ReceiverThread: %s", self.rejection_reason)
                        self._handshake_event.set()
                        return
                    self.layered_replay_active = bool(self.layered_replay and ho.LayeredReplay())
                    if self.layered_replay and not self.layered_replay_active:
                        self.hello_rejected = True
                        self.rejection_code = HelloRejectionCode.LayeredReplayRequired
                        self.rejection_reason = "server did not negotiate requested layered replay"
                        LOG.error(
                            "ReceiverThread: %s",
                            self.rejection_reason,
                        )
                        self._handshake_event.set()
                        return
                    issued = ho.Token()
                    if issued:
                        if isinstance(issued, bytes):
                            issued = issued.decode("utf-8")
                        self.token = issued
                        if self._on_token_issued:
                            try:
                                self._on_token_issued(issued)
                            except Exception:
                                LOG.exception(
                                    "ReceiverThread: on_token_issued callback failed",
                                )
                        LOG.info("ReceiverThread: token issued by server")
                    sm = ho.StageMetadata()
                    if sm is not None:
                        meta = _decode_stage_metadata_table(sm)
                        if meta:
                            self.stage_metadata = meta
                            if self._on_stage_metadata:
                                try:
                                    self._on_stage_metadata(meta)
                                except Exception:
                                    LOG.exception(
                                        "ReceiverThread: on_stage_metadata callback failed",
                                    )
                    self.connected = True
                    self._handshake_event.set()
                    LOG.info("ReceiverThread connected (sync_from=%d)", sync_from)
                continue

            # Post-handshake data messages only need their tag and sequence in
            # this thread; full decoding stays on the stage-owning consumer.
            pt, seq = payload_type_and_sequence(buf)
            if pt == PayloadType.Ping:
                continue

            # Playback messages are control-plane signals: fire callbacks and
            # do not enqueue (the queue is reserved for stage-event bytes).
            if pt == PayloadType.ReplayComplete:
                complete = message_to_dict(buf)
                result = self._inbox.accept_replay_complete(
                    connection_generation,
                    int(complete["head_seq"]),
                    int(complete["epoch"]),
                )
                if result == _client_backend.AcceptResult.STALE_GENERATION:
                    return
                self._received_replay_complete = (
                    connection_generation,
                    int(complete["head_seq"]),
                    int(complete["epoch"]),
                    0,
                )
                continue
            if pt in (
                PayloadType.PlaybackState,
                PayloadType.PlaybackClaimed,
                PayloadType.PlaybackRejected,
            ):
                msg = message_to_dict(buf)
                cb = None
                if pt == PayloadType.PlaybackState:
                    cb = self._on_playback_state
                elif pt == PayloadType.PlaybackClaimed:
                    cb = self._on_playback_claimed
                else:
                    cb = self._on_playback_rejected
                if cb is not None:
                    try:
                        cb(msg)
                    except Exception:
                        LOG.exception("ReceiverThread: playback callback failed")
                continue

            if pt == PayloadType.BroadcastEvent:
                kind = _client_backend.ReceiverMessageKind.EVENT
            elif pt == PayloadType.LayerGraphState:
                kind = _client_backend.ReceiverMessageKind.LAYER_GRAPH_STATE
            elif pt == PayloadType.Resync:
                kind = _client_backend.ReceiverMessageKind.RESYNC
                self._synchronized_event.clear()
                self._received_replay_complete = None
            else:
                kind = _client_backend.ReceiverMessageKind.OTHER

            result = self._inbox.accept(connection_generation, kind, seq, buf)
            if result == _client_backend.AcceptResult.STALE_GENERATION:
                return
            if result == _client_backend.AcceptResult.DUPLICATE:
                continue
            if result == _client_backend.AcceptResult.SEQUENCE_GAP:
                replay_from = self._inbox.last_applied_sequence + 1
                LOG.error(
                    "ReceiverThread: sequence gap before %d; replaying from applied %d",
                    seq,
                    replay_from,
                )
                self._inbox.request_replay_from(replay_from)
                return
            if result == _client_backend.AcceptResult.QUEUE_FULL:
                LOG.warning(
                    "ReceiverThread: queue full (%d), disconnecting to replay from server",
                    self.max_queue,
                )
                break

    def request_replay_from(self, seq_start: int) -> None:
        """Reconnect and request replay beginning at ``seq_start``.

        Consumers call this after a queued frame fails to decode. Frames queued
        after the failed frame are discarded, and the connection generation
        prevents an in-flight read from adding more stale frames.
        """
        seq_start = int(seq_start)
        if seq_start < 1:
            raise ValueError("replay sequence must be at least 1")

        self._inbox.request_replay_from(seq_start)
        self._synchronized_event.clear()
        self._received_replay_complete = None
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

    def stop(self):
        """Request clean shutdown."""
        self._stop_event.set()
        self._handshake_event.set()
        self._close_socket()
