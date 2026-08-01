"""Network coverage for flat receiver projections of department-layer edits."""

from __future__ import annotations

import threading
import time

import pytest
from pxr import Sdf, Usd

from openusdconnect.codec import message_to_dict
from openusdconnect.emitter import NoticeEmitter
from openusdconnect.event_apply import apply_events
from openusdconnect.protocol_constants import K_SET_SDF_SPEC_FIELDS, MSG_EVENT
from openusdconnect.receiver import ReceiverThread
from openusdconnect.sender import EventSender
from openusdconnect.server import UsdSyncServer
from openusdconnect.server.connection import ConnectionHandler, ThreadedTCPServer


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    return predicate()


def _drain_through(receiver, target_seq):
    messages = []

    def _received_target():
        messages.extend(receiver.drain_queue())
        return receiver.last_seq >= target_seq

    assert _wait_until(_received_target)
    messages.extend(receiver.drain_queue())
    return [message_to_dict(raw) for raw in messages]


def _custom_attribute_events(value, custom_data):
    stage = Usd.Stage.CreateInMemory()
    prim = stage.DefinePrim("/World/Thing", "Xform")
    attr = prim.CreateAttribute(
        "userProperties:value",
        Sdf.ValueTypeNames.Int,
        custom=True,
    )
    attr.Set(value)
    attr.SetCustomData(custom_data)
    emitter = NoticeEmitter(stage)
    try:
        return emitter.snapshot_events()
    finally:
        emitter.cleanup()


def _variant_events(label, variant_name):
    stage = Usd.Stage.CreateInMemory()
    stage.GetRootLayer().customLayerData = {label: True}
    thing = stage.DefinePrim("/World/Thing", "Xform")
    thing.SetDocumentation(label)
    thing.SetCustomData({label: True, "shared": label})
    variants = thing.GetVariantSets().AddVariantSet("look")
    variants.AddVariant(variant_name)
    variants.SetVariantSelection(variant_name)
    with variants.GetVariantEditContext():
        child = stage.DefinePrim(f"/World/Thing/{label.title()}", "Scope")
        child.SetDocumentation(f"{label} variant")
    variants.ClearVariantSelection()
    emitter = NoticeEmitter(stage)
    try:
        return emitter.snapshot_events()
    finally:
        emitter.cleanup()


def _attribute_value(stage, path):
    attr = stage.GetAttributeAtPath(path)
    return attr.Get() if attr and attr.IsValid() else None


@pytest.fixture
def department_server(tmp_path):
    sync_server = UsdSyncServer(
        log_path=str(tmp_path / "department-projection.db"),
        department_priority=["animation", "layout"],
    )
    tcp_server = ThreadedTCPServer(
        ("127.0.0.1", 0),
        ConnectionHandler,
        sync_server,
        max_workers=8,
    )
    thread = threading.Thread(target=tcp_server.serve_forever, daemon=True)
    thread.start()
    try:
        yield sync_server, tcp_server.server_address[1]
    finally:
        tcp_server.shutdown()
        tcp_server.server_close()
        thread.join(timeout=5)
        sync_server.shutdown()
        sync_server.store.close()


def test_generic_sdf_projection_is_ordered_and_replayable(department_server):
    sync_server, port = department_server
    observer = ReceiverThread(
        port=port,
        reconnect=False,
        client_id="observer",
        origin="observer-origin",
    )
    weak_origin = ReceiverThread(
        port=port,
        reconnect=False,
        client_id="layout-view",
        origin="layout-origin",
    )
    animation = EventSender(
        "127.0.0.1",
        port,
        client_id="animator",
        origin="animation-origin",
        department="animation",
    )
    layout = EventSender(
        "127.0.0.1",
        port,
        client_id="layout",
        origin="layout-origin",
        department="layout",
    )
    late_receiver = None
    observer.start()
    weak_origin.start()
    try:
        assert _wait_until(lambda: observer.connected and weak_origin.connected)
        assert animation.connect()
        assert layout.connect()

        assert animation.send_events(_custom_attribute_events(2, {"animation": 2}))
        path = "/World/Thing.userProperties:value"
        assert _wait_until(lambda: _attribute_value(sync_server.stage, path) == 2)

        assert layout.send_events(_custom_attribute_events(1, {"layout": 1}))
        assert _wait_until(
            lambda: (
                sync_server.resolve_layer("layout")
                and sync_server.resolve_layer("layout").GetAttributeAtPath(path)
                and sync_server.resolve_layer("layout").GetAttributeAtPath(path).default == 1
            )
        )
        max_seq = sync_server.store.get_max_seq()

        live_messages = _drain_through(observer, max_seq)
        live_records = [msg for msg in live_messages if msg.get("type") == MSG_EVENT]
        live_sequences = [record["seq"] for record in live_records]
        assert live_sequences == sorted(live_sequences)
        assert max(live_sequences) == max_seq

        live_stage = Usd.Stage.CreateInMemory()
        apply_events(live_stage, [record["event"] for record in live_records])
        live_attr = live_stage.GetAttributeAtPath(path)
        assert live_attr.Get() == 2
        assert live_attr.GetCustomData() == {"animation": 2, "layout": 1}

        own_messages = _drain_through(weak_origin, max_seq)
        own_generic = [
            msg
            for msg in own_messages
            if msg.get("type") == MSG_EVENT
            and msg["event"]["k"] == K_SET_SDF_SPEC_FIELDS
            and msg["seq"] == max_seq
        ]
        assert len(own_generic) == 1
        assert "origin" not in own_generic[0]

        observer.stop()
        observer.join(timeout=2)
        late_receiver = ReceiverThread(
            port=port,
            reconnect=False,
            client_id="late-observer",
            origin="late-origin",
            sync_from=1,
        )
        late_receiver.start()
        assert _wait_until(lambda: late_receiver.connected)
        replay_messages = _drain_through(late_receiver, max_seq)
        replay_records = [msg for msg in replay_messages if msg.get("type") == MSG_EVENT]

        replay_stage = Usd.Stage.CreateInMemory()
        apply_events(replay_stage, [record["event"] for record in replay_records])
        replay_attr = replay_stage.GetAttributeAtPath(path)
        assert replay_attr.Get() == 2
        assert replay_attr.GetCustomData() == {"animation": 2, "layout": 1}

        stored_generic = {
            record["seq"]
            for record in (message_to_dict(blob) for blob in sync_server.store.get_from_seq_bin(1))
            if record["event"]["k"] == K_SET_SDF_SPEC_FIELDS
        }
        replay_generic = {
            record["seq"]
            for record in replay_records
            if record["event"]["k"] == K_SET_SDF_SPEC_FIELDS
        }
        assert replay_generic == stored_generic
    finally:
        animation.disconnect()
        layout.disconnect()
        observer.stop()
        observer.join(timeout=2)
        weak_origin.stop()
        weak_origin.join(timeout=2)
        if late_receiver is not None:
            late_receiver.stop()
            late_receiver.join(timeout=2)


