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
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdShade  # noqa: F401

    from openusdconnect import codec
    from openusdconnect.emitter import NoticeEmitter
    from openusdconnect.event_apply import apply_events

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
        {"k": K_SET_XFORM_TRS, "prim": "/World/Cube",
         "fields": ["t"], "t": [0.0, 0.0, 0.0]},
        {"k": K_SET_XFORM_TRS, "prim": "/World/Cube",
         "fields": ["t"], "t": [10.0, 0.0, 0.0], "time": 24.0},
        {"k": K_SET_XFORM_TRS, "prim": "/World/Cube",
         "fields": ["t"], "t": [20.0, 0.0, 0.0], "time": 48.0},
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


def test_emitter_emits_single_sample_attrs():
    """An attr with exactly ONE time sample and no default opinion must
    still replicate. Usd value resolution returns the held sample at every
    numeric time, but ``Get(Default())`` is None and
    ``ValueMightBeTimeVarying()`` is certain-False below two samples — a
    gate based on either drops the attr entirely. Covers all four
    time-aware kinds.
    """
    stage = Usd.Stage.CreateInMemory()
    emitter = NoticeEmitter(stage)

    cube = UsdGeom.Cube.Define(stage, "/Cube")
    xf = UsdGeom.Xformable(cube)
    t_op = xf.AddTranslateOp(precision=UsdGeom.XformOp.PrecisionDouble)
    t_op.Set(Gf.Vec3d(10, 0, 0), Usd.TimeCode(24.0))
    cube.GetSizeAttr().Set(2.0, Usd.TimeCode(24.0))
    cube.GetVisibilityAttr().Set("invisible", Usd.TimeCode(24.0))
    light = UsdLux.SphereLight.Define(stage, "/Lamp")
    light.GetIntensityAttr().Set(500.0, Usd.TimeCode(24.0))

    events = emitter.build_events_for_dirty()
    by_kind = {
        e["k"]: e for e in events if e.get("time") is not None
    }
    assert by_kind[K_SET_XFORM_TRS]["t"] == pytest.approx([10.0, 0.0, 0.0])
    assert by_kind[K_SET_GPRIM_ATTRS]["attrs"]["size"] == pytest.approx(2.0)
    assert by_kind[K_SET_VISIBILITY]["visible"] is False
    assert by_kind[K_SET_CONNECTABLE_INPUT]["inputs"]["intensity"] == pytest.approx(500.0)
    assert all(e["time"] == 24.0 for e in by_kind.values())

    # Second cycle: nothing changed, zero events.
    assert emitter.build_events_for_dirty() == []


def test_emitter_emits_orient_quaternion_samples():
    """Regression: orient (Gf.Quatf) time samples used to be silently dropped
    because _usd_value_to_python returned None for quaternions. Now it
    returns [w, x, y, z] and the emit path produces one SetXformTRS event
    per (orient sample) carrying the quat in the wire form.
    """
    stage = Usd.Stage.CreateInMemory()
    emitter = NoticeEmitter(stage)
    cube = UsdGeom.Cube.Define(stage, "/Cube")
    xf = UsdGeom.Xformable(cube)
    o_op = xf.AddOrientOp(precision=UsdGeom.XformOp.PrecisionFloat)
    # 90-degree rotation around X at frame 24.
    import math
    half = math.sin(math.radians(45.0))
    o_op.Set(Gf.Quatf(math.cos(math.radians(45.0)), Gf.Vec3f(half, 0.0, 0.0)),
             Usd.TimeCode(24.0))
    o_op.Set(Gf.Quatf(1.0, Gf.Vec3f(0.0, 0.0, 0.0)), Usd.TimeCode(48.0))

    events = emitter.build_events_for_dirty()
    sampled = [
        e for e in events
        if e.get("k") == K_SET_XFORM_TRS
        and e.get("time") is not None
        and e.get("fields") == ["r"]
    ]
    assert len(sampled) == 2
    by_time = {e["time"]: e["r"] for e in sampled}
    assert set(by_time.keys()) == {24.0, 48.0}
    # Wire form is [w, x, y, z], not None / 4x4 matrix.
    assert len(by_time[24.0]) == 4
    assert by_time[24.0][0] == pytest.approx(math.cos(math.radians(45.0)))
    assert by_time[24.0][1] == pytest.approx(half)
    assert by_time[48.0] == pytest.approx([1.0, 0.0, 0.0, 0.0])

    # Round-trip through codec to confirm the wire format survives.
    raw = codec.encode_message({
        "type": MSG_TXN, "client_id": "c", "events": [sampled[0]],
    })
    decoded = codec.message_to_dict(raw)["events"][0]
    assert decoded["fields"] == ["r"]
    assert decoded["r"] == pytest.approx(sampled[0]["r"])


