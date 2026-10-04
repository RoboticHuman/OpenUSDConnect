"""Contract tests for the layered USD-native client API."""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd, UsdGeom

from openusdconnect import (
    ClientPhase,
    MockAdapter,
    RecoveryError,
    SyncUpdate,
    TransactionFailure,
    UsdStageAdapter,
)
from openusdconnect import coalescing as coalescing_module
from openusdconnect.codec import TransactionRejectionCode
from openusdconnect.managed_client import ManagedClient
from openusdconnect.recovery import (
    QuarantinedTransaction,
    RecoveryArtifact,
    make_recovery_incident,
)
from openusdconnect.usd_client import UsdPublisher, UsdReceiver
from tests.helpers import PeerTraffic, RecordingObserver, force_handshake


class _SenderStub:
    def __init__(self, results: list[bool]):
        self.connected = True
        self.auth_rejected = False
        self.hello_rejected = False
        self.rejection_reason = ""
        self.token = None
        self.results = iter(results)
        self.batches: list[list[dict]] = []
        self.disconnect_count = 0
        self.pending_event_count = 0
        self.recovery_required = False
        self.transaction_error = ""
        self.transaction_failure = None
        self.recovery_incident = None
        self.recovery_artifact = None
        self.acknowledged_event_count = 0
        self.connect_timeouts: list[float | None] = []
        self.connect_result = True
        self.repaired: list[tuple[list[dict], str]] = []
        self.abandoned_session_ids: list[str | None] = []
        self.connect_requests = 0

    def send_events(self, events: list[dict]) -> bool:
        self.batches.append(events)
        accepted = next(self.results)
        if accepted:
            self.pending_event_count += len(events)
        return accepted

    def drain_acknowledged_event_count(self):
        return 0

    def abandon_rejected_session(self, *, session_id=None):
        if self.recovery_artifact is None:
            raise RuntimeError("there is no rejected producer session to abandon")
        artifact = self.recovery_artifact
        self.abandoned_session_ids.append(session_id)
        self.recovery_required = False
        self.transaction_failure = None
        self.recovery_incident = None
        self.recovery_artifact = None
        self.connected = False
        return artifact

    def disconnect(self) -> None:
        self.connected = False
        self.disconnect_count += 1

    def flush(self, timeout=None) -> bool:
        return True

    def connect(self, timeout=None) -> bool:
        self.connect_timeouts.append(timeout)
        self.connected = self.connect_result
        return self.connect_result

    def request_connect(self, timeout=2.0) -> bool:
        self.connect_requests += 1
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


def test_receiver_parks_without_invalidating_dispatcher_adapter():
    stage = Usd.Stage.CreateInMemory()
    receiver = UsdReceiver(
        stage,
        app_name="parked-receiver",
        persist_token=False,
        reconnect=False,
    )
    try:
        adapter = receiver._dispatcher.adapter
        receiver.rebind_stage(None)
        receiver._started = True

        assert receiver._dispatcher.adapter is adapter
        assert receiver.update().applied_events == 0
    finally:
        receiver.close()


def test_receiver_update_forwards_message_budget(monkeypatch):
    receiver = UsdReceiver(
        Usd.Stage.CreateInMemory(),
        app_name="budgeted-receiver",
        persist_token=False,
        reconnect=False,
    )
    try:
        receiver._started = True
        calls = []
        monkeypatch.setattr(
            receiver._dispatcher,
            "drain_and_apply",
            lambda *, max_messages=None: calls.append(max_messages) or 3,
        )

        assert receiver.update(max_messages=128).applied_events == 3
        assert calls == [128]
    finally:
        receiver.close()


def test_receiver_owns_external_adapter_projection_lifecycle():
    mirror = Usd.Stage.CreateInMemory()
    adapter = MockAdapter()
    receiver = UsdReceiver(
        mirror,
        app_name="native-scene-receiver",
        adapter=adapter,
        persist_token=False,
        reconnect=False,
    )
    try:
        assert receiver.stage is mirror
        assert receiver._dispatcher.adapter is adapter
        assert receiver._dispatcher.mirror_stage is mirror

        replacement = Usd.Stage.CreateInMemory()
        receiver.rebind_stage(replacement)

        assert receiver.stage is replacement
        assert receiver._dispatcher.adapter is adapter
        assert receiver._dispatcher.mirror_stage is replacement
    finally:
        receiver.close()


