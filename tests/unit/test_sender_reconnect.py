"""Background reconnect lifecycle and real socket handshake tests."""

import socket
import threading
import time
from unittest.mock import MagicMock

import pytest

from openusdconnect.codec import encode_message
from openusdconnect.framing import recv_framed, send_framed
from openusdconnect.sender import EventSender


def _finish(sender):
    with sender._condition:
        assert sender._condition.wait_for(
            lambda: sender._connect_thread is None,
            timeout=3,
        )


@pytest.mark.parametrize("cancel", ["cancel_connect", "disconnect"])
def test_cancel_blocked_creation_prevents_late_publication(monkeypatch, cancel):
    sender = EventSender("localhost", 1, client_id="cancel")
    entered, release = threading.Event(), threading.Event()
    sock = MagicMock()

    def create(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return sock

    monkeypatch.setattr(socket, "create_connection", create)
    try:
        assert sender.request_connect()
        assert entered.wait(2)
        assert not sender.request_connect()
        started = time.monotonic()
        result = getattr(sender, cancel)()
        assert time.monotonic() - started < 0.5
        if cancel == "cancel_connect":
            assert result is False
        assert not sender.request_connect()
    finally:
        release.set()
        _finish(sender)
    assert sender.sock is None
    assert sender.cancel_connect()
    sock.close.assert_called_once()
    sock.sendall.assert_not_called()
    assert not sender.recovery_required


def test_connect_timeout_includes_lock_wait():
    sender = EventSender("localhost", 1, client_id="lock")
    sender._connect_lock.acquire()
    try:
        started = time.monotonic()
        assert not sender.connect(timeout=0.03)
        assert time.monotonic() - started < 0.5
    finally:
        sender._connect_lock.release()


def test_disconnect_invalidates_synchronous_handshake(monkeypatch):
    sender = EventSender("localhost", 1, client_id="sync-cancel")
    entered, release = threading.Event(), threading.Event()
    sock = MagicMock()
    results = []

    def create(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return sock

    monkeypatch.setattr(socket, "create_connection", create)
    worker = threading.Thread(target=lambda: results.append(sender.connect()))
    worker.start()
    try:
        assert entered.wait(2)
        sender.disconnect()
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert results == [False]
    assert not sender.connected
    sock.close.assert_called_once()


def test_background_success_and_reconnect_replays_outbox():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(3)
        sender = EventSender(*server.getsockname(), client_id="replay")
        try:
            assert sender.request_connect()
            conn, _ = server.accept()
            with conn:
                conn.settimeout(2)
                recv_framed(conn)
                send_framed(conn, encode_message({"type": "hello_ok"}))
                _finish(sender)
                assert sender.connected
                assert not sender.request_connect()
                assert sender.send_events(
                    [
                        {"k": "ensure_prim", "prim": "/World/X", "typeName": "Xform"},
                    ]
                )
                original = recv_framed(conn)
                sender.disconnect()
            assert sender.request_connect()
            conn, _ = server.accept()
            with conn:
                conn.settimeout(2)
                recv_framed(conn)
                send_framed(conn, encode_message({"type": "hello_ok"}))
                assert recv_framed(conn) == original
                _finish(sender)
                assert sender.connected
                sender.disconnect()
        finally:
            sender.disconnect()
            _finish(sender)


def test_retry_backoff_and_default_budget(monkeypatch):
    sender = EventSender("localhost", 1, client_id="backoff")
    clock = [100.0]
    monkeypatch.setattr("openusdconnect.sender.time.monotonic", lambda: clock[0])
    attempt = MagicMock(return_value=False)
    monkeypatch.setattr(sender, "_connect_attempt", attempt)
    for delay in (1, 2, 4, 8, 8):
        assert sender.request_connect()
        _finish(sender)
        assert sender._connect_retry_at == clock[0] + delay
        assert not sender.request_connect()
        clock[0] += delay
    assert attempt.call_args.args[0] == 2.0
    attempt.return_value = True
    assert sender.request_connect()
    _finish(sender)
    assert sender._connect_retry_delay == 1.0


@pytest.mark.parametrize(
    "response,flag",
    [
        ({"type": "auth_rejected", "reason": "denied"}, "auth_rejected"),
        ({"type": "hello_rejected", "reason": "denied"}, "hello_rejected"),
        ({"type": "hello_ok", "committed_through": 9}, "recovery_required"),
    ],
)
def test_terminal_handshake_stops_background_retry(response, flag):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(3)
        sender = EventSender(*server.getsockname(), client_id="rejected")
        try:
            assert sender.request_connect()
            conn, _ = server.accept()
            with conn:
                conn.settimeout(2)
                recv_framed(conn)
                send_framed(conn, encode_message(response))
                _finish(sender)
            assert getattr(sender, flag)
            sender._connect_retry_at = 0
            assert not sender.request_connect()
            assert not sender.connected
        finally:
            sender.disconnect()


def test_cancel_interrupts_real_blocked_handshake():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(3)
        sender = EventSender(*server.getsockname(), client_id="blocked")
        try:
            assert sender.request_connect()
            conn, _ = server.accept()
            with conn:
                conn.settimeout(2)
                recv_framed(conn)
                sender.disconnect()
                _finish(sender)
                assert conn.recv(1) == b""
            assert not sender.connected
        finally:
            sender.disconnect()


def test_background_handshake_honors_short_timeout():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(3)
        sender = EventSender(*server.getsockname(), client_id="timeout")
        try:
            started = time.monotonic()
            assert sender.request_connect(timeout=0.05)
            conn, _ = server.accept()
            with conn:
                conn.settimeout(2)
                recv_framed(conn)
                _finish(sender)
                assert time.monotonic() - started < 1.0
            assert not sender.connected
            assert not sender.auth_rejected
            assert not sender.hello_rejected
        finally:
            sender.disconnect()


def test_cancel_after_hello_before_publication(monkeypatch):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(3)
        entered, release = threading.Event(), threading.Event()
        sender = EventSender(*server.getsockname(), client_id="late")
        accept = sender._accept_handshake_response

        def blocked(*args):
            result = accept(*args)
            entered.set()
            assert release.wait(3)
            return result

        monkeypatch.setattr(sender, "_accept_handshake_response", blocked)
        try:
            assert sender.request_connect()
            conn, _ = server.accept()
            with conn:
                conn.settimeout(2)
                recv_framed(conn)
                send_framed(conn, encode_message({"type": "hello_ok"}))
                assert entered.wait(2)
                sender.disconnect()
                release.set()
                _finish(sender)
            assert not sender.connected
            assert not sender.recovery_required
        finally:
            release.set()
            sender.disconnect()
            _finish(sender)


def test_flush_passes_remaining_budget(monkeypatch):
    sender = EventSender("localhost", 1, client_id="flush")
    session = MagicMock()
    session.empty = False
    monkeypatch.setattr(sender, "_session", session)
    attempt = MagicMock(return_value=False)
    monkeypatch.setattr(sender, "connect", attempt)
    assert not sender.flush(timeout=0.02)
    assert 0 < attempt.call_args.kwargs["timeout"] <= 0.02
