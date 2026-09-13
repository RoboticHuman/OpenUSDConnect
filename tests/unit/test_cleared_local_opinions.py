"""Cleared edit-target opinions must also disappear on the receiving layer."""

import pytest
from pxr import Sdf, Usd, UsdGeom, UsdShade

from openusdconnect.emitter import NoticeEmitter
from openusdconnect.event_apply import apply_events
from openusdconnect.protocol_constants import K_SET_SDF_SPEC_FIELDS


@pytest.fixture(params=[False, True], ids=["unmasked", "masked"])
def stage_pair(request):
    base = Usd.Stage.CreateInMemory()
    UsdGeom.Camera.Define(base, "/Camera").GetFocalLengthAttr().Set(24.0)
    base.DefinePrim("/Model", "Xform").SetInstanceable(True)
    shader = UsdShade.Shader.Define(base, "/Shader")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.5)

    strong = Sdf.Layer.CreateAnonymous("strong")
    if request.param:
        strong_stage = Usd.Stage.Open(base.GetRootLayer())
        strong_stage.GetSessionLayer().subLayerPaths = [strong.identifier]
        strong_stage.SetEditTarget(Usd.EditTarget(strong))
        strong_stage.GetAttributeAtPath("/Camera.focalLength").Set(50.0)
        strong_stage.GetPrimAtPath("/Model").SetInstanceable(True)
        strong_stage.GetAttributeAtPath("/Shader.inputs:roughness").Set(0.8)

    stages = []
    for name in ("source", "target"):
        local = Sdf.Layer.CreateAnonymous(name)
        session = Sdf.Layer.CreateAnonymous(name + "-session")
        session.subLayerPaths = [strong.identifier, local.identifier]
        stage = Usd.Stage.Open(base.GetRootLayer(), session)
        stage.SetEditTarget(Usd.EditTarget(local))
        stages.append(stage)
    yield stages


def _assert_field_cleared(source, target, events, spec_path, field):
    assert any(
        event["k"] == K_SET_SDF_SPEC_FIELDS
        and event["spec_path"] == spec_path
        and field in event["fields"]
        for event in events
    ), events
    apply_events(target, events)
    for stage in (source, target):
        spec = stage.GetEditTarget().GetLayer().GetObjectAtPath(spec_path)
        assert spec is None or not spec.HasInfo(field)


@pytest.mark.parametrize("batch_resync", [False, True], ids=["alone", "with-resync"])
@pytest.mark.parametrize(
    "spec_path,value",
    [("/Camera.focalLength", 35.0), ("/Shader.inputs:roughness", 0.2)],
    ids=["camera", "shader-input"],
)
def test_clear_default_after_snapshot(stage_pair, spec_path, value, batch_resync):
    source, target = stage_pair
    target_layer = target.GetEditTarget().GetLayer()
    attr = source.GetAttributeAtPath(spec_path)
    prim = attr.GetPrim()
    attr.Set(value)
    source.GetAttributeAtPath("/Camera.horizontalAperture").Set(30.0)
    emitter = NoticeEmitter(source)
    try:
        apply_events(target, emitter.snapshot_events())
        assert target_layer.GetAttributeAtPath(spec_path).HasDefaultValue()

        with Sdf.ChangeBlock():
            attr.Clear()
            if batch_resync:
                prim.SetInstanceable(False)
        events = emitter.build_events_for_dirty()
        _assert_field_cleared(source, target, events, spec_path, "default")
        assert target.GetAttributeAtPath(spec_path).Get() == attr.Get()
        assert target_layer.GetAttributeAtPath("/Camera.horizontalAperture").default == 30.0
        assert emitter.build_events_for_dirty() == []

        attr.Set(value)
        apply_events(target, emitter.build_events_for_dirty())
        assert target_layer.GetAttributeAtPath(spec_path).default == pytest.approx(value)
    finally:
        emitter.cleanup()


def test_clear_instanceable_after_snapshot(stage_pair):
    source, target = stage_pair
    target_layer = target.GetEditTarget().GetLayer()
    prim = source.GetPrimAtPath("/Model")
    prim.SetInstanceable(False)
    emitter = NoticeEmitter(source)
    try:
        apply_events(target, emitter.snapshot_events())
        assert target_layer.GetPrimAtPath("/Model").HasInfo("instanceable")

        prim.ClearInstanceable()
        events = emitter.build_events_for_dirty()
        _assert_field_cleared(source, target, events, "/Model", "instanceable")
        assert target.GetPrimAtPath("/Model").IsInstanceable() == prim.IsInstanceable()
        assert emitter.build_events_for_dirty() == []

        prim.SetInstanceable(False)
        apply_events(target, emitter.build_events_for_dirty())
        target_spec = target_layer.GetPrimAtPath("/Model")
        assert target_spec.HasInfo("instanceable")
        assert target_spec.GetInfo("instanceable") is False
    finally:
        emitter.cleanup()


def test_clear_camera_default_after_cache_seed(stage_pair):
    source, target = stage_pair
    attr = source.GetAttributeAtPath("/Camera.focalLength")
    attr.Set(35.0)
    target.GetEditTarget().GetLayer().TransferContent(source.GetEditTarget().GetLayer())
    emitter = NoticeEmitter(source)
    try:
        emitter.seed_prim_cache(source, "/Camera")
        attr.Clear()
        _assert_field_cleared(
            source, target, emitter.build_events_for_dirty(), "/Camera.focalLength", "default"
        )
    finally:
        emitter.cleanup()


def test_clear_camera_default_in_variant(stage_pair):
    source, target = stage_pair
    camera = source.GetPrimAtPath("/Camera")
    variants = camera.GetVariantSets().AddVariantSet("lens")
    variants.AddVariant("wide")
    variants.SetVariantSelection("wide")
    source.SetEditTarget(variants.GetVariantEditTarget())
    attr = camera.GetAttribute("focalLength")
    attr.Set(35.0)
    spec_path = str(source.GetEditTarget().MapToSpecPath(attr.GetPath()))
    target.GetEditTarget().GetLayer().TransferContent(source.GetEditTarget().GetLayer())
    emitter = NoticeEmitter(source)
    try:
        emitter.snapshot_events()
        assert target.GetEditTarget().GetLayer().GetAttributeAtPath(spec_path).HasDefaultValue()
        attr.Clear()
        _assert_field_cleared(
            source, target, emitter.build_events_for_dirty(), spec_path, "default"
        )
        assert target.GetAttributeAtPath("/Camera.focalLength").Get() == attr.Get()
    finally:
        emitter.cleanup()
