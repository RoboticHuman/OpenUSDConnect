"""Composition-preserving emitter coverage for referenced descendants."""

from __future__ import annotations

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdShade

from openusdconnect.emitter import NoticeEmitter
from openusdconnect.event_apply import apply_events, ensure_canonical_ops
from openusdconnect.protocol_constants import (
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_SET_CONNECTABLE_CONNECTION,
    K_SET_CONNECTABLE_INPUT,
    K_SET_GPRIM_ATTRS,
    K_SET_REFERENCE,
    K_SET_SDF_SPEC_FIELDS,
    K_SET_VARIANT_SELECTIONS,
    K_SET_VISIBILITY,
    K_SET_XFORM_TRS,
)
from openusdconnect.server import UsdSyncServer


def _make_sphere_asset(tmp_path) -> str:
    path = str(tmp_path / "sphere_asset.usda")
    stage = Usd.Stage.CreateNew(path)
    stage.DefinePrim("/Model", "Xform")
    sphere = UsdGeom.Sphere.Define(stage, "/Model/Geom")
    sphere.GetRadiusAttr().Set(1.0)
    _, _, translate, _, _ = ensure_canonical_ops(stage, "/Model/Geom")
    translate.Set(Gf.Vec3d(1.0, 0.0, 0.0))
    stage.GetRootLayer().Save()
    return path


def _make_reference_stage(asset_path: str) -> Usd.Stage:
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/World", "Xform")
    root = stage.DefinePrim("/World/Ref", "Xform")
    root.GetReferences().AddReference(asset_path, "/Model")
    return stage


def _event_kinds(events, prim_path):
    return [event["k"] for event in events if event.get("prim") == prim_path]


def test_snapshot_emits_reference_without_composed_descendant_baseline(tmp_path):
    source = _make_reference_stage(_make_sphere_asset(tmp_path))
    emitter = NoticeEmitter(source)

    events = emitter.snapshot_events()

    assert K_SET_REFERENCE in _event_kinds(events, "/World/Ref")
    assert not [event for event in events if event.get("prim", "").startswith("/World/Ref/")]

    target = Usd.Stage.CreateInMemory()
    apply_events(target, events)
    assert UsdGeom.Sphere(target.GetPrimAtPath("/World/Ref/Geom")).GetRadiusAttr().Get() == 1.0
    assert target.GetRootLayer().GetPrimAtPath("/World/Ref/Geom") is None
    emitter.cleanup()


def test_internal_reference_does_not_flatten_same_layer_source():
    source = Usd.Stage.CreateInMemory()
    source.DefinePrim("/Asset", "Xform")
    UsdGeom.Sphere.Define(source, "/Asset/Geom").GetRadiusAttr().Set(2.0)
    source.DefinePrim("/World", "Xform")
    source.DefinePrim("/World/Ref", "Xform").GetReferences().AddInternalReference("/Asset")
    emitter = NoticeEmitter(source)

    events = emitter.snapshot_events()

    assert K_SET_REFERENCE in _event_kinds(events, "/World/Ref")
    assert not _event_kinds(events, "/World/Ref/Geom")
    target = Usd.Stage.CreateInMemory()
    apply_events(target, events)
    assert UsdGeom.Sphere(target.GetPrimAtPath("/World/Ref/Geom")).GetRadiusAttr().Get() == 2.0
    assert target.GetRootLayer().GetPrimAtPath("/World/Ref/Geom") is None
    emitter.cleanup()


def test_reference_override_on_composed_descendant_roundtrips_as_over(tmp_path):
    source = _make_reference_stage(_make_sphere_asset(tmp_path))
    addon_path = str(tmp_path / "addon.usda")
    addon = Usd.Stage.CreateNew(addon_path)
    addon.DefinePrim("/Addon", "Xform")
    addon.GetRootLayer().Save()
    source.GetPrimAtPath("/World/Ref/Geom").GetReferences().AddReference(
        addon_path,
        "/Addon",
    )
    emitter = NoticeEmitter(source)

    events = emitter.snapshot_events()
    assert K_ENSURE_PRIM not in _event_kinds(events, "/World/Ref/Geom")
    assert K_SET_REFERENCE in _event_kinds(events, "/World/Ref/Geom")

    target = Usd.Stage.CreateInMemory()
    apply_events(target, events)
    target_spec = target.GetRootLayer().GetPrimAtPath("/World/Ref/Geom")
    assert target_spec.specifier == Sdf.SpecifierOver
    assert target_spec.HasInfo("references")
    emitter.cleanup()