def test_prim_layer_and_variant_projection_is_live_and_replayable(department_server):
    sync_server, port = department_server
    observer = ReceiverThread(
        port=port,
        reconnect=False,
        client_id="observer",
        origin="observer-origin",
    )
    animation = EventSender(
        "127.0.0.1",
        port,
        client_id="animator",
        origin="animation-origin",
        department="animation",
    )
    layout = EventSender(
        "127.0.0.1",
        port,
        client_id="layout",
        origin="layout-origin",
        department="layout",
    )
    late_receiver = None

    def _assert_projection(records):
        stage = Usd.Stage.CreateInMemory()
        apply_events(stage, [record["event"] for record in records])
        thing = stage.GetPrimAtPath("/World/Thing")
        assert stage.GetRootLayer().customLayerData == {
            "strong": True,
            "weak": True,
        }
        assert thing.GetDocumentation() == "strong"
        assert dict(stage.GetRootLayer().GetPrimAtPath("/World/Thing").customData) == {
            "shared": "strong",
            "strong": True,
            "weak": True,
        }
        variants = thing.GetVariantSets().GetVariantSet("look")
        assert variants.GetVariantNames() == ["blue", "red"]
        variants.SetVariantSelection("blue")
        assert stage.GetPrimAtPath("/World/Thing/Strong")
        variants.SetVariantSelection("red")
        assert stage.GetPrimAtPath("/World/Thing/Weak")

    observer.start()
    try:
        assert _wait_until(lambda: observer.connected)
        assert animation.connect()
        assert layout.connect()
        assert animation.send_events(_variant_events("strong", "blue"))
        assert layout.send_events(_variant_events("weak", "red"))
        assert _wait_until(
            lambda: (
                sync_server.resolve_layer("layout")
                and sync_server.resolve_layer("layout").GetObjectAtPath("/World/Thing{look=red}")
                and sync_server.stage.GetPrimAtPath("/World/Thing")
                and sync_server.stage.GetPrimAtPath("/World/Thing").GetDocumentation() == "strong"
            )
        )
        max_seq = sync_server.store.get_max_seq()
        live_messages = _drain_through(observer, max_seq)
        live_records = [msg for msg in live_messages if msg.get("type") == MSG_EVENT]
        _assert_projection(live_records)

        observer.stop()
        observer.join(timeout=2)
        late_receiver = ReceiverThread(
            port=port,
            reconnect=False,
            client_id="late-observer",
            origin="late-origin",
            sync_from=1,
        )
        late_receiver.start()
        assert _wait_until(lambda: late_receiver.connected)
        replay_messages = _drain_through(late_receiver, max_seq)
        replay_records = [msg for msg in replay_messages if msg.get("type") == MSG_EVENT]
        _assert_projection(replay_records)
    finally:
        animation.disconnect()
        layout.disconnect()
        observer.stop()
        observer.join(timeout=2)
        if late_receiver is not None:
            late_receiver.stop()
            late_receiver.join(timeout=2)


def test_replay_failure_unregisters_receiver(department_server, monkeypatch):
    sync_server, port = department_server

    def _fail_replay(_handler, _seq_start):
        raise OSError("injected replay failure")

    monkeypatch.setattr(sync_server, "replay_from", _fail_replay)
    receiver = ReceiverThread(
        port=port,
        reconnect=False,
        client_id="failing-replay",
        origin="failing-replay-origin",
    )
    receiver.start()
    receiver.join(timeout=5)
    try:
        assert not receiver.is_alive()
        assert _wait_until(lambda: not sync_server.receivers)
        assert _wait_until(lambda: not sync_server.clients)
    finally:
        receiver.stop()
        receiver.join(timeout=2)
