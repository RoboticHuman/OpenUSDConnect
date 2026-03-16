"""Tests for ReceiverThread."""

import json
import socket
import time

from openusdconnect.receiver import ReceiverThread


def _make_server():
    """Create a listening socket on a random port, return (socket, port)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, srv.getsockname()[1]


def _accept(srv, timeout=2):
    """Accept one connection."""
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
    rt.join(timeout=2)
    srv.close()


class TestReceiverThread:
    def test_connects_and_sends_hello(self):
        """ReceiverThread connects and sends a hello message."""
        srv, port = _make_server()
        rt = ReceiverThread(host="127.0.0.1", port=port, sync_from=5)
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
        rt = ReceiverThread(host="127.0.0.1", port=port)
        rt.start()
        conn = _accept(srv)
        try:
            conn.makefile("r").readline()  # consume hello

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
            assert rt.drain_queue() == []
        finally:
            _teardown(rt, conn, srv)

    def test_stop_on_server_close(self):
        """ReceiverThread stops cleanly when server closes connection."""
        srv, port = _make_server()
        rt = ReceiverThread(host="127.0.0.1", port=port)
        rt.start()
        conn = _accept(srv)
        try:
            _poll_until(lambda: rt.connected)
            assert rt.connected
            conn.close()
            rt.join(timeout=2)
            assert not rt.connected
        finally:
            if rt.is_alive():
                rt.stop()
                rt.join(timeout=2)
            srv.close()

    def test_drain_empty_before_connect(self):
        """drain_queue returns empty list before any data arrives."""
        srv, port = _make_server()
        rt = ReceiverThread(host="127.0.0.1", port=port)
        assert rt.drain_queue() == []
        rt.start()
        conn = _accept(srv)
        try:
            assert rt.drain_queue() == []
        finally:
            _teardown(rt, conn, srv)
