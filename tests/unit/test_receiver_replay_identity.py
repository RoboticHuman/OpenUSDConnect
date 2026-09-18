"""Optional replay identities and receiver-owned prefix provenance."""

import pytest
from pxr import Usd

from openusdconnect.adapters import UsdStageAdapter
from openusdconnect.codec import decode_envelope, encode_message, message_to_dict, resolve_payload
from openusdconnect.dispatcher import EventDispatcher
from openusdconnect.protocol import make_hello
from openusdconnect.receiver import ReceiverThread


@pytest.mark.parametrize("identity", [None, ("", None), ("server", 0), ("server", 7)])
def test_optional_hello_prefix_identity(identity):
    hello = make_hello("receiver", sync_from=4)
    if identity is not None:
        hello["replay_server_instance"], epoch = identity
        if epoch is not None:
            hello["replay_epoch"] = epoch
    decoded = message_to_dict(encode_message(hello))
    assert decoded == hello
    _, table = resolve_payload(decode_envelope(encode_message(hello)))
    assert table.ReplayEpoch() == (identity[1] if identity else None)


@pytest.mark.parametrize("supported", [False, True])
def test_optional_hello_ok_replay_identity(supported):
    hello = {"type": "hello_ok"}
    if supported:
        hello["replay_identity"] = True
    assert message_to_dict(encode_message(hello)) == hello


def _hello(receiver, *, instance="server", supported=True, sync_from=1):
    return receiver._handle_handshake_message(
        encode_message({
            "type": "hello_ok", "server_instance": instance,
            "layered_replay": True, "replay_identity": supported,
        }),
        sync_from,
    )


def _receive(receiver, generation, **message):
    return receiver._handle_data_message(encode_message(message), generation)


def test_received_prefix_identity_is_not_published_until_applied():
    receiver = ReceiverThread()
    first = receiver._inbox.begin_connection()
    assert _hello(receiver, instance="old")
    assert _receive(receiver, first.generation, type="replay_complete", head_seq=0, epoch=2)
    assert receiver._received_replay_identity == ("old", 2)
    assert receiver.server_instance == ""
    assert receiver.mark_replay_applied()
    assert receiver.server_instance == "old"

    receiver.connected = False
    second = receiver._inbox.begin_connection()
    assert _hello(receiver, instance="new")
    assert receiver.server_instance == "old"
    assert not receiver.synchronized
    assert _receive(receiver, second.generation, type="resync")
    assert receiver._received_replay_identity is None
    assert _receive(receiver, second.generation, type="replay_complete", head_seq=0, epoch=0)
    assert not receiver.mark_replay_applied()
    receiver.drain_queue()
    assert receiver.mark_replay_applied()
    assert receiver.server_instance == "new"
    assert receiver.replay_epoch == 0


def test_interrupted_reset_does_not_reuse_old_prefix_identity():
    receiver = ReceiverThread()
    first = receiver._inbox.begin_connection()
    assert _hello(receiver)
    assert _receive(receiver, first.generation, type="replay_complete", head_seq=0, epoch=3)
    assert receiver.mark_replay_applied()
    assert _receive(receiver, first.generation, type="resync")
    assert receiver._received_replay_identity is None
    assert not receiver.synchronized
    receiver.connected = False
    receiver._inbox.begin_connection()
    assert receiver._received_replay_identity is None
    assert not receiver.mark_replay_applied()


def test_explicit_full_replay_queues_reset_before_colliding_events():
    receiver = ReceiverThread()
    stage = Usd.Stage.CreateInMemory()
    dispatcher = EventDispatcher(receiver=receiver, adapter=UsdStageAdapter(stage))
    first = receiver._inbox.begin_connection()
    assert _hello(receiver)
    stack = {"type": "layer_stack_state", "layers": [{"layer_key": "shared"}]}
    assert _receive(receiver, first.generation, **stack)
    assert _receive(receiver, first.generation, type="event", seq=1, layer_key="shared",
                    event={"k": "ensure_prim", "prim": "/Old", "typeName": "Xform"})
    dispatcher.drain_and_apply()
    assert stage.GetPrimAtPath("/Old")
    receiver.connected = False
    second = receiver._inbox.begin_connection()
    assert second.sync_from == 2
    receiver.request_replay_from(1)
    third = receiver._inbox.begin_connection()
    assert third.sync_from == 1
    assert _hello(receiver, sync_from=1)
    assert _receive(receiver, third.generation, **stack)
    assert _receive(receiver, third.generation, type="event", seq=1, layer_key="shared",
                    event={"k": "ensure_prim", "prim": "/Own", "typeName": "Xform"})
    assert _receive(receiver, third.generation, type="replay_complete", head_seq=1, epoch=0)
    dispatcher.drain_and_apply()
    assert stage.GetPrimAtPath("/Own")
    assert not stage.GetPrimAtPath("/Old")
    assert dispatcher.last_seq == 1
    assert receiver.synchronized
    dispatcher.close()