def test_receiver_rebinds_an_explicit_usd_stage_adapter():
    original = Usd.Stage.CreateInMemory()
    adapter = UsdStageAdapter(original)
    receiver = UsdReceiver(
        original,
        app_name="explicit-stage-adapter",
        adapter=adapter,
        persist_token=False,
        reconnect=False,
    )
    try:
        replacement = Usd.Stage.CreateInMemory()
        receiver.rebind_stage(replacement)

        assert receiver._dispatcher.adapter is adapter
        assert adapter.targets_stage() is replacement
        assert receiver._dispatcher.mirror_stage is None

        adapter.ensure_prim("/World/Rebound", "Xform")
        assert replacement.GetPrimAtPath("/World/Rebound")
        assert not original.GetPrimAtPath("/World/Rebound")
    finally:
        receiver.close()


def test_receiver_surfaces_and_acknowledges_native_scene_rebuild():
    class _ProjectionState:
        native_scene_rebuild_required = True

        def __init__(self):
            self.acknowledgements = 0

        def acknowledge_native_scene_rebuilt(self):
            self.acknowledgements += 1
            self.native_scene_rebuild_required = False

        def close(self):
            pass

    receiver = UsdReceiver(
        Usd.Stage.CreateInMemory(),
        app_name="native-scene-recovery",
        adapter=MockAdapter(),
        persist_token=False,
        reconnect=False,
    )
    state = _ProjectionState()
    receiver._dispatcher._projection_state = state
    receiver._started = True
    receiver._receiver.connected = True
    receiver._receiver._synchronized_event.set()
    try:
        assert receiver.status.phase is ClientPhase.RECOVERY_REQUIRED
        assert "must be rebuilt" in receiver.status.reason

        receiver.acknowledge_native_scene_rebuilt()

        assert state.acknowledgements == 1
        assert receiver.status.phase is ClientPhase.READY
    finally:
        receiver.close()


def test_receiver_status_distinguishes_connecting_replay_and_ready():
    receiver = UsdReceiver(
        Usd.Stage.CreateInMemory(),
        app_name="status-receiver",
        persist_token=False,
        reconnect=False,
    )
    try:
        assert receiver.status.phase is ClientPhase.OFFLINE
        receiver._started = True
        assert receiver.status.phase is ClientPhase.CONNECTING
        receiver._receiver.connected = True
        assert receiver.status.phase is ClientPhase.REPLAYING
        receiver._receiver._synchronized_event.set()
        assert receiver.status.phase is ClientPhase.READY
        assert receiver.status.receiver_connected is True
        assert receiver.status.sender_connected is None
    finally:
        receiver.close()
    assert receiver.status.phase is ClientPhase.CLOSED


def test_publisher_context_start_is_nonblocking_and_update_connects_in_background():
    publisher = UsdPublisher(
        Usd.Stage.CreateInMemory(),
        app_name="lifecycle-publisher",
        persist_token=False,
    )
    sender = _SenderStub([])
    sender.connected = False
    publisher._sender = sender
    assert publisher.status.phase is ClientPhase.OFFLINE

    with publisher as entered:
        assert entered is publisher
        assert sender.connect_timeouts == []
        assert publisher.status.phase is ClientPhase.CONNECTING
        assert publisher.update().submitted_events == 0
        assert sender.connect_requests == 1
        assert sender.connect_timeouts == []

        publisher.disconnect()
        assert publisher.status.phase is ClientPhase.OFFLINE
        publisher.update()
        assert sender.connect_requests == 1

        assert publisher.connect(timeout=0.25)
        assert sender.connect_timeouts == [0.25]
        assert publisher.status.phase is ClientPhase.READY

        sender.connected = False
        sender.transaction_failure = TransactionFailure(1, 0, "repair required")
        sender.recovery_artifact = object()
        assert publisher.status.phase is ClientPhase.RECOVERY_REQUIRED
        assert "repair required" in publisher.status.reason
        assert publisher.recovery_artifact is sender.recovery_artifact

    assert publisher.status.phase is ClientPhase.CLOSED


