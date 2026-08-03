"""Sdf field-delta coverage for exact authored spec state."""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd, UsdGeom, UsdShade, Vt

from openusdconnect.codec import encode_message, message_to_dict
from openusdconnect.emitter import NoticeEmitter
from openusdconnect.event_apply import apply_events
from openusdconnect.protocol_constants import (
    K_SET_GPRIM_ATTRS,
    K_SET_SDF_SPEC_FIELDS,
    K_SET_STAGE_METADATA,
    MSG_TXN,
)
from openusdconnect.sdf_spec_delta import (
    SDF_LAYER_TOPOLOGY_FIELDS,
    SDF_SPEC_KIND_ATTRIBUTE,
    SDF_SPEC_KIND_LAYER,
    SDF_SPEC_KIND_PRIM,
    SDF_SPEC_KIND_RELATIONSHIP,
    SDF_SPEC_KIND_VARIANT,
    SDF_SPEC_KIND_VARIANT_SET,
    apply_spec_delta,
    serialize_spec_fields,
)
from openusdconnect.server import UsdSyncServer


def _property_info(layer: Sdf.Layer, path: str) -> dict:
    spec = layer.GetPropertyAtPath(path)
    return {str(key): spec.GetInfo(key) for key in spec.ListInfoKeys()}


def _sdf_events(events: list[dict]) -> list[dict]:
    return [event for event in events if event["k"] == K_SET_SDF_SPEC_FIELDS]


def test_custom_attribute_and_relationship_snapshot_roundtrip():
    source = Usd.Stage.CreateInMemory()
    prim = source.DefinePrim("/World/Thing", "Xform")
    attr = prim.CreateAttribute("userProperties:weight", Sdf.ValueTypeNames.Double, True)
    attr.Set(1.23456789012345)
    attr.SetDocumentation("Exact authored documentation")
    attr.SetCustomData({"department": "lookdev"})
    attr.SetMetadata("allowedTokens", Vt.TokenArray(["light", "heavy"]))
    attr.AddConnection(Sdf.Path("/World/Target.outputs:weight"))
    rel = prim.CreateRelationship("userProperties:link", True)
    rel.AddTarget(Sdf.Path("/World/Target"), Usd.ListPositionFrontOfPrependList)

    emitter = NoticeEmitter(source)
    events = emitter.snapshot_events()
    target = Usd.Stage.CreateInMemory()
    apply_events(target, events)
    emitter.cleanup()

    for path in (
        "/World/Thing.userProperties:weight",
        "/World/Thing.userProperties:link",
    ):
        assert _property_info(source.GetRootLayer(), path) == _property_info(
            target.GetRootLayer(), path
        )


def test_unregistered_value_type_roundtrips_as_sdf_unregistered_value():
    layer = Sdf.Layer.CreateAnonymous("plugin-type.usda")
    assert layer.ImportFromString(
        """#usda 1.0

def Xform "Thing"
{
    custom MyPluginType userProperties:value = "payload"
}
"""
    )
    source = Usd.Stage.Open(layer)
    emitter = NoticeEmitter(source)

    events = emitter.snapshot_events()
    target = Usd.Stage.CreateInMemory()
    apply_events(target, events)
    emitter.cleanup()

    path = "/Thing.userProperties:value"
    assert _property_info(layer, path) == _property_info(target.GetRootLayer(), path)


def test_incremental_value_block_samples_metadata_clear_and_removal():
    source = Usd.Stage.CreateInMemory()
    prim = source.DefinePrim("/World/Thing", "Xform")
    attr = prim.CreateAttribute("userProperties:value", Sdf.ValueTypeNames.Double, True)
    attr.Set(1.0)
    emitter = NoticeEmitter(source)
    target = Usd.Stage.CreateInMemory()
    apply_events(target, emitter.snapshot_events())

    attr.SetDocumentation("temporary")
    attr.Set(Sdf.ValueBlock())
    attr.Set(3.0, 12.0)
    events = emitter.build_events_for_dirty()
    assert {field for event in _sdf_events(events) for field in event["fields"]} == {
        "default",
        "documentation",
        "timeSamples",
    }
    apply_events(target, events)

    attr.ClearDocumentation()
    events = emitter.build_events_for_dirty()
    assert _sdf_events(events)[0]["fields"] == ["documentation"]
    apply_events(target, events)

    path = "/World/Thing.userProperties:value"
    assert _property_info(source.GetRootLayer(), path) == _property_info(
        target.GetRootLayer(), path
    )

    prim.RemoveProperty("userProperties:value")
    events = emitter.build_events_for_dirty()
    assert _sdf_events(events) == [
        {
            "k": K_SET_SDF_SPEC_FIELDS,
            "prim": "/World/Thing",
            "spec_path": path,
            "spec_kind": SDF_SPEC_KIND_ATTRIBUTE,
            "fields": [
                "custom",
                "default",
                "timeSamples",
                "typeName",
                "variability",
            ],
            "fragment": "",
            "removed": True,
        }
    ]
    apply_events(target, events)
    assert not target.GetRootLayer().GetPropertyAtPath(path)
    emitter.cleanup()


