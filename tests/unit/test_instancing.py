"""Instancing replication: set_instanceable + set_point_instancer.

Covers the codec round-trip (int64 ids, quaternion layout, fields bitmask,
nullable time), the apply pipeline (create-before-value ordering,
ChangeBlock safety, instance-proxy guard), the emitter (first-encounter
full read, scoped partial reads, quath conversion, prototype-path
filtering, per-time events, invalidate suppression), and the adapters.
"""

from __future__ import annotations

import numpy as np
import pytest

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, Vt  # noqa: F401

    from openusdconnect import codec
    from openusdconnect.adapters import MockAdapter, UsdStageAdapter
    from openusdconnect.emitter import NoticeEmitter
    from openusdconnect.event_apply import apply_events
    from openusdconnect.protocol_constants import (
        K_ENSURE_PRIM,
        K_SET_GPRIM_ATTRS,
        K_SET_INSTANCEABLE,
        K_SET_POINT_INSTANCER,
        MSG_TXN,
    )

    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")

WXYZ = np.array([[1, 0, 0, 0], [0.5, 0.5, 0.5, 0.5]], dtype=np.float32)


def _pi_event(prim="/World/PI", **extra):
    ev = {
        "k": K_SET_POINT_INSTANCER,
        "prim": prim,
        "fields": ["prototypes", "proto_indices", "positions", "orientations", "ids"],
        "prototypes": ["/Protos/A", "/Protos/B"],
        "proto_indices": np.array([0, 1], dtype=np.int32),
        "positions": np.array([[0, 0, 0], [1, 2, 3]], dtype=np.float32),
        "orientations": WXYZ,
        "ids": np.array([2**40, 2**40 + 1], dtype=np.int64),
    }
    ev.update(extra)
    return ev


def _round_trip(events, *, numpy_arrays=True):
    raw = codec.encode_message({"type": MSG_TXN, "client_id": "t", "events": events})
    return codec.message_to_dict(bytes(raw), numpy_arrays=numpy_arrays)["events"]


def _make_instance(stage, path="/World/Tree", proto="/Protos/Tree"):
    proto_prim = stage.DefinePrim(proto, "Xform")
    UsdGeom.Sphere.Define(stage, proto + "/Geo")
    inst = stage.DefinePrim(path, "Xform")
    inst.GetReferences().AddInternalReference(proto)
    inst.SetInstanceable(True)
    return proto_prim, inst


# ---------------------------------------------------------------------------
# Mapping-table drift guard
# ---------------------------------------------------------------------------


def test_pi_mapping_tables_agree():
    """The PI field tables in constants, codec, and emitter must name the
    same wire fields; a field added to one module but not the others fails
    here instead of silently not replicating."""
    from openusdconnect.codec import _PI_ARRAYS, _PI_BITS
    from openusdconnect.emitter import _PI_WIRE_TO_USD
    from openusdconnect.protocol_constants import POINT_INSTANCER_FIELDS
    from openusdconnect.usd_state import POINT_INSTANCER_USD_TO_WIRE

    arrays = set(POINT_INSTANCER_FIELDS) - {"prototypes"}
    # inactive_ids is prim metadata: a wire array, but not a USD attribute,
    # so it stays out of the attr-name maps and the sample path.
    attrs = arrays - {"inactive_ids"}
    assert set(_PI_BITS) == set(POINT_INSTANCER_FIELDS)
    assert set(_PI_ARRAYS) == arrays
    assert set(POINT_INSTANCER_USD_TO_WIRE.values()) == attrs
    assert set(_PI_WIRE_TO_USD) == attrs
    assert set(_PI_WIRE_TO_USD.values()) <= set(POINT_INSTANCER_USD_TO_WIRE)


# ---------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------


def test_codec_point_instancer_round_trip_numpy():
    out = _round_trip([_pi_event(time=24.0)])[0]
    assert out["fields"] == ["prototypes", "proto_indices", "positions", "orientations", "ids"]
    assert out["prototypes"] == ["/Protos/A", "/Protos/B"]
    assert out["positions"].shape == (2, 3)
    assert out["orientations"].shape == (2, 4)
    assert np.array_equal(out["orientations"], WXYZ)
    assert out["time"] == pytest.approx(24.0)


