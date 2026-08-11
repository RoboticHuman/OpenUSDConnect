"""Opt-in per-event-kind wire byte counters on the sync server."""

import io
import threading
import time

from openusdconnect.codec import encode_message
from openusdconnect.framing import frame_batch
from openusdconnect.protocol import make_transaction_result
from openusdconnect.protocol_constants import MSG_RESYNC
from openusdconnect.sender import EventSender
from openusdconnect.server import connection as connection_module
from openusdconnect.server.connection import ConnectionHandler, ThreadedTCPServer
from openusdconnect.server.state import UsdSyncServer

EVENTS = [
    {"k": "ensure_prim", "prim": "/World/A", "typeName": "Xform"},
    {"k": "ensure_xform_ops", "prim": "/World/A"},
    {"k": "set_xform_trs", "prim": "/World/A", "fields": ["t"], "t": [1.0, 2.0, 3.0]},
    {"k": "set_xform_trs", "prim": "/World/A", "fields": ["t"], "t": [4.0, 5.0, 6.0]},
]


class _RecordingSocket:
    def __init__(self):
        self.payloads = []

    def sendall(self, payload):
        self.payloads.append(payload)


def test_unmeasured_single_result_uses_fast_path_without_preencoding(monkeypatch):
    sock = _RecordingSocket()
    result = make_transaction_result(1)
    sent = []

    def record_send_msg(actual_sock, actual_result):
        sent.append((actual_sock, actual_result))

    def reject_preencoding(_result):
        raise AssertionError("unmeasured single-result path encoded twice")

    monkeypatch.setattr(connection_module, "send_msg", record_send_msg)
    monkeypatch.setattr(connection_module, "encode_message", reject_preencoding)

    measured_bytes = connection_module._send_transaction_results(sock, [result])

    assert measured_bytes is None
    assert sent == [(sock, result)]
    assert sock.payloads == []


def test_unmeasured_result_batch_returns_none_after_one_socket_write():
    sock = _RecordingSocket()
    results = [make_transaction_result(1), make_transaction_result(2)]
    expected_payload = frame_batch([encode_message(result) for result in results])

    measured_bytes = connection_module._send_transaction_results(sock, results)

    assert measured_bytes is None
    assert sock.payloads == [expected_payload]


def test_measured_result_returns_actual_framed_byte_count():
    sock = _RecordingSocket()
    result = make_transaction_result(1)
    expected_payload = frame_batch([encode_message(result)])

    measured_bytes = connection_module._send_transaction_results(
        sock,
        [result],
        measure_bytes=True,
    )

    assert measured_bytes == len(expected_payload)
    assert sock.payloads == [expected_payload]


def test_disabled_by_default():
    s = UsdSyncServer(log_path=":memory:")
    assert s.wire_metrics is None
    s.process_txn(EVENTS, client_id="c1", origin="o1", client_addr="a:1")
    assert s.get_wire_metrics() == {"enabled": False}


def test_records_per_kind_on_txn():
    s = UsdSyncServer(log_path=":memory:", wire_metrics=True)
    s.process_txn(EVENTS, client_id="c1", origin="o1", client_addr="a:1")
    m = s.get_wire_metrics()
    assert m["enabled"] is True
    kinds = m["kinds"]
    assert kinds["ensure_prim"]["count"] == 1
    assert kinds["ensure_xform_ops"]["count"] == 1
    assert kinds["set_xform_trs"]["count"] == 2
    assert all(v["bytes"] > 0 for v in kinds.values())
    assert m["total_count"] == len(EVENTS)
    assert m["total_bytes"] == sum(v["bytes"] for v in kinds.values())


def test_records_server_broadcasts_by_message_type():
    s = UsdSyncServer(log_path=":memory:", wire_metrics=True)
    s.broadcast({"type": MSG_RESYNC, "reason": "test"})
    kinds = s.get_wire_metrics()["kinds"]
    assert kinds[MSG_RESYNC]["count"] == 1
    assert kinds[MSG_RESYNC]["bytes"] > 0


def test_transport_metrics_count_actual_receiver_fanout():
    class FakeHandler:
        def __init__(self):
            self.request = io.BytesIO()
            self.request.sendall = self.request.write
            self.send_lock = threading.Lock()
            self.client_address = ("receiver", 1)

    s = UsdSyncServer(log_path=":memory:", wire_metrics=True)
    receivers = (FakeHandler(), FakeHandler())
    payload = b"framed-payload"

    s._send_to_all(payload, targets=receivers)

    transport = s.get_wire_metrics()["transport"]
    assert transport["receiver_egress"] == {
        "count": 2,
        "bytes": len(payload) * 2,
    }
    s.shutdown()
    s.store.close()


def test_transport_metrics_count_real_producer_ingress_and_results():
    state = UsdSyncServer(log_path=":memory:", wire_metrics=True)
    tcp = ThreadedTCPServer(("127.0.0.1", 0), ConnectionHandler, state, max_workers=4)
    thread = threading.Thread(target=tcp.serve_forever, daemon=True)
    thread.start()
    sender = EventSender(
        "127.0.0.1",
        tcp.server_address[1],
        client_id="wire-metrics",
        session_id="wire-metrics",
    )
    try:
        assert sender.connect()
        assert sender.send_events(
            [{"k": "ensure_prim", "prim": "/World/Metrics", "typeName": "Xform"}]
        )
        assert sender.flush(timeout=5)

        deadline = time.monotonic() + 1.0
        while True:
            transport = state.get_wire_metrics()["transport"]
            if "producer_result_egress" in transport or time.monotonic() >= deadline:
                break
            # The client can consume the socket bytes immediately before the
            # server thread records its completed send in the metrics object.
            time.sleep(0.001)
        assert transport["client_ingress"]["count"] >= 2  # HELLO + Txn
        assert transport["client_ingress"]["bytes"] > 0
        assert transport["producer_result_egress"]["count"] == 1
        assert transport["producer_result_egress"]["bytes"] > 4
    finally:
        sender.disconnect()
        tcp.shutdown()
        tcp.server_close()
        thread.join(timeout=5)
        state.shutdown()
        state.store.close()
