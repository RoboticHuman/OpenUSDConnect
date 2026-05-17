"""Tests for time-sample replication on the four value-event kinds.

Covers:
  * Codec round-trip with ``time`` present vs absent.
  * Wire-cost check: ``time`` absent ⇒ byte-identical to a baseline that
    never sets the optional field (verifies "0 payload bytes when unset").
  * Apply pipeline writes the sample at the right ``Usd.TimeCode``.
  * Emitter detects time samples authored on the stage and emits one event
    per (attr, time); a second cycle with no changes emits zero events.
"""

from __future__ import annotations

import pytest

try:
    from pxr import Gf, Usd, UsdGeom, UsdLux, UsdShade  # noqa: F401
    from openusdconnect import codec
    from openusdconnect.event_apply import apply_events
    from openusdconnect.emitter import NoticeEmitter

    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")

from openusdconnect.protocol_constants import (
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_SET_CONNECTABLE_INPUT,
    K_SET_GPRIM_ATTRS,
    K_SET_VISIBILITY,
    K_SET_XFORM_TRS,
    MSG_TXN,
)


def _round_trip(ev: dict) -> dict:
    msg = {"type": MSG_TXN, "client_id": "c", "events": [ev]}
    raw = codec.encode_message(msg)
    return codec.message_to_dict(raw)["events"][0]


# ---------------------------------------------------------------------------
# Codec round-trip
# ---------------------------------------------------------------------------


def test_xform_trs_time_round_trip():
    ev = {"k": K_SET_XFORM_TRS, "prim": "/A", "fields": ["t"], "t": [1.0, 2.0, 3.0], "time": 12.5}
    out = _round_trip(ev)
    assert out["time"] == pytest.approx(12.5)
    assert out["t"] == pytest.approx([1.0, 2.0, 3.0])


def test_xform_trs_no_time_round_trip():
    ev = {"k": K_SET_XFORM_TRS, "prim": "/A", "fields": ["t"], "t": [1.0, 2.0, 3.0]}
    out = _round_trip(ev)
    assert "time" not in out


def test_visibility_time_round_trip():
    ev = {"k": K_SET_VISIBILITY, "prim": "/A", "visible": False, "time": 7.0}
    out = _round_trip(ev)
    assert out["visible"] is False
    assert out["time"] == pytest.approx(7.0)


def test_gprim_attrs_time_round_trip():
    ev = {
        "k": K_SET_GPRIM_ATTRS,
        "prim": "/A",
        "attrs": {"radius": 2.5},
        "time": 4.0,
    }
    out = _round_trip(ev)
    assert out["time"] == pytest.approx(4.0)
    assert out["attrs"] == {"radius": pytest.approx(2.5)}


def test_connectable_input_time_round_trip():
    ev = {
        "k": K_SET_CONNECTABLE_INPUT,
        "prim": "/M/S",
        "info_id": "",
        "inputs": {"intensity": 100.0},
        "input_types": {"intensity": "Float"},
        "time": 10.0,
    }
    out = _round_trip(ev)
    assert out["time"] == pytest.approx(10.0)


def test_time_absent_round_trips_without_time_key():
    """time=None must NOT round-trip as time=0.0 (FB optional scalar check)."""
    ev = {"k": K_SET_XFORM_TRS, "prim": "/A", "fields": ["t"], "t": [0.0, 0.0, 0.0]}
    out = _round_trip(ev)
    assert "time" not in out


def test_wire_cost_no_time_is_baseline():
    """time=None encoding must be byte-identical to a baseline encoding.

    Both must skip the FB optional slot entirely — proves the cost claim.
    """
    no_time = {
        "k": K_SET_XFORM_TRS, "prim": "/A", "fields": ["t"], "t": [1.0, 2.0, 3.0],
    }
    explicit_none = dict(no_time)
    explicit_none["time"] = None
    a = codec.encode_message({"type": MSG_TXN, "client_id": "c", "events": [no_time]})
    b = codec.encode_message({"type": MSG_TXN, "client_id": "c", "events": [explicit_none]})
    assert a == b


# ---------------------------------------------------------------------------
# Apply pipeline
# ---------------------------------------------------------------------------


def _fresh_stage_with_cube():
    stage = Usd.Stage.CreateInMemory()
    apply_events(stage, [
        {"k": K_ENSURE_PRIM, "prim": "/World", "typeName": "Xform"},
        {"k": K_ENSURE_PRIM, "prim": "/World/Cube", "typeName": "Cube"},
        {"k": K_ENSURE_XFORM_OPS, "prim": "/World/Cube"},
    ])
    return stage


def test_apply_time_sampled_trs():
    stage = _fresh_stage_with_cube()
    apply_events(stage, [
        {"k": K_SET_XFORM_TRS, "prim": "/World/Cube", "fields": ["t"], "t": [0.0, 0.0, 0.0]},
        {"k": K_SET_XFORM_TRS, "prim": "/World/Cube", "fields": ["t"], "t": [10.0, 0.0, 0.0], "time": 24.0},
        {"k": K_SET_XFORM_TRS, "prim": "/World/Cube", "fields": ["t"], "t": [20.0, 0.0, 0.0], "time": 48.0},
    ])
    cube = stage.GetPrimAtPath("/World/Cube")
    xf = UsdGeom.Xformable(cube)
    for op in xf.GetOrderedXformOps():
        if "translate" in op.GetName():
            samples = op.GetAttr().GetTimeSamples()
            assert samples == [24.0, 48.0]
            assert tuple(op.GetAttr().Get(24.0)) == (10.0, 0.0, 0.0)
            assert tuple(op.GetAttr().Get(48.0)) == (20.0, 0.0, 0.0)
            # Default-time opinion coexists.
            assert tuple(op.GetAttr().Get(Usd.TimeCode.Default())) == (0.0, 0.0, 0.0)
            return
    pytest.fail("translate op not found")


