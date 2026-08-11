"""Real-server coverage for the layered USD-native client API."""

from __future__ import annotations

import threading
import time

import pytest
from pxr import Sdf, Usd, UsdGeom

from openusdconnect.server import UsdSyncServer
from openusdconnect.server.connection import ConnectionHandler, ThreadedTCPServer
from openusdconnect.usd_client import UsdPublisher, UsdReceiver

_VALUE_PATH = "/World/Thing.userProperties:value"


@pytest.fixture
def live_server(tmp_path):
    sync_server = UsdSyncServer(
        log_path=str(tmp_path / "live-api.db"),
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
        yield tcp_server.server_address[1]
    finally:
        tcp_server.shutdown()
        tcp_server.server_close()
        thread.join(timeout=5)
        sync_server.shutdown()
        sync_server.store.close()


def _stage_with_base_value(value: int) -> Usd.Stage:
    stage = Usd.Stage.CreateInMemory()
    prim = stage.DefinePrim("/World/Thing", "Xform")
    prim.CreateAttribute(
        "userProperties:value",
        Sdf.ValueTypeNames.Int,
        custom=True,
    ).Set(value)
    return stage


def _pump_until(receiver: UsdReceiver, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        receiver.update()
        if predicate():
            return True
        time.sleep(0.01)
    receiver.update()
    return bool(predicate())


def _value(stage: Usd.Stage):
    attr = stage.GetAttributeAtPath(_VALUE_PATH)
    return attr.Get() if attr and attr.IsValid() else None


def test_layered_receiver_preserves_and_clears_an_override(live_server):
    source = _stage_with_base_value(5)
    target = _stage_with_base_value(5)
    source.SetEditTarget(Usd.EditTarget(source.GetSessionLayer()))

    publisher = UsdPublisher(
        source,
        app_name="live-api-author",
        port=live_server,
        department="layout",
        persist_token=False,
    )
    receiver = UsdReceiver(
        target,
        app_name="live-api-observer",
        port=live_server,
        persist_token=False,
        reconnect=False,
    )
    try:
        assert publisher.connect()
        source.GetAttributeAtPath(_VALUE_PATH).Set(17)
        UsdGeom.SetStageUpAxis(source, UsdGeom.Tokens.z)
        assert publisher.update() > 0

        receiver.start()
        assert receiver.connect(timeout=2)
        assert receiver.layered_replay_active
        assert _pump_until(receiver, lambda: _value(target) == 17)

        router = receiver._dispatcher.layer_router
        assert router is not None
        managed = router.layer_for("department:layout")
        assert managed.GetAttributeAtPath(_VALUE_PATH).default == 17
        assert target.GetRootLayer().GetAttributeAtPath(_VALUE_PATH).default == 5
        assert UsdGeom.GetStageUpAxis(target) == UsdGeom.Tokens.z

        original_target = target
        target = _stage_with_base_value(5)
        receiver.rebind_stage(target)
        assert _value(original_target) == 5
        assert _value(target) == 17
        assert UsdGeom.GetStageUpAxis(original_target) == UsdGeom.Tokens.y
        assert UsdGeom.GetStageUpAxis(target) == UsdGeom.Tokens.z

        source.GetAttributeAtPath(_VALUE_PATH).Clear()
        assert publisher.update() > 0
        assert _pump_until(receiver, lambda: _value(target) == 5)
        assert not managed.GetAttributeAtPath(_VALUE_PATH).HasInfo("default")

        receiver.close()
        assert UsdGeom.GetStageUpAxis(target) == UsdGeom.Tokens.y
    finally:
        receiver.close()
        publisher.close()
