"""Real-socket coverage for bidirectional shared-stage clients."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from pxr import Sdf, Usd

from openusdconnect.protocol_constants import LayerMode
from openusdconnect.recovery import RejectionDisposition
from openusdconnect.sender import EventSender, TransactionRejectedError
from openusdconnect.server import UsdSyncServer
from openusdconnect.server.connection import ConnectionHandler, ThreadedTCPServer
from openusdconnect.shared_stage_client import SharedStageClient


def _create_layer(path: Path, prim_path: str | None = None) -> Sdf.Layer:
    layer = Sdf.Layer.CreateNew(str(path))
    if prim_path:
        Sdf.CreatePrimInLayer(layer, prim_path)
    layer.Save()
    return layer


def _create_stage(directory: Path) -> Usd.Stage:
    directory.mkdir()
    asset = _create_layer(directory / "asset.usda")
    prim = Sdf.CreatePrimInLayer(asset, "/World")
    attr = Sdf.AttributeSpec(prim, "value", Sdf.ValueTypeNames.Int)
    attr.default = 1
    asset.Save()
    root = _create_layer(directory / "scene.usda")
    root.subLayerPaths.append("./asset.usda")
    root.Save()
    return Usd.Stage.Open(root.identifier)


def _pump_until(clients, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for client in clients:
            client.update()
        if predicate():
            return True
        time.sleep(0.01)
    for client in clients:
        client.update()
    return bool(predicate())


def _value_at(stage: Usd.Stage, path: str) -> int | None:
    attr = stage.GetAttributeAtPath(path)
    return attr.Get() if attr else None


def _value(stage: Usd.Stage) -> int | None:
    return _value_at(stage, "/World.value")


def _create_ref_stage(directory: Path) -> Usd.Stage:
    """Root layer with a reference and a payload arc to sibling asset layers."""
    directory.mkdir()
    ref_asset = _create_layer(directory / "ref_asset.usda", "/RefTarget")
    Sdf.AttributeSpec(
        ref_asset.GetPrimAtPath("/RefTarget"), "value", Sdf.ValueTypeNames.Int
    ).default = 1
    ref_asset.Save()
    payload_asset = _create_layer(directory / "payload_asset.usda", "/PayloadTarget")
    Sdf.AttributeSpec(
        payload_asset.GetPrimAtPath("/PayloadTarget"), "value", Sdf.ValueTypeNames.Int
    ).default = 10
    payload_asset.Save()
    root = _create_layer(directory / "scene.usda")
    Sdf.CreatePrimInLayer(root, "/World")
    ref = Sdf.CreatePrimInLayer(root, "/World/Ref")
    ref.referenceList.Add(Sdf.Reference("./ref_asset.usda", "/RefTarget"))
    payload = Sdf.CreatePrimInLayer(root, "/World/Payload")
    payload.payloadList.Add(Sdf.Payload("./payload_asset.usda", "/PayloadTarget"))
    root.Save()
    # This USD build's Usd.Stage.Open loads payloads by default; open with
    # LoadNone so the unloaded-by-default payload behavior is testable.
    return Usd.Stage.Open(root.identifier, load=Usd.Stage.LoadNone)


def _create_multi_layer_stage(directory: Path) -> Usd.Stage:
    """Root layer with two sublayers, one value prim per layer."""
    directory.mkdir()
    a = _create_layer(directory / "a.usda")
    alpha = Sdf.CreatePrimInLayer(a, "/Alpha")
    Sdf.AttributeSpec(alpha, "amount", Sdf.ValueTypeNames.Int).default = 1
    a.Save()
    b = _create_layer(directory / "b.usda")
    beta = Sdf.CreatePrimInLayer(b, "/Beta")
    Sdf.AttributeSpec(beta, "amount", Sdf.ValueTypeNames.Int).default = 1
    b.Save()
    root = _create_layer(directory / "scene.usda")
    Sdf.AttributeSpec(
        Sdf.CreatePrimInLayer(root, "/Root"), "value", Sdf.ValueTypeNames.Int
    ).default = 0
    root.subLayerPaths.append("./a.usda")
    root.subLayerPaths.append("./b.usda")
    root.Save()
    return Usd.Stage.Open(root.identifier)


def test_bidirectional_file_layer_sync_preserves_concurrent_fields(tmp_path):
    server_stage = _create_stage(tmp_path / "server")
    first_stage = _create_stage(tmp_path / "first")
    second_stage = _create_stage(tmp_path / "second")
    sync_server = UsdSyncServer(
        stage=server_stage,
        log_path=str(tmp_path / "events.db"),
        layer_mode=LayerMode.SHARED_STAGE,
    )
    tcp_server = ThreadedTCPServer(
        ("127.0.0.1", 0),
        ConnectionHandler,
        sync_server,
        max_workers=8,
    )
    thread = threading.Thread(target=tcp_server.serve_forever, daemon=True)
    thread.start()
    port = tcp_server.server_address[1]
    first = SharedStageClient(
        first_stage,
        app_name="shared-first",
        port=port,
        persist_token=False,
        reconnect=False,
        delegate_bridge_path=os.environ.get("OPENUSDCONNECT_SDF_DELEGATE_BRIDGE"),
    )
    second = SharedStageClient(
        second_stage,
        app_name="shared-second",
        port=port,
        persist_token=False,
        reconnect=False,
    )
    try:
        first.start()
        second.start()
        assert first.connect(timeout=2)
        assert second.connect(timeout=2)
        assert _pump_until([first, second], lambda: first.graph_ready and second.graph_ready)
        first_stage.SetEditTarget(
            Usd.EditTarget(first_stage.GetLayerStack(includeSessionLayers=False)[1])
        )
        second_stage.SetEditTarget(
            Usd.EditTarget(second_stage.GetLayerStack(includeSessionLayers=False)[1])
        )

        first_stage.GetAttributeAtPath("/World.value").Set(5)
        assert first.update().submitted_events == 1
        assert _pump_until([first, second], lambda: _value(second_stage) == 5)
        assert _value(server_stage) == 5

        first_stage.GetAttributeAtPath("/World.value").SetDocumentation("remote metadata")
        assert first.update().submitted_events == 1
        second_stage.GetAttributeAtPath("/World.value").Set(9)
        update = second.update()
        assert update.submitted_events == 1
        assert _pump_until(
            [first, second],
            lambda: (
                _value(first_stage) == 9
                and _value(second_stage) == 9
                and first_stage.GetAttributeAtPath("/World.value").GetDocumentation()
                == "remote metadata"
                and second_stage.GetAttributeAtPath("/World.value").GetDocumentation()
                == "remote metadata"
            ),
        )

        for directory in (tmp_path / "server", tmp_path / "first", tmp_path / "second"):
            extra = _create_layer(directory / "extra.usda")
            prim = Sdf.CreatePrimInLayer(extra, "/Extra")
            amount = Sdf.AttributeSpec(prim, "amount", Sdf.ValueTypeNames.Int)
            amount.default = 1
            extra.Save()
        first_stage.GetRootLayer().subLayerPaths.append("./extra.usda")
        assert first.update().submitted_events == 1
        assert _pump_until(
            [first, second],
            lambda: all(
                len(stage.GetLayerStack(includeSessionLayers=False)) == 3
                for stage in (server_stage, first_stage, second_stage)
            ),
        )

        first_extra = first_stage.GetLayerStack(includeSessionLayers=False)[2]
        assert first.is_layer_mapped(first_extra)
        first_stage.SetEditTarget(Usd.EditTarget(first_extra))
        first_stage.GetAttributeAtPath("/Extra.amount").Set(7)
        assert first.update().submitted_events == 1
        assert _pump_until(
            [first, second],
            lambda: second_stage.GetAttributeAtPath("/Extra.amount").Get() == 7,
        )

        sync_server.compact_log()
        compacted_seq = sync_server.store.get_max_seq()
        assert _pump_until(
            [first, second],
            lambda: first.last_seq == compacted_seq and second.last_seq == compacted_seq,
        )

        second_extra = second_stage.GetLayerStack(includeSessionLayers=False)[2]
        second_stage.SetEditTarget(Usd.EditTarget(second_extra))
        second_stage.GetAttributeAtPath("/Extra.amount").Set(11)
        assert second.update().submitted_events == 1
        assert _pump_until(
            [first, second],
            lambda: first_stage.GetAttributeAtPath("/Extra.amount").Get() == 11,
        )
    finally:
        first.close()
        second.close()
        tcp_server.shutdown()
        tcp_server.server_close()
        thread.join(timeout=5)
        sync_server.shutdown()
        sync_server.store.close()


def test_managed_server_rejects_a_shared_stage_client(tmp_path):
    server_stage = _create_stage(tmp_path / "server")
    client_stage = _create_stage(tmp_path / "client")
    sync_server = UsdSyncServer(
        stage=server_stage,
        log_path=str(tmp_path / "events.db"),
    )
    tcp_server = ThreadedTCPServer(
        ("127.0.0.1", 0),
        ConnectionHandler,
        sync_server,
        max_workers=2,
    )
    thread = threading.Thread(target=tcp_server.serve_forever, daemon=True)
    thread.start()
    client = SharedStageClient(
        client_stage,
        app_name="wrong-mode",
        port=tcp_server.server_address[1],
        persist_token=False,
        reconnect=False,
    )
    try:
        client.start()
        with pytest.raises(ConnectionError, match="server uses 'managed'"):
            client.connect(timeout=2)
    finally:
        client.close()
        tcp_server.shutdown()
        tcp_server.server_close()
        thread.join(timeout=5)
        sync_server.shutdown()
        sync_server.store.close()


def test_reference_override_and_payload_arcs_sync_via_sdf_deltas(tmp_path):
    server_stage = _create_ref_stage(tmp_path / "server")
    first_stage = _create_ref_stage(tmp_path / "first")
    second_stage = _create_ref_stage(tmp_path / "second")
    sync_server = UsdSyncServer(
        stage=server_stage,
        log_path=str(tmp_path / "events.db"),
        layer_mode=LayerMode.SHARED_STAGE,
    )
    tcp_server = ThreadedTCPServer(
        ("127.0.0.1", 0),
        ConnectionHandler,
        sync_server,
        max_workers=8,
    )
    thread = threading.Thread(target=tcp_server.serve_forever, daemon=True)
    thread.start()
    port = tcp_server.server_address[1]
    first = SharedStageClient(
        first_stage,
        app_name="shared-first",
        port=port,
        persist_token=False,
        reconnect=False,
        delegate_bridge_path=os.environ.get("OPENUSDCONNECT_SDF_DELEGATE_BRIDGE"),
    )
    second = SharedStageClient(
        second_stage,
        app_name="shared-second",
        port=port,
        persist_token=False,
        reconnect=False,
    )
    try:
        first.start()
        second.start()
        assert first.connect(timeout=2)
        assert second.connect(timeout=2)
        assert _pump_until([first, second], lambda: first.graph_ready and second.graph_ready)

        # Reference content composes in both clients; payloads start unloaded.
        assert _value_at(first_stage, "/World/Ref.value") == 1
        assert _value_at(second_stage, "/World/Ref.value") == 1
        assert _value_at(first_stage, "/World/Payload.value") is None
        assert _value_at(second_stage, "/World/Payload.value") is None

        # Editing the referenced prim authors a root-layer override.
        first_stage.SetEditTarget(
            Usd.EditTarget(first_stage.GetLayerStack(includeSessionLayers=False)[0])
        )
        first_stage.GetAttributeAtPath("/World/Ref.value").Set(5)
        assert first.update().submitted_events == 1
        assert _pump_until(
            [first, second],
            lambda: _value_at(second_stage, "/World/Ref.value") == 5,
        )
        assert _value_at(server_stage, "/World/Ref.value") == 5
        override = second_stage.GetRootLayer().GetAttributeAtPath("/World/Ref.value")
        assert override is not None and override.default == 5

        # Load/unload is local composition state over the same payload arc.
        first_stage.Load("/World/Payload")
        assert _value_at(first_stage, "/World/Payload.value") == 10
        first_stage.Unload("/World/Payload")
        assert _value_at(first_stage, "/World/Payload.value") is None
        second_stage.Load("/World/Payload")
        assert _value_at(second_stage, "/World/Payload.value") == 10
        second_stage.Unload("/World/Payload")
        assert _value_at(second_stage, "/World/Payload.value") is None

        # A payload arc authored at runtime travels as an Sdf field delta.
        first_stage.DefinePrim("/World/Payload2", "Xform").GetPayloads().AddPayload(
            "./payload_asset.usda",
            "/PayloadTarget",
        )
        assert first.update().submitted_events >= 1
        assert _pump_until(
            [first, second],
            lambda: second_stage.GetRootLayer().GetPrimAtPath("/World/Payload2") is not None,
        )
        assert server_stage.GetRootLayer().GetPrimAtPath("/World/Payload2") is not None
        first_stage.Load("/World/Payload2")
        second_stage.Load("/World/Payload2")
        assert _value_at(first_stage, "/World/Payload2.value") == 10
        assert _value_at(second_stage, "/World/Payload2.value") == 10
        second_stage.Unload("/World/Payload2")
        assert _value_at(second_stage, "/World/Payload2.value") is None
    finally:
        first.close()
        second.close()
        tcp_server.shutdown()
        tcp_server.server_close()
        thread.join(timeout=5)
        sync_server.shutdown()
        sync_server.store.close()


def test_clients_reconnect_and_converge_after_server_restart(tmp_path):
    server_stage = _create_stage(tmp_path / "server")
    first_stage = _create_stage(tmp_path / "first")
    second_stage = _create_stage(tmp_path / "second")
    log_path = str(tmp_path / "events.db")
    sync_server = UsdSyncServer(
        stage=server_stage,
        log_path=log_path,
        layer_mode=LayerMode.SHARED_STAGE,
    )
    tcp_server = ThreadedTCPServer(
        ("127.0.0.1", 0),
        ConnectionHandler,
        sync_server,
        max_workers=8,
    )
    thread = threading.Thread(target=tcp_server.serve_forever, daemon=True)
    thread.start()
    port = tcp_server.server_address[1]
    first = SharedStageClient(
        first_stage,
        app_name="shared-first",
        port=port,
        persist_token=False,
        reconnect=True,
        delegate_bridge_path=os.environ.get("OPENUSDCONNECT_SDF_DELEGATE_BRIDGE"),
    )
    second = SharedStageClient(
        second_stage,
        app_name="shared-second",
        port=port,
        persist_token=False,
        reconnect=True,
    )
    try:
        first.start()
        second.start()
        assert first.connect(timeout=2)
        assert second.connect(timeout=2)
        assert _pump_until([first, second], lambda: first.graph_ready and second.graph_ready)

        for stage in (first_stage, second_stage):
            stage.SetEditTarget(Usd.EditTarget(stage.GetLayerStack(includeSessionLayers=False)[1]))
        first_stage.GetAttributeAtPath("/World.value").Set(5)
        assert first.update().submitted_events == 1
        assert _pump_until([first, second], lambda: _value(second_stage) == 5)
        assert _value(server_stage) == 5

        # Restart the server on the same port, rebuilding from the same log.
        # Clients detect the death promptly instead of waiting out TCP
        # keepalive: senders are cleanly detached (quit) and receivers drop
        # their sockets, resuming from their last sequence on reconnect.
        for client in (first, second):
            client.sender.disconnect()
            client.receiver.request_replay_from(client.last_seq + 1)
        tcp_server.shutdown()
        tcp_server.server_close()
        thread.join(timeout=5)
        sync_server.shutdown()
        sync_server.store.close()
        restarted_stage = Usd.Stage.Open(str(tmp_path / "server" / "scene.usda"))
        sync_server = UsdSyncServer(
            stage=restarted_stage,
            log_path=log_path,
            layer_mode=LayerMode.SHARED_STAGE,
        )
        tcp_server = ThreadedTCPServer(
            ("127.0.0.1", port),
            ConnectionHandler,
            sync_server,
            max_workers=8,
        )
        thread = threading.Thread(target=tcp_server.serve_forever, daemon=True)
        thread.start()

        # Clients reconnect automatically and resume from the persisted log.
        assert _pump_until(
            [first, second],
            lambda: (
                first.connected
                and second.connected
                and _value(first_stage) == 5
                and _value(second_stage) == 5
                and _value(restarted_stage) == 5
            ),
            timeout=10,
        )

        # Edits after the restart continue to sync through the new server.
        log_seq_before = sync_server.store.get_max_seq()
        second_stage.GetAttributeAtPath("/World.value").Set(9)
        assert _pump_until(
            [first, second],
            lambda: _value(first_stage) == 9 and _value(second_stage) == 9,
        )
        assert _value(restarted_stage) == 9
        assert sync_server.store.get_max_seq() > log_seq_before
    finally:
        first.close()
        second.close()
        tcp_server.shutdown()
        tcp_server.server_close()
        thread.join(timeout=5)
        sync_server.shutdown()
        sync_server.store.close()


def test_detached_layer_rejection_is_reported_as_recoverable_conflict(tmp_path):
    server_stage = _create_stage(tmp_path / "server")
    sync_server = UsdSyncServer(
        stage=server_stage,
        log_path=str(tmp_path / "events.db"),
        layer_mode=LayerMode.SHARED_STAGE,
    )
    tcp_server = ThreadedTCPServer(
        ("127.0.0.1", 0), ConnectionHandler, sync_server, max_workers=4
    )
    thread = threading.Thread(target=tcp_server.serve_forever, daemon=True)
    thread.start()
    graph = sync_server.shared_layer_graph
    child_key = next(
        key for key in graph.reachable_layer_keys() if key != graph.root_layer_key
    )
    sender = EventSender(
        "127.0.0.1",
        tcp_server.server_address[1],
        client_id="stale-layer-client",
        layer_mode=LayerMode.SHARED_STAGE,
    )
    try:
        assert sender.connect()
        sync_server.process_txn(
            [
                {
                    "k": "set_sublayers",
                    "prim": "/",
                    "generation": graph.generation,
                    "revision": 0,
                    "sublayers": [],
                }
            ],
            layer_key=graph.root_layer_key,
        )
        assert child_key not in graph.reachable_layer_keys()

        assert sender.send_events(
            [{"k": "replace_sdf_layer_content", "prim": "/", "fragment": "#usda 1.0\n"}],
            layer_key=child_key,
        )
        with pytest.raises(TransactionRejectedError) as caught:
            sender.flush(timeout=5)

        assert caught.value.failure.code_name == "stale_layer_graph"
        assert caught.value.failure.disposition is RejectionDisposition.RECOVERABLE_CONFLICT
        assert sender.recovery_required
    finally:
        sender.disconnect()
        tcp_server.shutdown()
        tcp_server.server_close()
        thread.join(timeout=5)
        sync_server.shutdown()
        sync_server.store.close()


def test_three_clients_concurrently_edit_distinct_layers(tmp_path):
    server_stage = _create_multi_layer_stage(tmp_path / "server")
    stages = [_create_multi_layer_stage(tmp_path / name) for name in ("first", "second", "third")]
    sync_server = UsdSyncServer(
        stage=server_stage,
        log_path=str(tmp_path / "events.db"),
        layer_mode=LayerMode.SHARED_STAGE,
    )
    tcp_server = ThreadedTCPServer(
        ("127.0.0.1", 0),
        ConnectionHandler,
        sync_server,
        max_workers=8,
    )
    thread = threading.Thread(target=tcp_server.serve_forever, daemon=True)
    thread.start()
    port = tcp_server.server_address[1]
    clients = [
        SharedStageClient(
            stage,
            app_name=name,
            port=port,
            persist_token=False,
            reconnect=False,
            delegate_bridge_path=os.environ.get("OPENUSDCONNECT_SDF_DELEGATE_BRIDGE"),
        )
        for stage, name in zip(stages, ("shared-a", "shared-b", "shared-c"), strict=True)
    ]
    try:
        for client in clients:
            client.start()
        assert all(client.connect(timeout=2) for client in clients)
        assert _pump_until(clients, lambda: all(client.graph_ready for client in clients))

        # One client per layer: root, first sublayer, second sublayer.
        for stage, index in zip(stages, (0, 1, 2), strict=True):
            stage.SetEditTarget(
                Usd.EditTarget(stage.GetLayerStack(includeSessionLayers=False)[index])
            )
        assert all(
            client.is_layer_mapped(stage.GetLayerStack(includeSessionLayers=False)[index])
            for client, stage, index in zip(clients, stages, (0, 1, 2), strict=True)
        )
        stages[0].GetAttributeAtPath("/Root.value").Set(1)
        stages[1].GetAttributeAtPath("/Alpha.amount").Set(2)
        stages[2].GetAttributeAtPath("/Beta.amount").Set(3)
        for client in clients:
            client.update()

        paths = ("/Root.value", "/Alpha.amount", "/Beta.amount")

        def _converged(*values):
            return all(
                _value_at(stage, path) == expected
                for stage in stages
                for path, expected in zip(paths, values, strict=True)
            )

        assert _pump_until(clients, lambda: _converged(1, 2, 3))
        assert (
            server_stage.GetAttributeAtPath("/Root.value").Get() == 1
            and server_stage.GetAttributeAtPath("/Alpha.amount").Get() == 2
            and server_stage.GetAttributeAtPath("/Beta.amount").Get() == 3
        )
        assert (
            stages[0].GetLayerStack(includeSessionLayers=False)[0].GetAttributeAtPath("/Root.value")
        )
        assert (
            stages[1]
            .GetLayerStack(includeSessionLayers=False)[1]
            .GetAttributeAtPath("/Alpha.amount")
        )
        assert (
            stages[2]
            .GetLayerStack(includeSessionLayers=False)[2]
            .GetAttributeAtPath("/Beta.amount")
        )

        # A second round keeps all three layers live under three-way traffic.
        stages[0].GetAttributeAtPath("/Root.value").Set(4)
        stages[1].GetAttributeAtPath("/Alpha.amount").Set(5)
        stages[2].GetAttributeAtPath("/Beta.amount").Set(6)
        for client in clients:
            client.update()
        assert _pump_until(clients, lambda: _converged(4, 5, 6))
        assert (
            server_stage.GetAttributeAtPath("/Root.value").Get() == 4
            and server_stage.GetAttributeAtPath("/Alpha.amount").Get() == 5
            and server_stage.GetAttributeAtPath("/Beta.amount").Get() == 6
        )
    finally:
        for client in clients:
            client.close()
        tcp_server.shutdown()
        tcp_server.server_close()
        thread.join(timeout=5)
        sync_server.shutdown()
        sync_server.store.close()