def test_schema_value_keeps_fast_path_and_metadata_uses_sdf():
    source = Usd.Stage.CreateInMemory()
    sphere = UsdGeom.Sphere.Define(source, "/World/Sphere")
    sphere.GetRadiusAttr().Set(2.0)
    sphere.GetRadiusAttr().SetDocumentation("radius documentation")
    emitter = NoticeEmitter(source)

    events = emitter.snapshot_events()
    emitter.cleanup()

    values = [event for event in events if event["k"] == K_SET_GPRIM_ATTRS]
    assert values == [
        {
            "k": K_SET_GPRIM_ATTRS,
            "prim": "/World/Sphere",
            "attrs": {"radius": 2.0},
        }
    ]
    metadata = _sdf_events(events)
    assert len(metadata) == 1
    assert metadata[0]["spec_path"] == "/World/Sphere.radius"
    assert metadata[0]["fields"] == ["documentation"]


def test_connectable_channel_keeps_value_and_connection_ownership():
    source = Usd.Stage.CreateInMemory()
    shader = UsdShade.Shader.Define(source, "/World/Shader")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.25)
    shader.GetInput("roughness").GetAttr().SetDocumentation("roughness docs")
    emitter = NoticeEmitter(source)

    events = emitter.snapshot_events()
    emitter.cleanup()

    metadata = [
        event
        for event in _sdf_events(events)
        if event["spec_path"] == "/World/Shader.inputs:roughness"
    ]
    assert len(metadata) == 1
    assert metadata[0]["fields"] == ["documentation"]


def test_bare_usdshade_ports_roundtrip_as_sdf_declarations():
    source = Usd.Stage.CreateInMemory()
    shader = UsdShade.Shader.Define(source, "/World/Shader")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("bareInput", Sdf.ValueTypeNames.Float)
    shader.CreateOutput("bareOutput", Sdf.ValueTypeNames.Token)
    emitter = NoticeEmitter(source)

    events = emitter.snapshot_events()
    declaration_paths = {
        event["spec_path"]
        for event in _sdf_events(events)
        if set(event["fields"]) == {"custom", "typeName", "variability"}
    }
    assert declaration_paths == {
        "/World/Shader.inputs:bareInput",
        "/World/Shader.outputs:bareOutput",
    }

    target = Usd.Stage.CreateInMemory()
    apply_events(target, events)
    target_shader = UsdShade.Shader(target.GetPrimAtPath("/World/Shader"))
    assert target_shader.GetInput("bareInput")
    assert target_shader.GetInput("bareInput").GetTypeName() == Sdf.ValueTypeNames.Float
    assert target_shader.GetOutput("bareOutput")
    assert target_shader.GetOutput("bareOutput").GetTypeName() == Sdf.ValueTypeNames.Token
    emitter.cleanup()


def test_composed_custom_attribute_emits_only_local_override():
    asset_layer = Sdf.Layer.CreateAnonymous("asset.usda")
    asset = Usd.Stage.Open(asset_layer)
    asset_prim = asset.DefinePrim("/Asset", "Xform")
    asset_prim.CreateAttribute(
        "userProperties:weight",
        Sdf.ValueTypeNames.Double,
        True,
    ).Set(1.25)

    source = Usd.Stage.CreateInMemory()
    prim = source.DefinePrim("/World/Thing", "Xform")
    prim.GetReferences().AddReference(asset_layer.identifier, "/Asset")
    emitter = NoticeEmitter(source)

    events = emitter.snapshot_events()

    assert not _sdf_events(events)
    assert not [
        event
        for event in events
        if event["k"] == K_SET_GPRIM_ATTRS and "userProperties:weight" in event.get("attrs", {})
    ]

    prim.GetAttribute("userProperties:weight").Set(2.5)
    override = _sdf_events(emitter.build_events_for_dirty())
    assert len(override) == 1
    assert override[0]["spec_path"] == "/World/Thing.userProperties:weight"
    assert "default" in override[0]["fields"]
    emitter.cleanup()


