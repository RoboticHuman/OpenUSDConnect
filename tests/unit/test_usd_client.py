"""Contract tests for the layered USD-native client API."""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd, UsdGeom

from openusdconnect import SyncUpdate
from openusdconnect import coalescing as coalescing_module
from openusdconnect.managed_client import ManagedClient
from openusdconnect.usd_client import UsdPublisher, UsdReceiver


def test_bidirectional_clients_share_one_update_result_contract():
    assert SyncUpdate(received=1, sent=2, acknowledged=3, pending=4).pending == 4


class _SenderStub:
    def __init__(self, results: list[bool]):
        self.connected = True
        self.auth_rejected = False
        self.token = None
        self.results = iter(results)
        self.batches: list[list[dict]] = []
        self.disconnect_count = 0
        self.pending_event_count = 0
        self.recovery_required = False
        self.transaction_error = ""
        self.repaired: list[tuple[list[dict], str]] = []

    def send_events(self, events: list[dict]) -> bool:
        self.batches.append(events)
        accepted = next(self.results)
        if accepted:
            self.pending_event_count += len(events)
        return accepted

    def drain_acknowledged_event_count(self):
        return 0

    def disconnect(self) -> None:
        self.connected = False
        self.disconnect_count += 1

    def flush(self, timeout=None) -> bool:
        return True

    def connect(self) -> bool:
        self.connected = True
        return True

    def repair_rejected_transaction(self, events: list[dict], *, layer_key="") -> int:
        self.repaired.append((events, layer_key))
        self.connected = False
        return 7


def test_receiver_always_requests_full_layered_replay():
    receiver = UsdReceiver(
        Usd.Stage.CreateInMemory(),
        app_name="test-receiver",
        persist_token=False,
        reconnect=False,
    )
    try:
        assert receiver._receiver.sync_from == 1
        assert receiver._receiver.layered_replay is True
        with pytest.raises(RuntimeError, match="has not been started"):
            receiver.update()
    finally:
        receiver.close()


@pytest.mark.parametrize("composition_preserving", [False, True])
@pytest.mark.parametrize("snapshot_seq", [0, 7])
def test_receiver_rejects_a_stage_that_already_contains_live_state(
    composition_preserving,
    snapshot_seq,
):
    stage = Usd.Stage.CreateInMemory()
    stage.GetRootLayer().customLayerData = {
        "openusdconnect": {
            "live": True,
            "snapshot_seq": snapshot_seq,
            "composition_preserving": composition_preserving,
        }
    }

    with pytest.raises(ValueError, match="original base stage"):
        UsdReceiver(stage, app_name="test-receiver", persist_token=False)


def test_receiver_fails_closed_when_layered_replay_is_not_negotiated():
    receiver = UsdReceiver(
        Usd.Stage.CreateInMemory(),
        app_name="test-receiver",
        persist_token=False,
        reconnect=False,
    )
    receiver._started = True
    receiver._receiver.connected = True
    receiver._receiver.layered_replay_active = False

    with pytest.raises(RuntimeError, match="required layered replay"):
        receiver.update()

    assert not receiver.connected


def test_publisher_retains_exact_batch_until_send_succeeds():
    stage = Usd.Stage.CreateInMemory()
    publisher = UsdPublisher(
        stage,
        app_name="test-publisher",
        persist_token=False,
    )
    sender = _SenderStub([False, True, True])
    publisher._sender = sender
    try:
        prim = stage.DefinePrim("/World/Thing", "Xform")
        value = prim.CreateAttribute(
            "userProperties:value",
            Sdf.ValueTypeNames.Int,
            custom=True,
        )
        value.Set(1)

        assert publisher.update() == 0
        assert publisher.prepared_event_count > 0

        value.Set(2)
        assert publisher.update() > 0
        assert sender.batches[1] is sender.batches[0]
        assert publisher.prepared_event_count == 0

        assert publisher.update() > 0
        assert sender.batches[2] is not sender.batches[1]
    finally:
        publisher.close()


def _publisher_with_transform(monkeypatch, results):
    clock = [0.0]
    monkeypatch.setattr(coalescing_module, "monotonic", lambda: clock[0])
    stage = Usd.Stage.CreateInMemory()
    prim = UsdGeom.Xform.Define(stage, "/World/Thing").GetPrim()
    xformable = UsdGeom.Xformable(prim)
    translate = xformable.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble)
    xformable.AddOrientOp(UsdGeom.XformOp.PrecisionFloat)
    xformable.AddScaleOp(UsdGeom.XformOp.PrecisionDouble)
    publisher = UsdPublisher(
        stage,
        app_name="coalescing-publisher",
        persist_token=False,
        transform_coalesce_seconds=0.1,
    )
    sender = _SenderStub(results)
    publisher._sender = sender
    return publisher, sender, translate, clock


