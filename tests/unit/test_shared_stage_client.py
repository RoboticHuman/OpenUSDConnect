"""USD-native shared-stage client lifecycle without network transport."""

from __future__ import annotations

import pytest
from pxr import Ar, Sdf, Usd

from openusdconnect import ClientPhase, RecoveryError
from openusdconnect.codec import ReceivedEvent, TransactionRejectionCode
from openusdconnect.recovery import (
    QuarantinedTransaction,
    RecoveryArtifact,
    TransactionFailure,
    make_recovery_incident,
)
from openusdconnect.sdf_spec_delta import serialize_spec_fields
from openusdconnect.shared_stage_client import SharedStageClient


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

    def disconnect(self):
        self.connected = False

    def connect(self, timeout=None):
        self.connect_timeouts.append(timeout)
        self.connected = True
        return True


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

    sender = _StatusSender()
    client._sender = sender
    client._started = True
    client._receiver.connected = True
    client._receiver._synchronized_event.set()
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
        assert client.deferred_event_count == 1

        late = Sdf.Layer.CreateNew(str(tmp_path / "late.usda"))
        Sdf.CreatePrimInLayer(late, "/Late")
        late.Save()
        mapped = client.refresh_layer_graph()

        assert mapped == (child_key,)
        assert client.deferred_event_count == 0
        assert late.GetAttributeAtPath("/Late.value").default == 8
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

    def _fail_replay():
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

    class _RepairSender:
        recovery_artifact = _stale_artifact("layer:root")
        transaction_failure = recovery_artifact.failure

        def repair_rejected_transaction(self, events, *, layer_key=""):
            repaired.append((events, layer_key))
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
        client._sender = _RepairSender()
        client._started = True
        resumed = []

        def _resume():
            resumed.append(True)
            return True

        monkeypatch.setattr(client, "_connect_sender", _resume)
        events = [{"k": "replace_sdf_layer_content", "fragment": "#usda 1.0\n"}]

        assert client.repair_and_resume(events, layer=stage.GetRootLayer()) == 7
        assert repaired == [(events, "layer:root")]
        assert resumed == [True]

        detached = Sdf.Layer.CreateAnonymous()
        with pytest.raises(RecoveryError, match="not mapped") as error:
            client.repair_and_resume(events, layer=detached)
        assert error.value.code == "invalid_repair_target"
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
    client._started = True

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
        client._receiver.connected = True
        client._receiver._synchronized_event.set()

    monkeypatch.setattr(client, "_refresh_recovery_checkpoint", _detach)
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
    monkeypatch.setattr(client, "_refresh_recovery_checkpoint", lambda _timeout: None)
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
    monkeypatch.setattr(client, "_refresh_recovery_checkpoint", lambda _timeout: None)
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
        monkeypatch.setattr(client, "_refresh_recovery_checkpoint", lambda _timeout: None)
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

    monkeypatch.setattr(client, "_refresh_recovery_checkpoint", _timeout)
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

    monkeypatch.setattr(client, "_refresh_recovery_checkpoint", _detach_child)
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

    monkeypatch.setattr(client, "_refresh_recovery_checkpoint", _remap)
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
    client._started = True
    client._receiver.connected = True
    client._receiver._synchronized_event.set()
    monkeypatch.setattr(client, "_refresh_recovery_checkpoint", lambda _timeout: None)
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
    client._started = True
    client._receiver.connected = True
    client._receiver._synchronized_event.set()
    monkeypatch.setattr(client, "_refresh_recovery_checkpoint", lambda _timeout: None)
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
    client._started = True
    client._receiver.connected = True
    client._receiver._synchronized_event.set()
    monkeypatch.setattr(client, "_refresh_recovery_checkpoint", lambda _timeout: None)
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
    client._started = True

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
        client._receiver.connected = True
        client._receiver._synchronized_event.set()

    monkeypatch.setattr(client, "_refresh_recovery_checkpoint", _refresh)
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
    client._started = True
    client._receiver.connected = True
    client._receiver._synchronized_event.set()
    monkeypatch.setattr(client, "_refresh_recovery_checkpoint", lambda _timeout: None)

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
    client._started = True
    client._receiver.connected = True
    client._receiver._synchronized_event.set()
    monkeypatch.setattr(client, "_refresh_recovery_checkpoint", lambda _timeout: None)

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