def test_apply_time_sampled_visibility():
    stage = _fresh_stage_with_cube()
    apply_events(stage, [
        {"k": K_SET_VISIBILITY, "prim": "/World/Cube", "visible": False, "time": 30.0},
    ])
    vis = UsdGeom.Imageable(stage.GetPrimAtPath("/World/Cube")).GetVisibilityAttr()
    assert vis.GetTimeSamples() == [30.0]
    assert str(vis.Get(30.0)) == "invisible"


def test_apply_time_sampled_gprim_attr():
    stage = _fresh_stage_with_cube()
    apply_events(stage, [
        {"k": K_SET_GPRIM_ATTRS, "prim": "/World/Cube", "attrs": {"size": 2.0}, "time": 12.0},
        {"k": K_SET_GPRIM_ATTRS, "prim": "/World/Cube", "attrs": {"size": 3.0}, "time": 24.0},
    ])
    size_attr = stage.GetPrimAtPath("/World/Cube").GetAttribute("size")
    assert size_attr.GetTimeSamples() == [12.0, 24.0]
    assert size_attr.Get(12.0) == pytest.approx(2.0)
    assert size_attr.Get(24.0) == pytest.approx(3.0)


def test_apply_time_sampled_connectable_input():
    stage = Usd.Stage.CreateInMemory()
    UsdLux.SphereLight.Define(stage, "/Light")
    apply_events(stage, [
        {
            "k": K_SET_CONNECTABLE_INPUT, "prim": "/Light", "info_id": "",
            "inputs": {"intensity": 100.0}, "input_types": {"intensity": "float"},
            "time": 0.0,
        },
        {
            "k": K_SET_CONNECTABLE_INPUT, "prim": "/Light", "info_id": "",
            "inputs": {"intensity": 500.0}, "input_types": {"intensity": "float"},
            "time": 24.0,
        },
    ])
    light = UsdLux.SphereLight(stage.GetPrimAtPath("/Light"))
    intensity = light.GetIntensityAttr()
    assert intensity.GetTimeSamples() == [0.0, 24.0]
    assert intensity.Get(0.0) == pytest.approx(100.0)
    assert intensity.Get(24.0) == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# Emitter time-sample detection
# ---------------------------------------------------------------------------


def test_emitter_emits_one_event_per_authored_sample():
    stage = Usd.Stage.CreateInMemory()
    emitter = NoticeEmitter(stage)
    cube = UsdGeom.Cube.Define(stage, "/Cube")
    xf = UsdGeom.Xformable(cube)
    t_op = xf.AddTranslateOp(precision=UsdGeom.XformOp.PrecisionDouble)
    t_op.Set(Gf.Vec3d(10, 0, 0), Usd.TimeCode(24.0))
    t_op.Set(Gf.Vec3d(20, 0, 0), Usd.TimeCode(48.0))

    events = emitter.build_events_for_dirty()
    sampled = [e for e in events if e.get("k") == K_SET_XFORM_TRS and e.get("time") is not None]
    assert len(sampled) == 2
    assert {e["time"] for e in sampled} == {24.0, 48.0}

    # Second cycle: nothing changed, zero events.
    assert emitter.build_events_for_dirty() == []

    # Modify just one sample — exactly one event.
    t_op.Set(Gf.Vec3d(30, 0, 0), Usd.TimeCode(48.0))
    out = emitter.build_events_for_dirty()
    sampled = [e for e in out if e.get("time") == 48.0]
    assert len(sampled) == 1
    assert sampled[0]["t"] == pytest.approx([30.0, 0.0, 0.0])


def test_emitter_first_encounter_includes_all_existing_samples():
    """A pre-keyframed prim must replay every sample on first emit cycle."""
    stage = Usd.Stage.CreateInMemory()
    cube = UsdGeom.Cube.Define(stage, "/Cube")
    cube.GetSizeAttr().Set(1.0, Usd.TimeCode(0.0))
    cube.GetSizeAttr().Set(2.0, Usd.TimeCode(10.0))
    # Emitter is constructed AFTER the samples are authored, so no notices
    # fired for them; first-encounter must pick them up via the cache miss.
    emitter = NoticeEmitter(stage)
    emitter.mark_dirty("/Cube")
    events = emitter.build_events_for_dirty()
    sampled = [
        e for e in events
        if e.get("k") == K_SET_GPRIM_ATTRS and e.get("time") is not None
    ]
    assert {e["time"] for e in sampled} == {0.0, 10.0}


def test_emitter_invalidate_suppresses_reemit_after_remote_apply():
    """A locally-applied remote time-sample event must not be re-emitted."""
    stage = Usd.Stage.CreateInMemory()
    emitter = NoticeEmitter(stage)
    UsdGeom.Cube.Define(stage, "/Cube")
    # First cycle clears initial structural events.
    emitter.build_events_for_dirty()

    # Simulate a remote time-sample event arriving and being applied.
    ev = {
        "k": K_SET_GPRIM_ATTRS, "prim": "/Cube",
        "attrs": {"size": 4.0}, "time": 5.0,
    }
    with emitter.suppressed():
        apply_events(stage, [ev])
    emitter.invalidate_for_event(ev)

    # Next cycle should see no re-emission for that sample.
    events = emitter.build_events_for_dirty()
    sampled = [e for e in events if e.get("time") == 5.0]
    assert sampled == []