def test_composed_descendant_override_roundtrips_as_over(tmp_path):
    source = _make_reference_stage(_make_sphere_asset(tmp_path))
    emitter = NoticeEmitter(source)
    target = Usd.Stage.CreateInMemory()
    apply_events(target, emitter.snapshot_events())

    child = source.GetPrimAtPath("/World/Ref/Geom")
    UsdGeom.Sphere(child).GetRadiusAttr().Set(2.5)
    translate = next(
        op
        for op in UsdGeom.Xformable(child).GetOrderedXformOps()
        if op.GetOpType() == UsdGeom.XformOp.TypeTranslate
    )
    translate.Set(Gf.Vec3d(3.0, 0.0, 0.0))

    events = emitter.build_events_for_dirty()
    assert K_ENSURE_PRIM not in _event_kinds(events, "/World/Ref/Geom")
    assert K_SET_XFORM_TRS in _event_kinds(events, "/World/Ref/Geom")
    assert K_SET_GPRIM_ATTRS in _event_kinds(events, "/World/Ref/Geom")

    apply_events(target, events)
    target_child = target.GetPrimAtPath("/World/Ref/Geom")
    assert UsdGeom.Sphere(target_child).GetRadiusAttr().Get() == 2.5
    matrix = UsdGeom.Xformable(target_child).GetLocalTransformation()
    assert matrix.ExtractTranslation() == Gf.Vec3d(3.0, 0.0, 0.0)
    target_spec = target.GetRootLayer().GetPrimAtPath("/World/Ref/Geom")
    assert target_spec.specifier == Sdf.SpecifierOver
    emitter.cleanup()


def test_removing_local_definition_reveals_referenced_prim(tmp_path):
    source = _make_reference_stage(_make_sphere_asset(tmp_path))
    source.DefinePrim("/World/Ref/Geom", "Cube")
    emitter = NoticeEmitter(source)
    target = Usd.Stage.CreateInMemory()
    apply_events(target, emitter.snapshot_events())
    assert target.GetPrimAtPath("/World/Ref/Geom").GetTypeName() == "Cube"

    source.RemovePrim("/World/Ref/Geom")
    events = emitter.build_events_for_dirty()
    assert {"k": K_DELETE_PRIM, "prim": "/World/Ref/Geom"} in events

    apply_events(target, events)
    assert target.GetPrimAtPath("/World/Ref/Geom").GetTypeName() == "Sphere"
    assert target.GetRootLayer().GetPrimAtPath("/World/Ref/Geom") is None
    emitter.cleanup()


def test_removing_descendant_override_reveals_referenced_value(tmp_path):
    source = _make_reference_stage(_make_sphere_asset(tmp_path))
    emitter = NoticeEmitter(source)
    target = Usd.Stage.CreateInMemory()
    apply_events(target, emitter.snapshot_events())

    child = source.GetPrimAtPath("/World/Ref/Geom")
    UsdGeom.Sphere(child).GetRadiusAttr().Set(2.5)
    apply_events(target, emitter.build_events_for_dirty())
    assert UsdGeom.Sphere(target.GetPrimAtPath("/World/Ref/Geom")).GetRadiusAttr().Get() == 2.5

    child.RemoveProperty("radius")
    clear_events = emitter.build_events_for_dirty()
    assert [
        event
        for event in clear_events
        if event["k"] == K_SET_SDF_SPEC_FIELDS
        and event["spec_path"] == "/World/Ref/Geom.radius"
        and event["removed"]
    ]

    apply_events(target, clear_events)
    assert UsdGeom.Sphere(target.GetPrimAtPath("/World/Ref/Geom")).GetRadiusAttr().Get() == 1.0
    assert target.GetRootLayer().GetPropertyAtPath("/World/Ref/Geom.radius") is None
    emitter.cleanup()


def test_clearing_descendant_xform_value_reveals_referenced_value(tmp_path):
    source = _make_reference_stage(_make_sphere_asset(tmp_path))
    emitter = NoticeEmitter(source)
    target = Usd.Stage.CreateInMemory()
    apply_events(target, emitter.snapshot_events())

    child = source.GetPrimAtPath("/World/Ref/Geom")
    translate = next(
        op
        for op in UsdGeom.Xformable(child).GetOrderedXformOps()
        if op.GetOpType() == UsdGeom.XformOp.TypeTranslate
    )
    translate.Set(Gf.Vec3d(3.0, 0.0, 0.0))
    apply_events(target, emitter.build_events_for_dirty())
    target_xform = UsdGeom.Xformable(target.GetPrimAtPath("/World/Ref/Geom"))
    assert target_xform.GetLocalTransformation().ExtractTranslation() == Gf.Vec3d(
        3.0,
        0.0,
        0.0,
    )

    translate.GetAttr().Clear()
    clear_events = emitter.build_events_for_dirty()
    assert [
        event
        for event in clear_events
        if event["k"] == K_SET_SDF_SPEC_FIELDS
        and event["spec_path"] == "/World/Ref/Geom.xformOp:translate"
        and event["fields"] == ["default"]
    ]

    apply_events(target, clear_events)
    assert target_xform.GetLocalTransformation().ExtractTranslation() == Gf.Vec3d(
        1.0,
        0.0,
        0.0,
    )
    target_translate = target.GetRootLayer().GetPropertyAtPath("/World/Ref/Geom.xformOp:translate")
    assert target_translate and not target_translate.HasInfo("default")
    emitter.cleanup()


