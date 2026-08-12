"""Concurrency tests for responses written by a server connection."""

import threading

from openusdconnect.protocol_constants import (
    MSG_PLAYBACK_CLAIMED,
    MSG_PLAYBACK_REJECTED,
)
from openusdconnect.server import connection as connection_module
from openusdconnect.server.connection import ConnectionHandler


def _make_handler():
    handler = ConnectionHandler.__new__(ConnectionHandler)
    handler.request = object()
    handler.send_lock = threading.Lock()
    handler._client_id = "playback-client"
    return handler


def test_control_response_waits_for_inflight_socket_writer(monkeypatch):
    handler = _make_handler()
    send_started = threading.Event()
    monkeypatch.setattr(
        connection_module,
        "send_msg",
        lambda _request, _message: send_started.set(),
    )

    handler.send_lock.acquire()
    writer = threading.Thread(
        target=handler._send_control_response,
        args=({"type": MSG_PLAYBACK_CLAIMED},),
    )
    writer.start()
    try:
        assert not send_started.wait(timeout=0.1)
    finally:
        handler.send_lock.release()

    assert send_started.wait(timeout=2)
    writer.join(timeout=2)
    assert not writer.is_alive()


def test_playback_handlers_use_serialized_control_responses():
    class PlaybackServer:
        claim_granted = False

        def claim_playback(self, _client_id, *, initial_time):
            return self.claim_granted, "current-leader"

        def apply_playback_control(self, _client_id, _action, _time, _rate):
            return False, "not the playback leader", "current-leader"

    handler = _make_handler()
    server = PlaybackServer()
    responses = []
    broadcasts = []
    handler._send_control_response = responses.append
    handler._broadcast_playback_state = broadcasts.append

    handler._handle_claim_playback(server, {})
    server.claim_granted = True
    handler._handle_claim_playback(server, {})
    handler._handle_playback_control(server, {"action": "play"})

    assert [response["type"] for response in responses] == [
        MSG_PLAYBACK_REJECTED,
        MSG_PLAYBACK_CLAIMED,
        MSG_PLAYBACK_REJECTED,
    ]
    assert broadcasts == [server]
