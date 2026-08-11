"""USD-native shared-stage client lifecycle without network transport."""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from openusdconnect import ClientPhase
from openusdconnect.codec import ReceivedEvent
from openusdconnect.sdf_spec_delta import serialize_spec_fields
from openusdconnect.shared_stage_client import SharedStageClient


def _create_root(path) -> Usd.Stage:
    root = Sdf.Layer.CreateNew(str(path))
    root.Save()
    return Usd.Stage.Open(root)


def test_constructor_requires_a_stage_and_application_name():
    with pytest.raises(TypeError, match="Usd.Stage"):
        SharedStageClient(None, app_name="test", persist_token=False)
    with pytest.raises(ValueError, match="app_name"):
        SharedStageClient(Usd.Stage.CreateInMemory(), app_name=" ", persist_token=False)
    with pytest.raises(ValueError, match="portable root layer"):
        SharedStageClient(Usd.Stage.CreateInMemory(), app_name="test", persist_token=False)


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
        assert client.status.phase is ClientPhase.READY
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
                        "sublayers": [
                            {
                                "authored_path": "./late.usda",
                                "offset": 0.0,
                                "scale": 1.0,
                                "layer_key": child_key,
                            }
                        ],
                    },
                    {"layer_key": child_key, "sublayers": []},
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
        assert client.deferred_incoming_record_count == 1

        late = Sdf.Layer.CreateNew(str(tmp_path / "late.usda"))
        Sdf.CreatePrimInLayer(late, "/Late")
        late.Save()
        mapped = client.refresh_asset_dependency()

        assert mapped == (child_key,)
        assert client.deferred_incoming_record_count == 0
        assert late.GetAttributeAtPath("/Late.value").default == 8
    finally:
        client.close()


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
                "layers": [{"layer_key": "layer:root", "sublayers": []}],
            }
        )
        client._sender = _RepairSender()
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
        with pytest.raises(ValueError, match="not mapped"):
            client.repair_and_resume(events, layer=detached)
    finally:
        client._sender = original_sender
        client.close()