def test_snapshot_includes_samples_alongside_other_attrs():
    """Full-scan cycles must reach transform samples even when the prim has
    other authored attrs; the gprim expansion is filter-scoped and must not
    bound the sample paths."""
    stage = Usd.Stage.CreateInMemory()
    cube = UsdGeom.Cube.Define(stage, "/Cube")
    cube.GetSizeAttr().Set(2.0)
    xf = UsdGeom.Xformable(cube)
    t_op = xf.AddTranslateOp(precision=UsdGeom.XformOp.PrecisionDouble)
    t_op.Set(Gf.Vec3d(1, 0, 0), Usd.TimeCode(1.0))
    t_op.Set(Gf.Vec3d(2, 0, 0), Usd.TimeCode(2.0))
    emitter = NoticeEmitter(stage)
    events = emitter.snapshot_events()
    timed = [e for e in events if e.get("k") == K_SET_XFORM_TRS and e.get("time") is not None]
    assert {e["time"] for e in timed} == {1.0, 2.0}


def test_decompose_batch_recomposition_fidelity():
    """Property test for the vectorized decompose: across adversarial matrix
    families, recomposing canonical S*R*T from (t, quat, s) must reproduce
    the input within float tolerance. Shear and zero-scale axes are
    excluded: TRS cannot represent them (in the Gf path either)."""
    import numpy as np

    from openusdconnect.xform_decompose import decompose_trs_batch

    rng = np.random.default_rng(11)

    def quats(n, near180=False):
        v = rng.normal(size=(n, 4))
        if near180:
            v[:, 0] = rng.normal(scale=1e-7, size=n)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    def build(n, scales=None, near180=False):
        mats = []
        for i, q in enumerate(quats(n, near180)):
            m = Gf.Matrix4d().SetRotate(Gf.Quatd(*[float(c) for c in q]))
            if scales is not None:
                m = Gf.Matrix4d().SetScale(Gf.Vec3d(*[float(c) for c in scales[i]])) * m
            m.SetTranslateOnly(Gf.Vec3d(*[float(c) for c in rng.normal(scale=10, size=3)]))
            mats.append(np.array(m))
        return np.stack(mats)

    n = 100
    families = (
        build(n),
        build(n, scales=np.exp(rng.uniform(np.log(0.01), np.log(100), (n, 3)))),
        build(n, scales=np.column_stack([-2 * np.ones(n), np.ones(n), 3 * np.ones(n)])),
        build(n, near180=True),
        build(n, scales=np.full((n, 3), 1e-6)),
        build(n, scales=np.full((n, 3), 1e6)),
    )
    for mats in families:
        ts, qs, ss = decompose_trs_batch(mats)
        for i in range(len(mats)):
            back = (
                Gf.Matrix4d().SetScale(Gf.Vec3d(*[float(v) for v in ss[i]]))
                * Gf.Matrix4d().SetRotate(Gf.Quatd(*[float(v) for v in qs[i]]))
                * Gf.Matrix4d().SetTranslate(Gf.Vec3d(*[float(v) for v in ts[i]]))
            )
            a = mats[i]
            b = np.array(back)
            scale = max(1.0, np.abs(a).max())
            assert np.abs(a - b).max() / scale < 1e-12, f"matrix {i}: {np.abs(a - b).max()}"


