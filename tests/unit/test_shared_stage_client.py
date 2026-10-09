"""USD-native shared-stage client lifecycle and recovery."""

from __future__ import annotations

import pytest
from pxr import Ar, Sdf, Usd

from openusdconnect import ClientPhase, RecoveryError
from openusdconnect.codec import ReceivedEvent, TransactionRejectionCode, encode_message
from openusdconnect.protocol_constants import LayerMode
from openusdconnect.recovery import (
    QuarantinedTransaction,
    RecoveryArtifact,
    TransactionFailure,
    make_recovery_incident,
)
from openusdconnect.sdf_spec_delta import serialize_spec_fields
from openusdconnect.shared_stage_client import SharedStageClient
from tests.helpers import PeerTraffic, connect_client, embedded_server


def _create_root(path) -> Usd.Stage:
    root = Sdf.Layer.CreateNew(str(path))
    root.Save()
    return Usd.Stage.Open(root)


class _RecoverySender:
    def __init__(self, artifact: RecoveryArtifact):
        self.connected = False
        self.auth_rejected = False
        self.hello_rejected = False
        self.rejection_reason = ""
        self.token = None
        self.connect_timeouts: list[float | None] = []
        self.recovery_artifact = artifact
        self.transaction_failure = artifact.failure
        self.recovery_incident = make_recovery_incident(artifact)
        self.recovery_required = True
        self.pending_transaction_count = len(artifact.transactions)
        self.pending_event_count = artifact.event_count
        self.acknowledged_event_count = 0
        self.abandoned_session_ids: list[str | None] = []

    def abandon_rejected_session(self, *, session_id=None):
        self.abandoned_session_ids.append(session_id)
        artifact = self.recovery_artifact
        self.recovery_artifact = None
        self.transaction_failure = None
        self.recovery_incident = None
        self.recovery_required = False
        self.pending_transaction_count = 0
        return artifact

    def close(self, timeout=None):
        self.connected = False
        return True

    def connect(self, timeout=None):
        self.connect_timeouts.append(timeout)
        self.connected = True
        return True


class _ReceiverStub:
    """A receiver whose handshake and replay the test completes; it queues nothing."""

    stopped = False
    auth_rejected = False
    hello_rejected = False
    rejection_reason = ""
    reconnect = False
    generation = 1

    def __init__(self):
        self.connected = False
        self.synchronized = False

    def complete_replay(self):
        self.connected = self.synchronized = True

    def start(self):
        pass

    def close(self, timeout=None):
        return True

    def freeze_marker(self):
        return 0

    def drained_through(self, _marker):
        return True

    def drain_queue(self, max_messages=None):
        return []

    def mark_replay_applied(self):
        return self.synchronized

    def mark_applied_through(self, _generation, _sequence):
        return True

    def reset_applied_progress(self):
        pass

    def request_replay_from(self, _seq_start):
        pass


def _start_with_receiver(client, *, replayed=True):
    """Start *client* on a receiver stub, replayed unless a stubbed recovery replays it."""
    receiver = _ReceiverStub()
    if replayed:
        receiver.complete_replay()
    client._receiver = receiver
    client.start()
    return receiver


def _stale_artifact(layer_key: str) -> RecoveryArtifact:
    failure = TransactionFailure(
        txn_id=1,
        code=TransactionRejectionCode.StaleLayerGraph,
        reason="injected stale graph",
    )
    return RecoveryArtifact(
        producer_session_id="stale-session",
        failure=failure,
        transactions=(
            QuarantinedTransaction(
                txn_id=1,
                payload=b"encoded",
                event_count=1,
                layer_key=layer_key,
            ),
        ),
    )


def test_recovery_error_exposes_a_stable_code():
    error = RecoveryError("stale_assessment", "assessment changed")

    assert isinstance(error, RuntimeError)
    assert error.code == "stale_assessment"
    assert str(error) == "assessment changed"


def _bind_child_graph(client: SharedStageClient, child_key: str = "layer:child") -> Sdf.Layer:
    root = client.stage.GetRootLayer()
    child = Sdf.Layer.FindOrOpenRelativeToLayer(root, root.subLayerPaths[0])
    assert child is not None
    client._graph.apply_state(
        {
            "type": "layer_graph_state",
            "seq": 1,
            "generation": "graph-1",
            "revision": 1,
            "root_layer_key": "layer:root",
            "layers": [
                {
                    "layer_key": "layer:root",
                    "revision": 1,
                    "sublayers": [
                        {
                            "authored_path": root.subLayerPaths[0],
                            "offset": 0.0,
                            "scale": 1.0,
                            "layer_key": child_key,
                        }
                    ],
                },
                {"layer_key": child_key, "revision": 1, "sublayers": []},
            ],
        }
    )
    return child


def test_constructor_requires_a_stage_and_application_name():
    with pytest.raises(TypeError, match="Usd.Stage"):
        SharedStageClient(None, app_name="test", persist_token=False)
    with pytest.raises(ValueError, match="app_name"):
        SharedStageClient(Usd.Stage.CreateInMemory(), app_name=" ", persist_token=False)
    with pytest.raises(ValueError, match="portable root layer"):
        SharedStageClient(Usd.Stage.CreateInMemory(), app_name="test", persist_token=False)


def test_constructor_reports_the_layer_with_nonportable_nested_topology(tmp_path):
    leaf = Sdf.Layer.CreateNew(str(tmp_path / "leaf.usda"))
    leaf.Save()
    child = Sdf.Layer.CreateNew(str(tmp_path / "child.usda"))
    child.subLayerPaths.append(leaf.identifier)
    child.Save()
    stage = _create_root(tmp_path / "root.usda")
    stage.GetRootLayer().subLayerPaths.append("./child.usda")

    with pytest.raises(ValueError, match=r"child\.usda.*portable asset identifiers"):
        SharedStageClient(stage, app_name="invalid-topology", persist_token=False)


