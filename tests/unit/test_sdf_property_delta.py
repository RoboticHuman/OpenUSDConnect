"""Sdf field-delta coverage for generic property state."""

from __future__ import annotations

from pxr import Sdf, Usd, UsdGeom, UsdShade, Vt

from openusdconnect.codec import encode_message, message_to_dict
from openusdconnect.emitter import NoticeEmitter
from openusdconnect.event_apply import apply_events
from openusdconnect.protocol_constants import K_SET_GPRIM_ATTRS, K_SET_SDF_PROPERTY_FIELDS, MSG_TXN
from openusdconnect.sdf_property_delta import (
    apply_property_spec_delta,
    composed_property_spec_event,
)
from openusdconnect.server import UsdSyncServer


def _property_info(layer: Sdf.Layer, path: str) -> dict:
    spec = layer.GetPropertyAtPath(path)
    return {str(key): spec.GetInfo(key) for key in spec.ListInfoKeys()}


def _sdf_events(events: list[dict]) -> list[dict]:
    return [event for event in events if event["k"] == K_SET_SDF_PROPERTY_FIELDS]


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
            "k": K_SET_SDF_PROPERTY_FIELDS,
            "prim": "/World/Thing",
            "spec_path": path,
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
        "k": K_SET_SDF_PROPERTY_FIELDS,
        "prim": "/World/Thing",
        "spec_path": "/World/Thing.userProperties:value",
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

    apply_property_spec_delta(
        target,
        {
            "spec_path": path,
            "fields": ["documentation"],
            "fragment": source.GetRootLayer().ExportToString(),
            "removed": False,
        },
    )

    target_attr = target.GetAttributeAtPath(path)
    assert target_attr.GetDocumentation() == "selected"
    assert target_attr.Get() is None


def test_composed_projection_follows_usd_field_resolution():
    weak = Sdf.Layer.CreateAnonymous("weak.usda")
    source = Usd.Stage.Open(weak)
    prim = source.DefinePrim("/World/Thing", "Xform")
    value = prim.CreateAttribute(
        "userProperties:value",
        Sdf.ValueTypeNames.Int,
        custom=True,
    )
    value.Set(1)
    value.SetCustomData({"weak": 1})
    samples = prim.CreateAttribute(
        "userProperties:samples",
        Sdf.ValueTypeNames.Float,
        custom=True,
    )
    samples.Set(1.0, 1.0)
    samples.Set(2.0, 2.0)
    relation = prim.CreateRelationship("userProperties:targets", custom=True)
    relation.SetTargets([Sdf.Path("/World/A")])
    declaration = prim.CreateAttribute(
        "userProperties:declaration",
        Sdf.ValueTypeNames.Float,
        custom=True,
    )
    declaration.Set(1.0)

    strong = source.GetSessionLayer()
    source.SetEditTarget(strong)
    value.Set(Sdf.ValueBlock())
    value.SetCustomData({"strong": 2})
    samples.Set(3.0, 3.0)
    samples.Set(Sdf.ValueBlock(), 4.0)
    relation.AddTarget(
        Sdf.Path("/World/B"),
        Usd.ListPositionFrontOfPrependList,
    )
    declaration.SetMetadata("custom", False)
    declaration.SetDocumentation("strong documentation")

    target = Usd.Stage.CreateInMemory()
    requests = [
        ("/World/Thing.userProperties:value", ["default", "customData"]),
        ("/World/Thing.userProperties:samples", ["timeSamples"]),
        ("/World/Thing.userProperties:targets", ["targetPaths"]),
        ("/World/Thing.userProperties:declaration", ["documentation"]),
    ]
    corrections = [
        composed_property_spec_event(
            source,
            {
                "k": K_SET_SDF_PROPERTY_FIELDS,
                "prim": "/World/Thing",
                "spec_path": path,
                "fields": fields,
                "removed": False,
            },
        )
        for path, fields in requests
    ]
    apply_events(target, corrections)

    target_value = target.GetAttributeAtPath(requests[0][0])
    assert target_value.Get() is None
    assert isinstance(
        target.GetRootLayer().GetAttributeAtPath(requests[0][0]).default,
        Sdf.ValueBlock,
    )
    assert target_value.GetCustomData() == {"strong": 2, "weak": 1}

    target_samples = target.GetAttributeAtPath(requests[1][0])
    assert target_samples.GetTimeSamples() == samples.GetTimeSamples() == [3.0, 4.0]
    assert target_samples.Get(3.0) == samples.Get(3.0) == 3.0
    assert target_samples.Get(4.0) is None
    assert isinstance(
        target.GetRootLayer().GetAttributeAtPath(requests[1][0]).QueryTimeSample(4.0),
        Sdf.ValueBlock,
    )

    target_relation = target.GetRelationshipAtPath(requests[2][0])
    assert target_relation.GetTargets() == relation.GetTargets() == [
        Sdf.Path("/World/B"),
        Sdf.Path("/World/A"),
    ]

    target_declaration = target.GetAttributeAtPath(requests[3][0])
    assert target_declaration.GetDocumentation() == "strong documentation"
    assert target_declaration.IsCustom() is True


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
    assert len([event for event in replay if event["k"] == K_SET_SDF_PROPERTY_FIELDS]) == 1

    emitter.cleanup()
    server.store.close()