def test_emitter_matrix_op_samples_decompose():
    """Animated non-canonical ops (matrix transform) replicate as decomposed
    full-TRS sample events, and receivers reproduce the local transform."""
    import math

    stage = Usd.Stage.CreateInMemory()
    emitter = NoticeEmitter(stage)
    rig = UsdGeom.Xform.Define(stage, "/Rig")
    op = rig.AddTransformOp()
    m1 = Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(0, 0, 1), 90))
    m1.SetTranslateOnly(Gf.Vec3d(1, 2, 3))
    m2 = Gf.Matrix4d(1.0)
    m2.SetTranslateOnly(Gf.Vec3d(4, 5, 6))
    op.Set(m1, Usd.TimeCode(1.0))
    op.Set(m2, Usd.TimeCode(2.0))

    events = emitter.build_events_for_dirty()
    timed = {
        e["time"]: e for e in events
        if e.get("k") == K_SET_XFORM_TRS and e.get("time") is not None
    }
    assert set(timed) == {1.0, 2.0}
    assert sorted(timed[1.0]["fields"]) == ["r", "s", "t"]
    assert timed[1.0]["t"] == pytest.approx([1.0, 2.0, 3.0])
    half = math.sin(math.radians(45.0))
    assert timed[1.0]["r"] == pytest.approx(
        [math.cos(math.radians(45.0)), 0.0, 0.0, half], abs=1e-6
    )
    assert emitter.build_events_for_dirty() == []

    # One changed sample re-emits only that time.
    m2.SetTranslateOnly(Gf.Vec3d(7, 7, 7))
    op.Set(m2, Usd.TimeCode(2.0))
    out = emitter.build_events_for_dirty()
    timed2 = [e for e in out if e.get("time") is not None]
    assert [e["time"] for e in timed2] == [2.0]
    assert timed2[0]["t"] == pytest.approx([7.0, 7.0, 7.0])

    # Receiver reproduces the composed local transform at each sample.
    dst = Usd.Stage.CreateInMemory()
    apply_events(dst, NoticeEmitter(stage).snapshot_events())
    for t in (1.0, 2.0):
        for label, st in (("src", stage), ("dst", dst)):
            assert st.GetPrimAtPath("/Rig").IsValid(), label
        m_src = UsdGeom.Xformable(stage.GetPrimAtPath("/Rig")).GetLocalTransformation(Usd.TimeCode(t))
        m_dst = UsdGeom.Xformable(dst.GetPrimAtPath("/Rig")).GetLocalTransformation(Usd.TimeCode(t))
        if isinstance(m_src, tuple):
            m_src = m_src[0]
        if isinstance(m_dst, tuple):
            m_dst = m_dst[0]
        for row in range(4):
            for col in range(4):
                assert abs(m_src[row][col] - m_dst[row][col]) < 1e-5, f"t={t} [{row}][{col}]"


def test_emitter_mixed_stack_matrix_samples_decompose():
    """A sampled matrix op sandwiched between static ops must reproduce the
    composed local transform, locking the op composition order."""
    stage = Usd.Stage.CreateInMemory()
    rig = UsdGeom.Xform.Define(stage, "/Rig")
    xf = UsdGeom.Xformable(rig)
    t_op = xf.AddTranslateOp()
    t_op.Set(Gf.Vec3d(10, 0, 0))
    m_op = rig.AddTransformOp()
    s_op = xf.AddScaleOp()
    s_op.Set(Gf.Vec3f(2, 2, 2))
    for frame, deg in ((1.0, 30.0), (2.0, 120.0)):
        m = Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(0, 1, 0), deg))
        m.SetTranslateOnly(Gf.Vec3d(0, frame, 0))
        m_op.Set(m, Usd.TimeCode(frame))

    emitter = NoticeEmitter(stage)
    events = emitter.snapshot_events()
    dst = Usd.Stage.CreateInMemory()
    apply_events(dst, events)
    for t in (1.0, 2.0):
        m_src = xf.GetLocalTransformation(Usd.TimeCode(t))
        m_dst = UsdGeom.Xformable(dst.GetPrimAtPath("/Rig")).GetLocalTransformation(Usd.TimeCode(t))
        if isinstance(m_src, tuple):
            m_src = m_src[0]
        if isinstance(m_dst, tuple):
            m_dst = m_dst[0]
        for row in range(4):
            for col in range(4):
                assert abs(m_src[row][col] - m_dst[row][col]) < 1e-5, f"t={t} [{row}][{col}]"


def _assert_local_transform_parity(src_stage, dst_stage, prim_path, times):
    src_xf = UsdGeom.Xformable(src_stage.GetPrimAtPath(prim_path))
    dst_xf = UsdGeom.Xformable(dst_stage.GetPrimAtPath(prim_path))
    for t in times:
        m_src = src_xf.GetLocalTransformation(Usd.TimeCode(t))
        m_dst = dst_xf.GetLocalTransformation(Usd.TimeCode(t))
        if isinstance(m_src, tuple):
            m_src = m_src[0]
        if isinstance(m_dst, tuple):
            m_dst = m_dst[0]
        for row in range(4):
            for col in range(4):
                assert abs(m_src[row][col] - m_dst[row][col]) < 1e-5, f"t={t} [{row}][{col}]"