def test_constructor_accepts_search_paths_from_the_stage_resolver_context(tmp_path):
    root_dir = tmp_path / "root"
    search_dir = tmp_path / "search"
    root_dir.mkdir()
    search_dir.mkdir()
    child = Sdf.Layer.CreateNew(str(search_dir / "content.usda"))
    child.Save()
    root = Sdf.Layer.CreateNew(str(root_dir / "scene.usda"))
    root.subLayerPaths.append("content.usda")
    root.Save()
    context = Ar.DefaultResolverContext([str(search_dir)])
    stage = Usd.Stage.Open(root.identifier, context)

    client = SharedStageClient(stage, app_name="resolver-context", persist_token=False)
    try:
        assert client.stage.GetPathResolverContext() == stage.GetPathResolverContext()
    finally:
        client.close()


def test_constructor_rejects_an_initial_session_layer_edit_target(tmp_path):
    stage = _create_root(tmp_path / "root.usda")
    stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))

    with pytest.raises(ValueError, match="edit target.*outside the root/sublayer graph"):
        SharedStageClient(stage, app_name="session-target", persist_token=False)


def test_status_exposes_shared_stage_partial_connection(tmp_path):
    client = SharedStageClient(
        _create_root(tmp_path / "root.usda"),
        app_name="status-client",
        persist_token=False,
    )
    original_sender = client._sender

    class _StatusSender:
        connected = False
        transaction_failure = None
        rejection_reason = ""
        auth_rejected = False
        hello_rejected = False
        pending_event_count = 2
        acknowledged_event_count = 3
        recovery_required = False
        recovery_incident = None
        recovery_artifact = None

    sender = _StatusSender()
    client._sender = sender
    _start_with_receiver(client)
    try:
        assert client.status.phase is ClientPhase.CONNECTING
        assert client.status.receiver_connected is True
        assert client.status.sender_connected is False
        assert client.status.pending_events == 2

        sender.connected = True
        assert client.status.phase is ClientPhase.CONNECTING
        client._graph.apply_state(
            {
                "type": "layer_graph_state",
                "seq": 1,
                "generation": "graph-1",
                "revision": 1,
                "root_layer_key": "layer:root",
                "layers": [
                    {"layer_key": "layer:root", "revision": 1, "sublayers": []}
                ],
            }
        )
        assert client.status.phase is ClientPhase.READY

        artifact = _stale_artifact("layer:root")
        sender.transaction_failure = artifact.failure
        sender.recovery_incident = make_recovery_incident(artifact)
        sender.recovery_required = True
        status = client.status
        assert status.phase is ClientPhase.RECOVERY_REQUIRED
        assert status.failure is artifact.failure
        assert status.recovery.failure is artifact.failure
        assert status.reason == str(artifact.failure)
    finally:
        client._sender = original_sender
        client.close()


def test_status_distinguishes_local_edit_targets_and_unsubmitted_changes(tmp_path):
    stage = _create_root(tmp_path / "root.usda")
    client = SharedStageClient(stage, app_name="authoring-scope", persist_token=False)
    try:
        assert client.status.edit_target_is_published
        stage.SetEditTarget(stage.GetSessionLayer())
        stage.DefinePrim("/Local", "Xform")
        assert not client.status.edit_target_is_published
        assert not client.status.has_unsent_changes

        stage.SetEditTarget(stage.GetRootLayer())
        stage.DefinePrim("/Shared", "Xform")
        assert client.status.edit_target_is_published
        assert client.status.has_unsent_changes
        assert client.status.prepared_events == 0
        assert client.status.pending_events == 0

        client._started = True
        result = client.update()
        assert client.status.has_unsent_changes
        assert result.submitted_events == 0
        assert client.status.prepared_events > 0
        client.close()
        assert not client.status.has_unsent_changes
    finally:
        client.close()


def test_unresolved_layer_events_apply_after_dependency_refresh(tmp_path):
    stage = _create_root(tmp_path / "root.usda")
    root = stage.GetRootLayer()
    root.subLayerPaths.append("./late.usda")
    client = SharedStageClient(stage, app_name="late-client", persist_token=False)
    try:
        root_key = "layer:root"
        child_key = "layer:late"
        client._graph.apply_state(
            {
                "type": "layer_graph_state",
                "seq": 1,
                "generation": "graph-1",
                "revision": 1,
                "root_layer_key": root_key,
                "layers": [
                    {
                        "layer_key": root_key,
                        "revision": 1,
                        "sublayers": [
                            {
                                "authored_path": "./late.usda",
                                "offset": 0.0,
                                "scale": 1.0,
                                "layer_key": child_key,
                            }
                        ],
                    },
                    {"layer_key": child_key, "revision": 1, "sublayers": []},
                ],
            }
        )
        source = Sdf.Layer.CreateAnonymous()
        prim = Sdf.CreatePrimInLayer(source, "/Late")
        attr = Sdf.AttributeSpec(prim, "value", Sdf.ValueTypeNames.Int)
        attr.default = 8
        event = {
            "k": "set_sdf_spec_fields",
            "prim": "/Late",
            "spec_path": "/Late.value",
            "spec_kind": "attribute",
            "fields": ["custom", "default", "typeName", "variability"],
            "fragment": serialize_spec_fields(
                source,
                "/Late.value",
                "attribute",
                attr.ListInfoKeys(),
                stabilize_asset_paths=False,
            ),
            "removed": False,
        }
        assert not client._apply_record(ReceivedEvent(seq=2, event=event, layer_key=child_key))
        assert client.status.deferred_events == 1
        _start_with_receiver(client)
        assert client.status.synchronized
        assert client.status.deferred_events == 1
        assert client.status.deferred_layer_keys == (child_key,)

        late = Sdf.Layer.CreateNew(str(tmp_path / "late.usda"))
        Sdf.CreatePrimInLayer(late, "/Late")
        late.Save()
        mapped = client.refresh_layer_graph()

        assert mapped == (child_key,)
        assert client.status.deferred_events == 0
        assert client.status.deferred_layer_keys == ()
        assert late.GetAttributeAtPath("/Late.value").default == 8
    finally:
        client.close()


