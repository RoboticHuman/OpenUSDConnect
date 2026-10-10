"""ClientObserver wiring and typed notification payloads."""

from pxr import Usd

from openusdconnect import (
    AppliedBatch,
    ClientObserver,
    ClientPhase,
    ManagedClient,
    PlaybackClaim,
    PlaybackState,
    UsdReceiver,
)
from openusdconnect.codec import encode_message
from openusdconnect.protocol_constants import (
    K_ENSURE_PRIM,
    K_SET_REFERENCE,
    K_SET_VISIBILITY,
    MSG_PLAYBACK_CLAIMED,
    MSG_PLAYBACK_REJECTED,
)
from tests.helpers import RecordingObserver, embedded_server, wait_until


def test_applied_batch_derives_sorted_unique_paths_once():
    events = [
        {"k": K_SET_VISIBILITY, "prim": "/World/B", "visible": True},
        {"k": K_SET_REFERENCE, "prim": "/World/A", "references": []},
        {"k": K_ENSURE_PRIM, "prim": "/World/B", "typeName": "Xform"},
    ]
    batch = AppliedBatch(7, events)
    assert batch.prim_paths == ("/World/A", "/World/B")
    assert batch.prim_paths is batch.prim_paths
    assert batch.imported_paths == ("/World/A",)
    assert batch.seq == 7


def test_only_overridden_methods_are_wired():
    class Paths(ClientObserver):
        def on_applied(self, batch):
            pass

    def wiring(observer):
        client = UsdReceiver(
            Usd.Stage.CreateInMemory(), app_name="observer-wiring", persist_token=False,
            observer=observer,
        )
        dispatcher = client.dispatcher
        client.close()
        wired = client._notification_methods
        return dispatcher.on_applied_events is not None, dispatcher.on_resync, wired

    assert wiring(None) == wiring(ClientObserver()) == (False, None, {})
    assert wiring(Paths()) == (True, None, {})


def _event_frames(*paths):
    return [
        encode_message({
            "type": "event", "seq": seq,
            "event": {"k": K_ENSURE_PRIM, "prim": prim, "typeName": "Xform"},
        })
        for seq, prim in enumerate(paths, start=1)
    ]


def test_receiver_update_delivers_applied_batches_with_the_drained_sequence(monkeypatch):
    batches = []

    class Record(ClientObserver):
        def on_applied(self, batch):
            batches.append((batch.seq, batch.prim_paths))

    client = UsdReceiver(
        Usd.Stage.CreateInMemory(), app_name="observer-batches", persist_token=False,
        reconnect=False, observer=Record(),
    )
    frames = _event_frames("/World", "/World/A")
    monkeypatch.setattr(client.receiver, "drain_queue", lambda max_messages=None: frames)
    client._started = True
    try:
        assert client.update().applied_events == 2
        assert batches == [(2, ("/World", "/World/A"))]
    finally:
        client.close()


def test_close_from_a_delivery_method_takes_effect_after_the_apply(monkeypatch):
    class CloseOnApply(ClientObserver):
        def on_applied(self, batch):
            client.close()

    stage = Usd.Stage.CreateInMemory()
    client = ManagedClient(
        stage, app_name="close-on-apply", persist_token=False, reconnect=False,
        observer=CloseOnApply(),
    )
    monkeypatch.setattr(
        client.receiver, "drain_queue", lambda max_messages=None: _event_frames("/World"),
    )
    client._started = True
    assert client.update().applied_events == 1
    assert client.status.phase is ClientPhase.CLOSED
    assert stage.GetPrimAtPath("/World")


def test_stage_edits_made_in_on_resync_are_not_published(monkeypatch):
    class Reset(ClientObserver):
        def on_resync(self):
            client.stage.DefinePrim("/FromResync", "Xform")

    client = ManagedClient(
        Usd.Stage.CreateInMemory(), app_name="resync-edit", persist_token=False,
        reconnect=False, observer=Reset(),
    )
    monkeypatch.setattr(
        client.receiver, "drain_queue",
        lambda max_messages=None: [encode_message({"type": "resync"})],
    )
    client._started = True
    try:
        client.update()
        assert client.stage.GetPrimAtPath("/FromResync")
        assert not client.status.has_unsent_changes
    finally:
        client.close()


def test_notification_payloads_are_typed():
    observer = RecordingObserver()

    def playback():
        return [value for name, value, _thread in observer.calls if name.startswith("playback")]

    with embedded_server() as server:
        state = server.sync_server
        client = UsdReceiver(
            Usd.Stage.CreateInMemory(), app_name="typed-notifications",
            port=server.server_address[1], persist_token=False, observer=observer,
        )
        try:
            client.start()
            # The server sends its playback state after every accepted hello.
            wait_until(lambda: client.update() is not None and playback())
            state.broadcast_message({"type": MSG_PLAYBACK_CLAIMED, "leader_client_id": "a"})
            state.broadcast_message({
                "type": MSG_PLAYBACK_REJECTED, "reason": "busy", "current_leader_client_id": "b",
            })
            wait_until(lambda: client.update() is not None and len(playback()) == 3)
        finally:
            client.close()
    assert playback() == [
        PlaybackState(**state.get_playback_state()),
        PlaybackClaim(True, "a"),
        PlaybackClaim(False, "b", "busy"),
    ]
