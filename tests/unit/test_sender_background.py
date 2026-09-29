"""Deterministic backpressure and worker lifecycle checks for background writes."""

import socket
import threading

import pytest

from openusdconnect.codec import decode_envelope, encode_message, message_to_dict, resolve_payload
from openusdconnect.framing import recv_framed, send_framed
from openusdconnect.protocol import make_transaction_result
from openusdconnect.sender import EventSender
from openusdconnect.transport import send_raw


def _events(name):
    return [{"k": "ensure_prim", "prim": f"/World/{name}", "typeName": "Xform"}]


class _Socket:
    def __init__(self):
        self.closed = threading.Event()

    def settimeout(self, _timeout):
        pass

    def setsockopt(self, *_args):
        pass

    def shutdown(self, _how):
        self.closed.set()

    def close(self):
        self.closed.set()


def _mock_connections(monkeypatch, sender, sockets, *, highwater=0):
    sockets = iter(sockets)
    monkeypatch.setattr(socket, "create_connection", lambda *_args, **_kwargs: next(sockets))
    monkeypatch.setattr("openusdconnect.sender.send_msg", lambda *_args: None)
    monkeypatch.setattr(
        "openusdconnect.sender.recv_framed",
        lambda _sock: encode_message({"type": "hello_ok", "committed_through": highwater}),
    )
    monkeypatch.setattr(sender, "_read_results", lambda sock, _generation: sock.closed.wait(5))


def _acknowledge(sender, txn_id):
    _, result = resolve_payload(decode_envelope(encode_message(make_transaction_result(txn_id))))
    sender._accept_result(result, sender._socket_generation)


def _join(*threads):
    for thread in threads:
        if thread is not None:
            thread.join(timeout=2)
            assert not thread.is_alive()


def test_blocked_writer_does_not_block_submission_capacity_check_or_disconnect(monkeypatch):
    sender = EventSender(
        "localhost", 1, client_id="blocked-writer", background_send=True, max_pending_transactions=2
    )
    sock = _Socket()
    _mock_connections(monkeypatch, sender, [sock])
    entered = threading.Event()

    def blocked_write(current, _payload):
        entered.set()
        assert current.closed.wait(5)
        raise OSError("socket interrupted by shutdown")

    monkeypatch.setattr("openusdconnect.sender.send_raw", blocked_write)
    assert sender.connect()
    writer, reader = sender._writer_thread, sender._reader_thread
    finished = threading.Event()
    accepted = []
    events = _events("First")
    try:
        assert sender.send_events(events)
        assert entered.wait(2)
        events[0]["prim"] = "/World/ChangedAfterSubmission"

        def submit_then_disconnect():
            accepted.append(sender.send_events(_events("Second")))
            accepted.append(sender.send_events(_events("Full")))
            sender.disconnect()
            finished.set()

        caller = threading.Thread(target=submit_then_disconnect)
        caller.start()
        assert finished.wait(2), "submission or disconnect waited on the blocked writer"
        _join(caller, writer, reader)
        assert accepted == [True, False]
        assert not sender.connected
        assert sender.pending_transaction_count == 2
        assert sender.pending_event_count == 2
        assert sender._next_txn_id == 3
        assert message_to_dict(sender._session.entries()[0][1])["events"] == _events("First")
        assert sender._writer_thread is None
    finally:
        sock.closed.set()
        sender.disconnect()
        _join(writer, reader)


def test_concurrent_submissions_have_one_ordered_writer_and_acknowledged_capacity(monkeypatch):
    sender = EventSender(
        "localhost",
        1,
        client_id="ordered-writer",
        background_send=True,
        max_pending_transactions=16,
    )
    sock = _Socket()
    _mock_connections(monkeypatch, sender, [sock])
    wire = []
    writers = set()
    received = threading.Event()

    def write(_sock, payload):
        wire.append(message_to_dict(payload))
        writers.add(threading.current_thread())
        if len(wire) == 16:
            received.set()

    monkeypatch.setattr("openusdconnect.sender.send_raw", write)
    assert sender.connect()
    writer, reader = sender._writer_thread, sender._reader_thread
    barrier = threading.Barrier(16)
    accepted = []

    def submit(index):
        barrier.wait(timeout=2)
        accepted.append(sender.send_events(_events(f"Item{index}")))

    callers = [threading.Thread(target=submit, args=(index,)) for index in range(16)]
    try:
        for caller in callers:
            caller.start()
        _join(*callers)
        assert accepted == [True] * 16
        assert received.wait(2)
        assert [txn["txn_id"] for txn in wire] == list(range(1, 17))
        assert {txn["events"][0]["prim"] for txn in wire} == {
            f"/World/Item{index}" for index in range(16)
        }
        assert writers == {writer}
        assert not sender.send_events(_events("Full"))
        _acknowledge(sender, 16)
        assert sender.flush(timeout=0)
        assert sender.acknowledged_event_count == 16
        assert sender.send_events(_events("AfterAcknowledgement"))
    finally:
        sender.disconnect()
        _join(writer, reader, *callers)


