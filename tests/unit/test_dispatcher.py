"""Tests for EventDispatcher callbacks."""

import pytest
from pxr import Sdf, Usd

from openusdconnect.adapters import MockAdapter, UsdStageAdapter
from openusdconnect.codec import encode_message
from openusdconnect.dispatcher import EventDispatcher, _stage_sync_scope
from openusdconnect.protocol_constants import (
    K_ENSURE_PRIM,
    K_SET_SDF_SPEC_FIELDS,
    K_SET_VISIBILITY,
)
from openusdconnect.sdf_spec_delta import serialize_spec_fields


class _NullReceiver:
    layered_replay_active = False
    origin = None

    def drain_queue(self):
        return []

    def mark_replay_applied(self):
        return False

    def request_replay_from(self, _seq_start):
        pass


class _QueuedReceiver:
    layered_replay_active = False
    origin = None

    def __init__(self, messages):
        self.messages = list(messages)
        self.replay_requests = []

    def drain_queue(self):
        messages = self.messages
        self.messages = []
        return messages

    def request_replay_from(self, seq_start):
        self.replay_requests.append(seq_start)

    def mark_replay_applied(self):
        return False


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
    assert callback_sequences == [0]
    assert receiver.replay_requests == [2]
    assert "/World/A" in adapter._prims
    assert "/World/B" not in adapter._prims

    receiver.messages = [_event(2, "/World/B")]
    assert dispatcher.drain_and_apply() == 1
    assert dispatcher.last_seq == 2
    assert "/World/B" in adapter._prims


def test_adapter_failure_keeps_applied_cursor_and_requests_full_suffix_replay(
    monkeypatch,
):
    receiver = _QueuedReceiver([_event(1, "/World/Retry")])
    adapter = MockAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter)

    original_apply = adapter.apply_events

    def fail_apply(_events):
        raise RuntimeError("injected adapter failure")

    monkeypatch.setattr(adapter, "apply_events", fail_apply)
    with pytest.raises(RuntimeError, match="injected adapter failure"):
        dispatcher.drain_and_apply()

    assert dispatcher.last_seq == 0
    assert receiver.messages == []
    assert receiver.replay_requests == [1]

    monkeypatch.setattr(adapter, "apply_events", original_apply)
    receiver.messages = [_event(1, "/World/Retry")]
    assert dispatcher.drain_and_apply() == 1
    assert dispatcher.last_seq == 1
    assert "/World/Retry" in adapter._prims


def test_callback_failure_does_not_publish_applied_cursor():
    receiver = _QueuedReceiver([_event(1, "/World/Callback")])

    def fail_callback(_paths):
        raise RuntimeError("injected callback failure")

    dispatcher = EventDispatcher(
        receiver=receiver,
        adapter=MockAdapter(),
        on_applied=fail_callback,
    )

    with pytest.raises(RuntimeError, match="injected callback failure"):
        dispatcher.drain_and_apply()

    assert dispatcher.last_seq == 0
    assert receiver.replay_requests == [1]


def test_sequence_gap_is_not_applied_and_requests_missing_sequence():
    stage = Usd.Stage.CreateInMemory()
    receiver = _QueuedReceiver([_event(2, "/World/TooNew")])
    dispatcher = EventDispatcher(
        receiver=receiver,
        adapter=UsdStageAdapter(stage),
    )

    assert dispatcher.drain_and_apply() == 0
    assert dispatcher.last_seq == 0
    assert receiver.replay_requests == [1]
    assert not stage.GetPrimAtPath("/World/TooNew")


def test_unnegotiated_layered_request_uses_flat_dispatch():
    receiver = _QueuedReceiver([_event(1, "/World/A")])
    receiver.layered_replay = True
    receiver.layered_replay_active = False
    adapter = MockAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter)

    assert dispatcher.drain_and_apply() == 1
    assert dispatcher.layer_router is None
    assert "/World/A" in adapter._prims


def test_sdf_spec_batches_use_full_layer_atomic_rollback():
    source = Usd.Stage.CreateInMemory()
    source.GetRootLayer().documentation = "incoming"
    valid = {
        "k": K_SET_SDF_SPEC_FIELDS,
        "prim": "/",
        "spec_path": "/",
        "spec_kind": "layer",
        "fields": ["documentation"],
        "fragment": serialize_spec_fields(
            source.GetRootLayer(),
            Sdf.Path.absoluteRootPath,
            "layer",
            ["documentation"],
        ),
        "removed": False,
    }
    invalid = {
        "k": K_SET_SDF_SPEC_FIELDS,
        "prim": "/World",
        "spec_path": "/World",
        "spec_kind": "prim",
        "fields": ["documentation"],
        "fragment": "",
        "removed": False,
    }
    assert _stage_sync_scope([valid]) is None

    mirror = Usd.Stage.CreateInMemory()
    mirror.GetRootLayer().documentation = "original"
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=MockAdapter(),
        mirror_stage=mirror,
    )

    with pytest.raises(ValueError, match="valid Sdf fragment"):
        dispatcher._apply([valid, invalid])

    assert mirror.GetRootLayer().documentation == "original"
