"""Real-socket coverage for bidirectional shared-stage clients."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from pxr import Sdf, Usd

from openusdconnect.protocol_constants import LayerMode
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


def _value(stage: Usd.Stage) -> int | None:
    attr = stage.GetAttributeAtPath("/World.value")
    return attr.Get() if attr else None


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
        sdf_notice_bridge=os.environ.get("OPENUSDCONNECT_SDF_NOTICE_BRIDGE"),
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
        assert first.wait_connected(timeout=2)
        assert second.wait_connected(timeout=2)
        assert _pump_until([first, second], lambda: first.graph_ready and second.graph_ready)
        first_stage.SetEditTarget(
            Usd.EditTarget(first_stage.GetLayerStack(includeSessionLayers=False)[1])
        )
        second_stage.SetEditTarget(
            Usd.EditTarget(second_stage.GetLayerStack(includeSessionLayers=False)[1])
        )

        first_stage.GetAttributeAtPath("/World.value").Set(5)
        assert first.update().sent == 1
        assert _pump_until([first, second], lambda: _value(second_stage) == 5)
        assert _value(server_stage) == 5

        first_stage.GetAttributeAtPath("/World.value").SetDocumentation("remote metadata")
        assert first.update().sent == 1
        second_stage.GetAttributeAtPath("/World.value").Set(9)
        update = second.update()
        assert update.sent == 1
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
        assert first.update().sent == 1
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
        assert first.update().sent == 1
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
        assert second.update().sent == 1
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
            client.wait_connected(timeout=2)
    finally:
        client.close()
        tcp_server.shutdown()
        tcp_server.server_close()
        thread.join(timeout=5)
        sync_server.shutdown()
        sync_server.store.close()