def test_background_replay_uses_identical_bytes_and_discards_hello_acknowledged_prefix(monkeypatch):
    sender = EventSender("localhost", 1, client_id="replay-writer", background_send=True)
    first, second = _Socket(), _Socket()
    _mock_connections(monkeypatch, sender, [first, second])
    entered = threading.Event()
    replay_entered = threading.Event()
    release_replay = threading.Event()
    sent = threading.Event()
    wire = []

    def write(sock, payload):
        if sock is first:
            entered.set()
            assert sock.closed.wait(5)
            raise OSError("ambiguous write")
        replay_entered.set()
        assert release_replay.wait(5)
        wire.append(payload)
        if len(wire) == 2:
            _acknowledge(sender, 3)
            sent.set()

    monkeypatch.setattr("openusdconnect.sender.send_raw", write)
    assert sender.connect()
    first_writer, first_reader = sender._writer_thread, sender._reader_thread
    try:
        assert sender.send_events(_events("First"))
        assert entered.wait(2)
        assert sender.send_events(_events("Second"), layer_key="authored-layer")
        original = sender._session.entries()[1][1]
        sender.disconnect()
        _join(first_writer, first_reader)
        monkeypatch.setattr(
            "openusdconnect.sender.recv_framed",
            lambda _sock: encode_message({"type": "hello_ok", "committed_through": 1}),
        )
        # Connect completes the handshake even while the replay writer is blocked.
        assert sender.connect()
        assert replay_entered.wait(2)
        assert sender.send_events(_events("Third"))
        assert sender.pending_transaction_count == 2
        release_replay.set()
        assert sent.wait(2)
        assert sender.flush(timeout=0)
        assert wire[0] == original
        assert [message_to_dict(payload)["txn_id"] for payload in wire] == [2, 3]
        assert sender.acknowledged_event_count == 3
    finally:
        release_replay.set()
        writer, reader = sender._writer_thread, sender._reader_thread
        sender.disconnect()
        _join(first_writer, first_reader, writer, reader)


@pytest.mark.parametrize("thread_role", ["ack", "send"])
def test_worker_start_failure_rolls_back_connection(monkeypatch, thread_role):
    sender = EventSender("localhost", 1, client_id="failed-worker", background_send=True)
    sock = _Socket()
    _mock_connections(monkeypatch, sender, [sock])
    start = threading.Thread.start

    def fail_start(thread):
        if thread.name.startswith(f"openusdconnect-{thread_role}-"):
            raise RuntimeError("injected thread creation failure")
        start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="thread creation failure"):
        sender.connect()
    assert not sender.connected
    assert sock.closed.is_set()
    assert not sender.send_events(_events("AfterFailure"))
    assert sender._writer_thread is None
    if sender._reader_thread is not None and sender._reader_thread.ident is not None:
        _join(sender._reader_thread)


def test_old_writer_finishing_after_reconnect_cannot_close_the_new_connection(monkeypatch):
    sender = EventSender("localhost", 1, client_id="stale-writer", background_send=True)
    first, second = _Socket(), _Socket()
    _mock_connections(monkeypatch, sender, [first, second])
    old_finishing, release_old, replayed = (threading.Event() for _ in range(3))
    entered = threading.Event()
    wire = []
    close = sender._close

    def delayed_close(*, expected=None):
        if expected is first:
            old_finishing.set()
            assert release_old.wait(5)
        close(expected=expected)

    def write(sock, payload):
        if sock is first:
            entered.set()
            assert sock.closed.wait(5)
            raise OSError("old connection failed")
        wire.append(payload)
        replayed.set()

    monkeypatch.setattr(sender, "_close", delayed_close)
    monkeypatch.setattr("openusdconnect.sender.send_raw", write)
    assert sender.connect()
    first_writer, first_reader = sender._writer_thread, sender._reader_thread
    try:
        assert sender.send_events(_events("Retained"))
        assert entered.wait(2)
        original = sender._session.entries()[0][1]
        sender.disconnect()
        assert old_finishing.wait(2)
        # A decoded acknowledgement from the disconnected reader is also stale.
        _acknowledge(sender, 1)
        assert sender.transaction_failure is None
        assert sender.pending_transaction_count == 1
        assert sender.connect()
        assert replayed.wait(2)
        release_old.set()
        _join(first_writer, first_reader)
        assert sender.sock is second
        assert not second.closed.is_set()
        assert wire == [original]
        _acknowledge(sender, 1)
        assert sender.flush(timeout=0)
    finally:
        release_old.set()
        writer, reader = sender._writer_thread, sender._reader_thread
        sender.disconnect()
        _join(first_writer, first_reader, writer, reader)


def test_disconnect_interrupts_a_real_socket_with_a_full_send_buffer(monkeypatch):
    entered, sent, stop_server = (threading.Event() for _ in range(3))
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(3)

        def serve():
            conn, _ = server.accept()
            with conn:
                conn.settimeout(3)
                recv_framed(conn)
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                send_framed(conn, encode_message({"type": "hello_ok"}))
                assert stop_server.wait(5)  # Deliberately never consume transaction bytes.

        def write(sock, payload):
            entered.set()
            send_raw(sock, payload)
            sent.set()

        monkeypatch.setattr("openusdconnect.sender.send_raw", write)
        peer = threading.Thread(target=serve)
        peer.start()
        sender = EventSender(
            *server.getsockname(), client_id="real-backpressure", background_send=True
        )
        writer = reader = None
        try:
            assert sender.connect()
            writer, reader = sender._writer_thread, sender._reader_thread
            sender.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            assert sender.send_events(
                [
                    {
                        "k": "set_connectable_input",
                        "prim": "/World/Shader",
                        "info_id": "",
                        "inputs": {"large_string": "x" * (8 * 1024 * 1024)},
                    }
                ]
            )
            assert entered.wait(2)
            assert not sent.is_set()
            sender.disconnect()
            _join(writer, reader)
            assert not sent.is_set()
            assert sender.pending_transaction_count == 1
            assert not sender.connected
        finally:
            stop_server.set()
            sender.disconnect()
            _join(writer, reader, peer)