def test_schema_value_block_roundtrips_over_shared_base():
    base = Sdf.Layer.CreateAnonymous("value-block-base")
    base_stage = Usd.Stage.Open(base)
    UsdGeom.Sphere.Define(base_stage, "/Sphere").GetRadiusAttr().Set(2.0)

    source_session = Sdf.Layer.CreateAnonymous("value-block-source")
    source = Usd.Stage.Open(base, source_session)
    source.SetEditTarget(Usd.EditTarget(source_session))
    UsdGeom.Sphere(source.GetPrimAtPath("/Sphere")).GetRadiusAttr().Set(Sdf.ValueBlock())
    emitter = NoticeEmitter(source)
    events = emitter.snapshot_events()

    block_events = [
        event
        for event in events
        if event["k"] == K_SET_SDF_SPEC_FIELDS and event["spec_path"] == "/Sphere.radius"
    ]
    assert len(block_events) == 1
    assert block_events[0]["fields"] == ["default"]

    target_session = Sdf.Layer.CreateAnonymous("value-block-target")
    target = Usd.Stage.Open(base, target_session)
    target.SetEditTarget(Usd.EditTarget(target_session))
    apply_events(target, events)
    assert UsdGeom.Sphere(target.GetPrimAtPath("/Sphere")).GetRadiusAttr().Get() is None
    target_spec = target_session.GetPropertyAtPath("/Sphere.radius")
    assert isinstance(target_spec.GetInfo("default"), Sdf.ValueBlock)
    emitter.cleanup()


def test_channel_value_block_uses_sdf_fields_without_value_overwrite():
    base = Sdf.Layer.CreateAnonymous("visibility-block-base")
    base_stage = Usd.Stage.Open(base)
    sphere = UsdGeom.Sphere.Define(base_stage, "/Sphere")
    UsdGeom.Imageable(sphere).GetVisibilityAttr().Set(UsdGeom.Tokens.invisible)

    source_session = Sdf.Layer.CreateAnonymous("visibility-block-source")
    source = Usd.Stage.Open(base, source_session)
    source.SetEditTarget(Usd.EditTarget(source_session))
    emitter = NoticeEmitter(source)
    emitter.snapshot_events()

    source_visibility = UsdGeom.Imageable(source.GetPrimAtPath("/Sphere")).GetVisibilityAttr()
    source_visibility.Set(Sdf.ValueBlock())
    events = emitter.build_events_for_dirty()
    assert not [event for event in events if event["k"] == K_SET_VISIBILITY]
    assert [
        event
        for event in events
        if event["k"] == K_SET_SDF_SPEC_FIELDS
        and event["spec_path"] == "/Sphere.visibility"
        and event["fields"] == ["default"]
    ]

    target_session = Sdf.Layer.CreateAnonymous("visibility-block-target")
    target = Usd.Stage.Open(base, target_session)
    target.SetEditTarget(Usd.EditTarget(target_session))
    apply_events(target, events)
    target_visibility = UsdGeom.Imageable(target.GetPrimAtPath("/Sphere")).GetVisibilityAttr()
    assert target_visibility.Get() is None
    assert isinstance(
        target_session.GetPropertyAtPath("/Sphere.visibility").GetInfo("default"),
        Sdf.ValueBlock,
    )
    emitter.cleanup()


def test_active_local_variant_custom_property_roundtrips():
    source = Usd.Stage.CreateInMemory()
    world = source.DefinePrim("/World", "Xform")
    variants = world.GetVariantSets().AddVariantSet("choice")
    for name, value in (("A", 1.25), ("B", 2.5)):
        variants.AddVariant(name)
        variants.SetVariantSelection(name)
        with variants.GetVariantEditContext():
            prim = source.DefinePrim("/World/VariantSphere", "Sphere")
            prim.CreateAttribute(
                "user:weight",
                Sdf.ValueTypeNames.Float,
                custom=True,
            ).Set(value)
    variants.SetVariantSelection("A")
    emitter = NoticeEmitter(source)
    target = Usd.Stage.CreateInMemory()

    initial = emitter.snapshot_events()
    assert K_ENSURE_PRIM not in _event_kinds(initial, "/World/VariantSphere")
    variant_properties = {
        event["spec_path"]
        for event in initial
        if event["k"] == K_SET_SDF_SPEC_FIELDS and event["spec_kind"] == "attribute"
    }
    assert variant_properties == {
        "/World{choice=A}VariantSphere.user:weight",
        "/World{choice=B}VariantSphere.user:weight",
    }
    apply_events(target, initial)
    assert target.GetPrimAtPath("/World/VariantSphere").GetAttribute("user:weight").Get() == 1.25

    variants.SetVariantSelection("B")
    changed = emitter.build_events_for_dirty()
    apply_events(target, changed)
    assert target.GetPrimAtPath("/World/VariantSphere").GetAttribute("user:weight").Get() == 2.5
    emitter.cleanup()


