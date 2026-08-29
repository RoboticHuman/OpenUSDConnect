"""Tests for ReceiverThread."""

import logging
import socket
import time

import pytest

from openusdconnect import _client_backend
from openusdconnect.codec import HelloRejectionCode, encode_message, message_to_dict
from openusdconnect.framing import recv_framed, send_framed
from openusdconnect.receiver import ReceiverThread


def _make_server():
    """Create a listening socket on a random port, return (socket, port)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, srv.getsockname()[1]


def _accept_and_hello(srv, timeout=2, hello_ok=None):
    """Accept one connection, consume hello, send hello_ok. Return conn."""
    srv.settimeout(timeout)
    conn, _ = srv.accept()
    conn.settimeout(timeout)
    recv_framed(conn)  # consume hello
    if hello_ok is None:
        hello_ok = {"type": "hello_ok", "layered_replay": True}
    send_framed(conn, encode_message(hello_ok))
    return conn


def _accept(srv, timeout=2):
    """Accept one connection (no handshake)."""
    srv.settimeout(timeout)
    conn, _ = srv.accept()
    conn.settimeout(timeout)
    return conn


def _recv_hello(conn):
    """Read and decode a hello message from connection."""
    buf = recv_framed(conn)
    return message_to_dict(buf)


def _send_event(conn, seq, event=None):
    """Send a broadcast event message."""
    if event is None:
        event = {"k": "ensure_prim", "prim": "/World/X", "typeName": "Xform"}
    msg = {"type": "event", "seq": seq, "event": event}
    send_framed(conn, encode_message(msg))


def _flood_events(conn, seqs):
    """Send events, tolerating the receiver closing the socket mid-flood.

    Overflowing the bounded queue makes the receiver disconnect by design, so
    pushing past that point legitimately races with an RST from the peer the
    sender just stops. The test's real assertion is the receiver's reaction.
    """
    for i in seqs:
        try:
            _send_event(conn, i)
        except ConnectionError:
            return


def _send_ping(conn):
    """Send a ping message."""
    send_framed(conn, encode_message({"type": "ping"}))


def _send_replay_complete(conn, head_seq, epoch=1):
    send_framed(
        conn,
        encode_message(
            {"type": "replay_complete", "head_seq": head_seq, "epoch": epoch}
        ),
    )


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
    """Clean shutdown: close server-side socket first so recv unblocks."""
    conn.close()
    rt.stop()
    rt.join(timeout=1)
    srv.close()


class TestReceiverThread:
    def test_bounded_drain_preserves_suffix_and_replay_watermark(self):
        rt = ReceiverThread(reconnect=False)
        rt.connected = True
        connection = rt._inbox.begin_connection()
        for sequence, frame in enumerate((b"one", b"two", b"three"), start=1):
            rt._inbox.accept(
                connection.generation,
                _client_backend.ReceiverMessageKind.EVENT,
                sequence,
                frame,
            )
        rt._inbox.accept_replay_complete(connection.generation, 3, 7)

        assert list(rt.drain_queue(max_messages=2)) == [b"one", b"two"]
        assert rt._inbox.size == 1
        assert not rt.mark_replay_applied()

        assert list(rt.drain_queue(max_messages=2)) == [b"three"]
        assert rt.mark_replay_applied()
        assert rt.synchronized
        assert rt.replay_head_seq == 3
        assert rt.replay_epoch == 7

    @pytest.mark.parametrize("limit", [0, -1, True, 1.5])
    def test_bounded_drain_rejects_invalid_limit(self, limit):
        with pytest.raises(ValueError, match="max_messages"):
            ReceiverThread(reconnect=False).drain_queue(max_messages=limit)

    def test_replay_request_advances_past_discarded_queue_serials(self):
        rt = ReceiverThread(reconnect=False)
        connection = rt._inbox.begin_connection()
        rt._inbox.accept(
            connection.generation,
            _client_backend.ReceiverMessageKind.EVENT,
            1,
            b"stale-one",
        )
        rt._inbox.accept(
            connection.generation,
            _client_backend.ReceiverMessageKind.EVENT,
            2,
            b"stale-two",
        )

        rt.request_replay_from(4)

        assert rt._inbox.size == 0

    def test_terminal_transport_failure_wakes_connection_waiter(self, monkeypatch):
        def _fail_connect(*_args, **_kwargs):
            raise OSError("injected connection failure")

        monkeypatch.setattr(socket, "create_connection", _fail_connect)
        rt = ReceiverThread(host="127.0.0.1", port=1, reconnect=False)
        rt.start()
        try:
            assert not rt.wait_connected(timeout=1)
            rt.join(timeout=1)
            assert not rt.is_alive()
            assert isinstance(rt.connection_error, OSError)
        finally:
            rt.stop()
            rt.join(timeout=1)

    def test_token_callback_failure_does_not_abort_handshake(self):
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1",
            port=port,
            reconnect=False,
            on_token_issued=lambda _token: (_ for _ in ()).throw(
                RuntimeError("injected callback failure")
            ),
        )
        rt.start()
        conn = _accept(srv)
        try:
            _recv_hello(conn)
            send_framed(
                conn,
                encode_message(
                    {
                        "type": "hello_ok",
                        "layered_replay": True,
                        "token": "issued-token",
                    }
                ),
            )
            assert rt.wait_connected(timeout=1)
            assert rt.connected
            assert rt.token == "issued-token"
        finally:
            _teardown(rt, conn, srv)

    def test_connects_and_sends_hello(self):
        """ReceiverThread connects and sends a hello message."""
        srv, port = _make_server()
        rt = ReceiverThread(host="127.0.0.1", port=port, sync_from=5, reconnect=False)
        rt.start()
        conn = _accept(srv)
        try:
            hello = _recv_hello(conn)
            assert hello["type"] == "hello"
            assert hello["role"] == "receiver"
            assert hello["sync_from"] == 5
            assert hello["layered_replay"] is True
        finally:
            _teardown(rt, conn, srv)

    def test_socket_has_nodelay(self):
        """Small control frames must not sit in Nagle's buffer."""
        srv, port = _make_server()
        rt = ReceiverThread(host="127.0.0.1", port=port, reconnect=False)
        rt.start()
        conn = _accept_and_hello(srv)
        try:
            _poll_until(lambda: rt.connected)
            assert rt.connected
            assert rt.sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) != 0
        finally:
            _teardown(rt, conn, srv)

    def test_replay_ready_only_after_preceding_frames_are_drained_and_applied(self):
        srv, port = _make_server()
        rt = ReceiverThread(host="127.0.0.1", port=port, reconnect=False)
        rt.start()
        conn = _accept_and_hello(srv)
        try:
            _send_event(conn, 1)
            _send_replay_complete(conn, 1, epoch=4)
            _poll_until(lambda: rt.last_seq == 1)
            assert _poll_until(
                lambda: rt._received_replay_complete is not None
                and rt._received_replay_complete[2] == 4
            )

            assert not rt.synchronized
            queued = rt.drain_queue()
            assert len(queued) == 1
            assert message_to_dict(queued[0])["type"] == "event"
            assert rt.mark_replay_applied()
            assert rt.synchronized
            assert rt.replay_head_seq == 1
            assert rt.replay_epoch == 4
        finally:
            _teardown(rt, conn, srv)

    def test_reconnect_clears_ready_until_the_new_replay_marker_is_applied(self):
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1",
            port=port,
            reconnect=True,
            reconnect_base_delay=0.02,
            reconnect_max_delay=0.05,
        )
        rt.start()
        conn1 = _accept_and_hello(srv)
        conn2 = None
        try:
            _send_replay_complete(conn1, 0, epoch=1)
            _poll_until(lambda: rt._received_replay_complete is not None)
            rt.drain_queue()
            assert rt.mark_replay_applied()
            assert rt.synchronized

            conn1.close()
            _poll_until(lambda: not rt.connected)
            assert not rt.synchronized

            conn2 = _accept_and_hello(srv, timeout=5)
            _poll_until(lambda: rt.connected)
            assert not rt.synchronized
            _send_replay_complete(conn2, 0, epoch=1)
            _poll_until(lambda: rt._received_replay_complete is not None)
            rt.drain_queue()
            assert rt.mark_replay_applied()
            assert rt.synchronized
        finally:
            _teardown(rt, conn2 or conn1, srv)

    def test_negotiates_layered_replay(self):
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1",
            port=port,
            reconnect=False,
            layered_replay=True,
        )
        rt.start()
        conn = _accept(srv)
        try:
            hello = _recv_hello(conn)
            assert hello["layered_replay"] is True
            send_framed(
                conn,
                encode_message(
                    {"type": "hello_ok", "layered_replay": True},
                ),
            )
            assert _poll_until(lambda: rt.connected)
            assert rt.layered_replay_active is True
        finally:
            _teardown(rt, conn, srv)

    def test_sends_department(self):
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1",
            port=port,
            reconnect=False,
            department="layout",
        )
        rt.start()
        conn = _accept(srv)
        try:
            hello = _recv_hello(conn)
            assert hello["department"] == "layout"
            send_framed(
                conn,
                encode_message({"type": "hello_ok", "layered_replay": True}),
            )
            assert _poll_until(lambda: rt.connected)
        finally:
            _teardown(rt, conn, srv)

    def test_unacknowledged_layered_replay_rejects_handshake(self):
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1",
            port=port,
            reconnect=False,
            layered_replay=True,
        )
        rt.start()
        conn = _accept_and_hello(srv, hello_ok={"type": "hello_ok"})
        try:
            rt.join(timeout=1)
            assert not rt.is_alive()
            assert not rt.connected
            assert rt.hello_rejected
            assert rt.layered_replay_active is False
        finally:
            _teardown(rt, conn, srv)

    def test_explicit_flat_replay_accepts_unlayered_handshake(self):
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1",
            port=port,
            reconnect=False,
            layered_replay=False,
        )
        rt.start()
        conn = _accept_and_hello(srv, hello_ok={"type": "hello_ok"})
        try:
            assert _poll_until(lambda: rt.connected)
            assert not rt.layered_replay_active
            assert not rt.hello_rejected
        finally:
            _teardown(rt, conn, srv)

    def test_hello_rejection_stops_reconnects(self):
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1",
            port=port,
            reconnect=True,
            reconnect_base_delay=0.01,
        )
        rt.start()
        conn = _accept(srv)
        try:
            _recv_hello(conn)
            send_framed(
                conn,
                encode_message(
                    {
                        "type": "hello_rejected",
                        "code": HelloRejectionCode.LayeredReplayRequired,
                        "reason": "layered replay is required",
                    }
                ),
            )
            rt.join(timeout=1)
            assert not rt.is_alive()
            assert not rt.connected
            assert not rt.auth_rejected
            assert rt.hello_rejected
            assert rt.rejection_code == HelloRejectionCode.LayeredReplayRequired
            assert rt.rejection_reason == "layered replay is required"
        finally:
            _teardown(rt, conn, srv)

    def test_receives_and_drains(self):
        """ReceiverThread queues incoming FB messages for drain_queue."""
        srv, port = _make_server()
        rt = ReceiverThread(host="127.0.0.1", port=port, reconnect=False)
        rt.start()
        conn = _accept_and_hello(srv)
        try:
            _send_event(conn, 1)
            _send_event(conn, 2)

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
            host="127.0.0.1",
            port=port,
            reconnect=False,
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
            host="127.0.0.1",
            port=port,
            reconnect=True,
            reconnect_base_delay=0.05,
            reconnect_max_delay=0.2,
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
            host="127.0.0.1",
            port=port,
            reconnect=True,
            reconnect_base_delay=0.05,
            reconnect_max_delay=0.2,
        )
        rt.start()

        # First connection send some events
        conn1 = _accept_and_hello(srv)
        _send_event(conn1, 10)
        _poll_until(lambda: rt.last_seq == 10)

        # Drop connection
        conn1.close()
        _poll_until(lambda: not rt.connected)

        # Reconnect should request sync_from=11
        conn2 = _accept(srv, timeout=2)
        hello = _recv_hello(conn2)
        assert hello["sync_from"] == 11

        _teardown(rt, conn2, srv)

    def test_resync_rewinds_the_reconnect_cursor(self):
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1",
            port=port,
            reconnect=True,
            reconnect_base_delay=0.05,
            reconnect_max_delay=0.2,
        )
        rt.start()
        conn1 = _accept_and_hello(srv)
        conn2 = None
        try:
            _send_event(conn1, 10)
            assert _poll_until(lambda: rt.last_seq == 10)
            send_framed(conn1, encode_message({"type": "resync"}))
            _send_event(conn1, 1)
            assert _poll_until(lambda: rt.last_seq == 1)

            conn1.close()
            assert _poll_until(lambda: not rt.connected)
            conn2 = _accept(srv, timeout=2)
            assert _recv_hello(conn2)["sync_from"] == 2
        finally:
            if conn2 is not None:
                _teardown(rt, conn2, srv)
            else:
                rt.stop()
                rt.join(timeout=1)
                srv.close()

    def test_in_place_resync_clears_ready_until_new_watermark_is_applied(self):
        srv, port = _make_server()
        rt = ReceiverThread(host="127.0.0.1", port=port, reconnect=False)
        rt.start()
        conn = _accept_and_hello(srv)
        try:
            _send_replay_complete(conn, 0, epoch=1)
            assert _poll_until(lambda: rt._received_replay_complete is not None)
            rt.drain_queue()
            assert rt.mark_replay_applied()
            assert rt.synchronized

            send_framed(conn, encode_message({"type": "resync"}))
            _send_event(conn, 1)
            _send_replay_complete(conn, 1, epoch=2)
            assert _poll_until(lambda: rt.last_seq == 1)
            assert _poll_until(
                lambda: rt._received_replay_complete is not None
                and rt._received_replay_complete[2] == 2
            )
            assert not rt.synchronized

            queued = rt.drain_queue()
            assert [message_to_dict(raw)["type"] for raw in queued] == [
                "resync",
                "event",
            ]
            assert rt.mark_replay_applied()
            assert rt.synchronized
            assert rt.replay_head_seq == 1
            assert rt.replay_epoch == 2
        finally:
            _teardown(rt, conn, srv)

    def test_requested_replay_rewinds_sequence_and_clears_queue(self, caplog):
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1",
            port=port,
            reconnect=True,
            reconnect_base_delay=0.05,
            reconnect_max_delay=0.2,
        )
        rt.start()
        conn1 = _accept_and_hello(srv)
        conn2 = None
        try:
            _send_event(conn1, 1)
            _send_event(conn1, 2)
            assert _poll_until(lambda: rt.last_seq == 2)

            caplog.set_level(logging.WARNING, logger="openusdconnect.receiver")
            rt.request_replay_from(2)
            assert rt.last_seq == 1
            assert len(rt.drain_queue()) == 0

            conn2 = _accept(srv, timeout=2)
            hello = _recv_hello(conn2)
            assert hello["sync_from"] == 2
            send_framed(
                conn2,
                encode_message({"type": "hello_ok", "layered_replay": True}),
            )
            assert _poll_until(lambda: rt.connected)

            _send_event(conn2, 2)
            assert _poll_until(lambda: rt.last_seq == 2)
            assert "socket error during read" not in caplog.text
        finally:
            conn1.close()
            if conn2 is not None:
                _teardown(rt, conn2, srv)
            else:
                rt.stop()
                rt.join(timeout=1)
                srv.close()

    def test_requested_replay_closes_current_socket(self):
        rt = ReceiverThread()
        sock = socket.socket()
        rt.sock = sock
        try:
            rt.request_replay_from(1)
            assert sock.fileno() == -1
            assert rt.sock is None
        finally:
            sock.close()

    def test_close_socket_logs_cleanup_errors(self, caplog):
        class BrokenSocket:
            def shutdown(self, _how):
                raise OSError("shutdown failed")

            def close(self):
                raise OSError("close failed")

        rt = ReceiverThread()
        sock = BrokenSocket()
        rt.sock = sock

        with caplog.at_level(logging.DEBUG, logger="openusdconnect.receiver"):
            rt._close_socket()

        assert rt.sock is None
        assert "socket shutdown failed during close" in caplog.text
        assert "socket close failed" in caplog.text

    def test_no_reconnect_when_disabled(self):
        """With reconnect=False, thread exits after connection loss."""
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1",
            port=port,
            reconnect=False,
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
            host="127.0.0.1",
            port=port,
            reconnect=False,
            socket_timeout=0.05,
        )
        rt.start()
        conn = _accept_and_hello(srv)
        try:
            _poll_until(lambda: rt.connected)

            # Wait longer than socket timeout
            time.sleep(0.1)

            # Connection should still be alive timeout just means no data
            assert rt.connected

            # Send data after timeout should still be received
            _send_event(conn, 1)
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
            host="127.0.0.1",
            port=port,
            reconnect=True,
            max_queue=3,
            reconnect_base_delay=0.05,
            reconnect_max_delay=0.2,
        )
        rt.start()

        # First connection
        conn1 = _accept_and_hello(srv)

        # Send 5 events into a queue with max depth 3 overflow disconnects
        # the receiver mid-flood, so tolerate the RST from its closed socket.
        _flood_events(conn1, range(1, 6))

        # Wait for overflow to trigger disconnect
        _poll_until(lambda: not rt.connected, timeout=5)

        # Queue should have the events it managed to buffer (up to 3)
        msgs = rt.drain_queue()
        assert len(msgs) <= 3

        # Receiver should reconnect automatically
        conn2 = _accept(srv, timeout=5)
        hello = _recv_hello(conn2)
        assert hello["type"] == "hello"
        # Should request replay from where it left off
        assert hello["sync_from"] > 0

        _teardown(rt, conn2, srv)
        conn1.close()

    def test_queue_overflow_no_reconnect_when_disabled(self):
        """With reconnect=False, overflow stops the thread."""
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1",
            port=port,
            reconnect=False,
            max_queue=3,
            socket_timeout=0.1,
        )
        rt.start()
        conn = _accept_and_hello(srv)
        try:
            _flood_events(conn, range(1, 6))

            rt.join(timeout=5)
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
            _send_event(conn, 1)
            _send_ping(conn)
            _send_event(conn, 2)

            collected = []
            _poll_until(lambda: collected.extend(rt.drain_queue()) or len(collected) >= 2)
            assert len(collected) == 2
            assert rt.last_seq == 2
        finally:
            _teardown(rt, conn, srv)

    def test_ping_resets_timeout_counter(self):
        """Receiving a ping prevents consecutive timeout disconnect."""
        srv, port = _make_server()
        rt = ReceiverThread(
            host="127.0.0.1",
            port=port,
            reconnect=False,
            socket_timeout=0.05,
        )
        import openusdconnect.receiver as recv_mod

        original = recv_mod._MAX_CONSECUTIVE_TIMEOUTS
        recv_mod._MAX_CONSECUTIVE_TIMEOUTS = 4
        try:
            rt.start()
            conn = _accept_and_hello(srv)
            _poll_until(lambda: rt.connected)
            # Each wait is below four timeouts, while their sum is above it.
            time.sleep(0.12)
            _send_ping(conn)
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
            host="127.0.0.1",
            port=port,
            reconnect=False,
            socket_timeout=0.02,
        )
        import openusdconnect.receiver as recv_mod

        original = recv_mod._MAX_CONSECUTIVE_TIMEOUTS
        recv_mod._MAX_CONSECUTIVE_TIMEOUTS = 3
        try:
            rt.start()
            conn = _accept_and_hello(srv)
            _poll_until(lambda: rt.connected)
            # Don't send anything let timeouts accumulate
            rt.join(timeout=2)
            assert not rt.is_alive()
        finally:
            recv_mod._MAX_CONSECUTIVE_TIMEOUTS = original
            conn.close()
            srv.close()