def test_emitter_euler_op_samples_decompose():
    """A sampled rotateXYZ op is non-canonical and non-matrix: it takes the
    per-time resolution fallback, not the sandwich."""
    stage = Usd.Stage.CreateInMemory()
    rig = UsdGeom.Xform.Define(stage, "/Rig")
    xf = UsdGeom.Xformable(rig)
    t_op = xf.AddTranslateOp()
    t_op.Set(Gf.Vec3d(5, 0, 0))
    r_op = xf.AddRotateXYZOp()
    r_op.Set(Gf.Vec3f(0, 0, 30), Usd.TimeCode(1.0))
    r_op.Set(Gf.Vec3f(0, 45, 0), Usd.TimeCode(2.0))

    events = NoticeEmitter(stage).snapshot_events()
    timed = [e for e in events if e.get("k") == K_SET_XFORM_TRS and e.get("time") is not None]
    assert {e["time"] for e in timed} == {1.0, 2.0}
    dst = Usd.Stage.CreateInMemory()
    apply_events(dst, events)
    _assert_local_transform_parity(stage, dst, "/Rig", (1.0, 2.0))


def test_emitter_multiple_sampled_ops_decompose():
    """Two independently sampled ops emit decomposed TRS at the union of
    their sample times (the single-sampled-op sandwich does not apply)."""
    stage = Usd.Stage.CreateInMemory()
    rig = UsdGeom.Xform.Define(stage, "/Rig")
    xf = UsdGeom.Xformable(rig)
    t_op = xf.AddTranslateOp()
    t_op.Set(Gf.Vec3d(1, 0, 0), Usd.TimeCode(1.0))
    m_op = rig.AddTransformOp()
    m = Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(0, 0, 1), 90))
    m_op.Set(m, Usd.TimeCode(2.0))

    events = NoticeEmitter(stage).snapshot_events()
    timed = [e for e in events if e.get("k") == K_SET_XFORM_TRS and e.get("time") is not None]
    assert {e["time"] for e in timed} == {1.0, 2.0}
    for e in timed:
        assert sorted(e["fields"]) == ["r", "s", "t"]
    dst = Usd.Stage.CreateInMemory()
    apply_events(dst, events)
    _assert_local_transform_parity(stage, dst, "/Rig", (1.0, 2.0))


def test_emitter_animated_pivot_pair_decompose():
    """An animated pivot (translate:pivot + its !invert! pair around a
    static orient) shares one sampled attr across two ops; both routes
    through the fallback and the inverse op resolves per time."""
    import math

    stage = Usd.Stage.CreateInMemory()
    rig = UsdGeom.Xform.Define(stage, "/Rig")
    xf = UsdGeom.Xformable(rig)
    pivot = xf.AddTranslateOp(opSuffix="pivot")
    half = math.sin(math.radians(45.0))
    o_op = xf.AddOrientOp()
    o_op.Set(Gf.Quatf(math.cos(math.radians(45.0)), Gf.Vec3f(0, 0, half)))
    xf.AddTranslateOp(opSuffix="pivot", isInverseOp=True)
    pivot.Set(Gf.Vec3d(1, 0, 0), Usd.TimeCode(1.0))
    pivot.Set(Gf.Vec3d(0, 2, 0), Usd.TimeCode(2.0))

    events = NoticeEmitter(stage).snapshot_events()
    timed = [e for e in events if e.get("k") == K_SET_XFORM_TRS and e.get("time") is not None]
    assert {e["time"] for e in timed} == {1.0, 2.0}
    dst = Usd.Stage.CreateInMemory()
    apply_events(dst, events)
    _assert_local_transform_parity(stage, dst, "/Rig", (1.0, 2.0))


def test_invalidate_set_gprim_attrs_refreshes_default_time_cache():
    """After a remote default-time gprim_attrs apply, the per-attr cache
    must reflect the new value so a local edit back to it produces a diff
    against the right baseline (not the stale pre-remote value).
    """
    stage = Usd.Stage.CreateInMemory()
    cube = UsdGeom.Cube.Define(stage, "/Cube")
    cube.GetSizeAttr().Set(1.0)
    emitter = NoticeEmitter(stage)
    # Initial cycle to seed the cache with the authored size=1.0.
    emitter.build_events_for_dirty()

    # Simulate a remote default-time edit: server changed size to 5.0.
    ev = {"k": K_SET_GPRIM_ATTRS, "prim": "/Cube", "attrs": {"size": 5.0}}
    with emitter.suppressed():
        apply_events(stage, [ev])
    emitter.invalidate_for_event(ev)

    # The cache should now reflect 5.0. A subsequent local edit BACK to
    # the value the cache held before the remote apply (1.0) is genuinely
    # a change vs the server's 5.0, so it must emit.
    cube.GetSizeAttr().Set(1.0)
    out = emitter.build_events_for_dirty()
    gprim_events = [
        e for e in out
        if e.get("k") == K_SET_GPRIM_ATTRS and e.get("time") is None
    ]
    assert any(e["attrs"].get("size") == 1.0 for e in gprim_events)


