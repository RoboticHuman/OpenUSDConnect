"""ClientObserver wiring and typed notification payloads."""

from pxr import Usd

from openusdconnect import AppliedBatch, ClientObserver, PlaybackClaim, PlaybackState, UsdReceiver
from openusdconnect.client_observer import observer_callbacks
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

    assert observer_callbacks(None, lambda cb: cb, lambda: 0) == {}
    assert observer_callbacks(ClientObserver(), lambda cb: cb, lambda: 0) == {}
    assert set(observer_callbacks(Paths(), lambda cb: cb, lambda: 0)) == {"on_applied_events"}


def test_receiver_delivers_applied_batches_with_their_sequence():
    batches = []

    class Record(ClientObserver):
        def on_applied(self, batch):
            batches.append((batch.seq, batch.prim_paths))

    client = UsdReceiver(
        Usd.Stage.CreateInMemory(), app_name="observer-batches", persist_token=False,
        reconnect=False, observer=Record(),
    )
    try:
        client.dispatcher._applying_seq = 4
        client.dispatcher._apply([{"k": K_ENSURE_PRIM, "prim": "/World", "typeName": "Xform"}])
        assert batches == [(4, ("/World",))]
    finally:
        client.close()


def test_notification_payloads_are_typed():
    observer = RecordingObserver()
    callbacks = observer_callbacks(observer, lambda callback: callback, lambda: 0)
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