def test_managed_status_exposes_partial_connection_and_event_counts():
    client = ManagedClient(
        Usd.Stage.CreateInMemory(),
        app_name="status-managed",
        persist_token=False,
        reconnect=False,
    )
    sender = _SenderStub([])
    sender.connected = False
    sender.pending_event_count = 4
    sender.acknowledged_event_count = 7
    client._sender = sender
    force_handshake(client, synchronized=True)
    try:
        status = client.status
        assert status.phase is ClientPhase.CONNECTING
        assert status.connected is False
        assert status.synchronized is True
        assert status.receiver_connected is True
        assert status.sender_connected is False
        assert status.pending_events == 4
        assert status.acknowledged_events_total == 7

        sender.connected = True
        assert client.status.phase is ClientPhase.READY
    finally:
        client.close()


@pytest.fixture
def ready_managed_client(monkeypatch):
    client = ManagedClient(
        Usd.Stage.CreateInMemory(), app_name="managed-api", persist_token=False,
    )
    sender = _SenderStub([True] * 10)
    client._sender = sender
    force_handshake(client, synchronized=True)
    monkeypatch.setattr(client._dispatcher, "drain_and_apply", lambda max_messages=None: 0)
    try:
        yield client, sender
    finally:
        client.close()


@pytest.mark.parametrize("options", [
    {"transform_coalesce_seconds": -1},
    {"extra_channels": [object()]},
])
def test_managed_invalid_configuration_preserves_stage(options):
    stage = Usd.Stage.CreateInMemory()
    stage.SetEditTarget(stage.GetSessionLayer())
    target = stage.GetEditTarget()
    session = stage.GetSessionLayer().ExportToString()
    with pytest.raises((TypeError, ValueError)):
        ManagedClient(stage, app_name="invalid-options", persist_token=False, **options)
    assert stage.GetEditTarget() == target
    assert stage.GetSessionLayer().ExportToString() == session


def test_managed_metadata_only_changes_count_as_unsent_work(ready_managed_client):
    client, _sender = ready_managed_client
    with Usd.EditContext(client.stage, client.stage.GetRootLayer()):
        client.stage.SetFramesPerSecond(48)
    assert client.status.has_unsent_changes
    # Evaluated even when the authoring layer does not own the opinion.
    client.update()
    assert not client.status.has_unsent_changes

    client.stage.GetRootLayer().framesPerSecond = 24
    assert client.status.has_unsent_changes
    replacement = Usd.Stage.CreateInMemory()
    replacement.SetFramesPerSecond(30)
    client.rebind_stage(replacement, discard_unsent=True)
    assert not client.status.has_unsent_changes
    assert client.update().submitted_events == 0


def test_managed_snapshot_waits_for_replay_without_losing_newer_edits(
    ready_managed_client, monkeypatch,
):
    client, sender = ready_managed_client
    starts = []
    client._started = False
    monkeypatch.setattr(client._receiver, "start", lambda: starts.append(True))
    client._receiver._synchronized_event.clear()
    value = UsdGeom.Sphere.Define(client.stage, "/Local").GetRadiusAttr()
    value.Set(1)

    assert client.publish_current_edit_target() == 0
    assert starts == [True]
    assert sender.batches == []
    assert client.status.has_unsent_changes
    with pytest.raises(RuntimeError, match="earlier publisher batch"):
        client.publish_current_edit_target()
    value.Set(2)
    client._receiver._synchronized_event.set()
    assert client.update().submitted_events > 0
    assert client.status.has_unsent_changes
    assert client.update().submitted_events > 0
    assert not client.status.has_unsent_changes
    values = [
        event["attrs"]["radius"]
        for batch in sender.batches for event in batch
        if event["k"] == "set_gprim_attrs" and "radius" in event["attrs"]
    ]
    assert values == [1, 2]