@pytest.mark.parametrize("deferred", [False, True])
def test_content_apply_failure_preserves_layer_and_tracker_until_retry(
    tmp_path, monkeypatch, deferred,
):
    import openusdconnect.shared_stage_client as client_module

    stage = _create_root(tmp_path / "root.usda")
    root = stage.GetRootLayer()
    Sdf.CreatePrimInLayer(root, "/Original")
    client = SharedStageClient(stage, app_name="apply-retry", persist_token=False)
    try:
        client._graph.apply_state({
            "type": "layer_graph_state", "seq": 1, "generation": "graph-1",
            "revision": 1, "root_layer_key": "layer:root",
            "layers": [{"layer_key": "layer:root", "revision": 1, "sublayers": []}],
        })
        source = Sdf.Layer.CreateAnonymous()
        records = []
        for index in range(2 if deferred else 1):
            Sdf.CreatePrimInLayer(source, f"/Replacement{index}")
            records.append(ReceivedEvent(
                seq=index + 2, layer_key="layer:root",
                event={"k": "replace_sdf_layer_content", "fragment": source.ExportToString()},
            ))
        if deferred:
            client._set_deferred(records)
        before = root.ExportToString()
        stage.SetEditTarget(stage.GetSessionLayer())
        original_apply = client_module.apply_events
        applied_batches = []
        accepted = []
        original_accept = client._tracker.accept_authoritative_event

        def record_accept(layer, event):
            accepted.append(event)
            original_accept(layer, event)

        def apply_then_fail(target_stage, events):
            applied_batches.append(list(events))
            original_apply(target_stage, events)
            raise RuntimeError("content apply failed")

        monkeypatch.setattr(client._tracker, "accept_authoritative_event", record_accept)
        monkeypatch.setattr(client_module, "apply_events", apply_then_fail)

        def apply_records():
            with client._tracker.suppressed():
                return client._apply_pending() if deferred else client._apply_record(records[0])

        with pytest.raises(RuntimeError, match="content apply failed"):
            apply_records()
        assert root.ExportToString() == before
        assert stage.GetEditTarget().GetLayer() == stage.GetSessionLayer()
        assert accepted == []
        assert applied_batches == [[record.event for record in records]]
        assert client.status.deferred_events == (len(records) if deferred else 0)

        monkeypatch.setattr(client_module, "apply_events", original_apply)
        assert apply_records() == len(records)
        assert accepted == [record.event for record in records]
        assert root.ExportToString() == source.ExportToString()
        assert client.status.deferred_events == 0
    finally:
        client.close()


def test_refresh_layer_graph_rejects_a_closed_client(tmp_path):
    client = SharedStageClient(
        _create_root(tmp_path / "root.usda"),
        app_name="closed-refresh-client",
        persist_token=False,
    )
    client.close()

    with pytest.raises(RuntimeError, match="SharedStageClient is closed"):
        client.refresh_layer_graph()


def test_shared_record_requires_a_layer_key(tmp_path):
    client = SharedStageClient(
        _create_root(tmp_path / "root.usda"),
        app_name="missing-key",
        persist_token=False,
    )
    try:
        record = ReceivedEvent(
            seq=1,
            event={"k": "set_sdf_spec_fields"},
            layer_key=None,
        )
        with pytest.raises(ValueError, match="missing layer_key"):
            client._apply_record(record)
    finally:
        client.close()


def test_update_restores_frozen_edits_when_replay_fails(tmp_path, monkeypatch):
    client = SharedStageClient(
        _create_root(tmp_path / "root.usda"),
        app_name="failed-replay",
        persist_token=False,
    )
    calls = []
    client._started = True
    monkeypatch.setattr(
        client._tracker,
        "prepare_local_changes",
        lambda: calls.append("prepare"),
    )
    monkeypatch.setattr(
        client._tracker,
        "restore_prepared",
        lambda: calls.append("restore"),
    )

    def _fail_replay(max_messages=None):
        calls.append("replay")
        raise RuntimeError("bad authoritative record")

    monkeypatch.setattr(client, "_apply_incoming", _fail_replay)
    try:
        with pytest.raises(RuntimeError, match="bad authoritative record"):
            client.update()
        assert calls == ["prepare", "replay", "restore"]
    finally:
        client.close()