def test_codec_int64_ids_survive_beyond_int32():
    out = _round_trip([_pi_event()])[0]
    assert out["ids"].dtype == np.int64
    assert list(out["ids"]) == [2**40, 2**40 + 1]


def test_codec_list_path_nests_rows():
    out = _round_trip([_pi_event()], numpy_arrays=False)[0]
    assert out["positions"] == [[0.0, 0.0, 0.0], [1.0, 2.0, 3.0]]
    assert out["ids"] == [2**40, 2**40 + 1]


def test_codec_absent_vs_explicitly_empty():
    ev = {
        "k": K_SET_POINT_INSTANCER,
        "prim": "/PI",
        "fields": ["positions"],
        "positions": np.zeros((0, 3), dtype=np.float32),
    }
    out = _round_trip([ev])[0]
    assert out["fields"] == ["positions"]
    assert out["positions"].shape == (0, 3)
    assert "scales" not in out
    assert "time" not in out


def test_codec_instanceable_round_trip():
    ev = {"k": K_SET_INSTANCEABLE, "prim": "/World/T", "instanceable": False}
    out = _round_trip([ev])[0]
    assert out == ev


def test_codec_motion_and_visibility_arrays_round_trip():
    """velocities/accelerations/angularVelocities/invisibleIds end-to-end:
    wire round-trip plus authoring on a stage."""
    ev = {
        "k": K_SET_POINT_INSTANCER,
        "prim": "/PI",
        "fields": ["velocities", "accelerations", "angular_velocities", "invisible_ids"],
        "velocities": np.array([[0, 1, 0], [0, 2, 0]], dtype=np.float32),
        "accelerations": np.array([[0, 0, 1], [0, 0, 2]], dtype=np.float32),
        "angular_velocities": np.array([[3, 0, 0], [4, 0, 0]], dtype=np.float32),
        "invisible_ids": np.array([2**40 + 7], dtype=np.int64),
    }
    out = _round_trip([
        {"k": K_ENSURE_PRIM, "prim": "/PI", "typeName": "PointInstancer"}, ev,
    ])
    stage = Usd.Stage.CreateInMemory()
    apply_events(stage, out)
    pi = UsdGeom.PointInstancer(stage.GetPrimAtPath("/PI"))
    assert [tuple(v) for v in pi.GetVelocitiesAttr().Get()] == [(0, 1, 0), (0, 2, 0)]
    assert [tuple(v) for v in pi.GetAccelerationsAttr().Get()] == [(0, 0, 1), (0, 0, 2)]
    assert [tuple(v) for v in pi.GetAngularVelocitiesAttr().Get()] == [(3, 0, 0), (4, 0, 0)]
    assert list(pi.GetInvisibleIdsAttr().Get()) == [2**40 + 7]


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def test_apply_instanceable_composes_instance():
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/Protos/A", "Xform")
    apply_events(stage, [
        {"k": K_ENSURE_PRIM, "prim": "/World/T", "typeName": "Xform"},
        {"k": "set_reference", "prim": "/World/T",
         "refs": [{"asset_path": "", "prim_path": "/Protos/A"}]},
        {"k": K_SET_INSTANCEABLE, "prim": "/World/T", "instanceable": True},
    ])
    prim = stage.GetPrimAtPath("/World/T")
    assert prim.IsInstanceable()
    assert prim.IsInstance()
    apply_events(stage, [{"k": K_SET_INSTANCEABLE, "prim": "/World/T", "instanceable": False}])
    assert not prim.IsInstance()


def test_apply_ensure_and_point_instancer_in_one_batch():
    """The create pass defines the prim before the value run's ChangeBlock."""
    stage = Usd.Stage.CreateInMemory()
    apply_events(stage, [
        _pi_event(),
        {"k": K_ENSURE_PRIM, "prim": "/World", "typeName": "Xform"},
        {"k": K_ENSURE_PRIM, "prim": "/World/PI", "typeName": "PointInstancer"},
    ])
    pi = UsdGeom.PointInstancer(stage.GetPrimAtPath("/World/PI"))
    assert [str(t) for t in pi.GetPrototypesRel().GetTargets()] == ["/Protos/A", "/Protos/B"]
    assert list(pi.GetIdsAttr().Get()) == [2**40, 2**40 + 1]
    q = pi.GetOrientationsfAttr().Get()[1]
    assert q.GetReal() == pytest.approx(0.5)
    assert tuple(q.GetImaginary()) == pytest.approx((0.5, 0.5, 0.5))
    assert not pi.GetOrientationsAttr().IsAuthored()