def test_direct_over_does_not_hide_active_variant_definition():
    source = Usd.Stage.CreateInMemory()
    world = source.DefinePrim("/World", "Xform")
    variants = world.GetVariantSets().AddVariantSet("choice")
    variants.AddVariant("A")
    variants.SetVariantSelection("A")
    with variants.GetVariantEditContext():
        source.DefinePrim("/World/VariantSphere", "Sphere")
    source.OverridePrim("/World/VariantSphere").SetDocumentation("direct over")
    emitter = NoticeEmitter(source)

    events = emitter.snapshot_events()
    assert not [
        event
        for event in events
        if event["k"] == K_ENSURE_PRIM and event["prim"] == "/World/VariantSphere"
    ]
    exact_prims = {
        event["spec_path"]
        for event in events
        if event["k"] == K_SET_SDF_SPEC_FIELDS and event["spec_kind"] == "prim"
    }
    assert {
        "/World/VariantSphere",
        "/World{choice=A}VariantSphere",
    } <= exact_prims

    target = Usd.Stage.CreateInMemory()
    apply_events(target, events)
    assert target.GetPrimAtPath("/World/VariantSphere").GetTypeName() == "Sphere"
    emitter.cleanup()


def test_variant_switch_reauthors_changed_definition_type():
    source = Usd.Stage.CreateInMemory()
    world = source.DefinePrim("/World", "Xform")
    variants = world.GetVariantSets().AddVariantSet("shape")
    for selection, type_name in (("sphere", "Sphere"), ("cube", "Cube")):
        variants.AddVariant(selection)
        variants.SetVariantSelection(selection)
        with variants.GetVariantEditContext():
            source.DefinePrim("/World/Shape", type_name)
    variants.SetVariantSelection("sphere")

    emitter = NoticeEmitter(source)
    target = Usd.Stage.CreateInMemory()
    apply_events(target, emitter.snapshot_events())
    assert target.GetPrimAtPath("/World/Shape").GetTypeName() == "Sphere"

    variants.SetVariantSelection("cube")
    events = emitter.build_events_for_dirty()
    assert not [
        event for event in events if event["k"] == K_ENSURE_PRIM and event["prim"] == "/World/Shape"
    ]
    assert {
        "k": K_SET_VARIANT_SELECTIONS,
        "prim": "/World",
        "selections": {"shape": "cube"},
    } in events

    apply_events(target, events)
    assert target.GetPrimAtPath("/World/Shape").GetTypeName() == "Cube"
    emitter.cleanup()


def test_clearing_local_variant_selection_reveals_shared_base_selection():
    base = Sdf.Layer.CreateAnonymous("variant-selection-base")
    base_stage = Usd.Stage.Open(base)
    world = base_stage.DefinePrim("/World", "Xform")
    variants = world.GetVariantSets().AddVariantSet("choice")
    for name in ("A", "B"):
        variants.AddVariant(name)
    variants.SetVariantSelection("A")

    source_session = Sdf.Layer.CreateAnonymous("variant-selection-source")
    source = Usd.Stage.Open(base, source_session)
    source.SetEditTarget(Usd.EditTarget(source_session))
    source_variants = source.GetPrimAtPath("/World").GetVariantSets().GetVariantSet("choice")
    source_variants.SetVariantSelection("B")
    emitter = NoticeEmitter(source)

    target_session = Sdf.Layer.CreateAnonymous("variant-selection-target")
    target = Usd.Stage.Open(base, target_session)
    target.SetEditTarget(Usd.EditTarget(target_session))
    apply_events(target, emitter.snapshot_events())
    target_variants = target.GetPrimAtPath("/World").GetVariantSets().GetVariantSet("choice")
    assert target_variants.GetVariantSelection() == "B"

    source_variants.ClearVariantSelection()
    events = emitter.build_events_for_dirty()
    assert [
        event
        for event in events
        if event["k"] == K_SET_VARIANT_SELECTIONS
        and event["prim"] == "/World"
        and event["selections"] == {"choice": ""}
    ]
    apply_events(target, events)
    assert target_variants.GetVariantSelection() == "A"
    assert dict(target_session.GetPrimAtPath("/World").variantSelections) == {}
    emitter.cleanup()


def test_locally_defined_child_under_reference_still_emits_structure(tmp_path):
    source = _make_reference_stage(_make_sphere_asset(tmp_path))
    local = UsdGeom.Sphere.Define(source, "/World/Ref/Local")
    local.GetRadiusAttr().Set(4.0)
    emitter = NoticeEmitter(source)

    events = emitter.snapshot_events()

    assert K_ENSURE_PRIM in _event_kinds(events, "/World/Ref/Local")
    assert not _event_kinds(events, "/World/Ref/Geom")
    target = Usd.Stage.CreateInMemory()
    apply_events(target, events)
    target_spec = target.GetRootLayer().GetPrimAtPath("/World/Ref/Local")
    assert target_spec.specifier == Sdf.SpecifierDef
    assert UsdGeom.Sphere(target.GetPrimAtPath("/World/Ref/Local")).GetRadiusAttr().Get() == 4.0
    emitter.cleanup()


