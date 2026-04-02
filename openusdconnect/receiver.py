"""Background receiver thread — connects to server, queues incoming events.

ReceiverThread is DCC-agnostic. It connects to the server as a receiver,
reads JSON lines in a background thread, and provides a thread-safe queue
for the main thread to drain. DCC-specific timer/callback registration
is the plugin's responsibility.

Features:
- Automatic reconnection with exponential backoff on connection loss
- Socket timeout to detect hung connections
- Bounded queue — on overflow, disconnects, waits for drain, then reconnects for replay
"""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time
from collections import deque
from collections.abc import Callable

from .protocol import MSG_AUTH_REJECTED, MSG_HELLO_OK, MSG_PING, make_hello
from .transport import send_line

LOG = logging.getLogger(__name__)

# Reconnection defaults
_RECONNECT_BASE_DELAY = 1.0   # seconds
_RECONNECT_MAX_DELAY = 30.0   # seconds
_SOCKET_TIMEOUT = 30.0        # seconds — detect hung connections
_MAX_QUEUE_DEPTH = 50_000     # max queued lines before overflow (disconnect + replay)
_MAX_CONSECUTIVE_TIMEOUTS = 10  # 10 x 30s = 5 min max idle before reconnect

_SEQ_RE = re.compile(r'"seq"\s*:\s*(\d+)')


def _is_ping(line: str) -> bool:
    return f'"type":"{MSG_PING}"' in line or f'"type": "{MSG_PING}"' in line


class ReceiverThread(threading.Thread):
    """Background TCP client that connects to server and queues incoming events.

    Automatically reconnects on connection loss with exponential backoff.
    Uses socket timeouts to detect hung servers. Queue depth is bounded
    to prevent unbounded memory growth.

    Usage:
        rt = ReceiverThread(host="127.0.0.1", port=7200)
        rt.start()
        # ... periodically on main thread:
        events = rt.drain_queue()
        for raw_line in events:
            msg = json.loads(raw_line)
            # process msg
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
            (self.host, self.port), timeout=self.socket_timeout,
        )
        self.sock.settimeout(self.socket_timeout)

        # Send hello as receiver — use last_seq + 1 for replay on reconnect
        sync_from = self.last_seq + 1 if self.last_seq > 0 else self.sync_from
        hello = make_hello(
            "receiver", sync_from=sync_from,
            client_id=self.client_id, origin=self.origin,
            token=self.token,
        )
        send_line(self.sock, hello)
        self.auth_rejected = False

        buf = bytearray()
        consecutive_timeouts = 0
        while not self._stop_event.is_set():
            try:
                data = self.sock.recv(65536)
            except TimeoutError:
                consecutive_timeouts += 1
                if consecutive_timeouts >= _MAX_CONSECUTIVE_TIMEOUTS:
                    LOG.warning(
                        "ReceiverThread: %d consecutive timeouts, reconnecting",
                        consecutive_timeouts,
                    )
                    break
                LOG.debug("ReceiverThread: recv timeout (%d/%d)",
                          consecutive_timeouts, _MAX_CONSECUTIVE_TIMEOUTS)
                continue
            except OSError:
                if not self._stop_event.is_set():
                    LOG.warning("ReceiverThread: socket error during read")
                break

            if not data:
                LOG.info("ReceiverThread: EOF, server closed connection")
                break

            consecutive_timeouts = 0
            buf.extend(data)

            parts = buf.split(b"\n")
            buf = bytearray(parts[-1])

            overflow = False
            for part in parts[:-1]:
                line = part.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                # Pre-handshake: full parse for auth/hello messages
                if not self.connected:
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if parsed.get("type") == MSG_AUTH_REJECTED:
                        LOG.error("ReceiverThread: auth rejected — %s",
                                  parsed.get("reason"))
                        self.auth_rejected = True
                        return

                    if parsed.get("type") == MSG_HELLO_OK:
                        issued = parsed.get("token")
                        if issued:
                            self.token = issued
                            if self._on_token_issued:
                                self._on_token_issued(issued)
                            LOG.info("ReceiverThread: token issued by server")
                        self.connected = True
                        LOG.info("ReceiverThread connected (sync_from=%d)", sync_from)
                    continue

                # Post-handshake: skip pings, extract seq via regex
                if _is_ping(line):
                    continue

                with self._incoming_lock:
                    if len(self._incoming) >= self.max_queue:
                        overflow = True
                        break
                    self._incoming.append(line)

                seq_match = _SEQ_RE.search(line)
                if seq_match:
                    self.last_seq = max(self.last_seq, int(seq_match.group(1)))

            if overflow:
                LOG.warning(
                    "ReceiverThread: queue full (%d), disconnecting to replay from server",
                    self.max_queue,
                )
                self._queue_overflow = True
                break

    def _close_socket(self):
        """Close the socket, ignoring errors."""
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        self.sock = None

    def drain_queue(self) -> list[str]:
        """Drain all queued raw JSON lines. Thread-safe, call from main thread."""
        with self._incoming_lock:
            old = self._incoming
            self._incoming = deque()
        return list(old)

    def stop(self):
        """Request clean shutdown."""
        self._stop_event.set()
        sock = self.sock  # Local ref avoids TOCTOU with _close_socket()
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
