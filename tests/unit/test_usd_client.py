"""Contract tests for the layered USD-native client API."""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from openusdconnect.usd_client import UsdPublisher, UsdReceiver


class _SenderStub:
    def __init__(self, results: list[bool]):
        self.connected = True
        self.auth_rejected = False
        self.token = None
        self.results = iter(results)
        self.batches: list[list[dict]] = []
        self.disconnect_count = 0

    def send_events(self, events: list[dict]) -> bool:
        self.batches.append(events)
        return next(self.results)

    def disconnect(self) -> None:
        self.connected = False
        self.disconnect_count += 1


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
