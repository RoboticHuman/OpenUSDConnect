"""Concurrency tests for responses written by a server connection."""

import threading

import pytest

from openusdconnect.codec import decode_envelope, encode_message, resolve_payload
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


def _playback_table(message_type, **fields):
    wire = encode_message({"type": message_type, **fields})
    _, table = resolve_payload(decode_envelope(wire))
    return table


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

    handler._handle_claim_playback(server, _playback_table("claim_playback"))
    server.claim_granted = True
    handler._handle_claim_playback(server, _playback_table("claim_playback"))
    handler._handle_playback_control(server, _playback_table("playback_control", action="play"))

    assert [response["type"] for response in responses] == [
        MSG_PLAYBACK_REJECTED,
        MSG_PLAYBACK_CLAIMED,
        MSG_PLAYBACK_REJECTED,
    ]
    assert broadcasts == [server]


@pytest.mark.parametrize("initial_time", [None, 0.0, 12.5])
def test_playback_claim_preserves_optional_initial_time(initial_time):
    class PlaybackServer:
        def claim_playback(self, client_id, *, initial_time):
            calls.append((client_id, initial_time))
            return True, client_id

    calls = []
    handler = _make_handler()
    handler._send_control_response = lambda message: None
    handler._broadcast_playback_state = lambda server: None
    fields = {} if initial_time is None else {"time": initial_time}

    handler._handle_claim_playback(PlaybackServer(), _playback_table("claim_playback", **fields))

    assert calls == [("playback-client", initial_time)]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, ("", 0.0, 1.0)),
        ({"action": "play"}, ("play", 0.0, 1.0)),
        ({"action": "set_time", "time": 0.0}, ("set_time", 0.0, 1.0)),
        ({"action": "set_rate", "rate": 0.0}, ("set_rate", 0.0, 0.0)),
        ({"action": "set_rate", "rate": -2.0}, ("set_rate", 0.0, -2.0)),
    ],
)
def test_playback_control_defaults_only_absent_scalars(fields, expected):
    class PlaybackServer:
        def apply_playback_control(self, client_id, action, time_value, rate):
            calls.append((client_id, action, time_value, rate))
            return False, "rejected", client_id

    calls = []
    handler = _make_handler()
    handler._send_control_response = lambda message: None

    handler._handle_playback_control(
        PlaybackServer(), _playback_table("playback_control", **fields),
    )

    assert calls == [("playback-client", *expected)]
