"""Replay-mode admission and layer-stack mutation invariants."""

from __future__ import annotations

import pytest

from openusdconnect.server import UsdSyncServer
from openusdconnect.server.types import ReplayModeConflictError


@pytest.fixture
def server(tmp_path):
    instance = UsdSyncServer(log_path=str(tmp_path / "replay-mode.db"))
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.store.close()


def test_flat_replay_reservation_is_available_for_default_stack(server):
    accepted, reason = server.reserve_receiver_replay_mode(False)
    assert accepted
    assert reason == ""
    assert server._flat_receiver_count == 1

    server.release_receiver_replay_mode(False)
    assert server._flat_receiver_count == 0


def test_layered_replay_does_not_reserve_flat_capacity(server):
    assert server.reserve_receiver_replay_mode(True) == (True, "")
    assert server._flat_receiver_count == 0
    server.release_receiver_replay_mode(True)


def test_department_policy_requires_layered_replay(server):
    server.set_department_priority(["animation", "layout"])

    accepted, reason = server.reserve_receiver_replay_mode(False)
    assert not accepted
    assert "department" in reason
    assert server._flat_receiver_count == 0


def test_materialized_layer_stack_requires_layered_replay(server):
    server.set_department_priority(["animation"])
    server.get_or_create_client_layer("artist", department="animation")
    server.set_department_priority([])

    accepted, reason = server.reserve_receiver_replay_mode(False)
    assert not accepted
    assert "multiple collaboration layers" in reason


def test_muted_default_layer_requires_layered_replay(server):
    assert server.mute_layer("default")

    accepted, reason = server.reserve_receiver_replay_mode(False)
    assert not accepted
    assert "muted collaboration layers" in reason


def test_flat_receiver_blocks_layer_stack_changes(server):
    assert server.reserve_receiver_replay_mode(False)[0]
    try:
        with pytest.raises(ReplayModeConflictError, match="layer-stack changes"):
            server.set_department_priority(["animation"])
        with pytest.raises(ReplayModeConflictError, match="layer-stack changes"):
            server.get_or_create_client_layer("artist", department="animation")
        with pytest.raises(ReplayModeConflictError, match="layer-stack changes"):
            server.mute_layer("default")

        assert server.department_priority == []
        assert "artist" not in server.client_layers
        assert "artist" not in server._client_departments

        server.set_department_priority([])
        assert server.unmute_layer("default")
    finally:
        server.release_receiver_replay_mode(False)

    server.set_department_priority(["animation"])
    assert server.department_priority == ["animation"]
