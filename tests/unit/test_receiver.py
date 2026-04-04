"""Tests for ReceiverThread."""

import json
import socket
import time

from openusdconnect.receiver import ReceiverThread

_HELLO_OK = b'{"type":"hello_ok"}\n'


def _make_server():
    """Create a listening socket on a random port, return (socket, port)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, srv.getsockname()[1]


def _accept_and_hello(srv, timeout=2):
    """Accept one connection, consume hello, send hello_ok. Return conn."""
    srv.settimeout(timeout)
    conn, _ = srv.accept()
    conn.makefile("r").readline()  # consume hello
    conn.sendall(_HELLO_OK)
    return conn


def _accept(srv, timeout=2):
    """Accept one connection (no handshake)."""
    srv.settimeout(timeout)
    conn, _ = srv.accept()
    return conn


def _poll_until(predicate, timeout=2, interval=0.02):
    """Poll predicate() until truthy or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return predicate()


def _teardown(rt, conn, srv):
    """Clean shutdown: close server-side socket first so readline() unblocks."""
    conn.close()
    rt.stop()
    rt.join(timeout=1)
    srv.close()


class TestReceiverThread:
    def test_connects_and_sends_hello(self):
        """ReceiverThread connects and sends a hello message."""
        srv, port = _make_server()
        rt = ReceiverThread(host="127.0.0.1", port=port, sync_from=5, reconnect=False)
        rt.start()
        conn = _accept(srv)
        try:
            hello = json.loads(conn.makefile("r").readline())
            assert hello["type"] == "hello"
            assert hello["role"] == "receiver"
            assert hello["sync_from"] == 5
        finally:
            _teardown(rt, conn, srv)

    def test_receives_and_drains(self):
        """ReceiverThread queues incoming lines for drain_queue."""
        srv, port = _make_server()
        rt = ReceiverThread(host="127.0.0.1", port=port, reconnect=False)
        rt.start()
        conn = _accept_and_hello(srv)
        try:
            conn.sendall(b'{"type":"event","seq":1,"event":{"k":"ensure_prim"}}\n')
            conn.sendall(b'{"type":"event","seq":2,"event":{"k":"ensure_prim"}}\n')

            # Poll until both messages arrive
            collected = []

            def _drain_all():
                collected.extend(rt.drain_queue())
                return len(collected) >= 2

            _poll_until(_drain_all)
            assert len(collected) == 2
            assert rt.last_seq == 2
            assert len(rt.drain_queue()) == 0
        finally:
            _teardown(rt, conn, srv)

    def test_stop_on_server_close(self):
        """ReceiverThread stops cleanly when server closes connection."""
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1", port=port, reconnect=False,
            socket_timeout=0.1,
        )
        rt.start()
        conn = _accept_and_hello(srv)
        try:
            _poll_until(lambda: rt.connected)
            assert rt.connected
            conn.close()
            rt.join(timeout=1)
            assert not rt.connected
        finally:
            if rt.is_alive():
                rt.stop()
                rt.join(timeout=1)
            srv.close()

    def test_drain_empty_before_connect(self):
        """drain_queue returns empty deque before any data arrives."""
        srv, port = _make_server()
        rt = ReceiverThread(host="127.0.0.1", port=port, reconnect=False)
        assert len(rt.drain_queue()) == 0
        rt.start()
        conn = _accept_and_hello(srv)
        try:
            assert len(rt.drain_queue()) == 0
        finally:
            _teardown(rt, conn, srv)


class TestReconnection:
    """ReceiverThread reconnects automatically after connection loss."""

    def test_reconnects_after_server_close(self):
        """After server closes, receiver reconnects to a new server."""
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1", port=port, reconnect=True,
            reconnect_base_delay=0.05, reconnect_max_delay=0.2,
        )
        rt.start()

        # First connection
        conn1 = _accept_and_hello(srv)
        _poll_until(lambda: rt.connected)
        assert rt.connected

        # Server drops connection
        conn1.close()
        _poll_until(lambda: not rt.connected)

        # Receiver should reconnect
        conn2 = _accept_and_hello(srv)
        _poll_until(lambda: rt.connected, timeout=2)
        assert rt.connected

        _teardown(rt, conn2, srv)

    def test_reconnect_uses_last_seq(self):
        """Reconnection sends sync_from based on last received seq."""
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1", port=port, reconnect=True,
            reconnect_base_delay=0.05, reconnect_max_delay=0.2,
        )
        rt.start()

        # First connection — send some events
        conn1 = _accept_and_hello(srv)
        conn1.sendall(b'{"type":"event","seq":10,"event":{"k":"ensure_prim"}}\n')
        _poll_until(lambda: rt.last_seq == 10)

        # Drop connection
        conn1.close()
        _poll_until(lambda: not rt.connected)

        # Reconnect — should request sync_from=11
        conn2 = _accept(srv, timeout=2)
        hello = json.loads(conn2.makefile("r").readline())
        assert hello["sync_from"] == 11

        _teardown(rt, conn2, srv)

    def test_no_reconnect_when_disabled(self):
        """With reconnect=False, thread exits after connection loss."""
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1", port=port, reconnect=False,
            socket_timeout=0.1,
        )
        rt.start()

        conn = _accept_and_hello(srv)
        _poll_until(lambda: rt.connected)
        conn.close()

        rt.join(timeout=1)
        assert not rt.is_alive()
        srv.close()