def test_codec_roundtrip_preserves_fragment_exactly():
    event = {
        "k": K_SET_SDF_SPEC_FIELDS,
        "prim": "/World/Thing",
        "spec_path": "/World/Thing.userProperties:value",
        "spec_kind": SDF_SPEC_KIND_ATTRIBUTE,
        "fields": ["default", "documentation"],
        "fragment": '#usda 1.0\n\nover "World" {}\n',
        "removed": False,
    }
    encoded = encode_message({"type": MSG_TXN, "client_id": "test", "events": [event]})
    assert message_to_dict(encoded)["events"] == [event]


def test_apply_ignores_fragment_fields_not_named_by_event():
    source = Usd.Stage.CreateInMemory()
    prim = source.DefinePrim("/World/Thing", "Xform")
    attr = prim.CreateAttribute("userProperties:value", Sdf.ValueTypeNames.Double, True)
    attr.Set(99.0)
    attr.SetDocumentation("selected")
    path = "/World/Thing.userProperties:value"
    target = Usd.Stage.CreateInMemory()

    apply_spec_delta(
        target,
        {
            "spec_path": path,
            "spec_kind": SDF_SPEC_KIND_ATTRIBUTE,
            "fields": ["documentation"],
            "fragment": source.GetRootLayer().ExportToString(),
            "removed": False,
        },
    )

    target_attr = target.GetAttributeAtPath(path)
    assert target_attr.GetDocumentation() == "selected"
    assert target_attr.Get() is None


@pytest.mark.parametrize("field", sorted(SDF_LAYER_TOPOLOGY_FIELDS))
def test_sublayer_topology_is_rejected_by_spec_delta_apply(field):
    source = Usd.Stage.CreateInMemory()
    with pytest.raises(ValueError, match="sublayer topology"):
        serialize_spec_fields(
            source.GetRootLayer(),
            "/",
            SDF_SPEC_KIND_LAYER,
            [field],
        )

    target = Usd.Stage.CreateInMemory()
    with pytest.raises(ValueError, match="sublayer topology"):
        apply_spec_delta(
            target,
            {
                "spec_path": "/",
                "spec_kind": SDF_SPEC_KIND_LAYER,
                "fields": [field],
                "fragment": "#usda 1.0\n",
                "removed": False,
            },
        )


def test_snapshot_omits_sublayer_topology_fields():
    root = Sdf.Layer.CreateAnonymous("root.usda")
    weak = Sdf.Layer.CreateAnonymous("weak.usda")
    root.subLayerPaths = [weak.identifier]
    root.subLayerOffsets[0] = Sdf.LayerOffset(7.0, 2.0)
    emitter = NoticeEmitter(Usd.Stage.Open(root))

    events = emitter.snapshot_events()
    emitter.cleanup()

    assert (
        not {
            field
            for event in _sdf_events(events)
            if event["spec_kind"] == SDF_SPEC_KIND_LAYER
            for field in event["fields"]
        }
        & SDF_LAYER_TOPOLOGY_FIELDS
    )


def test_spec_delta_rejects_mismatched_routing_prim():
    target = Usd.Stage.CreateInMemory()
    with pytest.raises(ValueError, match="belongs to /World/Thing"):
        apply_spec_delta(
            target,
            {
                "prim": "/World/Other",
                "spec_path": "/World/Thing.user:value",
                "spec_kind": SDF_SPEC_KIND_ATTRIBUTE,
                "fields": [],
                "fragment": "#usda 1.0\n",
                "removed": True,
            },
        )


def test_batch_preflight_rejects_invalid_fragment_before_mutation():
    source = Usd.Stage.CreateInMemory()
    source.GetRootLayer().documentation = "incoming"
    valid = {
        "k": K_SET_SDF_SPEC_FIELDS,
        "prim": "/",
        "spec_path": "/",
        "spec_kind": SDF_SPEC_KIND_LAYER,
        "fields": ["documentation"],
        "fragment": serialize_spec_fields(
            source.GetRootLayer(),
            Sdf.Path.absoluteRootPath,
            SDF_SPEC_KIND_LAYER,
            ["documentation"],
        ),
        "removed": False,
    }
    invalid = {
        "k": K_SET_SDF_SPEC_FIELDS,
        "prim": "/World",
        "spec_path": "/World",
        "spec_kind": SDF_SPEC_KIND_PRIM,
        "fields": ["documentation"],
        "fragment": "",
        "removed": False,
    }
    target = Usd.Stage.CreateInMemory()
    target.GetRootLayer().documentation = "original"

    with pytest.raises(ValueError, match="valid Sdf fragment"):
        apply_events(target, [valid, invalid])

    assert target.GetRootLayer().documentation == "original"