def test_emitter_layer_scoped_time_samples():
    """Per-client-layer mode: emit only samples authored on the stage's
    edit target, never samples from a stronger sibling layer (another
    client). The composed view falls afoul of USD's "strongest layer with
    samples wins the time domain" rule — both shadowing the weaker
    client's own samples AND leaking the stronger client's into the
    weaker emitter.
    """
    stage = Usd.Stage.CreateInMemory()
    cube = UsdGeom.Cube.Define(stage, "/Cube")

    # Stack two anonymous layers as session sublayers — layer_a is stronger.
    layer_b = Sdf.Layer.CreateAnonymous("clientB")
    layer_a = Sdf.Layer.CreateAnonymous("clientA")
    stage.GetSessionLayer().subLayerPaths.insert(0, layer_b.identifier)
    stage.GetSessionLayer().subLayerPaths.insert(0, layer_a.identifier)

    # Client A authors samples on the stronger layer.
    stage.SetEditTarget(Usd.EditTarget(layer_a))
    cube.GetSizeAttr().Set(1.0, Usd.TimeCode(0.0))
    cube.GetSizeAttr().Set(2.0, Usd.TimeCode(10.0))

    # Client B's emitter starts up with layer_b as edit target.
    stage.SetEditTarget(Usd.EditTarget(layer_b))
    emitter = NoticeEmitter(stage)
    # Consume any initial structural events so the next cycle is clean.
    emitter.build_events_for_dirty()

    # Client B authors a sample of its own.
    cube.GetSizeAttr().Set(3.0, Usd.TimeCode(20.0))

    events = emitter.build_events_for_dirty()
    sampled = [
        e for e in events
        if e.get("k") == K_SET_GPRIM_ATTRS and e.get("time") is not None
    ]
    sample_times = {e["time"] for e in sampled}
    # Must include B's actual keyframe (would be shadowed in the composed
    # view since A's layer has samples and is stronger).
    assert 20.0 in sample_times
    # Must NOT include A's samples (leaked from composed view in the bug).
    assert 0.0 not in sample_times
    assert 10.0 not in sample_times


def test_default_edit_does_not_reread_sample_tables(monkeypatch):
    """A default-time edit on a keyframed attr must not re-read the attr's
    sample table; the per-attr notice classifies it as default-only.
    """
    from openusdconnect import emitter as emitter_mod

    stage = Usd.Stage.CreateInMemory()
    emitter = NoticeEmitter(stage)
    cube = UsdGeom.Cube.Define(stage, "/Cube")
    xf = UsdGeom.Xformable(cube)
    t_op = xf.AddTranslateOp(precision=UsdGeom.XformOp.PrecisionDouble)
    t_op.Set(Gf.Vec3d(10, 0, 0), Usd.TimeCode(24.0))
    t_op.Set(Gf.Vec3d(20, 0, 0), Usd.TimeCode(48.0))
    emitter.build_events_for_dirty()

    calls: list[str] = []
    real = emitter_mod._diff_time_samples

    def _spy(attr, cached, layer=None):
        calls.append(attr.GetName())
        return real(attr, cached, layer)

    monkeypatch.setattr(emitter_mod, "_diff_time_samples", _spy)

    # Default-time write: emits a TRS diff but reads no sample tables.
    t_op.Set(Gf.Vec3d(1, 2, 3))
    events = emitter.build_events_for_dirty()
    assert calls == []
    default_trs = [
        e for e in events
        if e.get("k") == K_SET_XFORM_TRS and e.get("time") is None
    ]
    assert len(default_trs) == 1

    # Sample write: the table is re-read and only the changed key emits.
    t_op.Set(Gf.Vec3d(30, 0, 0), Usd.TimeCode(48.0))
    events = emitter.build_events_for_dirty()
    assert "xformOp:translate" in calls
    sampled = [e for e in events if e.get("time") == 48.0]
    assert len(sampled) == 1
    assert sampled[0]["t"] == pytest.approx([30.0, 0.0, 0.0])


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