class TestSocketTimeout:
    """Socket timeout prevents hanging on unresponsive server."""

    def test_timeout_does_not_kill_connection(self):
        """Socket timeout triggers but connection stays alive if server responds later."""
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1", port=port, reconnect=False, socket_timeout=0.05,
        )
        rt.start()
        conn = _accept_and_hello(srv)
        try:
            _poll_until(lambda: rt.connected)

            # Wait longer than socket timeout
            time.sleep(0.1)

            # Connection should still be alive — timeout just means no data
            assert rt.connected

            # Send data after timeout — should still be received
            conn.sendall(b'{"type":"event","seq":1,"event":{"k":"ensure_prim"}}\n')
            collected = []
            _poll_until(lambda: collected.extend(rt.drain_queue()) or len(collected) >= 1)
            assert len(collected) == 1
        finally:
            _teardown(rt, conn, srv)


class TestBoundedQueue:
    """Queue overflow triggers reconnect instead of unbounded growth."""

    def test_queue_overflow_triggers_reconnect(self):
        """When queue is full, receiver disconnects and reconnects for replay."""
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1", port=port, reconnect=True, max_queue=3,
            reconnect_base_delay=0.05, reconnect_max_delay=0.2,
        )
        rt.start()

        # First connection
        conn1 = _accept_and_hello(srv)

        # Send 5 events into a queue with max depth 3
        for i in range(1, 6):
            conn1.sendall(
                (json.dumps({"type": "event", "seq": i, "event": {"k": "ensure_prim"}}) + "\n")
                .encode()
            )

        # Wait for overflow to trigger disconnect
        _poll_until(lambda: not rt.connected, timeout=2)

        # Queue should have the events it managed to buffer (up to 3)
        lines = rt.drain_queue()
        assert len(lines) <= 3

        # Receiver should reconnect automatically
        conn2 = _accept(srv, timeout=2)
        hello = json.loads(conn2.makefile("r").readline())
        assert hello["type"] == "hello"
        # Should request replay from where it left off
        assert hello["sync_from"] > 0

        _teardown(rt, conn2, srv)
        conn1.close()

    def test_queue_overflow_no_reconnect_when_disabled(self):
        """With reconnect=False, overflow stops the thread."""
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1", port=port, reconnect=False, max_queue=3,
            socket_timeout=0.1,
        )
        rt.start()
        conn = _accept_and_hello(srv)
        try:
            for i in range(1, 6):
                conn.sendall(
                    (json.dumps({"type": "event", "seq": i, "event": {"k": "ensure_prim"}}) + "\n")
                    .encode()
                )

            rt.join(timeout=1)
            assert not rt.is_alive()
        finally:
            conn.close()
            srv.close()


class TestPingHandling:
    """Server pings are handled transparently by the receiver."""

    def test_ping_not_queued(self):
        """Ping messages from server are silently dropped, not queued."""
        srv, port = _make_server()
        rt = ReceiverThread(host="127.0.0.1", port=port, reconnect=False)
        rt.start()
        conn = _accept_and_hello(srv)
        try:
            conn.sendall(b'{"type":"event","seq":1,"event":{"k":"ensure_prim"}}\n')
            conn.sendall(b'{"type":"ping"}\n')
            conn.sendall(b'{"type":"event","seq":2,"event":{"k":"ensure_prim"}}\n')

            collected = []
            _poll_until(lambda: collected.extend(rt.drain_queue()) or len(collected) >= 2)
            assert len(collected) == 2
            for line in collected:
                assert '"ping"' not in line
            assert rt.last_seq == 2
        finally:
            _teardown(rt, conn, srv)

    def test_ping_resets_timeout_counter(self):
        """Receiving a ping prevents consecutive timeout disconnect."""
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1", port=port, reconnect=False,
            socket_timeout=0.05,
        )
        import openusdconnect.receiver as recv_mod
        original = recv_mod._MAX_CONSECUTIVE_TIMEOUTS
        recv_mod._MAX_CONSECUTIVE_TIMEOUTS = 3
        try:
            rt.start()
            conn = _accept_and_hello(srv)
            _poll_until(lambda: rt.connected)
            # Wait ~2 timeouts, then send a ping to reset counter
            time.sleep(0.12)
            conn.sendall(b'{"type":"ping"}\n')
            time.sleep(0.12)
            # Should still be alive because ping reset the counter
            assert rt.connected
        finally:
            recv_mod._MAX_CONSECUTIVE_TIMEOUTS = original
            _teardown(rt, conn, srv)


class TestConsecutiveTimeouts:
    """Receiver disconnects after too many consecutive recv timeouts."""

    def test_max_consecutive_timeouts(self):
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1", port=port, reconnect=False,
            socket_timeout=0.02,
        )
        import openusdconnect.receiver as recv_mod
        original = recv_mod._MAX_CONSECUTIVE_TIMEOUTS
        recv_mod._MAX_CONSECUTIVE_TIMEOUTS = 3
        try:
            rt.start()
            conn = _accept_and_hello(srv)
            _poll_until(lambda: rt.connected)
            # Don't send anything — let timeouts accumulate
            rt.join(timeout=2)
            assert not rt.is_alive()
        finally:
            recv_mod._MAX_CONSECUTIVE_TIMEOUTS = original
            conn.close()
            srv.close()