def test_server_preflights_sdf_events_before_shared_stage_edits(tmp_path):
    server = UsdSyncServer(log_path=str(tmp_path / "preflight.db"))
    original_axis = server.stage.GetMetadata("upAxis")
    try:
        with pytest.raises(ValueError, match="valid Sdf fragment"):
            server.apply_txn(
                [
                    {"k": K_SET_STAGE_METADATA, "upAxis": "Z"},
                    {
                        "k": K_SET_SDF_SPEC_FIELDS,
                        "prim": "/World",
                        "spec_path": "/World",
                        "spec_kind": SDF_SPEC_KIND_PRIM,
                        "fields": ["documentation"],
                        "fragment": "",
                        "removed": False,
                    },
                ]
            )
        assert server.stage.GetMetadata("upAxis") == original_axis
    finally:
        server.shutdown()
        server.store.close()


def test_variant_set_identity_preserves_authored_name_list_op():
    source_layer = Sdf.Layer.CreateAnonymous("variant-order.usda")
    prim = Sdf.CreatePrimInLayer(source_layer, "/World/Thing")
    prim.specifier = Sdf.SpecifierDef
    prim.typeName = "Xform"
    prim.variantSetNameList.explicitItems = ["other", "look"]
    variant_set = Sdf.VariantSetSpec(prim, "look")
    Sdf.VariantSpec(variant_set, "A")

    source = Usd.Stage.Open(source_layer)
    emitter = NoticeEmitter(source)
    events = emitter.snapshot_events()
    emitter.cleanup()

    target = Usd.Stage.CreateInMemory()
    apply_events(target, events)

    source_names = source_layer.GetPrimAtPath("/World/Thing").GetInfo("variantSetNames")
    target_names = target.GetRootLayer().GetPrimAtPath("/World/Thing").GetInfo("variantSetNames")
    assert target_names == source_names
    assert list(target_names.explicitItems) == ["other", "look"]


def test_layer_custom_data_preserves_receiver_transport_metadata():
    source = Usd.Stage.CreateInMemory()
    source.GetRootLayer().customLayerData = {
        "pipeline": {"department": "lookdev"},
        "openusdconnect": {"snapshot_seq": 10},
    }
    fragment = serialize_spec_fields(
        source.GetRootLayer(),
        "/",
        SDF_SPEC_KIND_LAYER,
        ["customLayerData"],
    )
    event = {
        "k": K_SET_SDF_SPEC_FIELDS,
        "prim": "/",
        "spec_path": "/",
        "spec_kind": SDF_SPEC_KIND_LAYER,
        "fields": ["customLayerData"],
        "fragment": fragment,
        "removed": False,
    }

    target = Usd.Stage.CreateInMemory()
    target.GetRootLayer().customLayerData = {
        "stale": True,
        "openusdconnect": {"snapshot_seq": 99},
    }
    apply_events(target, [event])

    assert target.GetRootLayer().customLayerData == {
        "pipeline": {"department": "lookdev"},
        "openusdconnect": {"snapshot_seq": 99},
    }

    source.GetRootLayer().ClearCustomLayerData()
    event["fragment"] = serialize_spec_fields(
        source.GetRootLayer(),
        "/",
        SDF_SPEC_KIND_LAYER,
        ["customLayerData"],
    )
    apply_events(target, [event])
    assert target.GetRootLayer().customLayerData == {"openusdconnect": {"snapshot_seq": 99}}