def test_apply_point_instancer_time_sample():
    stage = Usd.Stage.CreateInMemory()
    apply_events(stage, [
        {"k": K_ENSURE_PRIM, "prim": "/PI", "typeName": "PointInstancer"},
        {"k": K_SET_POINT_INSTANCER, "prim": "/PI", "fields": ["positions"],
         "positions": np.array([[1, 1, 1]], dtype=np.float32)},
        {"k": K_SET_POINT_INSTANCER, "prim": "/PI", "fields": ["positions"],
         "positions": np.array([[9, 9, 9]], dtype=np.float32), "time": 24.0},
    ])
    attr = UsdGeom.PointInstancer(stage.GetPrimAtPath("/PI")).GetPositionsAttr()
    assert tuple(attr.Get()[0]) == (1.0, 1.0, 1.0)
    assert tuple(attr.Get(Usd.TimeCode(24.0))[0]) == (9.0, 9.0, 9.0)


def test_apply_point_instancer_missing_prim_is_noop():
    stage = Usd.Stage.CreateInMemory()
    apply_events(stage, [_pi_event(prim="/Nowhere/PI")])
    assert not stage.GetPrimAtPath("/Nowhere/PI")


def test_apply_drops_events_under_instances():
    """Edits beneath an instance are discarded by composition per the spec;
    the guard skips them instead of surfacing Tf coding errors."""
    stage = Usd.Stage.CreateInMemory()
    _make_instance(stage)
    geo = stage.GetPrimAtPath("/World/Tree/Geo")
    assert geo.IsInstanceProxy()
    apply_events(stage, [
        {"k": "set_visibility", "prim": "/World/Tree/Geo", "visible": False},
        {"k": "set_xform_trs", "prim": "/World/Tree/Geo", "fields": ["t"],
         "t": [5.0, 0.0, 0.0]},
        {"k": K_SET_GPRIM_ATTRS, "prim": "/World/Tree/Geo", "attrs": {"radius": 9.0}},
        {"k": K_ENSURE_PRIM, "prim": "/World/Tree/Geo/Child", "typeName": "Xform"},
    ])
    assert UsdGeom.Sphere(geo).GetRadiusAttr().Get() == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------


def _author_pi(stage, path="/World/PI", quat_half=False):
    pi = UsdGeom.PointInstancer(stage.DefinePrim(path, "PointInstancer"))
    pi.CreatePrototypesRel().SetTargets([Sdf.Path("/Protos/A")])
    pi.CreateProtoIndicesAttr().Set(Vt.IntArray([0, 0]))
    pi.CreatePositionsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(0, 0, 0), Gf.Vec3f(1, 1, 1)]))
    if quat_half:
        pi.CreateOrientationsAttr().Set(
            Vt.QuathArray([Gf.Quath(1, 0, 0, 0), Gf.Quath(0.5, 0.5, 0.5, 0.5)])
        )
    else:
        pi.CreateOrientationsfAttr().Set(Vt.QuatfArray.FromNumpy(WXYZ[:, [1, 2, 3, 0]]))
    pi.CreateIdsAttr().Set(Vt.Int64Array.FromNumpy(np.array([2**40, 2**40 + 1], np.int64)))
    return pi


def test_emitter_first_encounter_emits_full_point_instancer():
    stage = Usd.Stage.CreateInMemory()
    emitter = NoticeEmitter(stage)
    stage.DefinePrim("/Protos/A", "Xform")
    _author_pi(stage, quat_half=True)
    events = emitter.build_events_for_dirty()
    pi_evs = [e for e in events if e["k"] == K_SET_POINT_INSTANCER]
    assert len(pi_evs) == 1
    ev = pi_evs[0]
    assert sorted(ev["fields"]) == [
        "ids", "orientations", "positions", "proto_indices", "prototypes",
    ]
    # quath source converts to float32 wxyz rows on the wire
    assert np.allclose(ev["orientations"][1], [0.5, 0.5, 0.5, 0.5])
    assert np.asarray(ev["ids"]).dtype == np.int64
    # ensure_prim carries the typed schema for the receive side
    ensure = [e for e in events if e["k"] == K_ENSURE_PRIM and e["prim"] == "/World/PI"]
    assert ensure and ensure[0]["typeName"] == "PointInstancer"
    assert emitter.build_events_for_dirty() == []


