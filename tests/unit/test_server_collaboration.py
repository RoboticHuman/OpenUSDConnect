"""Collaboration policy keeps client assignments and layer lifecycle consistent."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from pxr import Usd

from openusdconnect.protocol_constants import LayerMode
from openusdconnect.server.collaboration import CollaborationPolicy
from openusdconnect.server.scene import SceneState


@pytest.fixture
def scene():
    return SceneState(Usd.Stage.CreateInMemory(), layer_mode=LayerMode.MANAGED, op_cache_size=16)


def _policy(scene, **callbacks):
    return CollaborationPolicy(
        scene,
        department_priority=[],
        bump_snapshot_epoch=callbacks.get("bump_snapshot_epoch", lambda _reason: None),
        broadcast_layer_stack_state=callbacks.get("broadcast_layer_stack_state", lambda: None),
    )


@pytest.mark.parametrize("merge_into_root", [False, True])
def test_join_cannot_lose_layer_to_last_client_departure(scene, monkeypatch, merge_into_root):
    policy = _policy(scene)
    original_layer = policy.get_or_create_client_layer("leaving", "animation")
    assigning = threading.Event()
    resume_join = threading.Event()
    departure_started = threading.Event()
    departure_finished = threading.Event()

    class PausedAssignments(dict):
        def __setitem__(self, client_id, layer_key):
            if client_id == "joining":
                # Pause after resolving the existing layer, before recording
                # its new user: this was the last-client removal race window.
                assigning.set()
                assert resume_join.wait(5)
            super().__setitem__(client_id, layer_key)

    monkeypatch.setattr(policy, "_client_layer_keys", PausedAssignments(policy._client_layer_keys))

    def depart():
        departure_started.set()
        try:
            return policy.release_client_layer("leaving", merge_into_root=merge_into_root)
        finally:
            departure_finished.set()

    with ThreadPoolExecutor(max_workers=2) as workers:
        joined = workers.submit(policy.get_or_create_client_layer, "joining", "animation")
        try:
            assert assigning.wait(5)
            departed = workers.submit(depart)
            assert departure_started.wait(5)
            assert not departure_finished.wait(0.1)
        finally:
            resume_join.set()
        assert joined.result(timeout=5) is original_layer
        assert departed.result(timeout=5)

    assert policy.client_layers == {"joining": original_layer}
    assert policy.resolve_layer("animation") is original_layer
    assert policy.get_layer_stack_info()[0]["clients"] == ["joining"]


def test_callbacks_observe_complete_assignments_without_holding_scene_lock(scene):
    snapshots = []

    def observe(*_args):
        finished = threading.Event()
        result = []

        def read():
            try:
                result.append((policy.client_layers, policy.get_layer_stack_info()))
            finally:
                finished.set()

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        assert finished.wait(5), "publication held the scene lock and blocked an observer"
        reader.join()
        snapshots.extend(result)

    policy = _policy(scene, bump_snapshot_epoch=observe, broadcast_layer_stack_state=observe)
    layer = policy.get_or_create_client_layer("artist", "animation")
    assert all(assignments == {"artist": layer} for assignments, _info in snapshots)
    assert all(info[0]["clients"] == ["artist"] for _assignments, info in snapshots)

    policy.set_department_priority(["animation"])
    assert policy.set_muted("artist", True)
    assert policy.set_muted("artist", False)
    assert policy.release_client_layer("artist", merge_into_root=True)
    assert snapshots[-1] == ({}, [])

    policy.get_or_create_client_layer("artist", "animation")
    assert policy.release_client_layer("artist", merge_into_root=False)
    assert snapshots[-1] == ({}, [])


def test_priority_snapshot_cannot_change_policy(scene):
    policy = _policy(scene)
    priority = policy.department_priority
    priority.append("animation")

    assert policy.department_priority == []
    assert policy.reserve_receiver_replay_mode(False) == (True, "")
    policy.release_receiver_replay_mode(False)