def test_snapshot_preserves_layer_prim_and_inactive_variant_opinions():
    source = Usd.Stage.CreateInMemory()
    layer = source.GetRootLayer()
    layer.defaultPrim = "World"
    layer.documentation = "layer documentation"
    layer.customLayerData = {
        "pipeline": {"department": "lookdev"},
        "openusdconnect": {"snapshot_seq": 42},
    }

    world = source.DefinePrim("/World", "Xform")
    world.SetDocumentation("world documentation")
    world_spec = layer.GetPrimAtPath("/World")
    world_spec.SetInfo(
        "apiSchemas",
        Sdf.TokenListOp.CreateExplicit(["UnknownPipelineAPI"]),
    )

    class_spec = Sdf.CreatePrimInLayer(layer, "/_Class")
    class_spec.specifier = Sdf.SpecifierClass
    class_spec.typeName = "Scope"
    class_spec.documentation = "class documentation"

    typed_over = Sdf.CreatePrimInLayer(layer, "/World/TypedOver")
    typed_over.specifier = Sdf.SpecifierOver
    typed_over.typeName = "Scope"
    typed_over.customData = {"purpose": "test"}

    variants = world.GetVariantSets().AddVariantSet("choice")
    for name, value in (("A", 1.0), ("B", 2.0)):
        variants.AddVariant(name)
        variants.SetVariantSelection(name)
        with variants.GetVariantEditContext():
            child = source.DefinePrim("/World/VariantSphere", "Sphere")
            child.CreateAttribute(
                "user:weight",
                Sdf.ValueTypeNames.Float,
                custom=True,
            ).Set(value)
    variants.SetVariantSelection("A")

    emitter = NoticeEmitter(source)
    events = emitter.snapshot_events()
    exact_paths = [event["spec_path"] for event in _sdf_events(events)]
    for parent, child in (
        ("/", "/World"),
        ("/World", "/World{choice=}"),
        ("/World{choice=}", "/World{choice=A}"),
        ("/World{choice=A}", "/World{choice=A}VariantSphere"),
        (
            "/World{choice=A}VariantSphere",
            "/World{choice=A}VariantSphere.user:weight",
        ),
    ):
        assert exact_paths.index(parent) < exact_paths.index(child)
    target = Usd.Stage.CreateInMemory()
    apply_events(target, events)
    emitter.cleanup()

    target_layer = target.GetRootLayer()
    assert target_layer.defaultPrim == "World"
    assert target_layer.documentation == "layer documentation"
    assert target_layer.customLayerData == {"pipeline": {"department": "lookdev"}}
    for path in ("/_Class", "/World/TypedOver", "/World"):
        source_spec = layer.GetPrimAtPath(path)
        target_spec = target_layer.GetPrimAtPath(path)
        assert target_spec
        assert {
            str(key): source_spec.GetInfo(key)
            for key in source_spec.ListInfoKeys()
            if str(key) not in {"references", "payload", "variantSelection"}
        } == {
            str(key): target_spec.GetInfo(key)
            for key in target_spec.ListInfoKeys()
            if str(key) not in {"references", "payload", "variantSelection"}
        }

    assert not target_layer.GetPrimAtPath("/World/VariantSphere")
    for name, value in (("A", 1.0), ("B", 2.0)):
        path = f"/World{{choice={name}}}VariantSphere.user:weight"
        assert target_layer.GetAttributeAtPath(path).default == value
    target_variants = target.GetPrimAtPath("/World").GetVariantSets().GetVariantSet("choice")
    target_variants.SetVariantSelection("B")
    assert target.GetAttributeAtPath("/World/VariantSphere.user:weight").Get() == 2.0


def test_generic_only_prim_and_empty_variant_set_lifecycle():
    source = Usd.Stage.CreateInMemory()
    class_prim = source.CreateClassPrim("/_Class")
    class_prim.SetDocumentation("class")
    typed_over = source.OverridePrim("/TypedOver")
    typed_over.SetTypeName("Scope")
    typed_over.SetDocumentation("typed over")
    world = source.DefinePrim("/World", "Xform")
    world.GetVariantSets().AddVariantSet("empty")

    emitter = NoticeEmitter(source)
    target = Usd.Stage.CreateInMemory()
    events = emitter.snapshot_events()
    apply_events(target, events)
    assert target.GetRootLayer().GetPrimAtPath("/_Class").specifier == Sdf.SpecifierClass
    assert target.GetRootLayer().GetPrimAtPath("/TypedOver").typeName == "Scope"
    assert target.GetRootLayer().GetObjectAtPath("/World{empty=}")
    assert "__openusdconnect_fragment__" not in target.GetRootLayer().ExportToString()

    source.RemovePrim("/_Class")
    source.RemovePrim("/TypedOver")
    removed = _sdf_events(emitter.build_events_for_dirty())
    assert {(event["spec_path"], event["removed"]) for event in removed} == {
        ("/_Class", True),
        ("/TypedOver", True),
    }
    apply_events(target, removed)
    assert not target.GetRootLayer().GetPrimAtPath("/_Class")
    assert not target.GetRootLayer().GetPrimAtPath("/TypedOver")
    emitter.cleanup()