@pytest.mark.parametrize(("prepare", "park"), [(False, False), (True, True)])
def test_managed_rebind_requires_explicit_unsent_discard(
    ready_managed_client, prepare, park,
):
    client, _sender = ready_managed_client
    old_stage = client.stage
    old_stage.DefinePrim("/Local", "Xform")
    if prepare:
        client.emitter.prepare_events_for_send()
    replacement = None if park else Usd.Stage.CreateInMemory()
    with pytest.raises(RuntimeError, match="unsent changes"):
        client.rebind_stage(replacement)
    assert client.stage is old_stage
    assert client.status.has_unsent_changes
    client.rebind_stage(replacement, discard_unsent=True)
    assert client.stage is replacement
    assert not client.status.has_unsent_changes
    assert client.status.edit_target_is_published is not park


def test_managed_rebind_cannot_discard_submitted_work(ready_managed_client):
    client, _sender = ready_managed_client
    old_stage = client.stage
    old_stage.DefinePrim("/Local", "Xform")
    assert client.update().submitted_events > 0
    with pytest.raises(RuntimeError, match="submitted work pending"):
        client.rebind_stage(None, discard_unsent=True)
    assert client.stage is old_stage


def test_managed_parked_client_is_not_ready(ready_managed_client):
    client, _sender = ready_managed_client
    client.rebind_stage(None)
    assert client.status.connected
    assert not client.status.synchronized
    assert client.status.phase is ClientPhase.PARKED
    assert not client.status.edit_target_is_published
    with pytest.raises(RuntimeError, match="no bound stage"):
        client.wait_until_ready()
    with pytest.raises(RuntimeError, match="no bound stage"):
        client.submit_and_wait()


def test_managed_queued_callback_can_close_before_stage_work(monkeypatch):
    client = ManagedClient(
        Usd.Stage.CreateInMemory(), app_name="close-from-callback", persist_token=False,
        observer=RecordingObserver(on_call=lambda _name, _value: client.close()),
    )
    client._started = True
    monkeypatch.setattr(
        client.dispatcher, "drain_and_apply", lambda: pytest.fail("closed client applied work"),
    )
    try:
        client.receiver._on_playback_state(
            {"playing": False, "time": 0.0, "rate": 1.0, "leader_client_id": ""}
        )
        assert client.update() == SyncUpdate(applied_events=0, submitted_events=0)
        assert client.status.phase is ClientPhase.CLOSED
        with pytest.raises(RuntimeError, match="ManagedClient is closed"):
            client.update()
    finally:
        client.close()


@pytest.mark.parametrize("reconnects", [True, False])
def test_managed_use_server_preserves_and_clears_owned_authoring_layer(reconnects):
    stage = Usd.Stage.CreateInMemory()
    stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))
    client = ManagedClient(
        stage,
        app_name="managed-recovery",
        persist_token=False,
        reconnect=False,
    )
    authoring = client.authoring_layer
    assert authoring is not None
    stage.DefinePrim("/World/Local", "Xform")

    sender = _SenderStub([])
    failure = TransactionFailure(
        txn_id=1,
        code=TransactionRejectionCode.InvalidTransaction,
        reason="injected invalid operation",
    )
    artifact = RecoveryArtifact(
        producer_session_id="rejected-session",
        failure=failure,
        transactions=(
            QuarantinedTransaction(
                txn_id=1,
                payload=b"encoded-transaction",
                event_count=1,
            ),
        ),
    )
    sender.recovery_required = True
    sender.transaction_failure = failure
    sender.recovery_artifact = artifact
    sender.recovery_incident = make_recovery_incident(artifact)
    sender.connect_result = reconnects
    client._sender = sender
    client._replay_to_fresh_checkpoint = lambda timeout: None
    force_handshake(client, synchronized=True)
    try:
        assert client.recovery_artifact is artifact
        result = client.recover_use_server(session_id="replacement-session")

        assert result.recovery_artifact is artifact
        assert result.preserved_authoring_layer.GetPrimAtPath("/World/Local")
        assert not authoring.GetPrimAtPath("/World/Local")
        assert stage.GetEditTarget().GetLayer() is authoring
        assert sender.abandoned_session_ids == ["replacement-session"]
        assert sender.connected is reconnects
        assert sender.connect_timeouts
        assert client.status.recovery is None
        assert client.last_recovery_result is result
        client.dismiss_recovery_result()
        assert client.last_recovery_result is None
    finally:
        client.close()


