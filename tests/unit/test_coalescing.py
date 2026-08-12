from __future__ import annotations

from pxr import Gf, Sdf, Usd, UsdGeom

from openusdconnect.coalescing import merge_latest_transform_events
from openusdconnect.event_apply import apply_events


def _trs(prim: str, fields: list[str], *, time=None, **values):
    event = {"k": "set_xform_trs", "prim": prim, "fields": fields, **values}
    if time is not None:
        event["time"] = time
    return event


def _stage_with_canonical_ops():
    stage = Usd.Stage.CreateInMemory()
    prim = UsdGeom.Xform.Define(stage, "/World/Cube").GetPrim()
    xformable = UsdGeom.Xformable(prim)
    xformable.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble)
    xformable.AddOrientOp(UsdGeom.XformOp.PrecisionFloat)
    xformable.AddScaleOp(UsdGeom.XformOp.PrecisionDouble)
    return stage


def test_latest_transform_merge_preserves_list_identity_and_partial_fields():
    pending = [_trs("/World/Cube", ["t"], t=[1, 2, 3])]

    result = merge_latest_transform_events(
        pending,
        [
            _trs("/World/Cube", ["r"], r=[1, 0, 0, 0]),
            _trs("/World/Cube", ["t", "s"], t=[4, 5, 6], s=[2, 2, 2]),
        ],
    )

    assert result is pending
    assert pending == [
        _trs(
            "/World/Cube",
            ["t", "r", "s"],
            t=[4, 5, 6],
            r=[1, 0, 0, 0],
            s=[2, 2, 2],
        )
    ]


def test_distinct_prims_and_time_samples_are_not_collapsed():
    pending = [_trs("/World/A", ["t"], time=1.0, t=[1, 0, 0])]

    merge_latest_transform_events(
        pending,
        [
            _trs("/World/A", ["t"], time=2.0, t=[2, 0, 0]),
            _trs("/World/B", ["t"], time=1.0, t=[3, 0, 0]),
            _trs("/World/A", ["t"], time=1.0, t=[4, 0, 0]),
        ],
    )

    assert len(pending) == 3
    assert pending[0]["t"] == [4, 0, 0]
    assert [(event["prim"], event["time"]) for event in pending] == [
        ("/World/A", 1.0),
        ("/World/A", 2.0),
        ("/World/B", 1.0),
    ]


def test_non_transform_event_is_an_ordering_barrier():
    pending = [_trs("/World/Cube", ["t"], t=[1, 0, 0])]

    merge_latest_transform_events(
        pending,
        [
            {"k": "ensure_xform_ops", "prim": "/World/Cube"},
            _trs("/World/Cube", ["t"], t=[2, 0, 0]),
        ],
    )

    assert [event["k"] for event in pending] == [
        "set_xform_trs",
        "ensure_xform_ops",
        "set_xform_trs",
    ]


def test_collapsed_events_author_the_same_openusd_layer_and_composed_transform():
    sequential = _stage_with_canonical_ops()
    collapsed = _stage_with_canonical_ops()
    events = [
        _trs("/World/Cube", ["t"], t=[1, 2, 3]),
        _trs("/World/Cube", ["r"], r=[1, 0, 0, 0]),
        _trs("/World/Cube", ["t", "s"], t=[4, 5, 6], s=[2, 2, 2]),
    ]
    merged: list[dict] = []
    merge_latest_transform_events(merged, events)

    apply_events(sequential, events)
    apply_events(collapsed, merged)

    assert sequential.GetRootLayer().ExportToString() == collapsed.GetRootLayer().ExportToString()
    sequential_xf = UsdGeom.Xformable(sequential.GetPrimAtPath("/World/Cube"))
    collapsed_xf = UsdGeom.Xformable(collapsed.GetPrimAtPath("/World/Cube"))
    assert sequential_xf.GetLocalTransformation() == collapsed_xf.GetLocalTransformation()


def test_same_time_latest_wins_while_distinct_openusd_samples_survive():
    stage = _stage_with_canonical_ops()
    events = [
        _trs("/World/Cube", ["t"], time=1.0, t=[1, 0, 0]),
        _trs("/World/Cube", ["t"], time=2.0, t=[2, 0, 0]),
        _trs("/World/Cube", ["t"], time=1.0, t=[3, 0, 0]),
    ]
    merged: list[dict] = []
    merge_latest_transform_events(merged, events)

    apply_events(stage, merged)

    attr = stage.GetAttributeAtPath("/World/Cube.xformOp:translate")
    assert attr.GetTimeSamples() == [1.0, 2.0]
    assert attr.Get(1.0) == Gf.Vec3d(3, 0, 0)
    assert attr.Get(2.0) == Gf.Vec3d(2, 0, 0)


def test_edit_targets_retain_independent_authored_opinions():
    root = Sdf.Layer.CreateAnonymous("root")
    weak = Sdf.Layer.CreateAnonymous("weak")
    strong = Sdf.Layer.CreateAnonymous("strong")
    root.subLayerPaths = [strong.identifier, weak.identifier]
    stage = Usd.Stage.Open(root)
    stage.SetEditTarget(weak)
    prim = UsdGeom.Xform.Define(stage, "/World/Cube").GetPrim()
    UsdGeom.Xformable(prim).AddTranslateOp(UsdGeom.XformOp.PrecisionDouble)
    apply_events(stage, [_trs("/World/Cube", ["t"], t=[1, 0, 0])])

    stage.SetEditTarget(strong)
    stage.OverridePrim("/World/Cube")
    apply_events(stage, [_trs("/World/Cube", ["t"], t=[2, 0, 0])])

    assert "(1, 0, 0)" in weak.ExportToString()
    assert "(2, 0, 0)" in strong.ExportToString()
    strong.Clear()
    assert stage.GetAttributeAtPath("/World/Cube.xformOp:translate").Get() == Gf.Vec3d(1, 0, 0)
