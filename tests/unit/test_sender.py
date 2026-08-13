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

    def test_concurrent_calls_preserve_transaction_id_wire_order(self, monkeypatch):
        """Socket order must match IDs even when the second caller wins the race."""

        class _SecondCallerFirstLock:
            def __init__(self):
                self._lock = threading.Lock()
                self._state_lock = threading.Lock()
                self._calls = 0
                self._second_released = threading.Event()

            def __enter__(self):
                with self._state_lock:
                    self._calls += 1
                    call = self._calls
                if call == 1:
                    assert self._second_released.wait(timeout=2)
                self._lock.acquire()
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self._lock.release()
                with self._state_lock:
                    if self._calls >= 2 and not self._second_released.is_set():
                        self._second_released.set()

        sender = EventSender("127.0.0.1", 1, client_id="concurrent-client")
        sender.sock = object()
        sender._send_lock = _SecondCallerFirstLock()
        wire_txn_ids = []
        monkeypatch.setattr(
            "openusdconnect.sender.send_raw",
            lambda _sock, payload: wire_txn_ids.append(
                message_to_dict(payload)["txn_id"]
            ),
        )

        results = []
        threads = [
            threading.Thread(
                target=lambda prim=prim: results.append(
                    sender.send_events(
                        [{"k": "ensure_prim", "prim": prim, "typeName": "Xform"}]
                    )
                )
            )
            for prim in ("/World/First", "/World/Second")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert all(not thread.is_alive() for thread in threads)
        assert results == [True, True]
        assert wire_txn_ids == [1, 2]
        assert list(sender._pending) == [1, 2]

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

    def test_token_callback_failure_does_not_poison_completed_handshake(self):
        srv, port = _make_server()
        sender = EventSender(
            "127.0.0.1",
            port,
            client_id="callback-client",
            on_token_issued=lambda _token: (_ for _ in ()).throw(
                RuntimeError("injected callback failure")
            ),
        )
        accepted = {}

        def _serve():
            conn = srv.accept()[0]
            recv_framed(conn)
            send_framed(
                conn,
                encode_message({"type": "hello_ok", "token": "issued-token"}),
            )
            accepted["conn"] = conn

        thread = threading.Thread(target=_serve)
        thread.start()
        try:
            assert sender.connect()
            assert sender.connected
            assert sender.token == "issued-token"
        finally:
            sender.disconnect()
            thread.join(timeout=2)
            if conn := accepted.get("conn"):
                conn.close()
            srv.close()

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
            assert sender.pending_transaction_count == 1
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
            assert sender.pending_transaction_count == 2
            incident = sender.recovery_incident
            assert incident is not None
            assert incident.incident_id == "quarantine-session:1"
            assert incident.transaction_ids == (1, 2)
            assert incident.transaction_count == 2
            assert incident.event_count == 2
            artifact = sender.recovery_artifact
            assert artifact is not None
            assert artifact.producer_session_id == "quarantine-session"
            assert [transaction.txn_id for transaction in artifact.transactions] == [1, 2]
            assert all(transaction.payload for transaction in artifact.transactions)
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

    def test_abandon_rejected_session_never_replays_its_suffix(self):
        srv, port = _make_server()
        sender = EventSender(
            "127.0.0.1",
            port,
            client_id="abandon-client",
            session_id="rejected-session",
        )
        observed = []

        def _serve():
            first, first_hello = _accept_and_hello_ok(srv)
            observed.append(first_hello)
            observed.append(message_to_dict(recv_framed(first)))
            observed.append(message_to_dict(recv_framed(first)))
            send_framed(
                first,
                encode_message(
                    make_transaction_result(
                        1,
                        status="rejected",
                        rejection_code="invalid_transaction",
                        reason="injected failure",
                    )
                ),
            )
            first.close()

            second, second_hello = _accept_and_hello_ok(srv)
            observed.append(second_hello)
            rebuilt = message_to_dict(recv_framed(second))
            observed.append(rebuilt)
            send_framed(second, encode_message(make_transaction_result(1)))
            time.sleep(0.05)
            second.close()

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()
        rejected = {"k": "ensure_prim", "prim": "/World/Rejected", "typeName": "Xform"}
        suffix = {"k": "ensure_prim", "prim": "/World/Suffix", "typeName": "Xform"}
        rebuilt = {"k": "ensure_prim", "prim": "/World/Rebuilt", "typeName": "Xform"}
        try:
            assert sender.connect()
            assert sender.send_events([rejected])
            assert sender.send_events([suffix])
            with pytest.raises(TransactionRejectedError):
                sender.flush(timeout=2)

            artifact = sender.recovery_artifact
            assert artifact is not None
            assert sender.abandon_rejected_session(session_id="replacement-session") is artifact
            assert sender.session_id == "replacement-session"
            assert sender.pending_transaction_count == 0
            assert not sender.recovery_required
            assert sender.recovery_incident is None

            assert sender.connect()
            assert sender.send_events([rebuilt])
            assert sender.flush(timeout=2)

            assert observed[0]["producer_session_id"] == "rejected-session"
            assert [observed[1]["txn_id"], observed[2]["txn_id"]] == [1, 2]
            assert observed[3]["producer_session_id"] == "replacement-session"
            assert observed[4]["txn_id"] == 1
            assert observed[4]["events"] == [rebuilt]
        finally:
            sender.disconnect()
            thread.join(timeout=2)
            srv.close()