def test_managed_client_rejects_an_edit_target_switch_before_publishing():
    stage = Usd.Stage.CreateInMemory()
    client = ManagedClient(
        stage,
        app_name="managed-edit-target",
        persist_token=False,
        reconnect=False,
    )
    sender = _SenderStub([])
    client._sender = sender
    force_handshake(client, synchronized=True)
    try:
        stage.SetEditTarget(Usd.EditTarget(stage.GetRootLayer()))
        stage.DefinePrim("/World/WrongLayer", "Xform")

        with pytest.raises(RuntimeError, match="client.authoring_layer"):
            client.update()
        assert sender.batches == []
    finally:
        client.close()


def test_managed_use_server_refuses_an_edit_target_switch():
    stage = Usd.Stage.CreateInMemory()
    client = ManagedClient(
        stage,
        app_name="managed-custom-recovery",
        persist_token=False,
        reconnect=False,
    )
    authoring = client.authoring_layer
    assert authoring is not None
    assert authoring is not stage.GetRootLayer()
    assert stage.GetEditTarget().GetLayer() is authoring
    stage.DefinePrim("/World/Local", "Xform")
    stage.SetEditTarget(Usd.EditTarget(stage.GetRootLayer()))
    sender = _SenderStub([])
    sender.recovery_required = True
    client._sender = sender
    client._replay_to_fresh_checkpoint = lambda timeout: None
    force_handshake(client, synchronized=True)
    try:
        with pytest.raises(RecoveryError, match="edit target changed") as error:
            client.recover_use_server()
        assert error.value.code == "edit_target_changed"
        assert stage.GetPrimAtPath("/World/Local")
    finally:
        client.close()


def test_managed_use_server_restores_local_layer_when_session_abandonment_fails():
    stage = Usd.Stage.CreateInMemory()
    stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))
    client = ManagedClient(
        stage,
        app_name="managed-recovery-rollback",
        persist_token=False,
        reconnect=False,
    )
    authoring = client.authoring_layer
    assert authoring is not None
    stage.DefinePrim("/World/Local", "Xform")
    sender = _SenderStub([])
    sender.recovery_required = True

    def _fail_abandonment(*, session_id=None):
        raise RuntimeError("injected abandonment failure")

    sender.abandon_rejected_session = _fail_abandonment
    client._sender = sender
    client._replay_to_fresh_checkpoint = lambda timeout: None
    force_handshake(client, synchronized=True)
    try:
        with pytest.raises(RuntimeError, match="injected abandonment failure"):
            client.recover_use_server()
        assert authoring.GetPrimAtPath("/World/Local")
        assert client.last_recovery_result is None
    finally:
        client.close()


def test_managed_use_server_retains_result_when_emitter_reattach_fails():
    stage = Usd.Stage.CreateInMemory()
    stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))
    client = ManagedClient(
        stage,
        app_name="managed-recovery-reattach",
        persist_token=False,
        reconnect=False,
    )
    stage.DefinePrim("/World/Local", "Xform")
    sender = _SenderStub([])
    failure = TransactionFailure(
        txn_id=1,
        code=TransactionRejectionCode.InvalidTransaction,
        reason="injected invalid operation",
    )
    artifact = RecoveryArtifact(
        producer_session_id="rejected-session",
        failure=failure,
        transactions=(QuarantinedTransaction(1, b"encoded", 1),),
    )
    sender.recovery_required = True
    sender.transaction_failure = failure
    sender.recovery_artifact = artifact
    sender.recovery_incident = make_recovery_incident(artifact)
    client._sender = sender
    client._started = True
    client._replay_to_fresh_checkpoint = lambda timeout: None

    def _fail_reattach(stage):
        raise RuntimeError("injected emitter reattach failure")

    client._emitter.rebind_stage = _fail_reattach
    try:
        with pytest.raises(RuntimeError, match="injected emitter reattach failure"):
            client.recover_use_server(session_id="replacement-session")
        result = client.last_recovery_result
        assert result is not None
        assert result.recovery_artifact is artifact
        assert result.preserved_authoring_layer.GetPrimAtPath("/World/Local")
    finally:
        client.close()


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