def test_all_sdf_spec_kinds_apply_without_copying_fragment_scaffolding():
    source = Usd.Stage.CreateInMemory()
    layer = source.GetRootLayer()
    layer.documentation = "layer"
    prim = source.DefinePrim("/World", "Xform")
    variants = prim.GetVariantSets().AddVariantSet("look")
    variants.AddVariant("red")
    variants.SetVariantSelection("red")
    with variants.GetVariantEditContext():
        child = source.DefinePrim("/World/Child", "Scope")
        child.CreateAttribute(
            "user:value",
            Sdf.ValueTypeNames.Int,
            custom=True,
        ).Set(7)
        child.CreateRelationship("user:link", custom=True).SetTargets([Sdf.Path("/World")])

    cases = (
        ("/", SDF_SPEC_KIND_LAYER),
        ("/World", SDF_SPEC_KIND_PRIM),
        ("/World{look=}", SDF_SPEC_KIND_VARIANT_SET),
        ("/World{look=red}", SDF_SPEC_KIND_VARIANT),
        ("/World{look=red}Child", SDF_SPEC_KIND_PRIM),
        (
            "/World{look=red}Child.user:value",
            SDF_SPEC_KIND_ATTRIBUTE,
        ),
        (
            "/World{look=red}Child.user:link",
            SDF_SPEC_KIND_RELATIONSHIP,
        ),
    )
    target = Usd.Stage.CreateInMemory()
    for path, kind in cases:
        spec = layer.pseudoRoot if path == "/" else layer.GetObjectAtPath(path)
        fields = [str(key) for key in spec.ListInfoKeys()]
        apply_spec_delta(
            target,
            {
                "spec_path": path,
                "spec_kind": kind,
                "fields": fields,
                "fragment": serialize_spec_fields(
                    layer,
                    path,
                    kind,
                    fields,
                ),
                "removed": False,
            },
        )

    assert "__openusdconnect_fragment__" not in target.GetRootLayer().ExportToString()
    assert target.GetRootLayer().GetObjectAtPath("/World{look=red}")
    assert target.GetRootLayer().GetAttributeAtPath("/World{look=red}Child.user:value").default == 7


def test_serialized_specs_do_not_include_authored_descendants():
    source = Usd.Stage.CreateInMemory()
    layer = source.GetRootLayer()
    layer.documentation = "layer"
    world = source.DefinePrim("/World", "Xform")
    world.SetDocumentation("world")
    source.DefinePrim("/World/Child", "Scope")
    world.CreateAttribute("user:value", Sdf.ValueTypeNames.Int, custom=True).Set(7)
    variants = world.GetVariantSets().AddVariantSet("look")
    variants.AddVariant("red")
    variants.SetVariantSelection("red")
    with variants.GetVariantEditContext():
        source.DefinePrim("/World/VariantChild", "Scope")

    layer_fragment = Sdf.Layer.CreateAnonymous("layer-fragment.usda")
    assert layer_fragment.ImportFromString(
        serialize_spec_fields(
            layer,
            "/",
            SDF_SPEC_KIND_LAYER,
            ["documentation"],
        )
    )
    assert not layer_fragment.GetPrimAtPath("/World")

    prim_fragment = Sdf.Layer.CreateAnonymous("prim-fragment.usda")
    assert prim_fragment.ImportFromString(
        serialize_spec_fields(
            layer,
            "/World",
            SDF_SPEC_KIND_PRIM,
            ["documentation"],
        )
    )
    assert prim_fragment.GetPrimAtPath("/World")
    assert not prim_fragment.GetPrimAtPath("/World/Child")
    assert not prim_fragment.GetPropertyAtPath("/World.user:value")
    assert not prim_fragment.GetObjectAtPath("/World{look=}")

    variant_fragment = Sdf.Layer.CreateAnonymous("variant-fragment.usda")
    assert variant_fragment.ImportFromString(
        serialize_spec_fields(
            layer,
            "/World{look=red}",
            SDF_SPEC_KIND_VARIANT,
            [],
        )
    )
    assert variant_fragment.GetObjectAtPath("/World{look=red}")
    assert not variant_fragment.GetPrimAtPath("/World{look=red}VariantChild")


