"""ClientObserver wiring and typed notification payloads."""

from pxr import Usd

from openusdconnect import AppliedBatch, ClientObserver, PlaybackClaim, PlaybackState, UsdReceiver
from openusdconnect._observer_hooks import ObserverHooks, observer_hooks
from openusdconnect.codec import encode_message
from openusdconnect.protocol_constants import K_ENSURE_PRIM, K_SET_REFERENCE, K_SET_VISIBILITY
from tests.helpers import RecordingObserver


def test_applied_batch_derives_paths_lazily_and_caches_them():
    events = [
        {"k": K_SET_VISIBILITY, "prim": "/World/B", "visible": True},
        {"k": K_SET_REFERENCE, "prim": "/World/A", "references": []},
        {"k": K_ENSURE_PRIM, "prim": "/World/B", "typeName": "Xform"},
    ]
    batch = AppliedBatch(7, events)
    assert batch._prim_paths is None and batch._imported_paths is None
    assert batch.prim_paths == ("/World/A", "/World/B")
    assert batch.prim_paths is batch.prim_paths
    assert batch.imported_paths == ("/World/A",)
    assert batch.seq == 7


def test_only_overridden_methods_are_wired():
    class Paths(ClientObserver):
        def on_applied(self, batch):
            pass

    assert observer_hooks(None, lambda cb: cb) == ObserverHooks()
    assert observer_hooks(ClientObserver(), lambda cb: cb) == ObserverHooks()
    hooks = observer_hooks(Paths(), lambda cb: cb)
    assert hooks.on_applied is not None
    assert set(hooks.receiver_callbacks().values()) == {None}


def test_receiver_update_delivers_applied_batches_with_the_drained_sequence(monkeypatch):
    batches = []

    class Record(ClientObserver):
        def on_applied(self, batch):
            batches.append((batch.seq, batch.prim_paths))

    client = UsdReceiver(
        Usd.Stage.CreateInMemory(), app_name="observer-batches", persist_token=False,
        reconnect=False, observer=Record(),
    )
    frames = [
        encode_message({
            "type": "event", "seq": seq,
            "event": {"k": K_ENSURE_PRIM, "prim": prim, "typeName": "Xform"},
        })
        for seq, prim in ((1, "/World"), (2, "/World/A"))
    ]
    monkeypatch.setattr(client.receiver, "drain_queue", lambda max_messages=None: frames)
    client._started = True
    try:
        assert client.update().applied_events == 2
        assert batches == [(2, ("/World", "/World/A"))]
    finally:
        client.close()


def test_notification_payloads_are_typed():
    observer = RecordingObserver()
    callbacks = observer_hooks(observer, lambda callback: callback).receiver_callbacks()
    callbacks["on_playback_state"](
        {"type": "playback_state", "playing": True, "time": 2.0, "rate": 1.0,
         "leader_client_id": "a"}
    )
    callbacks["on_playback_claimed"]({"type": "playback_claimed", "leader_client_id": "a"})
    callbacks["on_playback_rejected"](
        {"type": "playback_rejected", "reason": "busy", "current_leader_client_id": "b"}
    )
    assert [value for _name, value, _thread in observer.calls] == [
        PlaybackState(True, 2.0, 1.0, "a"),
        PlaybackClaim(True, "a"),
        PlaybackClaim(False, "b", "busy"),
    ]
