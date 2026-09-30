"""Readiness and local-publication completion through the public client APIs."""

import time

import pytest
from pxr import Gf, Usd, UsdGeom

from openusdconnect import (
    ClientPhase,
    ManagedClient,
    ServerConfig,
    ServerRuntime,
    SharedStageClient,
    UsdPublisher,
    UsdReceiver,
)
from openusdconnect.protocol_constants import LayerMode


def _stage(path):
    stage = Usd.Stage.CreateNew(str(path))
    prim = stage.DefinePrim("/World", "Xform")
    UsdGeom.Xformable(prim).AddTranslateOp().Set(Gf.Vec3d(0))
    stage.GetRootLayer().Save()
    return stage


@pytest.mark.parametrize("kind", [ManagedClient, SharedStageClient])
def test_finish_includes_unprepared_edits_and_waits_for_server_commit(tmp_path, kind):
    # Distinct files prevent the in-process server and client from sharing a
    # mutable Sdf.Layer through USD's layer registry.
    server_base = tmp_path / "server.usda"
    _stage(server_base)
    stage = _stage(tmp_path / "client.usda")
    managed = kind is ManagedClient
    config = ServerConfig(
        host="127.0.0.1",
        port=0,
        base_usd_path=str(server_base),
        log_path=str(tmp_path / "events.db"),
        layer_mode=LayerMode.MANAGED if managed else LayerMode.SHARED_STAGE,
    )
    with ServerRuntime(config) as server:
        options = {"transform_coalesce_seconds": 30.0} if managed else {}
        with kind(
            stage,
            app_name="completion-test",
            port=server.server_address[1],
            persist_token=False,
            **options,
        ) as client:
            assert client.wait_until_ready(timeout=5)
            assert client.status.phase is ClientPhase.READY
            assert client.status.edit_target_is_shared

            stage.GetAttributeAtPath("/World.xformOp:translate").Set(Gf.Vec3d(2, 3, 4))
            assert client.status.has_unsent_changes
            assert client.status.has_unsent_changes
            assert client.status.prepared_events == 0
            assert client.status.pending_events == 0
            assert client.flush(timeout=0)  # ACK-only flush has no submitted work yet.

            assert client.submit_and_wait(timeout=5)
            assert not client.status.has_unsent_changes
            assert client.status.pending_events == 0
            assert client.status.acknowledged_events_total > 0
            assert server.sync_server.stage.GetAttributeAtPath(
                "/World.xformOp:translate"
            ).Get() == Gf.Vec3d(2, 3, 4)


def test_directional_clients_share_the_blocking_helpers(tmp_path):
    server_base = tmp_path / "server.usda"
    _stage(server_base)
    author = _stage(tmp_path / "author.usda")
    author.SetEditTarget(Usd.EditTarget(author.GetSessionLayer()))
    viewer = _stage(tmp_path / "viewer.usda")
    config = ServerConfig(
        host="127.0.0.1",
        port=0,
        base_usd_path=str(server_base),
        log_path=str(tmp_path / "events.db"),
    )
    with ServerRuntime(config) as server:
        port = server.server_address[1]
        with (
            UsdPublisher(
                author, app_name="completion-author", port=port, persist_token=False,
            ) as publisher,
            UsdReceiver(
                viewer, app_name="completion-viewer", port=port, persist_token=False,
            ) as receiver,
        ):
            assert publisher.wait_until_ready(timeout=5)
            assert receiver.wait_until_ready(timeout=5)
            assert publisher.status.phase is ClientPhase.READY
            assert receiver.status.phase is ClientPhase.READY

            author.GetAttributeAtPath("/World.xformOp:translate").Set(Gf.Vec3d(2, 3, 4))
            assert publisher.status.has_unsent_changes
            assert publisher.submit_and_wait(timeout=5)
            assert not publisher.status.has_unsent_changes

            received = viewer.GetAttributeAtPath("/World.xformOp:translate")
            deadline = time.monotonic() + 5
            while received.Get() != Gf.Vec3d(2, 3, 4) and time.monotonic() < deadline:
                assert receiver.update().submitted_events == 0
                time.sleep(0.01)
            assert received.Get() == Gf.Vec3d(2, 3, 4)

            receiver.rebind_stage(None)
            assert receiver.status.phase is ClientPhase.PARKED
            with pytest.raises(RuntimeError, match="no bound stage"):
                receiver.wait_until_ready(timeout=0)