def test_publisher_retains_exact_batch_until_send_succeeds():
    stage = Usd.Stage.CreateInMemory()
    publisher = UsdPublisher(
        stage,
        app_name="test-publisher",
        persist_token=False,
    )
    sender = _SenderStub([False, True, True])
    publisher._sender = sender
    publisher.start()
    try:
        prim = stage.DefinePrim("/World/Thing", "Xform")
        value = prim.CreateAttribute(
            "userProperties:value",
            Sdf.ValueTypeNames.Int,
            custom=True,
        )
        value.Set(1)

        assert publisher.update().submitted_events == 0
        assert publisher.status.prepared_events > 0

        value.Set(2)
        assert publisher.update().submitted_events > 0
        assert sender.batches[1] is sender.batches[0]
        assert publisher.status.prepared_events == 0

        assert publisher.update().submitted_events > 0
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
    publisher.start()
    return publisher, sender, translate, clock


def test_publisher_coalesces_latest_default_time_transform_before_submission(monkeypatch):
    publisher, sender, translate, clock = _publisher_with_transform(
        monkeypatch,
        [True, True],
    )
    try:
        translate.Set((1, 0, 0))
        # Definition and op-order barriers publish immediately.
        assert publisher.update().submitted_events == 3

        translate.Set((2, 0, 0))
        assert publisher.update().submitted_events == 0
        translate.Set((3, 0, 0))
        assert publisher.update().submitted_events == 0
        assert len(sender.batches) == 1
        assert publisher.status.prepared_events == 1

        clock[0] = 0.11
        assert publisher.update().submitted_events == 1
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
        assert publisher.update().submitted_events == 3

        translate.Set((2, 0, 0))
        assert publisher.update().submitted_events == 0
        clock[0] = 0.11
        assert publisher.update().submitted_events == 0
        failed_batch = sender.batches[-1]

        sender.connected = False
        translate.Set((3, 0, 0))
        assert publisher.update().submitted_events == 0
        sender.connected = True
        assert publisher.update().submitted_events == 1
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
        assert publisher.update().submitted_events == 3
        translate.Set((2, 0, 0))
        assert publisher.update().submitted_events == 0

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
    force_handshake(client)
    monkeypatch.setattr(client, "_connect_sender", lambda: None)
    try:
        prim = stage.DefinePrim("/World/Thing", "Xform")
        value = prim.CreateAttribute("value", Sdf.ValueTypeNames.Int)
        value.Set(1)

        replaying = client.update()
        assert replaying.submitted_events == 0
        assert sender.batches == []
        assert not client.status.synchronized

        client._receiver._synchronized_event.set()
        first = client.update()
        assert first.submitted_events > 0
        assert client.status.synchronized
        assert sender.pending_event_count == first.submitted_events

        value.Set(2)
        second = client.update()
        assert second.submitted_events > 0
        assert len(sender.batches) == 2
        assert second.acknowledged_events_delta == 0
        assert second.pending_events > first.pending_events
    finally:
        client.close()


def test_managed_client_uses_the_same_pre_submission_transform_window(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(coalescing_module, "monotonic", lambda: clock[0])
    stage = Usd.Stage.CreateInMemory()
    client = ManagedClient(
        stage,
        app_name="managed-coalescing",
        persist_token=False,
        reconnect=False,
        transform_coalesce_seconds=0.1,
    )
    prim = UsdGeom.Xform.Define(stage, "/World/Thing").GetPrim()
    xformable = UsdGeom.Xformable(prim)
    translate = xformable.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble)
    xformable.AddOrientOp(UsdGeom.XformOp.PrecisionFloat)
    xformable.AddScaleOp(UsdGeom.XformOp.PrecisionDouble)
    sender = _SenderStub([True, True])
    client._sender = sender
    force_handshake(client, synchronized=True)
    monkeypatch.setattr(client, "_connect_sender", lambda: None)
    try:
        translate.Set((1, 0, 0))
        assert client.update().submitted_events > 0
        translate.Set((2, 0, 0))
        assert client.update().submitted_events == 0
        translate.Set((3, 0, 0))
        assert client.update().submitted_events == 0

        clock[0] = 0.11
        assert client.update().submitted_events == 1
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