def test_emitter_scoped_partial_diff_after_edit():
    stage = Usd.Stage.CreateInMemory()
    emitter = NoticeEmitter(stage)
    stage.DefinePrim("/Protos/A", "Xform")
    pi = _author_pi(stage)
    emitter.build_events_for_dirty()

    pi.GetPositionsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(5, 5, 5), Gf.Vec3f(6, 6, 6)]))
    out = emitter.build_events_for_dirty()
    pi_evs = [e for e in out if e["k"] == K_SET_POINT_INSTANCER]
    assert len(pi_evs) == 1
    assert pi_evs[0]["fields"] == ["positions"]
    # a later full-state cycle diffs against the merged cache cleanly
    emitter.mark_dirty("/World/PI")
    emitter._notice_resynced_prims.add("/World/PI")
    again = emitter.build_events_for_dirty()
    assert [e for e in again if e["k"] == K_SET_POINT_INSTANCER] == []


def test_emitter_pi_attrs_do_not_ride_gprim_events():
    stage = Usd.Stage.CreateInMemory()
    emitter = NoticeEmitter(stage)
    stage.DefinePrim("/Protos/A", "Xform")
    _author_pi(stage)
    events = emitter.build_events_for_dirty()
    gprim = [e for e in events if e["k"] == K_SET_GPRIM_ATTRS and e["prim"] == "/World/PI"]
    assert gprim == []


def test_emitter_points_prim_keeps_generic_attrs():
    """UsdGeomPoints shares velocities/ids names with PointInstancer; the
    per-prim exclusion must not strip them from non-instancer prims."""
    stage = Usd.Stage.CreateInMemory()
    emitter = NoticeEmitter(stage)
    pts = UsdGeom.Points.Define(stage, "/Cloud")
    pts.GetPointsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(0, 0, 0)]))
    pts.GetVelocitiesAttr().Set(Vt.Vec3fArray([Gf.Vec3f(1, 2, 3)]))
    pts.GetIdsAttr().Set(Vt.Int64Array.FromNumpy(np.array([7], np.int64)))
    events = emitter.build_events_for_dirty()
    attrs: set = set()
    for e in events:
        if e["k"] == K_SET_GPRIM_ATTRS and e["prim"] == "/Cloud":
            attrs.update(e["attrs"])
    assert {"points", "velocities", "ids"} <= attrs


def test_emitter_instanceable_flag_without_prototype_leak():
    stage = Usd.Stage.CreateInMemory()
    emitter = NoticeEmitter(stage)
    _make_instance(stage)
    events = emitter.build_events_for_dirty()
    inst = [e for e in events if e["k"] == K_SET_INSTANCEABLE]
    assert inst == [{"k": K_SET_INSTANCEABLE, "prim": "/World/Tree", "instanceable": True}]
    assert not [e for e in events if str(e.get("prim", "")).startswith("/__Prototype")]
    assert emitter.build_events_for_dirty() == []

    stage.GetPrimAtPath("/World/Tree").SetInstanceable(False)
    out = emitter.build_events_for_dirty()
    inst = [e for e in out if e["k"] == K_SET_INSTANCEABLE]
    assert inst == [{"k": K_SET_INSTANCEABLE, "prim": "/World/Tree", "instanceable": False}]


def test_emitter_animated_point_instancer_per_time_events():
    stage = Usd.Stage.CreateInMemory()
    emitter = NoticeEmitter(stage)
    stage.DefinePrim("/Protos/A", "Xform")
    pi = _author_pi(stage)
    pos_attr = pi.GetPositionsAttr()
    pos_attr.Set(Vt.Vec3fArray([Gf.Vec3f(1, 0, 0), Gf.Vec3f(2, 0, 0)]), Usd.TimeCode(1.0))
    pos_attr.Set(Vt.Vec3fArray([Gf.Vec3f(3, 0, 0), Gf.Vec3f(4, 0, 0)]), Usd.TimeCode(2.0))
    events = emitter.build_events_for_dirty()
    timed = [e for e in events if e["k"] == K_SET_POINT_INSTANCER and "time" in e]
    assert {e["time"] for e in timed} == {1.0, 2.0}
    for e in timed:
        assert e["fields"] == ["positions"]
        assert "prototypes" not in e
    assert emitter.build_events_for_dirty() == []

    # changing one sample re-emits only that time
    pi.GetPositionsAttr().Set(
        Vt.Vec3fArray([Gf.Vec3f(9, 0, 0), Gf.Vec3f(9, 0, 0)]), Usd.TimeCode(2.0),
    )
    out = emitter.build_events_for_dirty()
    timed = [e for e in out if e["k"] == K_SET_POINT_INSTANCER and "time" in e]
    assert [e["time"] for e in timed] == [2.0]