def test_session_override_syncs_against_shared_base_without_replaying_it(tmp_path):
    asset_path = _make_sphere_asset(tmp_path)
    scene_path = str(tmp_path / "scene.usda")
    scene = Usd.Stage.CreateNew(scene_path)
    scene.DefinePrim("/World", "Xform")
    root = scene.DefinePrim("/World/Ref", "Xform")
    root.GetReferences().AddReference(asset_path, "/Model")
    scene.GetRootLayer().Save()

    source = Usd.Stage.Open(scene_path)
    source.SetEditTarget(Usd.EditTarget(source.GetSessionLayer()))
    UsdGeom.Sphere(source.GetPrimAtPath("/World/Ref/Geom")).GetRadiusAttr().Set(6.0)
    emitter = NoticeEmitter(source)

    events = emitter.snapshot_events()
    assert _event_kinds(events, "/World/Ref/Geom") == [K_SET_GPRIM_ATTRS]
    assert not [event for event in events if event["k"] == K_SET_REFERENCE]

    target = Usd.Stage.Open(scene_path)
    target.SetEditTarget(Usd.EditTarget(target.GetSessionLayer()))
    apply_events(target, events)
    assert UsdGeom.Sphere(target.GetPrimAtPath("/World/Ref/Geom")).GetRadiusAttr().Get() == 6.0
    assert target.GetSessionLayer().GetPrimAtPath("/World/Ref/Geom").specifier == Sdf.SpecifierOver
    emitter.cleanup()


def test_masked_canonical_transform_emits_current_edit_target_opinion():
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/World/Thing", "Xform")
    _, _, translate, _, _ = ensure_canonical_ops(stage, "/World/Thing")
    translate.Set(Gf.Vec3d(0.0))

    strong = Sdf.Layer.CreateAnonymous("strong-transform")
    weak = Sdf.Layer.CreateAnonymous("weak-transform")
    stage.GetSessionLayer().subLayerPaths = [strong.identifier, weak.identifier]

    weak_target = Usd.EditTarget(weak)
    with Usd.EditContext(stage, weak_target):
        translate.Set(Gf.Vec3d(1.0, 0.0, 0.0))
    with Usd.EditContext(stage, Usd.EditTarget(strong)):
        translate.Set(Gf.Vec3d(2.0, 0.0, 0.0))

    stage.SetEditTarget(weak_target)
    emitter = NoticeEmitter(stage)
    initial = emitter.snapshot_events()
    initial_trs = next(event for event in initial if event["k"] == K_SET_XFORM_TRS)
    assert initial_trs["t"] == pytest.approx([1.0, 0.0, 0.0])

    translate.Set(Gf.Vec3d(3.0, 0.0, 0.0))
    changed = emitter.build_events_for_dirty()
    changed_trs = next(event for event in changed if event["k"] == K_SET_XFORM_TRS)
    assert changed_trs["t"] == pytest.approx([3.0, 0.0, 0.0])
    assert translate.Get() == Gf.Vec3d(2.0, 0.0, 0.0)
    emitter.cleanup()


def test_session_layer_masks_root_transform_without_changing_emitted_opinion():
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/World/Thing", "Xform")
    _, _, translate, _, _ = ensure_canonical_ops(stage, "/World/Thing")
    translate.Set(Gf.Vec3d(1.0, 0.0, 0.0))

    root_target = stage.GetEditTarget()
    with Usd.EditContext(stage, Usd.EditTarget(stage.GetSessionLayer())):
        translate.Set(Gf.Vec3d(2.0, 0.0, 0.0))

    stage.SetEditTarget(root_target)
    emitter = NoticeEmitter(stage)
    emitter.snapshot_events()

    translate.Set(Gf.Vec3d(3.0, 0.0, 0.0))
    changed = emitter.build_events_for_dirty()

    changed_trs = next(event for event in changed if event["k"] == K_SET_XFORM_TRS)
    assert changed_trs["t"] == pytest.approx([3.0, 0.0, 0.0])
    assert translate.Get() == Gf.Vec3d(2.0, 0.0, 0.0)
    emitter.cleanup()