def test_repair_and_resume_targets_current_mapped_layer(tmp_path, monkeypatch):
    stage = _create_root(tmp_path / "root.usda")
    client = SharedStageClient(stage, app_name="repair-client", persist_token=False)
    original_sender = client._sender
    repaired = []

    class _RepairSender(_RecoverySender):
        def repair_rejected_transaction(self, events, *, layer_key=""):
            if not events:
                raise ValueError("repair events must not be empty")
            repaired.append((events, layer_key))
            self.recovery_artifact = self.transaction_failure = None
            return 7

    try:
        client._graph.apply_state(
            {
                "type": "layer_graph_state",
                "seq": 1,
                "generation": "graph-1",
                "revision": 1,
                "root_layer_key": "layer:root",
                "layers": [
                    {"layer_key": "layer:root", "revision": 1, "sublayers": []}
                ],
            }
        )
        client._sender = _RepairSender(_stale_artifact("layer:root"))
        client._started = True
        client._recovery_rebind_artifact = client._sender.recovery_artifact
        resumed = []

        def _resume():
            resumed.append(True)
            return True

        monkeypatch.setattr(client, "_connect_sender", _resume)
        events = [{"k": "replace_sdf_layer_content", "fragment": "#usda 1.0\n"}]

        detached = Sdf.Layer.CreateAnonymous()
        with pytest.raises(RecoveryError, match="not mapped") as error:
            client.repair_and_resume(events, layer=detached)
        assert error.value.code == "invalid_repair_target"
        with pytest.raises(ValueError, match="must not be empty"):
            client.repair_and_resume([], layer=stage.GetRootLayer())
        assert client.status.recovery_stage_pending, "a failed repair keeps the incident"

        assert client.repair_and_resume(events, layer=stage.GetRootLayer()) == 7
        assert repaired == [(events, "layer:root")]
        assert resumed == [True]
        assert not client.status.recovery_stage_pending
    finally:
        client._sender = original_sender
        client.close()


def test_shared_use_server_abandons_only_after_rejected_layer_detaches(
    tmp_path,
    monkeypatch,
):
    child = Sdf.Layer.CreateNew(str(tmp_path / "child.usda"))
    Sdf.CreatePrimInLayer(child, "/Local")
    child.Save()
    stage = _create_root(tmp_path / "root.usda")
    stage.GetRootLayer().subLayerPaths.append("./child.usda")
    client = SharedStageClient(stage, app_name="shared-recovery", persist_token=False)
    original_sender = client._sender
    child = _bind_child_graph(client)
    Sdf.CreatePrimInLayer(child, "/Local/Rejected")
    sender = _RecoverySender(_stale_artifact("layer:child"))
    client._sender = sender
    receiver = _start_with_receiver(client, replayed=False)

    def _detach(_timeout):
        client._graph.apply_sublayers(
            "layer:root",
            {
                "k": "set_sublayers",
                "prim": "/",
                "generation": "graph-1",
                "revision": 2,
                "sublayers": [],
            },
        )
        client._tracker.sync_graph(force=True)
        client._last_seq = 2
        receiver.complete_replay()

    monkeypatch.setattr(client, "_replay_to_fresh_checkpoint", _detach)
    try:
        assessment = client.refresh_recovery_assessment()
        assert assessment.all_layers_detached
        result = client.complete_recovery(
            assessment,
            session_id="replacement-session",
        )

        assert result.recovery_artifact.producer_session_id == "stale-session"
        assert result.checkpoint_seq == 2
        assert len(result.rejected_snapshots) == 1
        preserved = result.rejected_snapshots[0]
        assert result.layers[0].rejected_layer_key == "layer:child"
        assert result.layers[0].source_layer is child
        assert preserved.GetPrimAtPath("/Local/Rejected")
        assert sender.abandoned_session_ids == ["replacement-session"]
        assert not client.is_layer_reachable(child)
    finally:
        client._sender = original_sender
        client.close()


def test_shared_use_server_refuses_a_quarantined_reachable_layer(tmp_path, monkeypatch):
    child = Sdf.Layer.CreateNew(str(tmp_path / "child.usda"))
    child.Save()
    stage = _create_root(tmp_path / "root.usda")
    stage.GetRootLayer().subLayerPaths.append("./child.usda")
    client = SharedStageClient(stage, app_name="shared-unsafe-recovery", persist_token=False)
    original_sender = client._sender
    _bind_child_graph(client)
    sender = _RecoverySender(_stale_artifact("layer:child"))
    client._sender = sender
    client._started = True
    monkeypatch.setattr(client, "_replay_to_fresh_checkpoint", lambda _timeout: None)
    try:
        assessment = client.refresh_recovery_assessment()
        assert assessment.recovery_artifact is sender.recovery_artifact
        assert assessment.unchanged_mapping_layers == assessment.layers
        assert assessment.detached_layers == ()
        assert assessment.remapped_layers == ()
        assert assessment.source_unavailable_layers == ()
        assert not assessment.all_layers_detached
        assert client.recovery_artifact is sender.recovery_artifact
        assert sender.abandoned_session_ids == []
        assert sender.recovery_required
    finally:
        client._sender = original_sender
        client.close()


def test_shared_assessment_reports_an_unavailable_source_layer(tmp_path, monkeypatch):
    stage = _create_root(tmp_path / "root.usda")
    client = SharedStageClient(stage, app_name="shared-unresolved-recovery", persist_token=False)
    original_sender = client._sender
    client._graph.apply_state(
        {
            "type": "layer_graph_state",
            "seq": 1,
            "generation": "graph-1",
            "revision": 1,
            "root_layer_key": "layer:root",
            "layers": [
                {"layer_key": "layer:root", "revision": 1, "sublayers": []}
            ],
        }
    )
    sender = _RecoverySender(_stale_artifact("layer:missing"))
    client._sender = sender
    client._started = True
    monkeypatch.setattr(client, "_replay_to_fresh_checkpoint", lambda _timeout: None)
    try:
        assessment = client.refresh_recovery_assessment()
        assert assessment.source_unavailable_layers == assessment.layers
        assert assessment.layers[0].source_unavailable
        assert assessment.layers[0].source_layer is None
        assert assessment.layers[0].rejected_snapshot is None
        assert not assessment.all_layers_detached
        assert sender.recovery_required
    finally:
        client._sender = original_sender
        client.close()


