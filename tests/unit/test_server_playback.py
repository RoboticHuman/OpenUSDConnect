"""Playback ownership and concurrency without a running server."""

import threading
from concurrent.futures import ThreadPoolExecutor

from openusdconnect.server.playback import PlaybackController


def test_competing_claims_grant_one_leader_and_its_initial_time():
    playback = PlaybackController()
    ready = threading.Barrier(8)

    def claim(index):
        ready.wait(timeout=5)
        return playback.claim(f"client-{index}", initial_time=float(index))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(8)))

    winners = [index for index, (granted, _leader) in enumerate(results) if granted]
    assert len(winners) == 1
    winner = winners[0]
    assert {leader for _granted, leader in results} == {f"client-{winner}"}
    assert playback.snapshot() == {
        "time": float(winner),
        "playing": False,
        "rate": 1.0,
        "leader_client_id": f"client-{winner}",
    }


def test_returned_snapshots_cannot_mutate_playback():
    playback = PlaybackController()
    playback.claim("leader", initial_time=12.0)
    accepted, state, _leader = playback.apply_control("leader", "play")
    assert accepted
    state["time"] = -1.0
    state["leader_client_id"] = "other"
    playback.snapshot()["playing"] = False

    assert playback.snapshot() == {
        "time": 12.0,
        "playing": True,
        "rate": 1.0,
        "leader_client_id": "leader",
    }


def test_leadership_handoff_preserves_playhead_and_rate():
    playback = PlaybackController()
    playback.claim("first", initial_time=24.0)
    playback.apply_control("first", "set_rate", rate=2.0)
    playback.apply_control("first", "play")
    assert not playback.release("other")
    assert playback.release("first")
    assert playback.claim("second") == (True, "second")

    assert playback.snapshot() == {
        "time": 24.0,
        "playing": True,
        "rate": 2.0,
        "leader_client_id": "second",
    }
    assert playback.apply_control("first", "stop") == (
        False,
        "not the playback leader (current: second)",
        "second",
    )