def test_emitter_animated_orientations_round_trip():
    """Time-sampled quaternions: the sample diff reads the layer's xyzw
    layout, the wire carries wxyz, and the applier restores xyzw into
    orientationsf at each sample."""
    src = Usd.Stage.CreateInMemory()
    emitter = NoticeEmitter(src)
    src.DefinePrim("/Protos/A", "Xform")
    pi = _author_pi(src)
    q1 = Vt.QuatfArray([Gf.Quatf(0.0, Gf.Vec3f(1, 0, 0)), Gf.Quatf(1.0, Gf.Vec3f(0, 0, 0))])
    q2 = Vt.QuatfArray([Gf.Quatf(0.0, Gf.Vec3f(0, 1, 0)), Gf.Quatf(1.0, Gf.Vec3f(0, 0, 0))])
    pi.GetOrientationsfAttr().Set(q1, Usd.TimeCode(1.0))
    pi.GetOrientationsfAttr().Set(q2, Usd.TimeCode(2.0))

    events = emitter.build_events_for_dirty()
    timed = {
        e["time"]: e for e in events
        if e["k"] == K_SET_POINT_INSTANCER and "time" in e
    }
    assert set(timed) == {1.0, 2.0}
    assert np.allclose(timed[1.0]["orientations"][0], [0.0, 1.0, 0.0, 0.0])
    assert np.allclose(timed[2.0]["orientations"][0], [0.0, 0.0, 1.0, 0.0])
    assert emitter.build_events_for_dirty() == []

    dst = Usd.Stage.CreateInMemory()
    apply_events(dst, _round_trip(events))
    out = UsdGeom.PointInstancer(dst.GetPrimAtPath("/World/PI"))
    got = out.GetOrientationsfAttr().Get(Usd.TimeCode(1.0))
    assert tuple(got[0].GetImaginary()) == pytest.approx((1.0, 0.0, 0.0))

    # Receive side: applying + invalidating the same timed events on a
    # fresh peer must not echo them back.
    peer = Usd.Stage.CreateInMemory()
    peer_emitter = NoticeEmitter(peer)
    decoded = _round_trip(events)
    with peer_emitter.suppressed():
        apply_events(peer, decoded)
    peer_emitter.invalidate_for_events(decoded)
    echo = [
        e for e in peer_emitter.build_events_for_dirty()
        if e["k"] == K_SET_POINT_INSTANCER
    ]
    assert echo == []


def test_emitter_emits_explicitly_empty_arrays():
    """Zero-instance frames (sim with no particles) author empty arrays;
    the wire must carry explicitly-empty, not drop the field."""
    stage = Usd.Stage.CreateInMemory()
    emitter = NoticeEmitter(stage)
    stage.DefinePrim("/Protos/A", "Xform")
    pi = _author_pi(stage)
    emitter.build_events_for_dirty()

    pi.GetPositionsAttr().Set(Vt.Vec3fArray())
    out = emitter.build_events_for_dirty()
    pi_evs = [e for e in out if e["k"] == K_SET_POINT_INSTANCER]
    assert len(pi_evs) == 1
    assert pi_evs[0]["fields"] == ["positions"]
    assert np.asarray(pi_evs[0]["positions"]).shape == (0, 3)

    dst = Usd.Stage.CreateInMemory()
    apply_events(dst, _round_trip([
        {"k": K_ENSURE_PRIM, "prim": "/World/PI", "typeName": "PointInstancer"},
        *pi_evs,
    ]))
    got = UsdGeom.PointInstancer(dst.GetPrimAtPath("/World/PI")).GetPositionsAttr().Get()
    assert got is not None and len(got) == 0