def test_masked_connectable_default_emits_current_edit_target_opinion():
    weak = Sdf.Layer.CreateAnonymous("weak-material")
    weak_stage = Usd.Stage.Open(weak)
    weak_shader = UsdShade.Shader.Define(weak_stage, "/Surface")
    weak_shader.CreateIdAttr("UsdPreviewSurface")
    weak_shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.2)

    strong = Sdf.Layer.CreateAnonymous("strong-material")
    strong.subLayerPaths = [weak.identifier]
    stage = Usd.Stage.Open(strong)
    stage.SetEditTarget(Usd.EditTarget(strong))
    shader = UsdShade.Shader(stage.GetPrimAtPath("/Surface"))
    shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.8)
    stage.SetEditTarget(Usd.EditTarget(weak))
    emitter = NoticeEmitter(stage)

    initial = emitter.snapshot_events()
    initial_input = next(
        event
        for event in initial
        if event["k"] == K_SET_CONNECTABLE_INPUT and event["prim"] == "/Surface"
    )
    assert initial_input["inputs"]["roughness"] == pytest.approx(0.2)

    shader.GetInput("roughness").Set(0.3)
    changed = emitter.build_events_for_dirty()
    update = next(
        event
        for event in changed
        if event["k"] == K_SET_CONNECTABLE_INPUT and event["prim"] == "/Surface"
    )
    assert update["inputs"]["roughness"] == pytest.approx(0.3)
    assert shader.GetInput("roughness").Get() == pytest.approx(0.8)

    shader.GetInput("roughness").GetAttr().Clear()
    cleared = emitter.build_events_for_dirty()
    exact_clear = next(
        event
        for event in cleared
        if event["k"] == K_SET_SDF_SPEC_FIELDS
        and event["spec_path"] == "/Surface.inputs:roughness"
    )
    assert "default" in exact_clear["fields"]

    target = Usd.Stage.CreateInMemory()
    apply_events(target, [*initial, *changed, *cleared])
    assert target.GetAttributeAtPath("/Surface.inputs:roughness").Get() is None
    emitter.cleanup()


def test_connectable_input_does_not_materialize_composed_shader_id():
    base = Sdf.Layer.CreateAnonymous("base-material")
    base_stage = Usd.Stage.Open(base)
    base_shader = UsdShade.Shader.Define(base_stage, "/Surface")
    base_shader.CreateIdAttr("UsdPreviewSurface")

    local = Sdf.Layer.CreateAnonymous("local-material")
    local.subLayerPaths = [base.identifier]
    stage = Usd.Stage.Open(local)
    shader = UsdShade.Shader(stage.GetPrimAtPath("/Surface"))
    shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.4)
    emitter = NoticeEmitter(stage)

    event = next(
        event
        for event in emitter.snapshot_events()
        if event["k"] == K_SET_CONNECTABLE_INPUT and event["prim"] == "/Surface"
    )

    assert event["info_id"] == ""
    assert event["inputs"]["roughness"] == pytest.approx(0.4)
    emitter.cleanup()


def test_masked_connectable_connection_emits_current_edit_target_opinion():
    weak = Sdf.Layer.CreateAnonymous("weak-network")
    weak_stage = Usd.Stage.Open(weak)
    for name in ("A", "B", "C", "Surface"):
        shader = UsdShade.Shader.Define(weak_stage, f"/{name}")
        shader.CreateIdAttr("TestNode")
        if name != "Surface":
            shader.CreateOutput("out", Sdf.ValueTypeNames.Color3f)
    weak_surface = UsdShade.Shader(weak_stage.GetPrimAtPath("/Surface"))
    weak_surface.CreateInput("color", Sdf.ValueTypeNames.Color3f).ConnectToSource(
        UsdShade.Shader(weak_stage.GetPrimAtPath("/A")).GetOutput("out")
    )

    strong = Sdf.Layer.CreateAnonymous("strong-network")
    strong.subLayerPaths = [weak.identifier]
    stage = Usd.Stage.Open(strong)
    stage.SetEditTarget(Usd.EditTarget(strong))
    surface = UsdShade.Shader(stage.GetPrimAtPath("/Surface"))
    surface.GetInput("color").ConnectToSource(
        UsdShade.Shader(stage.GetPrimAtPath("/B")).GetOutput("out")
    )
    stage.SetEditTarget(Usd.EditTarget(weak))
    emitter = NoticeEmitter(stage)

    initial = emitter.snapshot_events()
    initial_connection = next(
        event
        for event in initial
        if event["k"] == K_SET_CONNECTABLE_CONNECTION and event["prim"] == "/Surface"
    )
    assert initial_connection["connections"]["inputs:color"]["source_prim"] == "/A"

    surface.GetInput("color").ConnectToSource(
        UsdShade.Shader(stage.GetPrimAtPath("/C")).GetOutput("out")
    )
    changed = emitter.build_events_for_dirty()
    update = next(
        event
        for event in changed
        if event["k"] == K_SET_CONNECTABLE_CONNECTION and event["prim"] == "/Surface"
    )
    assert update["connections"]["inputs:color"]["source_prim"] == "/C"
    composed_source, _name, _type = surface.GetInput("color").GetConnectedSource()
    assert composed_source.GetPath() == Sdf.Path("/B")

    surface.GetInput("color").DisconnectSource()
    disconnected = emitter.build_events_for_dirty()
    disconnect = next(
        event
        for event in disconnected
        if event["k"] == K_SET_CONNECTABLE_CONNECTION and event["prim"] == "/Surface"
    )
    assert disconnect["disconnections"] == ["inputs:color"]

    surface.GetInput("color").ClearSources()
    cleared = emitter.build_events_for_dirty()
    exact_clear = next(
        event
        for event in cleared
        if event["k"] == K_SET_SDF_SPEC_FIELDS
        and event["spec_path"] == "/Surface.inputs:color"
    )
    assert "connectionPaths" in exact_clear["fields"]
    composed_source, _name, _type = surface.GetInput("color").GetConnectedSource()
    assert composed_source.GetPath() == Sdf.Path("/B")

    target_weak = Sdf.Layer.CreateAnonymous("target-weak-network")
    target_strong = Sdf.Layer.CreateAnonymous("target-strong-network")
    target_strong.subLayerPaths = [target_weak.identifier]
    target = Usd.Stage.Open(target_strong)
    for name in ("A", "B", "C", "Surface"):
        target_shader = UsdShade.Shader.Define(target, f"/{name}")
        target_shader.CreateIdAttr("TestNode")
        if name != "Surface":
            target_shader.CreateOutput("out", Sdf.ValueTypeNames.Color3f)
    target_surface = UsdShade.Shader(target.GetPrimAtPath("/Surface"))
    target_surface.CreateInput("color", Sdf.ValueTypeNames.Color3f).ConnectToSource(
        UsdShade.Shader(target.GetPrimAtPath("/B")).GetOutput("out")
    )
    target.SetEditTarget(Usd.EditTarget(target_weak))

    apply_events(target, [*initial, *changed])
    target_local = target_weak.GetAttributeAtPath("/Surface.inputs:color")
    assert target_local.connectionPathList.explicitItems == [
        Sdf.Path("/C.outputs:out")
    ]

    apply_events(target, disconnected)
    assert target_local.HasInfo("connectionPaths")
    assert target_local.connectionPathList.isExplicit
    assert target_local.connectionPathList.explicitItems == []

    apply_events(target, cleared)
    assert not target_local.HasInfo("connectionPaths")
    target_source, _name, _type = target_surface.GetInput("color").GetConnectedSource()
    assert target_source.GetPath() == Sdf.Path("/B")
    emitter.cleanup()


