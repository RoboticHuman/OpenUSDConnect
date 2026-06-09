"""Tests for EventSender."""

import socket

from openusdconnect.codec import encode_message, message_to_dict
from openusdconnect.framing import recv_framed, send_framed
from openusdconnect.sender import EventSender


def _make_server():
    """Create a listening socket on a random port, return (socket, port)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, srv.getsockname()[1]


def _accept_and_hello_ok(srv, timeout=2):
    """Accept one connection, consume hello, send hello_ok. Return (conn, hello)."""
    srv.settimeout(timeout)
    conn, _ = srv.accept()
    conn.settimeout(timeout)
    hello = message_to_dict(recv_framed(conn))
    send_framed(conn, encode_message({"type": "hello_ok"}))
    return conn, hello


class TestEventSenderConnect:
    def test_handshake_and_send_events(self):
        srv, port = _make_server()
        sender = EventSender("127.0.0.1", port, client_id="test-client")
        conn = None
        try:
            import threading

            result = {}

            def _serve():
                result["conn"], result["hello"] = _accept_and_hello_ok(srv)

            t = threading.Thread(target=_serve)
            t.start()
            assert sender.connect() is True
            t.join(timeout=2)
            conn = result["conn"]

            assert result["hello"]["type"] == "hello"
            assert result["hello"]["client_id"] == "test-client"

            events = [{"k": "ensure_prim", "prim": "/World/X", "typeName": "Xform"}]
            assert sender.send_events(events) is True
            txn = message_to_dict(recv_framed(conn))
            assert txn["type"] == "txn"
            assert txn["client_id"] == "test-client"
            assert txn["events"] == events
        finally:
            sender.disconnect()
            if conn is not None:
                conn.close()
            srv.close()

    def test_socket_has_nodelay(self):
        """Interactive txn frames are small; Nagle must not delay them."""
        srv, port = _make_server()
        sender = EventSender("127.0.0.1", port, client_id="test-client")
        conn = None
        try:
            import threading

            result = {}

            def _serve():
                result["conn"], _ = _accept_and_hello_ok(srv)

            t = threading.Thread(target=_serve)
            t.start()
            assert sender.connect() is True
            t.join(timeout=2)
            conn = result["conn"]

            assert sender.sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) != 0
        finally:
            sender.disconnect()
            if conn is not None:
                conn.close()
            srv.close()

    def test_connect_failure_returns_false(self):
        srv, port = _make_server()
        srv.close()  # nothing listening on the port anymore
        sender = EventSender(
            "127.0.0.1", port, client_id="test-client", handshake_timeout=0.5
        )
        assert sender.connect() is False
        assert sender.sock is None