def test_inactive_ids_round_trip_and_mask():
    """inactiveIds prim metadata: emitted as the resolved id set, applied as
    an explicit list op, and reflected in ComputeMaskAtTime alongside ids."""
    src = Usd.Stage.CreateInMemory()
    emitter = NoticeEmitter(src)
    src.DefinePrim("/Protos/A", "Xform")
    pi = _author_pi(src)
    emitter.build_events_for_dirty()

    pi.DeactivateId(2**40)
    out = emitter.build_events_for_dirty()
    pi_evs = [e for e in out if e["k"] == K_SET_POINT_INSTANCER]
    assert len(pi_evs) == 1
    assert pi_evs[0]["fields"] == ["inactive_ids"]
    assert pi_evs[0]["inactive_ids"] == [2**40]
    assert emitter.build_events_for_dirty() == []

    dst = Usd.Stage.CreateInMemory()
    apply_events(dst, _round_trip([
        {"k": K_ENSURE_PRIM, "prim": "/World/PI", "typeName": "PointInstancer"},
        _pi_event(prim="/World/PI"),
        *pi_evs,
    ]))
    pi_dst = UsdGeom.PointInstancer(dst.GetPrimAtPath("/World/PI"))
    default = Usd.TimeCode.Default()
    assert list(pi_dst.ComputeMaskAtTime(default)) == [False, True]

    # Reactivation replicates as an explicit empty set.
    pi.ActivateAllIds()
    out = emitter.build_events_for_dirty()
    pi_evs = [e for e in out if e["k"] == K_SET_POINT_INSTANCER]
    assert pi_evs and pi_evs[0]["inactive_ids"] == []
    apply_events(dst, _round_trip(pi_evs))
    # An empty mask is USD's "nothing masked" convention.
    assert list(pi_dst.ComputeMaskAtTime(default)) in ([], [True, True])


def test_inactive_ids_with_same_cycle_attr_edit():
    """A metadata change and an array edit in one notice cycle must both
    emit; the scoped read covers the metadata via the recorded field name."""
    stage = Usd.Stage.CreateInMemory()
    emitter = NoticeEmitter(stage)
    stage.DefinePrim("/Protos/A", "Xform")
    pi = _author_pi(stage)
    emitter.build_events_for_dirty()

    pi.GetPositionsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(9, 9, 9), Gf.Vec3f(8, 8, 8)]))
    pi.DeactivateId(2**40 + 1)
    out = emitter.build_events_for_dirty()
    fields: set = set()
    for e in out:
        if e["k"] == K_SET_POINT_INSTANCER:
            fields.update(e["fields"])
    assert fields == {"positions", "inactive_ids"}
    assert emitter.build_events_for_dirty() == []


def test_emitter_invalidate_suppresses_instancing_echo():
    stage = Usd.Stage.CreateInMemory()
    emitter = NoticeEmitter(stage)
    remote = [
        {"k": K_ENSURE_PRIM, "prim": "/Protos", "typeName": "Scope"},
        {"k": K_ENSURE_PRIM, "prim": "/Protos/A", "typeName": "Xform"},
        {"k": K_ENSURE_PRIM, "prim": "/World", "typeName": "Xform"},
        {"k": K_ENSURE_PRIM, "prim": "/World/PI", "typeName": "PointInstancer"},
        _pi_event(),
        {"k": K_SET_POINT_INSTANCER, "prim": "/World/PI", "fields": ["positions"],
         "positions": np.array([[7, 7, 7], [8, 8, 8]], dtype=np.float32), "time": 12.0},
        {"k": K_ENSURE_PRIM, "prim": "/World/T", "typeName": "Xform"},
        {"k": "set_reference", "prim": "/World/T",
         "refs": [{"asset_path": "", "prim_path": "/Protos/A"}]},
        {"k": K_SET_INSTANCEABLE, "prim": "/World/T", "instanceable": True},
    ]
    with emitter.suppressed():
        apply_events(stage, remote)
    emitter.invalidate_for_events(remote)
    out = emitter.build_events_for_dirty()
    echo = [e for e in out if e["k"] in (K_SET_POINT_INSTANCER, K_SET_INSTANCEABLE)]
    assert echo == []


# ---------------------------------------------------------------------------
# Adapters + round trip
# ---------------------------------------------------------------------------