def test_multi_source_connection_uses_sdf_delta_for_exact_state():
    source = Usd.Stage.CreateInMemory()
    for name in ("A", "B", "Surface"):
        shader = UsdShade.Shader.Define(source, f"/{name}")
        shader.CreateIdAttr("TestNode")
        if name != "Surface":
            shader.CreateOutput("out", Sdf.ValueTypeNames.Float)
    UsdShade.Shader(source.GetPrimAtPath("/Surface")).CreateInput(
        "value",
        Sdf.ValueTypeNames.Float,
    )
    input_spec = source.GetRootLayer().GetAttributeAtPath("/Surface.inputs:value")
    input_spec.connectionPathList.explicitItems = [
        Sdf.Path("/A.outputs:out"),
        Sdf.Path("/B.outputs:out"),
    ]
    emitter = NoticeEmitter(source)

    events = emitter.snapshot_events()
    exact = next(
        event
        for event in events
        if event["k"] == K_SET_SDF_SPEC_FIELDS
        and event["spec_path"] == "/Surface.inputs:value"
    )
    assert "connectionPaths" in exact["fields"]

    target = Usd.Stage.CreateInMemory()
    apply_events(target, events)
    assert target.GetAttributeAtPath("/Surface.inputs:value").GetConnections() == [
        Sdf.Path("/A.outputs:out"),
        Sdf.Path("/B.outputs:out"),
    ]
    emitter.cleanup()


