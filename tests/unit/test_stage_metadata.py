"""Tests for stage-level metadata replication.

Covers:
  * Codec round-trip of the SetStageMetadata event (sparse + full).
  * hello_ok carries a stage_metadata snapshot when the server stage has it.
  * Apply pipeline writes only the keys present in the event.
  * Emitter detects pseudo-root metadata changes and emits one event.
"""

from __future__ import annotations

import pytest

try:
    from pxr import Usd, UsdGeom  # noqa: F401
    from openusdconnect import codec
    from openusdconnect.emitter import NoticeEmitter
    from openusdconnect.event_apply import apply_events

    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")

from openusdconnect.protocol_constants import K_SET_STAGE_METADATA, MSG_HELLO_OK, MSG_TXN


def _round_trip(msg):
    raw = codec.encode_message(msg)
    return codec.message_to_dict(raw)


def test_set_stage_metadata_full_round_trip():
    ev = {
        "k": K_SET_STAGE_METADATA,
        "timeCodesPerSecond": 24.0,
        "framesPerSecond": 30.0,
        "startTimeCode": 0.0,
        "endTimeCode": 240.0,
        "metersPerUnit": 0.01,
        "upAxis": "Y",
    }
    out = _round_trip({"type": MSG_TXN, "client_id": "c", "events": [ev]})
    assert out["events"][0] == {
        "k": K_SET_STAGE_METADATA,
        "timeCodesPerSecond": 24.0,
        "framesPerSecond": 30.0,
        "startTimeCode": 0.0,
        "endTimeCode": 240.0,
        "metersPerUnit": 0.01,
        "upAxis": "Y",
    }


def test_set_stage_metadata_sparse_round_trip():
    ev = {"k": K_SET_STAGE_METADATA, "endTimeCode": 96.0}
    out = _round_trip({"type": MSG_TXN, "client_id": "c", "events": [ev]})
    decoded = out["events"][0]
    assert decoded == {"k": K_SET_STAGE_METADATA, "endTimeCode": 96.0}


def test_hello_ok_carries_stage_metadata():
    msg = {
        "type": MSG_HELLO_OK,
        "token": "tok",
        "stage_metadata": {"timeCodesPerSecond": 24.0, "upAxis": "Z"},
    }
    out = _round_trip(msg)
    assert out["stage_metadata"] == {"timeCodesPerSecond": 24.0, "upAxis": "Z"}


def test_hello_ok_without_stage_metadata():
    out = _round_trip({"type": MSG_HELLO_OK, "token": "x"})
    assert "stage_metadata" not in out


def test_apply_set_stage_metadata_only_touches_present_keys():
    stage = Usd.Stage.CreateInMemory()
    stage.SetTimeCodesPerSecond(12.0)
    UsdGeom.SetStageUpAxis(stage, "Y")

    apply_events(stage, [{"k": K_SET_STAGE_METADATA, "framesPerSecond": 30.0}])

    # timeCodesPerSecond and upAxis must be unchanged.
    assert stage.GetTimeCodesPerSecond() == 12.0
    assert UsdGeom.GetStageUpAxis(stage) == "Y"
    assert stage.GetFramesPerSecond() == 30.0


def test_apply_set_stage_metadata_writes_all_fields():
    stage = Usd.Stage.CreateInMemory()
    apply_events(stage, [{
        "k": K_SET_STAGE_METADATA,
        "timeCodesPerSecond": 24.0,
        "framesPerSecond": 30.0,
        "startTimeCode": 0.0,
        "endTimeCode": 48.0,
        "metersPerUnit": 0.01,
        "upAxis": "Z",
    }])
    assert stage.GetTimeCodesPerSecond() == 24.0
    assert stage.GetFramesPerSecond() == 30.0
    assert stage.GetStartTimeCode() == 0.0
    assert stage.GetEndTimeCode() == 48.0
    assert UsdGeom.GetStageMetersPerUnit(stage) == 0.01
    assert UsdGeom.GetStageUpAxis(stage) == "Z"


def test_emitter_detects_stage_metadata_change():
    stage = Usd.Stage.CreateInMemory()
    emitter = NoticeEmitter(stage)
    stage.SetTimeCodesPerSecond(24.0)
    stage.SetFramesPerSecond(30.0)
    UsdGeom.SetStageUpAxis(stage, "Y")

    events = emitter.build_events_for_dirty()
    meta_events = [e for e in events if e.get("k") == K_SET_STAGE_METADATA]
    assert len(meta_events) == 1
    payload = meta_events[0]
    assert payload["timeCodesPerSecond"] == 24.0
    assert payload["framesPerSecond"] == 30.0
    assert payload["upAxis"] == "Y"

    # Second cycle with no changes → no new event.
    assert emitter.build_events_for_dirty() == []