def test_shared_recovery_commands_distinguish_expected_policy_failures(
    tmp_path,
    monkeypatch,
):
    stage = _create_root(tmp_path / "root.usda")
    client = SharedStageClient(stage, app_name="shared-recovery-errors", persist_token=False)
    original_sender = client._sender
    client._started = True
    try:
        with pytest.raises(RecoveryError) as no_incident:
            client.refresh_recovery_assessment()
        assert no_incident.value.code == "no_incident"

        invalid = _stale_artifact("layer:root")
        invalid = RecoveryArtifact(
            producer_session_id=invalid.producer_session_id,
            failure=TransactionFailure(
                txn_id=1,
                code=TransactionRejectionCode.InvalidTransaction,
                reason="injected invalid operation",
            ),
            transactions=invalid.transactions,
        )
        client._sender = _RecoverySender(invalid)
        with pytest.raises(RecoveryError) as wrong_kind:
            client.refresh_recovery_assessment()
        assert wrong_kind.value.code == "wrong_recovery_kind"

        client._sender = _RecoverySender(_stale_artifact("layer:root"))
        with pytest.raises(TypeError, match="assessment"):
            client.complete_recovery()

        client._graph.apply_state(
            {
                "type": "layer_graph_state",
                "seq": 1,
                "generation": "graph-1",
                "revision": 1,
                "root_layer_key": "layer:root",
                "layers": [
                    {"layer_key": "layer:root", "revision": 1, "sublayers": []}
                ],
            }
        )
        monkeypatch.setattr(client, "_replay_to_fresh_checkpoint", lambda _timeout: None)
        assessment = client.refresh_recovery_assessment()
        with pytest.raises(RecoveryError) as not_synchronized:
            client.complete_recovery(assessment)
        assert not_synchronized.value.code == "stage_not_synchronized"
    finally:
        client._sender = original_sender
        client.close()


def test_shared_use_server_keeps_incident_when_checkpoint_refresh_fails(
    tmp_path,
    monkeypatch,
):
    child = Sdf.Layer.CreateNew(str(tmp_path / "child.usda"))
    child.Save()
    stage = _create_root(tmp_path / "root.usda")
    stage.GetRootLayer().subLayerPaths.append("./child.usda")
    client = SharedStageClient(stage, app_name="shared-timeout-recovery", persist_token=False)
    original_sender = client._sender
    _bind_child_graph(client)
    sender = _RecoverySender(_stale_artifact("layer:child"))
    client._sender = sender
    client._started = True

    def _timeout(_timeout):
        raise TimeoutError("injected checkpoint timeout")

    monkeypatch.setattr(client, "_replay_to_fresh_checkpoint", _timeout)
    try:
        with pytest.raises(TimeoutError, match="injected checkpoint timeout"):
            client.refresh_recovery_assessment()
        assert sender.abandoned_session_ids == []
        assert sender.recovery_required
    finally:
        client._sender = original_sender
        client.close()


def test_shared_use_server_keeps_session_when_a_suffix_layer_is_still_live(
    tmp_path,
    monkeypatch,
):
    child = Sdf.Layer.CreateNew(str(tmp_path / "child.usda"))
    child.Save()
    stage = _create_root(tmp_path / "root.usda")
    stage.GetRootLayer().subLayerPaths.append("./child.usda")
    client = SharedStageClient(stage, app_name="shared-suffix-recovery", persist_token=False)
    original_sender = client._sender
    _bind_child_graph(client)
    artifact = _stale_artifact("layer:child")
    artifact = RecoveryArtifact(
        producer_session_id=artifact.producer_session_id,
        failure=artifact.failure,
        transactions=(
            artifact.transactions[0],
            QuarantinedTransaction(2, b"suffix", 1, "layer:root"),
        ),
    )
    sender = _RecoverySender(artifact)
    client._sender = sender
    client._started = True

    def _detach_child(_timeout):
        client._graph.apply_sublayers(
            "layer:root",
            {
                "k": "set_sublayers",
                "prim": "/",
                "generation": "graph-1",
                "revision": 2,
                "sublayers": [],
            },
        )

    monkeypatch.setattr(client, "_replay_to_fresh_checkpoint", _detach_child)
    try:
        assessment = client.refresh_recovery_assessment()
        assert [layer.rejected_layer_key for layer in assessment.detached_layers] == [
            "layer:child"
        ]
        assert [
            layer.rejected_layer_key
            for layer in assessment.unchanged_mapping_layers
        ] == [
            "layer:root"
        ]
        assert sender.abandoned_session_ids == []
        assert sender.recovery_required
    finally:
        client._sender = original_sender
        client.close()


