"""Tests for the playback-leader claim/control flow on UsdSyncServer."""

from __future__ import annotations

import pytest

try:
    from pxr import Usd  # noqa: F401

    from openusdconnect import codec

    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")

from openusdconnect.protocol import make_claim_playback, make_playback_control
from openusdconnect.protocol_constants import (
    MSG_CLAIM_PLAYBACK,
    MSG_PLAYBACK_CONTROL,
    MSG_PLAYBACK_STATE,
)
from openusdconnect.server.state import UsdSyncServer


@pytest.fixture
def server():
    s = UsdSyncServer(log_path=":memory:")
    try:
        yield s
    finally:
        s.shutdown()


def test_initial_playback_state(server):
    assert server.get_playback_state() == {
        "time": 0.0,
        "playing": False,
        "rate": 1.0,
        "leader_client_id": "",
    }


def test_first_claim_grants(server):
    ok, leader = server.claim_playback("c1")
    assert ok is True
    assert leader == "c1"
    assert server.get_playback_state()["leader_client_id"] == "c1"


def test_second_claim_rejected(server):
    server.claim_playback("c1")
    ok, leader = server.claim_playback("c2")
    assert ok is False
    assert leader == "c1"


def test_same_client_reclaim_is_idempotent(server):
    server.claim_playback("c1")
    ok, leader = server.claim_playback("c1")
    assert ok is True
    assert leader == "c1"


def test_non_leader_control_rejected(server):
    server.claim_playback("c1")
    ok, reason, current_leader = server.apply_playback_control("c2", "play")
    assert ok is False
    assert "not the playback leader" in reason
    assert current_leader == "c1"


def test_leader_set_time(server):
    server.claim_playback("c1")
    ok, payload, leader = server.apply_playback_control(
        "c1", "set_time", time_value=12.5,
    )
    assert ok is True
    assert payload["time"] == 12.5
    assert payload["leader_client_id"] == "c1"
    assert leader == "c1"


def test_leader_play_pause(server):
    server.claim_playback("c1")
    ok, payload, _ = server.apply_playback_control("c1", "play")
    assert ok and payload["playing"] is True
    ok, payload, _ = server.apply_playback_control("c1", "pause")
    assert ok and payload["playing"] is False


def test_unknown_action_returns_leader(server):
    """Rejection for unknown action still returns the current leader so the
    handler doesn't have to peek at server.playback outside the lock."""
    server.claim_playback("c1")
    ok, reason, leader = server.apply_playback_control("c1", "bogus")
    assert ok is False
    assert "unknown" in reason
    assert leader == "c1"


def test_release_clears_leader(server):
    server.claim_playback("c1")
    assert server.release_playback("c1") is True
    assert server.get_playback_state()["leader_client_id"] == ""


def test_release_by_non_leader_is_noop(server):
    server.claim_playback("c1")
    assert server.release_playback("c2") is False
    assert server.get_playback_state()["leader_client_id"] == "c1"


def test_protocol_builders_round_trip():
    msg = make_claim_playback("client-7")
    raw = codec.encode_message(msg)
    out = codec.message_to_dict(raw)
    assert out["type"] == MSG_CLAIM_PLAYBACK
    assert out["client_id"] == "client-7"

    msg = make_playback_control("set_time", time=4.5)
    raw = codec.encode_message(msg)
    out = codec.message_to_dict(raw)
    assert out["type"] == MSG_PLAYBACK_CONTROL
    assert out["action"] == "set_time"
    assert out["time"] == pytest.approx(4.5)


def test_playback_control_omits_time_when_unset():
    """make_playback_control("play") must round-trip without `time`/`rate` keys.

    Earlier the FB scalars defaulted to 0/1.0, so decoded dicts always
    carried `time: 0.0, rate: 1.0` — masking whether the sender authored
    a value. Nullable scalars + conditional encode/decode fix that.
    """
    raw = codec.encode_message(make_playback_control("play"))
    out = codec.message_to_dict(raw)
    assert out["action"] == "play"
    assert "time" not in out
    assert "rate" not in out


def test_playback_control_carries_only_authored_fields():
    raw = codec.encode_message(make_playback_control("set_rate", rate=2.5))
    out = codec.message_to_dict(raw)
    assert out["rate"] == pytest.approx(2.5)
    assert "time" not in out


def test_playback_state_message_round_trip():
    raw = codec.encode_message({
        "type": MSG_PLAYBACK_STATE,
        "time": 9.0,
        "playing": True,
        "rate": 1.5,
        "leader_client_id": "c1",
    })
    out = codec.message_to_dict(raw)
    assert out["time"] == pytest.approx(9.0)
    assert out["playing"] is True
    assert out["rate"] == pytest.approx(1.5)
    assert out["leader_client_id"] == "c1"