def test_publisher_coalesces_latest_default_time_transform_before_submission(monkeypatch):
    publisher, sender, translate, clock = _publisher_with_transform(
        monkeypatch,
        [True, True],
    )
    try:
        translate.Set((1, 0, 0))
        assert publisher.update() == 3  # definition and op-order barriers publish immediately

        translate.Set((2, 0, 0))
        assert publisher.update() == 0
        translate.Set((3, 0, 0))
        assert publisher.update() == 0
        assert len(sender.batches) == 1
        assert publisher.prepared_event_count == 1

        clock[0] = 0.11
        assert publisher.update() == 1
        assert sender.batches[-1] == [
            {
                "k": "set_xform_trs",
                "prim": "/World/Thing",
                "fields": ["t"],
                "t": [3.0, 0.0, 0.0],
            }
        ]
    finally:
        publisher.close()


def test_publisher_coalesced_batch_survives_failure_and_reconnect(monkeypatch):
    publisher, sender, translate, clock = _publisher_with_transform(
        monkeypatch,
        [True, False, True],
    )
    try:
        translate.Set((1, 0, 0))
        assert publisher.update() == 3

        translate.Set((2, 0, 0))
        assert publisher.update() == 0
        clock[0] = 0.11
        assert publisher.update() == 0
        failed_batch = sender.batches[-1]

        sender.connected = False
        translate.Set((3, 0, 0))
        assert publisher.update() == 0
        sender.connected = True
        assert publisher.update() == 1
        assert sender.batches[-1] is failed_batch
        assert sender.batches[-1][0]["t"] == [3.0, 0.0, 0.0]
    finally:
        publisher.close()


def test_publisher_flush_forces_a_buffered_transform_to_sender(monkeypatch):
    publisher, sender, translate, _clock = _publisher_with_transform(
        monkeypatch,
        [True, True],
    )
    try:
        translate.Set((1, 0, 0))
        assert publisher.update() == 3
        translate.Set((2, 0, 0))
        assert publisher.update() == 0

        assert publisher.flush(timeout=1.0)
        assert len(sender.batches) == 2
        assert sender.batches[-1][0]["t"] == [2.0, 0.0, 0.0]
    finally:
        publisher.close()


@pytest.mark.parametrize("value", [-0.1, float("inf"), float("nan")])
def test_publisher_rejects_invalid_transform_coalesce_window(value):
    with pytest.raises(ValueError, match="transform_coalesce_seconds"):
        UsdPublisher(
            Usd.Stage.CreateInMemory(),
            app_name="invalid-coalescing-window",
            persist_token=False,
            transform_coalesce_seconds=value,
        )


def test_managed_client_gates_new_edits_until_replay_is_applied_but_not_on_acks(
    monkeypatch,
):
    stage = Usd.Stage.CreateInMemory()
    client = ManagedClient(
        stage,
        app_name="readiness-gate",
        persist_token=False,
        reconnect=False,
    )
    sender = _SenderStub([True, True])
    client._sender = sender
    client._started = True
    client._receiver.connected = True
    client._receiver.layered_replay_active = True
    monkeypatch.setattr(client, "_connect_sender", lambda: None)
    try:
        prim = stage.DefinePrim("/World/Thing", "Xform")
        value = prim.CreateAttribute("value", Sdf.ValueTypeNames.Int)
        value.Set(1)

        replaying = client.update()
        assert replaying.sent == 0
        assert sender.batches == []
        assert not client.synchronized

        client._receiver._synchronized_event.set()
        first = client.update()
        assert first.sent > 0
        assert client.synchronized
        assert sender.pending_event_count == first.sent

        value.Set(2)
        second = client.update()
        assert second.sent > 0
        assert len(sender.batches) == 2
        assert second.acknowledged == 0
        assert second.pending > first.pending
    finally:
        client.close()


def test_managed_client_uses_the_same_pre_submission_transform_window(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(coalescing_module, "monotonic", lambda: clock[0])
    stage = Usd.Stage.CreateInMemory()
    prim = UsdGeom.Xform.Define(stage, "/World/Thing").GetPrim()
    xformable = UsdGeom.Xformable(prim)
    translate = xformable.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble)
    xformable.AddOrientOp(UsdGeom.XformOp.PrecisionFloat)
    xformable.AddScaleOp(UsdGeom.XformOp.PrecisionDouble)
    client = ManagedClient(
        stage,
        app_name="managed-coalescing",
        persist_token=False,
        reconnect=False,
        transform_coalesce_seconds=0.1,
    )
    sender = _SenderStub([True, True])
    client._sender = sender
    client._started = True
    client._receiver.connected = True
    client._receiver.layered_replay_active = True
    client._receiver._synchronized_event.set()
    monkeypatch.setattr(client, "_connect_sender", lambda: None)
    try:
        translate.Set((1, 0, 0))
        assert client.update().sent == 3
        translate.Set((2, 0, 0))
        assert client.update().sent == 0
        translate.Set((3, 0, 0))
        assert client.update().sent == 0

        clock[0] = 0.11
        assert client.update().sent == 1
        assert sender.batches[-1] == [
            {
                "k": "set_xform_trs",
                "prim": "/World/Thing",
                "fields": ["t"],
                "t": [3.0, 0.0, 0.0],
            }
        ]
    finally:
        client.close()


