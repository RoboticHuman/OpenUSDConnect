"""Tests for EventDispatcher callbacks."""

from openusdconnect.adapters import MockAdapter
from openusdconnect.codec import encode_message
from openusdconnect.dispatcher import EventDispatcher
from openusdconnect.protocol_constants import K_ENSURE_PRIM, K_SET_VISIBILITY


class _NullReceiver:
    def drain_queue(self):
        return []


class _QueuedReceiver:
    def __init__(self, messages):
        self.messages = list(messages)
        self.replay_requests = []

    def drain_queue(self):
        messages = self.messages
        self.messages = []
        return messages

    def request_replay_from(self, seq_start):
        self.replay_requests.append(seq_start)


def _event(seq, prim):
    return encode_message(
        {
            "type": "event",
            "seq": seq,
            "event": {"k": K_ENSURE_PRIM, "prim": prim, "typeName": "Xform"},
        }
    )


def test_on_applied_receives_applied_prim_paths():
    captured = []
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=MockAdapter(),
        on_applied=captured.append,
    )

    dispatcher._apply(
        [
            {"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Xform"},
            {"k": K_SET_VISIBILITY, "prim": "/World/B", "visible": True},
        ]
    )

    assert captured == [["/World/A", "/World/B"]]


def test_on_applied_optional():
    """A dispatcher without on_applied applies cleanly (no callback)."""
    dispatcher = EventDispatcher(receiver=_NullReceiver(), adapter=MockAdapter())
    assert dispatcher._apply([{"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Xform"}]) == 1


def test_decode_failure_applies_prefix_and_requests_replay():
    receiver = _QueuedReceiver(
        [
            _event(1, "/World/A"),
            b"not-a-flatbuffer",
            _event(2, "/World/B"),
        ]
    )
    adapter = MockAdapter()
    callback_sequences = []
    dispatcher = EventDispatcher(
        receiver=receiver,
        adapter=adapter,
        on_applied=lambda _paths: callback_sequences.append(dispatcher.last_seq),
    )

    assert dispatcher.drain_and_apply() == 1
    assert dispatcher.last_seq == 1
    assert callback_sequences == [1]
    assert receiver.replay_requests == [2]
    assert "/World/A" in adapter._prims
    assert "/World/B" not in adapter._prims

    receiver.messages = [_event(2, "/World/B")]
    assert dispatcher.drain_and_apply() == 1
    assert dispatcher.last_seq == 2
    assert "/World/B" in adapter._prims


def test_unnegotiated_layered_request_uses_flat_dispatch():
    receiver = _QueuedReceiver([_event(1, "/World/A")])
    receiver.layered_replay = True
    receiver.layered_replay_active = False
    adapter = MockAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter)

    assert dispatcher.drain_and_apply() == 1
    assert dispatcher.layer_router is None
    assert "/World/A" in adapter._prims