def test_shared_use_server_refuses_automatic_layer_key_redirection(
    tmp_path,
    monkeypatch,
):
    child = Sdf.Layer.CreateNew(str(tmp_path / "child.usda"))
    child.Save()
    stage = _create_root(tmp_path / "root.usda")
    root = stage.GetRootLayer()
    root.subLayerPaths.append("./child.usda")
    client = SharedStageClient(stage, app_name="shared-remap-recovery", persist_token=False)
    original_sender = client._sender
    _bind_child_graph(client)
    sender = _RecoverySender(_stale_artifact("layer:child"))
    client._sender = sender
    client._started = True

    def _remap(_timeout):
        client._graph.apply_state(
            {
                "type": "layer_graph_state",
                "seq": 1,
                "generation": "graph-2",
                "revision": 1,
                "root_layer_key": "layer:new-root",
                "layers": [
                    {
                        "layer_key": "layer:new-root",
                        "revision": 1,
                        "sublayers": [
                            {
                                "authored_path": "./child.usda",
                                "offset": 0.0,
                                "scale": 1.0,
                                "layer_key": "layer:new-child",
                            }
                        ],
                    },
                    {
                        "layer_key": "layer:new-child",
                        "revision": 1,
                        "sublayers": [],
                    },
                ],
            }
        )

    monkeypatch.setattr(client, "_replay_to_fresh_checkpoint", _remap)
    try:
        assessment = client.refresh_recovery_assessment()
        remapped = assessment.remapped_layers
        assert len(remapped) == 1
        assert remapped[0].current_layer_key == "layer:new-child"
        assert sender.abandoned_session_ids == []
        assert sender.recovery_required
    finally:
        client._sender = original_sender
        client.close()


def test_shared_external_recovery_completes_a_structured_reachable_assessment(
    tmp_path,
    monkeypatch,
):
    child = Sdf.Layer.CreateNew(str(tmp_path / "child.usda"))
    child.Save()
    stage = _create_root(tmp_path / "root.usda")
    stage.GetRootLayer().subLayerPaths.append("./child.usda")
    client = SharedStageClient(stage, app_name="shared-external-recovery", persist_token=False)
    original_sender = client._sender
    _bind_child_graph(client)
    sender = _RecoverySender(_stale_artifact("layer:child"))
    client._sender = sender
    _start_with_receiver(client)
    monkeypatch.setattr(client, "_replay_to_fresh_checkpoint", lambda _timeout: None)
    try:
        assessment = client.refresh_recovery_assessment()
        assert not assessment.all_layers_detached

        result = client.complete_recovery(
            assessment,
            session_id="external-replacement",
        )

        assert result is assessment
        assert result.recovery_artifact is assessment.recovery_artifact
        assert sender.abandoned_session_ids == ["external-replacement"]
        assert not sender.recovery_required
        assert client._last_recovery_assessment is None
    finally:
        client._sender = original_sender
        client.close()


def test_shared_external_recovery_rejects_an_assessment_from_another_incident(
    tmp_path,
    monkeypatch,
):
    child = Sdf.Layer.CreateNew(str(tmp_path / "child.usda"))
    child.Save()
    stage = _create_root(tmp_path / "root.usda")
    stage.GetRootLayer().subLayerPaths.append("./child.usda")
    client = SharedStageClient(stage, app_name="shared-stale-assessment", persist_token=False)
    original_sender = client._sender
    _bind_child_graph(client)
    sender = _RecoverySender(_stale_artifact("layer:child"))
    client._sender = sender
    _start_with_receiver(client)
    monkeypatch.setattr(client, "_replay_to_fresh_checkpoint", lambda _timeout: None)
    try:
        assessment = client.refresh_recovery_assessment()
        sender.recovery_artifact = _stale_artifact("layer:child")
        sender.transaction_failure = sender.recovery_artifact.failure

        with pytest.raises(RecoveryError, match="does not match") as error:
            client.complete_recovery(assessment)
        assert error.value.code == "stale_assessment"
        assert sender.abandoned_session_ids == []
    finally:
        client._sender = original_sender
        client.close()


def test_shared_external_recovery_rejects_a_stale_graph_assessment(
    tmp_path,
    monkeypatch,
):
    child = Sdf.Layer.CreateNew(str(tmp_path / "child.usda"))
    child.Save()
    stage = _create_root(tmp_path / "root.usda")
    stage.GetRootLayer().subLayerPaths.append("./child.usda")
    client = SharedStageClient(stage, app_name="shared-stale-graph", persist_token=False)
    original_sender = client._sender
    _bind_child_graph(client)
    sender = _RecoverySender(_stale_artifact("layer:child"))
    client._sender = sender
    _start_with_receiver(client)
    monkeypatch.setattr(client, "_replay_to_fresh_checkpoint", lambda _timeout: None)
    try:
        assessment = client.refresh_recovery_assessment()
        client._last_seq += 1

        with pytest.raises(RecoveryError, match="assessment is stale") as error:
            client.complete_recovery(assessment)
        assert error.value.code == "stale_assessment"
        assert sender.abandoned_session_ids == []
        assert sender.recovery_required
    finally:
        client._sender = original_sender
        client.close()