def test_managed_client_playback_commands_reject_a_closed_client():
    client = ManagedClient(
        Usd.Stage.CreateInMemory(),
        app_name="closed-playback-client",
        persist_token=False,
    )
    client.close()

    with pytest.raises(RuntimeError, match="ManagedClient is closed"):
        client.claim_playback()
    with pytest.raises(RuntimeError, match="ManagedClient is closed"):
        client.send_playback_control("play")


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
    publisher.start()
    try:
        stage.DefinePrim("/World/Thing", "Xform")

        assert publisher.update().submitted_events == 0
        assert publisher.status.prepared_events == 0
        assert publisher.status.has_unsent_changes
        assert sender.connect_requests == 1

        sender.connected = True
        assert publisher.update().submitted_events > 0
    finally:
        publisher.close()


def test_publisher_repair_and_resume_reconnects_the_ordered_outbox():
    publisher = UsdPublisher(
        Usd.Stage.CreateInMemory(),
        app_name="repair-publisher",
        persist_token=False,
    )
    sender = _SenderStub([True])
    sender.transaction_failure = TransactionFailure(
        txn_id=1,
        code=TransactionRejectionCode.StaleLayerGraph,
        reason="injected stale graph",
    )
    publisher._sender = sender
    events = [{"k": "set_visibility", "prim": "/World", "visible": True}]
    try:
        assert publisher.repair_and_resume(events) == 7
        assert sender.repaired == [(events, "")]
        assert sender.connected
    finally:
        publisher.close()


def test_publisher_repair_reports_expected_recovery_errors():
    publisher = UsdPublisher(
        Usd.Stage.CreateInMemory(),
        app_name="repair-errors-publisher",
        persist_token=False,
    )
    sender = _SenderStub([])
    publisher._sender = sender
    events = [{"k": "delete_prim", "prim": "/World"}]
    try:
        with pytest.raises(RecoveryError) as no_incident:
            publisher.repair_and_resume(events)
        assert no_incident.value.code == "no_incident"

        sender.transaction_failure = TransactionFailure(
            txn_id=1,
            code=TransactionRejectionCode.InvalidTransaction,
            reason="injected invalid operation",
        )
        with pytest.raises(RecoveryError) as wrong_kind:
            publisher.repair_and_resume(events)
        assert wrong_kind.value.code == "wrong_recovery_kind"
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
    publisher.start()
    try:
        stage.DefinePrim("/World/Thing", "Xform")
        assert publisher.update().submitted_events == 0
        assert publisher.status.prepared_events > 0

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
        assert publisher.status.prepared_events > 0

        assert publisher.update().submitted_events > 0
        assert sender.batches[1] is sender.batches[0]
        assert publisher.status.prepared_events == 0
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


def test_managed_budget_releases_local_edits_under_sustained_traffic():
    client = ManagedClient(
        Usd.Stage.CreateInMemory(), app_name="managed-budget", persist_token=False,
    )
    client._sender = _SenderStub([True] * 10)
    force_handshake(client, synchronized=True)
    traffic = PeerTraffic(client.receiver, queued=3)
    try:
        client.stage.DefinePrim("/Local", "Xform")
        submitted = []
        for _ in range(2):
            submitted.append(client.update(max_messages=2).submitted_events)
            traffic.arrive(2)
        # Held behind the three queued messages, then released although the
        # peers keep every later drain at its budget.
        assert submitted[0] == 0 and submitted[1] > 0
        assert not client.status.has_unsent_changes
    finally:
        client.close()