def test_connectable_overrides_apply_before_weaker_prim_definitions():
    lookdev_layer = Sdf.Layer.CreateAnonymous("lookdev-material")
    lookdev = Usd.Stage.Open(lookdev_layer)
    source_shader = UsdShade.Shader.Define(lookdev, "/Source")
    source_shader.CreateIdAttr("TestNode")
    source_shader.CreateOutput("out", Sdf.ValueTypeNames.Color3f)
    surface = UsdShade.Shader.Define(lookdev, "/Surface")
    surface.CreateIdAttr("UsdPreviewSurface")
    surface.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.28)
    surface.CreateInput("color", Sdf.ValueTypeNames.Color3f)
    lookdev_emitter = NoticeEmitter(lookdev)
    lookdev_events = lookdev_emitter.snapshot_events()

    shot_layer = Sdf.Layer.CreateAnonymous("shot-material")
    source_root = Sdf.Layer.CreateAnonymous("source-root")
    source_root.subLayerPaths = [shot_layer.identifier, lookdev_layer.identifier]
    source = Usd.Stage.Open(source_root)
    source.SetEditTarget(Usd.EditTarget(shot_layer))
    shot_surface = UsdShade.Shader(source.GetPrimAtPath("/Surface"))
    shot_source = UsdShade.Shader(source.GetPrimAtPath("/Source"))
    shot_surface.GetInput("roughness").Set(0.08)
    shot_surface.GetInput("color").ConnectToSource(shot_source.GetOutput("out"))
    shot_emitter = NoticeEmitter(source)
    shot_events = shot_emitter.snapshot_events()

    assert any(
        event["k"] == K_SET_CONNECTABLE_INPUT
        and event["prim"] == "/Surface"
        and event["inputs"].get("roughness") == pytest.approx(0.08)
        for event in shot_events
    )
    assert any(
        event["k"] == K_SET_SDF_SPEC_FIELDS
        and event["spec_path"] == "/Surface.inputs:color"
        and "connectionPaths" in event["fields"]
        for event in shot_events
    )

    target_shot = Sdf.Layer.CreateAnonymous("target-shot-material")
    target_lookdev = Sdf.Layer.CreateAnonymous("target-lookdev-material")
    target_root = Sdf.Layer.CreateAnonymous("target-root")
    target_root.subLayerPaths = [target_shot.identifier, target_lookdev.identifier]
    target = Usd.Stage.Open(target_root)
    target.SetEditTarget(Usd.EditTarget(target_shot))
    apply_events(target, shot_events)

    target_shot_prim = target_shot.GetPrimAtPath("/Surface")
    assert target_shot_prim.specifier == Sdf.SpecifierOver
    assert not target_shot_prim.typeName
    assert target_shot.GetAttributeAtPath("/Surface.inputs:roughness").default == (
        pytest.approx(0.08)
    )
    assert target_shot.GetAttributeAtPath(
        "/Surface.inputs:color"
    ).connectionPathList.explicitItems == [Sdf.Path("/Source.outputs:out")]

    target.SetEditTarget(Usd.EditTarget(target_lookdev))
    apply_events(target, lookdev_events)
    target_surface = UsdShade.Shader(target.GetPrimAtPath("/Surface"))
    assert target_surface.GetInput("roughness").Get() == pytest.approx(0.08)
    connected, _name, _type = target_surface.GetInput("color").GetConnectedSource()
    assert connected.GetPath() == Sdf.Path("/Source")

    shot_emitter.cleanup()
    lookdev_emitter.cleanup()


def test_api_schema_override_does_not_define_referenced_child(tmp_path):
    asset_path = str(tmp_path / "light_asset.usda")
    asset = Usd.Stage.CreateNew(asset_path)
    asset.DefinePrim("/Model", "Xform")
    UsdLux.SphereLight.Define(asset, "/Model/Light")
    asset.GetRootLayer().Save()

    source = _make_reference_stage(asset_path)
    light = source.GetPrimAtPath("/World/Ref/Light")
    UsdLux.ShapingAPI.Apply(light).CreateShapingConeAngleAttr(30.0)
    emitter = NoticeEmitter(source)
    events = emitter.snapshot_events()

    ensure = [
        event
        for event in events
        if event["k"] == K_ENSURE_PRIM and event["prim"] == "/World/Ref/Light"
    ]
    assert ensure and ensure[0]["typeName"] == ""
    assert ensure[0]["api_schemas"] == ["ShapingAPI"]

    target = Usd.Stage.CreateInMemory()
    apply_events(target, events)
    target_light = target.GetPrimAtPath("/World/Ref/Light")
    assert target_light.HasAPI(UsdLux.ShapingAPI)
    assert UsdLux.ShapingAPI(target_light).GetShapingConeAngleAttr().Get() == 30.0
    assert target.GetRootLayer().GetPrimAtPath("/World/Ref/Light").specifier == Sdf.SpecifierOver
    emitter.cleanup()


def test_referenced_descendant_override_survives_server_log_replay(tmp_path):
    """The production transaction and persistence path preserves an over."""
    source = _make_reference_stage(_make_sphere_asset(tmp_path))
    emitter = NoticeEmitter(source)
    db_path = str(tmp_path / "events.db")

    server = UsdSyncServer(log_path=db_path)
    try:
        server.process_txn(emitter.snapshot_events(), client_id="source")
        UsdGeom.Sphere(source.GetPrimAtPath("/World/Ref/Geom")).GetRadiusAttr().Set(7.0)
        server.process_txn(emitter.build_events_for_dirty(), client_id="source")
    finally:
        emitter.cleanup()
        server.shutdown()
        server.store.close()

    replayed = UsdSyncServer(log_path=db_path)
    try:
        sphere = UsdGeom.Sphere(replayed.stage.GetPrimAtPath("/World/Ref/Geom"))
        assert sphere.GetRadiusAttr().Get() == 7.0
        child_spec = replayed.edit_layer.GetPrimAtPath("/World/Ref/Geom")
        assert child_spec.specifier == Sdf.SpecifierOver
    finally:
        replayed.shutdown()
        replayed.store.close()