def test_notice_uses_authoring_layer_after_edit_target_switch():
    source = Usd.Stage.CreateInMemory()
    attr = source.DefinePrim("/World", "Xform").CreateAttribute(
        "user:value",
        Sdf.ValueTypeNames.Double,
        custom=True,
    )
    emitter = NoticeEmitter(source)
    emitter.snapshot_events()

    attr.SetDocumentation("authored in root")
    source.SetEditTarget(source.GetSessionLayer())
    events = _sdf_events(emitter.build_events_for_dirty())

    assert source.GetEditTarget().GetLayer() == source.GetSessionLayer()
    assert len(events) == 1
    assert events[0]["spec_path"] == "/World.user:value"
    incoming = Sdf.Layer.CreateAnonymous("inspect")
    assert incoming.ImportFromString(events[0]["fragment"])
    assert incoming.GetAttributeAtPath("/World.user:value").documentation == "authored in root"
    emitter.cleanup()


def test_one_batch_rejects_edits_from_different_layers():
    source = Usd.Stage.CreateInMemory()
    attr = source.DefinePrim("/World", "Xform").CreateAttribute(
        "user:value",
        Sdf.ValueTypeNames.Double,
        custom=True,
    )
    emitter = NoticeEmitter(source)
    emitter.snapshot_events()

    attr.SetDocumentation("root")
    source.SetEditTarget(source.GetSessionLayer())
    attr.SetDocumentation("session")
    with pytest.raises(RuntimeError, match="multiple USD layers"):
        emitter.build_events_for_dirty()
    emitter.cleanup()


def test_inactive_variant_removal_emits_exact_subtree_tombstones():
    source = Usd.Stage.CreateInMemory()
    world = source.DefinePrim("/World", "Xform")
    variants = world.GetVariantSets().AddVariantSet("look")
    for name in ("red", "blue"):
        variants.AddVariant(name)
        variants.SetVariantSelection(name)
        with variants.GetVariantEditContext():
            source.DefinePrim("/World/Child", "Scope")
    variants.SetVariantSelection("red")

    emitter = NoticeEmitter(source)
    target = Usd.Stage.CreateInMemory()
    apply_events(target, emitter.snapshot_events())

    variant_set = source.GetRootLayer().GetObjectAtPath("/World{look=}")
    variant_set.RemoveVariant(variant_set.variants["blue"])
    events = emitter.build_events_for_dirty()
    removed_paths = [event["spec_path"] for event in _sdf_events(events) if event["removed"]]
    assert removed_paths == [
        "/World{look=blue}Child",
        "/World{look=blue}",
    ]
    apply_events(target, events)
    assert not target.GetRootLayer().GetObjectAtPath("/World{look=blue}")
    assert target.GetRootLayer().GetObjectAtPath("/World{look=red}Child")
    emitter.cleanup()


def test_usd_variant_edit_context_emits_inactive_child_removal():
    source = Usd.Stage.CreateInMemory()
    world = source.DefinePrim("/World", "Xform")
    variants = world.GetVariantSets().AddVariantSet("look")
    variants.AddVariant("active")
    variants.AddVariant("inactive")
    variants.SetVariantSelection("active")
    emitter = NoticeEmitter(source)
    target = Usd.Stage.CreateInMemory()
    apply_events(target, emitter.snapshot_events())

    variants.SetVariantSelection("inactive")
    with variants.GetVariantEditContext():
        source.DefinePrim("/World/Probe", "Scope")
    variants.SetVariantSelection("active")
    added = emitter.build_events_for_dirty()
    apply_events(target, added)
    exact_path = "/World{look=inactive}Probe"
    assert target.GetRootLayer().GetPrimAtPath(exact_path)

    variants.SetVariantSelection("inactive")
    with variants.GetVariantEditContext():
        source.RemovePrim("/World/Probe")
    variants.SetVariantSelection("active")
    removed = _sdf_events(emitter.build_events_for_dirty())
    assert [(event["spec_path"], event["removed"]) for event in removed] == [
        (exact_path, True),
    ]
    apply_events(target, removed)
    assert not target.GetRootLayer().GetPrimAtPath(exact_path)
    emitter.cleanup()


