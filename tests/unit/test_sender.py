"""Tests for EventSender."""

import socket
import threading
import time

import pytest

from openusdconnect.codec import TransactionRejectionCode, encode_message, message_to_dict
from openusdconnect.framing import recv_framed, send_framed
from openusdconnect.protocol import make_transaction_result
from openusdconnect.recovery import RejectionDisposition, TransactionFailure
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

    def test_connect_timeout_never_extends_configured_handshake_timeout(self, monkeypatch):
        observed = []

        def _fail(_endpoint, *, timeout):
            observed.append(timeout)
            raise TimeoutError

        monkeypatch.setattr(socket, "create_connection", _fail)
        sender = EventSender(
            "127.0.0.1",
            7200,
            client_id="timeout-client",
            handshake_timeout=0.5,
        )

        assert sender.connect(timeout=0.1) is False
        assert sender.connect(timeout=2.0) is False
        assert sender.connect(timeout=0.0) is False
        assert observed == [0.1, 0.5]

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
            assert sender.transaction_failure.code_name == "unexpected_id"
            assert sender.transaction_failure.expected_txn_id == 1
            assert sender.recovery_disposition is RejectionDisposition.SESSION_FATAL
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
            assert sender.transaction_failure.code_name == "invalid_transaction"
            assert sender.recovery_disposition is RejectionDisposition.INVALID_OPERATION
            assert not sender.connected
            assert sender.pending_transaction_count == 1
            assert not sender.send_events([event])
        finally:
            sender.disconnect()
            thread.join(timeout=2)
            srv.close()

    @pytest.mark.parametrize(
        ("rejection_code", "expected"),
        [
            ("stale_layer_graph", RejectionDisposition.RECOVERABLE_CONFLICT),
            ("invalid_transaction", RejectionDisposition.INVALID_OPERATION),
            ("invalid_identity", RejectionDisposition.SESSION_FATAL),
        ],
    )
    def test_rejection_exposes_recovery_disposition(self, rejection_code, expected):
        srv, port = _make_server()
        sender = EventSender("127.0.0.1", port, client_id="classified-client")

        def _serve():
            conn, _ = _accept_and_hello_ok(srv)
            recv_framed(conn)
            send_framed(
                conn,
                encode_message(
                    make_transaction_result(
                        1,
                        status="rejected",
                        rejection_code=rejection_code,
                        reason="classified rejection",
                    )
                ),
            )
            time.sleep(0.05)
            conn.close()

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()
        try:
            assert sender.connect()
            assert sender.send_events(
                [{"k": "ensure_prim", "prim": "/World/X", "typeName": "Xform"}]
            )
            with pytest.raises(TransactionRejectedError) as caught:
                sender.flush(timeout=2)
            assert caught.value.failure is sender.transaction_failure
            assert sender.recovery_disposition is expected
        finally:
            sender.disconnect()
            thread.join(timeout=2)
            srv.close()

    def test_recoverable_rejection_reuses_boundary_before_later_transactions(self):
        srv, port = _make_server()
        sender = EventSender(
            "127.0.0.1",
            port,
            client_id="retry-client",
            session_id="retry-session",
        )
        observed = []

        def _serve():
            first, _ = _accept_and_hello_ok(srv)
            observed.append(message_to_dict(recv_framed(first)))
            observed.append(message_to_dict(recv_framed(first)))
            send_framed(
                first,
                encode_message(
                    make_transaction_result(
                        1,
                        status="rejected",
                        rejection_code="stale_layer_graph",
                        reason="layer was remapped",
                    )
                ),
            )
            first.close()

            second, _ = _accept_and_hello_ok(srv)
            observed.append(message_to_dict(recv_framed(second)))
            observed.append(message_to_dict(recv_framed(second)))
            send_framed(second, encode_message(make_transaction_result(2)))
            time.sleep(0.05)
            second.close()

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()
        stale = {"k": "ensure_prim", "prim": "/World/Stale", "typeName": "Xform"}
        later = {"k": "ensure_prim", "prim": "/World/Later", "typeName": "Xform"}
        repaired = {"k": "ensure_prim", "prim": "/World/Repaired", "typeName": "Xform"}
        try:
            assert sender.connect()
            assert sender.send_events([stale], layer_key="old-layer")
            assert sender.send_events([later], layer_key="stable-layer")
            with pytest.raises(TransactionRejectedError):
                sender.flush(timeout=2)

            assert (
                sender.repair_rejected_transaction([repaired], layer_key="new-layer")
                == 1
            )
            assert sender.connect()
            assert sender.flush(timeout=2)

            assert [txn["txn_id"] for txn in observed] == [1, 2, 1, 2]
            assert observed[2]["layer_key"] == "new-layer"
            assert observed[2]["events"] == [repaired]
            assert observed[3] == observed[1]
        finally:
            sender.disconnect()
            thread.join(timeout=2)
            srv.close()

    def test_nonrecoverable_rejection_cannot_be_retried(self):
        sender = EventSender("127.0.0.1", 1, client_id="fatal-client")
        sender._failure = TransactionFailure(
            txn_id=1,
            code=TransactionRejectionCode.UnexpectedId,
            reason="sequence gap",
        )

        with pytest.raises(RuntimeError, match="not recoverable"):
            sender.repair_rejected_transaction(
                [{"k": "ensure_prim", "prim": "/World/X", "typeName": "Xform"}]
            )
