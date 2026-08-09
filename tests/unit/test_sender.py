"""Tests for EventSender."""

import socket
import threading
import time

import pytest

from openusdconnect.codec import encode_message, message_to_dict
from openusdconnect.framing import recv_framed, send_framed
from openusdconnect.protocol import make_transaction_result
from openusdconnect.sender import EventSender, TransactionRejectedError


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
            assert result["hello"]["producer_session_id"] == sender.session_id

            events = [{"k": "ensure_prim", "prim": "/World/X", "typeName": "Xform"}]
            assert sender.send_events(events) is True
            txn = message_to_dict(recv_framed(conn))
            assert txn["type"] == "txn"
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

    def test_reconnect_replays_identical_bytes_until_duplicate_ack(self):
        srv, port = _make_server()
        sender = EventSender(
            "127.0.0.1", port, client_id="test-client", session_id="stable-session"
        )
        observed = []
        first_closed = threading.Event()

        def _serve():
            first, _ = _accept_and_hello_ok(srv)
            observed.append(recv_framed(first))
            first.close()  # committed outcome was lost with the connection
            first_closed.set()

            second, _ = _accept_and_hello_ok(srv)
            observed.append(recv_framed(second))
            send_framed(
                second,
                encode_message(
                    make_transaction_result(
                        1,
                        status="acknowledged",
                    )
                ),
            )
            time.sleep(0.05)
            second.close()

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()
        try:
            assert sender.connect()
            events = [{"k": "ensure_prim", "prim": "/World/X", "typeName": "Xform"}]
            assert sender.send_events(events)
            assert first_closed.wait(timeout=2)
            deadline = time.monotonic() + 2
            while sender.connected and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not sender.connected
            assert sender.pending_transaction_count == 1

            assert sender.connect()
            assert sender.flush(timeout=2)
            assert observed[0] == observed[1]
            assert sender.pending_transaction_count == 0
            assert sender.acknowledged_event_count == 1
            assert sender.drain_acknowledged_event_count() == 1
        finally:
            sender.disconnect()
            thread.join(timeout=2)
            srv.close()

    def test_bounded_outbox_and_rejection_are_terminal(self):
        srv, port = _make_server()
        sender = EventSender(
            "127.0.0.1",
            port,
            client_id="test-client",
            session_id="bounded-session",
            max_pending_transactions=1,
        )

        def _serve():
            conn, _ = _accept_and_hello_ok(srv)
            recv_framed(conn)
            send_framed(
                conn,
                encode_message(
                    make_transaction_result(
                        1,
                        status="rejected",
                        expected_txn_id=1,
                        rejection_code="unexpected_id",
                        reason="injected rejection",
                    )
                ),
            )
            time.sleep(0.05)
            conn.close()

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()
        event = {"k": "ensure_prim", "prim": "/World/X", "typeName": "Xform"}
        try:
            assert sender.connect()
            assert sender.send_events([event])
            assert not sender.send_events([event])
            with pytest.raises(TransactionRejectedError, match="injected rejection"):
                sender.flush(timeout=2)
            assert sender.pending_transaction_count == 0
            assert sender.transaction_error
        finally:
            sender.disconnect()
            thread.join(timeout=2)
            srv.close()

    def test_rejection_closes_transport_and_quarantines_later_transactions(self):
        srv, port = _make_server()
        sender = EventSender(
            "127.0.0.1",
            port,
            client_id="test-client",
            session_id="quarantine-session",
        )
        received = threading.Event()

        def _serve():
            conn, _ = _accept_and_hello_ok(srv)
            recv_framed(conn)
            recv_framed(conn)
            received.set()
            send_framed(
                conn,
                encode_message(
                    make_transaction_result(
                        1,
                        status="rejected",
                        rejection_code="invalid_transaction",
                        reason="invalid first transaction",
                    )
                ),
            )
            time.sleep(0.1)
            conn.close()

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()
        event = {"k": "ensure_prim", "prim": "/World/X", "typeName": "Xform"}
        try:
            assert sender.connect()
            assert sender.send_events([event])
            assert sender.send_events([event])
            assert received.wait(timeout=2)
            with pytest.raises(TransactionRejectedError, match="invalid first"):
                sender.flush(timeout=2)
            assert sender.recovery_required
            assert not sender.connected
            assert sender.pending_transaction_count == 1
            assert not sender.send_events([event])
        finally:
            sender.disconnect()
            thread.join(timeout=2)
            srv.close()