def test_mock_adapter_records_instancing():
    adapter = MockAdapter()
    adapter.apply_event({"k": K_ENSURE_PRIM, "prim": "/PI", "typeName": "PointInstancer"})
    adapter.apply_event(_pi_event(prim="/PI"))
    adapter.apply_event({"k": K_SET_POINT_INSTANCER, "prim": "/PI", "fields": ["positions"],
                         "positions": np.zeros((2, 3), np.float32), "time": 5.0})
    adapter.apply_event({"k": K_ENSURE_PRIM, "prim": "/T", "typeName": "Xform"})
    adapter.apply_event({"k": K_SET_INSTANCEABLE, "prim": "/T", "instanceable": True})
    pi_state = adapter.get_prim("/PI")["point_instancer"]
    assert pi_state["prototypes"] == ["/Protos/A", "/Protos/B"]
    assert 5.0 in adapter.get_prim("/PI")["point_instancer_samples"]
    assert adapter.get_prim("/T")["instanceable"] is True


def test_round_trip_emitter_codec_apply():
    src = Usd.Stage.CreateInMemory()
    emitter = NoticeEmitter(src)
    _make_instance(src, path="/World/Tree", proto="/Protos/Tree")
    src.DefinePrim("/Protos/A", "Xform")
    pi = _author_pi(src, quat_half=True)
    pi.GetPositionsAttr().Set(
        Vt.Vec3fArray([Gf.Vec3f(7, 7, 7), Gf.Vec3f(8, 8, 8)]), Usd.TimeCode(48.0),
    )

    decoded = _round_trip(emitter.build_events_for_dirty())
    dst = Usd.Stage.CreateInMemory()
    apply_events(dst, decoded)

    tree = dst.GetPrimAtPath("/World/Tree")
    assert tree.IsInstance()
    pi2 = UsdGeom.PointInstancer(dst.GetPrimAtPath("/World/PI"))
    assert [str(t) for t in pi2.GetPrototypesRel().GetTargets()] == ["/Protos/A"]
    assert list(pi2.GetIdsAttr().Get()) == [2**40, 2**40 + 1]
    q = pi2.GetOrientationsfAttr().Get()[1]
    assert q.GetReal() == pytest.approx(0.5)
    assert tuple(pi2.GetPositionsAttr().Get(Usd.TimeCode(48.0))[0]) == (7.0, 7.0, 7.0)


def test_round_trip_survives_shuffled_order():
    events = [
        _pi_event(),
        {"k": K_SET_INSTANCEABLE, "prim": "/World/T", "instanceable": True},
        {"k": "set_reference", "prim": "/World/T",
         "refs": [{"asset_path": "", "prim_path": "/Protos/A"}]},
        {"k": K_ENSURE_PRIM, "prim": "/World/PI", "typeName": "PointInstancer"},
        {"k": K_ENSURE_PRIM, "prim": "/World/T", "typeName": "Xform"},
        {"k": K_ENSURE_PRIM, "prim": "/Protos/A", "typeName": "Xform"},
        {"k": K_ENSURE_PRIM, "prim": "/World", "typeName": "Xform"},
        {"k": K_ENSURE_PRIM, "prim": "/Protos", "typeName": "Scope"},
    ]
    stage = Usd.Stage.CreateInMemory()
    apply_events(stage, _round_trip(events))
    assert stage.GetPrimAtPath("/World/T").IsInstance()
    pi = UsdGeom.PointInstancer(stage.GetPrimAtPath("/World/PI"))
    assert len(pi.GetPositionsAttr().Get()) == 2


def test_usd_stage_adapter_applies_instancing():
    stage = Usd.Stage.CreateInMemory()
    adapter = UsdStageAdapter(stage)
    for ev in [
        {"k": K_ENSURE_PRIM, "prim": "/Protos/A", "typeName": "Xform"},
        {"k": K_ENSURE_PRIM, "prim": "/PI", "typeName": "PointInstancer"},
        _pi_event(prim="/PI"),
        {"k": K_ENSURE_PRIM, "prim": "/T", "typeName": "Xform"},
        {"k": "set_reference", "prim": "/T",
         "refs": [{"asset_path": "", "prim_path": "/Protos/A"}]},
        {"k": K_SET_INSTANCEABLE, "prim": "/T", "instanceable": True},
    ]:
        adapter.apply_event(ev)
    assert stage.GetPrimAtPath("/T").IsInstance()
    pi = UsdGeom.PointInstancer(stage.GetPrimAtPath("/PI"))
    assert list(pi.GetIdsAttr().Get()) == [2**40, 2**40 + 1]