def test_shared_rebind_recovery_preserves_work_and_replays_clean_stage(
    tmp_path,
    monkeypatch,
):
    old_child = Sdf.Layer.CreateNew(str(tmp_path / "old-child.usda"))
    Sdf.CreatePrimInLayer(old_child, "/Rejected")
    old_child.Save()
    old_stage = _create_root(tmp_path / "old-root.usda")
    old_stage.GetRootLayer().subLayerPaths.append("./old-child.usda")
    client = SharedStageClient(old_stage, app_name="shared-rebind-recovery", persist_token=False)
    original_sender = client._sender
    with client._tracker.suppressed():
        _bind_child_graph(client)
    sender = _RecoverySender(_stale_artifact("layer:child"))
    client._sender = sender
    receiver = _start_with_receiver(client, replayed=False)

    fresh_child = Sdf.Layer.CreateNew(str(tmp_path / "fresh-child.usda"))
    fresh_child.Save()
    fresh_stage = _create_root(tmp_path / "fresh-root.usda")
    fresh_stage.GetRootLayer().subLayerPaths.append("./fresh-child.usda")
    calls = []

    def _refresh(_timeout):
        calls.append(client.stage)
        if client.stage is fresh_stage:
            with client._tracker.suppressed():
                _bind_child_graph(client)
            client._last_seq = 4
        receiver.complete_replay()

    monkeypatch.setattr(client, "_replay_to_fresh_checkpoint", _refresh)
    try:
        with pytest.raises(RecoveryError, match="different clean stage") as error:
            client.recover_use_server(clean_stage=old_stage)
        assert error.value.code == "invalid_clean_stage"
        assert calls == []
        shared_stage = Usd.Stage.Open(old_stage.GetRootLayer())
        with pytest.raises(RecoveryError, match="shares loaded layers") as error:
            client.recover_use_server(clean_stage=shared_stage)
        assert error.value.code == "shared_loaded_layers"
        assert calls == []

        result = client.recover_use_server(
            clean_stage=fresh_stage,
            session_id="rebind-replacement",
        )

        assert calls == [old_stage, fresh_stage]
        assert client.stage is fresh_stage
        assert client._graph.ready
        assert result.checkpoint_seq == 4
        assert result.rejected_snapshots[0].GetPrimAtPath("/Rejected")
        assert result.layers[0].source_layer is old_child
        assert sender.abandoned_session_ids == ["rebind-replacement"]
        assert not sender.recovery_required
        assert sender.connected
        assert sender.connect_timeouts
    finally:
        client._sender = original_sender
        client.close()


@pytest.mark.parametrize("after_timeout", ["resume", "update", "local_edits", "different_incident"])
def test_shared_rebind_recovery_resumes_after_replacement_replay_timeout(
    tmp_path,
    monkeypatch,
    after_timeout,
):
    old_stage = _create_root(tmp_path / "old-root.usda")
    old_child = Sdf.Layer.CreateNew(str(tmp_path / "old-child.usda"))
    Sdf.CreatePrimInLayer(old_child, "/Rejected")
    old_child.Save()
    old_stage.GetRootLayer().subLayerPaths.append("./old-child.usda")
    client = SharedStageClient(old_stage, app_name="resume-recovery", persist_token=False)
    with client._tracker.suppressed():
        _bind_child_graph(client)
    original_sender = client._sender
    sender = _RecoverySender(_stale_artifact("layer:child"))
    client._sender = sender
    receiver = _start_with_receiver(client, replayed=False)

    fresh_stage = _create_root(tmp_path / "fresh-root.usda")
    fresh_child = Sdf.Layer.CreateNew(str(tmp_path / "fresh-child.usda"))
    fresh_child.Save()
    fresh_stage.GetRootLayer().subLayerPaths.append("./fresh-child.usda")
    checkpoints = []

    def refresh(_timeout):
        checkpoints.append(client.stage)
        receiver.complete_replay()
        if len(checkpoints) == 2:
            with client._tracker.suppressed():
                _bind_child_graph(client)
                client.stage.DefinePrim("/Replayed", "Xform")
            client._last_seq = 3
            raise TimeoutError("replacement replay timed out")
        if len(checkpoints) == 3:
            assert client.last_seq == (4 if after_timeout == "update" else 3)
            assert client.stage.GetPrimAtPath("/Replayed")
            client._last_seq += 1

    monkeypatch.setattr(client, "_replay_to_fresh_checkpoint", refresh)
    try:
        with pytest.raises(TimeoutError, match="replacement replay timed out"):
            client.recover_use_server(clean_stage=fresh_stage)

        assert client.stage is fresh_stage
        assert client.status.recovery_stage_pending
        assert client.status.phase is ClientPhase.RECOVERY_REQUIRED
        assert sender.abandoned_session_ids == []

        if after_timeout == "update":
            source = Sdf.Layer.CreateAnonymous()
            Sdf.CreatePrimInLayer(source, "/BetweenAttempts")
            buffers = [encode_message({
                "type": "event", "seq": 4, "layer_key": "layer:child",
                "event": {
                    "k": "replace_sdf_layer_content", "prim": "/",
                    "fragment": source.ExportToString(),
                },
            })]
            monkeypatch.setattr(client._receiver, "drain_queue", lambda max_messages=None: buffers)
            monkeypatch.setattr(sender, "drain_acknowledged_event_count", lambda: 0, raising=False)
            monkeypatch.setattr(
                sender, "send_events",
                lambda *_args, **_kwargs: pytest.fail("pending recovery must not publish"),
                raising=False,
            )
            sender.connected = True
            assert client.update().applied_events == 1
            assert fresh_stage.GetPrimAtPath("/BetweenAttempts")
            assert client.last_seq == 4
            assert client.status.recovery_stage_pending
            assert client.status.phase is ClientPhase.RECOVERY_REQUIRED

        if after_timeout == "local_edits":
            fresh_stage.DefinePrim("/Unsubmitted", "Xform")
            with pytest.raises(RecoveryError) as error:
                client.resume_recovery()
            assert error.value.code == "local_changes_pending"
            assert checkpoints == [old_stage, fresh_stage]
            assert sender.recovery_required
        elif after_timeout == "different_incident":
            sender.recovery_artifact = _stale_artifact("layer:root")
            assert not client.status.recovery_stage_pending
            with pytest.raises(RecoveryError) as error:
                client.resume_recovery()
            assert error.value.code == "no_pending_recovery_stage"
            with pytest.raises(RecoveryError) as error:
                client.recover_use_server(clean_stage=fresh_stage)
            assert error.value.code == "invalid_clean_stage"
            assert checkpoints == [old_stage, fresh_stage]
        else:
            with pytest.raises(RecoveryError, match=r"resume_recovery\(\)") as error:
                client.recover_use_server(clean_stage=fresh_stage)
            assert error.value.code == "invalid_clean_stage"
            result = client.resume_recovery()
            assert checkpoints == [old_stage, fresh_stage, fresh_stage]
            assert result.layers[0].source_layer is old_child
            assert result.rejected_snapshots[0].GetPrimAtPath("/Rejected")
            assert result.checkpoint_seq == (5 if after_timeout == "update" else 4)
            assert client.stage is fresh_stage
            assert not client.status.recovery_stage_pending
            assert not sender.recovery_required
    finally:
        client._sender = original_sender
        client.close()