def test_namespace_edit_of_variant_child_roundtrips_layer_relocates():
    source = Usd.Stage.CreateInMemory()
    world = source.DefinePrim("/World", "Xform")
    variants = world.GetVariantSets().AddVariantSet("look")
    variants.AddVariant("A")
    variants.SetVariantSelection("A")
    with variants.GetVariantEditContext():
        source.DefinePrim("/World/Child", "Scope")

    emitter = NoticeEmitter(source)
    target = Usd.Stage.CreateInMemory()
    apply_events(target, emitter.snapshot_events())

    editor = Usd.NamespaceEditor(source)
    assert editor.RenamePrim(source.GetPrimAtPath("/World/Child"), "Renamed")
    assert editor.ApplyEdits()
    events = emitter.build_events_for_dirty()
    layer_events = [
        event for event in _sdf_events(events) if event["spec_kind"] == SDF_SPEC_KIND_LAYER
    ]
    assert len(layer_events) == 1
    assert layer_events[0]["fields"] == ["layerRelocates"]

    apply_events(target, events)
    assert target.GetPrimAtPath("/World/Renamed")
    assert not target.GetPrimAtPath("/World/Child")
    assert target.GetRootLayer().GetPrimAtPath("/World{look=A}Child")
    emitter.cleanup()


def test_compaction_merges_fields_and_replays_exact_state(tmp_path):
    source = Usd.Stage.CreateInMemory()
    prim = source.DefinePrim("/World/Thing", "Xform")
    attr = prim.CreateAttribute("userProperties:value", Sdf.ValueTypeNames.Double, True)
    attr.Set(1.23456789012345)
    emitter = NoticeEmitter(source)
    server = UsdSyncServer(log_path=str(tmp_path / "sdf-delta.db"))

    server.process_txn(emitter.snapshot_events())
    attr.SetDocumentation("after initial value")
    attr.SetCustomData({"reviewed": True})
    server.process_txn(emitter.build_events_for_dirty())
    attr.ClearDocumentation()
    server.process_txn(emitter.build_events_for_dirty())
    server.compact_log()

    replay = [message_to_dict(record)["event"] for _seq, record in server.store.get_all_asc()]
    target = Usd.Stage.CreateInMemory()
    apply_events(target, replay)
    path = "/World/Thing.userProperties:value"
    assert _property_info(source.GetRootLayer(), path) == _property_info(
        target.GetRootLayer(), path
    )
    assert len([event for event in replay if event["k"] == K_SET_SDF_SPEC_FIELDS]) == 1


def test_compaction_replays_layer_prim_and_variant_lifecycle(tmp_path):
    source = Usd.Stage.CreateInMemory()
    source.GetRootLayer().documentation = "initial layer documentation"
    world = source.DefinePrim("/World", "Xform")
    world.SetDocumentation("initial prim documentation")
    variants = world.GetVariantSets().AddVariantSet("look")
    for name in ("red", "blue"):
        variants.AddVariant(name)
        variants.SetVariantSelection(name)
        with variants.GetVariantEditContext():
            child = source.DefinePrim("/World/Child", "Scope")
            child.SetDocumentation(f"{name} child")
    variants.SetVariantSelection("red")

    emitter = NoticeEmitter(source)
    db = str(tmp_path / "sdf-spec-lifecycle.db")
    server = UsdSyncServer(log_path=db)
    try:
        server.process_txn(emitter.snapshot_events())
        source.GetRootLayer().documentation = "final layer documentation"
        world.SetDocumentation("final prim documentation")
        world.SetCustomData({"reviewed": True})
        server.process_txn(emitter.build_events_for_dirty())

        variant_set = source.GetRootLayer().GetObjectAtPath("/World{look=}")
        variant_set.RemoveVariant(variant_set.variants["blue"])
        server.process_txn(emitter.build_events_for_dirty())
        server.compact_log()

        replay = [message_to_dict(record)["event"] for _seq, record in server.store.get_all_asc()]
        generic_keys = [
            (event["spec_kind"], event["spec_path"])
            for event in replay
            if event["k"] == K_SET_SDF_SPEC_FIELDS
        ]
        assert len(generic_keys) == len(set(generic_keys))

        target = Usd.Stage.CreateInMemory()
        apply_events(target, replay)
        assert target.GetRootLayer().documentation == "final layer documentation"
        assert target.GetPrimAtPath("/World").GetDocumentation() == "final prim documentation"
        assert dict(target.GetRootLayer().GetPrimAtPath("/World").customData) == {
            "reviewed": True,
        }
        target_variants = target.GetPrimAtPath("/World").GetVariantSets().GetVariantSet("look")
        assert target_variants.GetVariantNames() == ["red"]
        assert target.GetRootLayer().GetPrimAtPath("/World{look=red}Child")
        assert "__openusdconnect_fragment__" not in target.GetRootLayer().ExportToString()
    finally:
        emitter.cleanup()
        server.shutdown()
        server.store.close()

    emitter.cleanup()
    server.store.close()