def test_publisher_does_not_consume_edits_while_disconnected():
    stage = Usd.Stage.CreateInMemory()
    publisher = UsdPublisher(
        stage,
        app_name="test-publisher",
        persist_token=False,
    )
    sender = _SenderStub([True])
    sender.connected = False
    publisher._sender = sender
    try:
        stage.DefinePrim("/World/Thing", "Xform")

        assert publisher.update() == 0
        assert publisher.prepared_event_count == 0

        sender.connected = True
        assert publisher.update() > 0
    finally:
        publisher.close()


def test_publisher_repair_and_resume_reconnects_the_ordered_outbox():
    publisher = UsdPublisher(
        Usd.Stage.CreateInMemory(),
        app_name="repair-publisher",
        persist_token=False,
    )
    sender = _SenderStub([True])
    publisher._sender = sender
    events = [{"k": "set_visibility", "prim": "/World", "visible": True}]
    try:
        assert publisher.repair_and_resume(events) == 7
        assert sender.repaired == [(events, "")]
        assert sender.connected
    finally:
        publisher.close()


def test_publisher_requires_a_prepared_batch_to_be_retried_before_snapshot():
    stage = Usd.Stage.CreateInMemory()
    publisher = UsdPublisher(
        stage,
        app_name="test-publisher",
        persist_token=False,
    )
    publisher._sender = _SenderStub([False])
    try:
        stage.DefinePrim("/World/Thing", "Xform")
        assert publisher.update() == 0
        assert publisher.prepared_event_count > 0

        with pytest.raises(RuntimeError, match=r"call update\(\)"):
            publisher.publish_current_edit_target()
    finally:
        publisher.close()


def test_publisher_can_publish_a_stage_authored_before_construction():
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/World/Thing", "Xform")
    publisher = UsdPublisher(
        stage,
        app_name="test-publisher",
        persist_token=False,
    )
    sender = _SenderStub([True])
    publisher._sender = sender
    try:
        assert publisher.publish_current_edit_target() > 0
        assert any(event.get("prim") == "/World/Thing" for event in sender.batches[0])
    finally:
        publisher.close()


def test_current_edit_target_publication_is_retained_for_update_retry():
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/World/Thing", "Xform")
    publisher = UsdPublisher(
        stage,
        app_name="test-publisher",
        persist_token=False,
    )
    sender = _SenderStub([False, True])
    publisher._sender = sender
    try:
        assert publisher.publish_current_edit_target() == 0
        assert publisher.prepared_event_count > 0

        assert publisher.update() > 0
        assert sender.batches[1] is sender.batches[0]
        assert publisher.prepared_event_count == 0
    finally:
        publisher.close()


def test_inner_blocks_are_read_only():
    """Inner blocks are accessible for advanced use but not reassignable."""
    stage = Usd.Stage.CreateInMemory()
    publisher = UsdPublisher(stage, app_name="test-publisher", persist_token=False)
    receiver = UsdReceiver(
        stage,
        app_name="test-receiver",
        persist_token=False,
        reconnect=False,
    )
    replacement = Usd.Stage.CreateInMemory()
    try:
        # Public escape hatches exist (read-only).
        assert publisher.sender is not None
        assert publisher.emitter is not None
        assert receiver.receiver is not None
        assert receiver.dispatcher is not None
        # Stage and inner blocks are not reassignable.
        with pytest.raises(AttributeError):
            publisher.sender = None
        with pytest.raises(AttributeError):
            publisher.emitter = None
        with pytest.raises(AttributeError):
            receiver.receiver = None
        with pytest.raises(AttributeError):
            receiver.dispatcher = None
        with pytest.raises(AttributeError):
            publisher.stage = replacement
        with pytest.raises(AttributeError):
            receiver.stage = replacement
    finally:
        receiver.close()
        publisher.close()


def test_app_name_is_required():
    stage = Usd.Stage.CreateInMemory()
    with pytest.raises(ValueError, match="app_name"):
        UsdPublisher(stage, app_name=" ", persist_token=False)
    with pytest.raises(ValueError, match="app_name"):
        UsdReceiver(stage, app_name=" ", persist_token=False)


def test_failed_publisher_context_entry_releases_the_emitter(monkeypatch):
    publisher = UsdPublisher(
        Usd.Stage.CreateInMemory(),
        app_name="test-publisher",
        persist_token=False,
    )
    monkeypatch.setattr(publisher, "connect", lambda: False)

    with pytest.raises(ConnectionError, match="could not connect"):
        publisher.__enter__()
    with pytest.raises(RuntimeError, match="is closed"):
        publisher.update()
