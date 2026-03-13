"""Background receiver thread — connects to server, queues incoming events.

ReceiverThread is DCC-agnostic. It connects to the server as a receiver,
reads JSON lines in a background thread, and provides a thread-safe queue
for the main thread to drain. DCC-specific timer/callback registration
is the plugin's responsibility.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from collections import deque

from .protocol import make_hello
from .transport import send_line

LOG = logging.getLogger(__name__)


class ReceiverThread(threading.Thread):
    """Background TCP client that connects to server and queues incoming events.

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

    def __init__(self, host: str = "127.0.0.1", port: int = 7200, sync_from: int = 1):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.sync_from = sync_from
        self._stop_event = threading.Event()
        self.sock: socket.socket | None = None
        self._incoming: deque = deque()
        self._incoming_lock = threading.Lock()
        self.connected = False
        self.last_seq: int = 0

    def run(self):
        try:
            LOG.info("ReceiverThread connecting to %s:%s", self.host, self.port)
            self.sock = socket.create_connection((self.host, self.port))
            self.connected = True

            # Send hello as receiver
            send_line(self.sock, make_hello("receiver", sync_from=self.sync_from))

            f = self.sock.makefile("r")
            while not self._stop_event.is_set():
                line = f.readline()
                if line == "":
                    LOG.info("ReceiverThread: EOF, server closed connection")
                    break
                line = line.strip()
                if not line:
                    continue
                with self._incoming_lock:
                    self._incoming.append(line)
                # Track last_seq for reconnect replay
                try:
                    parsed = json.loads(line)
                    seq = parsed.get("seq")
                    if seq is not None:
                        self.last_seq = max(self.last_seq, int(seq))
                except Exception:
                    pass
        except Exception:
            if not self._stop_event.is_set():
                LOG.exception("ReceiverThread: connection error")
        finally:
            self.connected = False
            try:
                if self.sock:
                    self.sock.close()
            except Exception:
                pass
            LOG.info("ReceiverThread stopped")

    def drain_queue(self) -> list[str]:
        """Drain all queued raw JSON lines. Thread-safe, call from main thread."""
        result = []
        with self._incoming_lock:
            while self._incoming:
                result.append(self._incoming.popleft())
        return result

    def stop(self):
        """Request clean shutdown."""
        self._stop_event.set()
        try:
            if self.sock:
                self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