def test_shared_rebind_recovery_preflights_the_clean_stage(tmp_path, monkeypatch):
    old_stage = _create_root(tmp_path / "old-root.usda")
    client = SharedStageClient(old_stage, app_name="shared-invalid-clean", persist_token=False)
    original_sender = client._sender
    client._graph.apply_state(
        {
            "type": "layer_graph_state",
            "seq": 1,
            "generation": "graph-1",
            "revision": 1,
            "root_layer_key": "layer:root",
            "layers": [
                {"layer_key": "layer:root", "revision": 1, "sublayers": []}
            ],
        }
    )
    sender = _RecoverySender(_stale_artifact("layer:root"))
    client._sender = sender
    _start_with_receiver(client)
    monkeypatch.setattr(client, "_replay_to_fresh_checkpoint", lambda _timeout: None)

    clean_stage = _create_root(tmp_path / "clean-root.usda")
    clean_stage.SetEditTarget(Usd.EditTarget(clean_stage.GetSessionLayer()))
    try:
        with pytest.raises(RecoveryError, match="outside the root/sublayer graph") as error:
            client.recover_use_server(clean_stage=clean_stage)

        assert error.value.code == "invalid_clean_stage"
        assert client.stage is old_stage
        assert sender.recovery_required
        assert sender.abandoned_session_ids == []
    finally:
        client._sender = original_sender
        client.close()


def test_shared_rebind_recovery_rejects_a_detached_source_reused_by_clean_stage(
    tmp_path,
    monkeypatch,
):
    child = Sdf.Layer.CreateNew(str(tmp_path / "child.usda"))
    Sdf.CreatePrimInLayer(child, "/Rejected")
    child.Save()
    old_stage = _create_root(tmp_path / "old-root.usda")
    old_stage.GetRootLayer().subLayerPaths.append("./child.usda")
    client = SharedStageClient(
        old_stage,
        app_name="shared-detached-overlap",
        persist_token=False,
    )
    original_sender = client._sender
    bound_child = _bind_child_graph(client)
    assert bound_child is child
    client._graph.apply_sublayers(
        "layer:root",
        {
            "k": "set_sublayers",
            "prim": "/",
            "generation": "graph-1",
            "revision": 2,
            "sublayers": [],
        },
    )
    client._tracker.sync_graph(force=True)
    assert child not in old_stage.GetLayerStack(includeSessionLayers=False)

    sender = _RecoverySender(_stale_artifact("layer:child"))
    client._sender = sender
    _start_with_receiver(client)
    monkeypatch.setattr(client, "_replay_to_fresh_checkpoint", lambda _timeout: None)

    clean_stage = _create_root(tmp_path / "clean-root.usda")
    clean_stage.GetRootLayer().subLayerPaths.append("./child.usda")
    assert child in clean_stage.GetLayerStack(includeSessionLayers=False)
    try:
        with pytest.raises(RecoveryError, match="shares loaded layers") as error:
            client.recover_use_server(clean_stage=clean_stage)

        assert error.value.code == "shared_loaded_layers"
        assert client.stage is old_stage
        assert client._last_recovery_assessment.layers[0].source_layer is child
        assert sender.recovery_required
        assert sender.abandoned_session_ids == []
    finally:
        client._sender = original_sender
        client.close()


def _prim_spec_event(path):
    source = Sdf.Layer.CreateAnonymous()
    Sdf.CreatePrimInLayer(source, path).specifier = Sdf.SpecifierDef
    return {
        "k": "set_sdf_spec_fields",
        "prim": path,
        "spec_path": path,
        "spec_kind": "prim",
        "fields": ["specifier"],
        "fragment": serialize_spec_fields(
            source, path, "prim", ["specifier"], stabilize_asset_paths=False,
        ),
        "removed": False,
    }


def test_shared_budget_releases_local_edits_under_sustained_traffic(tmp_path, monkeypatch):
    base = tmp_path / "server-root.usda"
    Sdf.Layer.CreateNew(str(base)).Save()
    stage = _create_root(tmp_path / "root.usda")
    sent = []
    with embedded_server(base_usd_path=str(base), layer_mode=LayerMode.SHARED_STAGE) as server:
        state = server.sync_server
        client = SharedStageClient(
            stage, app_name="shared-budget", persist_token=False, port=server.server_address[1],
        )
        traffic = PeerTraffic(
            state, client._receiver, _prim_spec_event,
            layer_key=state.shared_layer_graph.root_layer_key,
        )
        try:
            connect_client(client)
            traffic.arrive(3)
            assert client._sender.connect(timeout=5)
            monkeypatch.setattr(
                client._sender, "send_events",
                lambda events, layer_key="": sent.append(events) or True,
            )
            stage.DefinePrim("/Shared", "Xform")

            submitted = []
            for _ in range(2):
                submitted.append(client.update(max_messages=2).submitted_events)
                traffic.arrive(2)
            assert submitted[0] == 0 and submitted[1] > 0
            assert sent
        finally:
            client.close()
