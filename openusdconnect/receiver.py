"""Background receiver thread — connects to server, queues incoming events.

ReceiverThread is DCC-agnostic. It connects to the server as a receiver,
reads length-prefixed FlatBuffers messages in a background thread, and
provides a thread-safe queue for the main thread to drain. DCC-specific
timer/callback registration is the plugin's responsibility.

Features:
- Automatic reconnection with exponential backoff on connection loss
- Socket timeout to detect hung connections
- Bounded queue — on overflow, disconnects, waits for drain, then reconnects for replay
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections import deque
from collections.abc import Callable

from .codec import (
    PayloadType,
    _decode_stage_metadata_table,
    decode_envelope,
    is_ping,
    message_to_dict,
    resolve_payload,
)
from .framing import IncompleteRead, MessageTooLarge, recv_framed
from .protocol import make_hello
from .transport import send_msg

LOG = logging.getLogger(__name__)

# Reconnection defaults
_RECONNECT_BASE_DELAY = 1.0  # seconds
_RECONNECT_MAX_DELAY = 30.0  # seconds
_SOCKET_TIMEOUT = 30.0  # seconds — detect hung connections
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
        host: str = "127.0.0.1",
        port: int = 7200,
        sync_from: int = 1,
        reconnect: bool = True,
        max_queue: int = _MAX_QUEUE_DEPTH,
        socket_timeout: float = _SOCKET_TIMEOUT,
        client_id: str | None = None,
        reconnect_base_delay: float = _RECONNECT_BASE_DELAY,
        reconnect_max_delay: float = _RECONNECT_MAX_DELAY,
        origin: str | None = None,
        token: str | None = None,
        on_token_issued: Callable | None = None,
        on_stage_metadata: Callable[[dict], None] | None = None,
        on_playback_state: Callable[[dict], None] | None = None,
        on_playback_claimed: Callable[[dict], None] | None = None,
        on_playback_rejected: Callable[[dict], None] | None = None,
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
        self.token = token
        self._on_token_issued = on_token_issued
        self._on_stage_metadata = on_stage_metadata
        self._on_playback_state = on_playback_state
        self._on_playback_claimed = on_playback_claimed
        self._on_playback_rejected = on_playback_rejected
        self._reconnect_base_delay = reconnect_base_delay
        self._reconnect_max_delay = reconnect_max_delay
        self._stop_event = threading.Event()
        self.sock: socket.socket | None = None
        self._incoming: deque = deque()
        self._incoming_lock = threading.Lock()
        self._connected_event = threading.Event()
        self.last_seq: int = 0
        self._queue_overflow = False
        self.auth_rejected = False
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

    def run(self):
        delay = self._reconnect_base_delay
        while not self._stop_event.is_set():
            try:
                self._connect_and_recv()
            except Exception:
                if not self._stop_event.is_set():
                    LOG.exception("ReceiverThread: connection error")
            finally:
                self.connected = False
                self._close_socket()

            if not self.reconnect or self._stop_event.is_set() or self.auth_rejected:
                break

            if self._queue_overflow:
                # Intentional disconnect — wait for main thread to drain
                # before reconnecting, otherwise we'll overflow again.
                self._queue_overflow = False
                delay = self._reconnect_base_delay
                LOG.info("ReceiverThread: waiting for queue to drain before reconnect")
                drain_start = time.monotonic()
                while not self._stop_event.is_set():
                    with self._incoming_lock:
                        if len(self._incoming) == 0:
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
        self.sock = socket.create_connection(
            (self.host, self.port),
            timeout=self.socket_timeout,
        )
        self.sock.settimeout(self.socket_timeout)

        # Send hello as receiver — use last_seq + 1 for replay on reconnect
        sync_from = self.last_seq + 1 if self.last_seq > 0 else self.sync_from
        hello = make_hello(
            "receiver",
            sync_from=sync_from,
            client_id=self.client_id,
            origin=self.origin,
            token=self.token,
        )
        send_msg(self.sock, hello)
        self.auth_rejected = False

        consecutive_timeouts = 0
        while not self._stop_event.is_set():
            try:
                buf = recv_framed(self.sock)
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
                if not self._stop_event.is_set():
                    LOG.warning("ReceiverThread: framing error during read")
                break
            except OSError:
                if not self._stop_event.is_set():
                    LOG.warning("ReceiverThread: socket error during read")
                break

            consecutive_timeouts = 0

            # Pre-handshake: check for auth/hello_ok messages
            if not self.connected:
                env = decode_envelope(buf)
                pt = env.PayloadType()

                if pt == PayloadType.AuthRejected:
                    _, ar = resolve_payload(env)
                    reason = ar.Reason()
                    if isinstance(reason, bytes):
                        reason = reason.decode("utf-8")
                    LOG.error("ReceiverThread: auth rejected — %s", reason)
                    self.auth_rejected = True
                    return

                if pt == PayloadType.HelloOk:
                    _, ho = resolve_payload(env)
                    issued = ho.Token()
                    if issued:
                        if isinstance(issued, bytes):
                            issued = issued.decode("utf-8")
                        self.token = issued
                        if self._on_token_issued:
                            self._on_token_issued(issued)
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
                    LOG.info("ReceiverThread connected (sync_from=%d)", sync_from)
                continue

            # Post-handshake: skip pings
            if is_ping(buf):
                continue

            # Playback messages are control-plane signals: fire callbacks and
            # do not enqueue (the queue is reserved for stage-event bytes).
            env = decode_envelope(buf)
            pt = env.PayloadType()
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

            # Extract seq for tracking (read from BroadcastEvent without
            # full dict conversion)
            if pt == PayloadType.BroadcastEvent:
                _, be = resolve_payload(env)
                seq = be.Seq()
                if seq > self.last_seq:
                    self.last_seq = seq

            with self._incoming_lock:
                if len(self._incoming) >= self.max_queue:
                    LOG.warning(
                        "ReceiverThread: queue full (%d), disconnecting to replay from server",
                        self.max_queue,
                    )
                    self._queue_overflow = True
                    break
                self._incoming.append(buf)

    def _close_socket(self):
        """Close the socket, ignoring errors."""
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        self.sock = None

    def drain_queue(self) -> deque:
        """Drain all queued raw FlatBuffers messages. Thread-safe, call from main thread."""
        with self._incoming_lock:
            old = self._incoming
            self._incoming = deque()
        return old

    def stop(self):
        """Request clean shutdown."""
        self._stop_event.set()
        sock = self.sock  # Local ref avoids TOCTOU with _close_socket()
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
