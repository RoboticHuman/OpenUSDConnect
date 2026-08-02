from __future__ import annotations

from pxr import Sdf, Usd, UsdShade, UsdVol

from openusdconnect.asset_paths import (
    stabilize_layer_asset_paths,
    transport_asset_identifier,
)
from openusdconnect.emitter import NoticeEmitter
from openusdconnect.event_apply import apply_events
from openusdconnect.sdf_arc_state import (
    deserialize_reference_custom_data,
    read_arc_state,
)
from openusdconnect.sdf_spec_delta import (
    SDF_SPEC_KIND_ATTRIBUTE,
    SDF_SPEC_KIND_PRIM,
    apply_spec_delta,
    serialize_composed_property_spec_fields,
    serialize_spec_fields,
)


def _usd_path(path) -> str:
    """Return a filesystem path in USD asset-identifier form."""
    return path.as_posix()


def test_transport_identifier_uses_owning_file_layer_as_anchor(tmp_path):
    source_path = tmp_path / "asset" / "looks" / "look.usda"
    source_path.parent.mkdir(parents=True)
    source = Sdf.Layer.CreateNew(str(source_path))

    identifier = transport_asset_identifier(source, "../textures/albedo.tx")

    assert identifier == _usd_path(tmp_path / "asset" / "textures" / "albedo.tx")
    assert "\\" not in identifier
    assert transport_asset_identifier(source, "search-path.tx") == "search-path.tx"
    assert transport_asset_identifier(source, "") == ""


def test_transport_identifier_preserves_anonymous_layer_text():
    source = Sdf.Layer.CreateAnonymous("source")

    assert transport_asset_identifier(source, "./textures/albedo.tx") == ("./textures/albedo.tx")


def test_transport_identifier_evaluates_expression_without_resolving_custom_uri(tmp_path):
    source = Sdf.Layer.CreateNew(str(tmp_path / "look.usda"))
    identifier = "asset:Buzz/{$VERSION}/albedo.tx"

    transported = transport_asset_identifier(
        source,
        '`"${URI}"`',
        expression_variables={"URI": identifier},
    )

    assert transported == identifier


def test_transport_identifier_preserves_invalid_expression(tmp_path, caplog):
    source = Sdf.Layer.CreateNew(str(tmp_path / "look.usda"))
    identifier = "`${MISSING}`"

    transported = transport_asset_identifier(source, identifier)

    assert transported == identifier
    assert "Could not evaluate asset-path expression" in caplog.text


def test_stabilize_layer_asset_paths_visits_nested_and_array_values(tmp_path):
    source_path = tmp_path / "asset" / "look.usda"
    source_path.parent.mkdir()
    source = Sdf.Layer.CreateNew(str(source_path))
    fragment = Sdf.Layer.CreateAnonymous("fragment")
    stage = Usd.Stage.Open(fragment)
    prim = stage.DefinePrim("/Look", "Shader")
    prim.CreateAttribute("file", Sdf.ValueTypeNames.Asset, custom=True).Set(
        Sdf.AssetPath("./textures/base.tx")
    )
    prim.CreateAttribute("files", Sdf.ValueTypeNames.AssetArray, custom=True).Set(
        [Sdf.AssetPath("./textures/a.tx"), Sdf.AssetPath()]
    )
    prim.SetCustomDataByKey(
        "resources",
        {"normal": Sdf.AssetPath("./textures/normal.tx")},
    )

    stabilize_layer_asset_paths(fragment, source)

    prefix = _usd_path(tmp_path / "asset")
    assert fragment.GetAttributeAtPath("/Look.file").default.path == (f"{prefix}/textures/base.tx")
    files = fragment.GetAttributeAtPath("/Look.files").default
    assert [value.path for value in files] == [f"{prefix}/textures/a.tx", ""]
    assert fragment.GetPrimAtPath("/Look").customData["resources"]["normal"].path == (
        f"{prefix}/textures/normal.tx"
    )


def test_connectable_event_anchors_file_relative_asset_input(tmp_path):
    source_path = tmp_path / "scene" / "look.usda"
    source_path.parent.mkdir()
    stage = Usd.Stage.CreateNew(str(source_path))
    shader = UsdShade.Shader.Define(stage, "/Texture")
    shader.CreateIdAttr("UsdUVTexture")
    shader.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath("./textures/albedo.tx"))
    emitter = NoticeEmitter(stage)

    event = next(
        event for event in emitter.snapshot_events() if event["k"] == "set_connectable_input"
    )

    assert event["inputs"]["file"] == _usd_path(source_path.parent / "textures" / "albedo.tx")
    emitter.cleanup()


def test_connectable_event_preserves_context_dependent_expression_identifier(tmp_path):
    stage = Usd.Stage.CreateNew(str(tmp_path / "look.usda"))
    identifier = "asset:Buzz/{$VERSION}/albedo.tx"
    stage.GetRootLayer().expressionVariables = {"URI": identifier}
    shader = UsdShade.Shader.Define(stage, "/Texture")
    shader.CreateIdAttr("UsdUVTexture")
    shader.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath('`"${URI}"`'))
    emitter = NoticeEmitter(stage)

    event = next(
        event for event in emitter.snapshot_events() if event["k"] == "set_connectable_input"
    )

    assert event["inputs"]["file"] == identifier
    emitter.cleanup()


def test_typed_schema_asset_attribute_uses_owning_layer_anchor(tmp_path):
    source_path = tmp_path / "scene" / "volume.usda"
    source_path.parent.mkdir()
    source = Usd.Stage.CreateNew(str(source_path))
    field = UsdVol.Field3DAsset.Define(source, "/Field")
    field.CreateFilePathAttr(Sdf.AssetPath("./volumes/density.vdb"))
    emitter = NoticeEmitter(source)

    events = emitter.snapshot_events()
    event = next(
        event for event in events if event["k"] == "set_gprim_attrs" and event["prim"] == "/Field"
    )
    expected = _usd_path(source_path.parent / "volumes" / "density.vdb")
    assert event["attrs"]["filePath"] == expected

    target = Usd.Stage.CreateInMemory()
    apply_events(target, events)
    assert target.GetAttributeAtPath("/Field.filePath").Get().path == expected
    emitter.cleanup()


def test_typed_schema_asset_time_sample_uses_owning_layer_anchor(tmp_path):
    source_path = tmp_path / "scene" / "volume.usda"
    source_path.parent.mkdir()
    source = Usd.Stage.CreateNew(str(source_path))
    field = UsdVol.Field3DAsset.Define(source, "/Field")
    file_path = field.CreateFilePathAttr()
    file_path.Set(Sdf.AssetPath("./volumes/frame-1.vdb"), 1.0)
    emitter = NoticeEmitter(source)

    event = next(
        event
        for event in emitter.snapshot_events()
        if event["k"] == "set_gprim_attrs"
        and event["prim"] == "/Field"
        and event.get("time") == 1.0
    )

    assert event["attrs"]["filePath"] == _usd_path(source_path.parent / "volumes" / "frame-1.vdb")
    emitter.cleanup()


def test_sdf_fragment_anchors_asset_value_before_anonymous_apply(tmp_path):
    source_path = tmp_path / "source" / "scene.usda"
    source_path.parent.mkdir()
    source = Usd.Stage.CreateNew(str(source_path))
    attr = source.DefinePrim("/World", "Xform").CreateAttribute(
        "user:file",
        Sdf.ValueTypeNames.Asset,
        custom=True,
    )
    attr.Set(Sdf.AssetPath("./textures/albedo.tx"))
    fragment = serialize_spec_fields(
        source.GetRootLayer(),
        attr.GetPath(),
        SDF_SPEC_KIND_ATTRIBUTE,
        ["default"],
    )
    target = Usd.Stage.CreateInMemory()

    apply_spec_delta(
        target,
        {
            "k": "set_sdf_spec_fields",
            "prim": "/World",
            "spec_path": str(attr.GetPath()),
            "spec_kind": SDF_SPEC_KIND_ATTRIBUTE,
            "fields": ["default"],
            "fragment": fragment,
            "removed": False,
        },
    )

    value = target.GetAttributeAtPath(attr.GetPath()).Get()
    assert value.path == _usd_path(source_path.parent / "textures" / "albedo.tx")


def test_composed_asset_projection_uses_the_value_owning_layer_anchor(tmp_path):
    weak_path = tmp_path / "weak" / "weak.usda"
    weak_path.parent.mkdir()
    weak = Usd.Stage.CreateNew(str(weak_path))
    weak_attr = weak.DefinePrim("/World", "Xform").CreateAttribute(
        "user:file",
        Sdf.ValueTypeNames.Asset,
        custom=True,
    )
    weak_attr.Set(Sdf.AssetPath("./textures/albedo.tx"))
    weak.GetRootLayer().Save()

    strong_path = tmp_path / "strong" / "strong.usda"
    strong_path.parent.mkdir()
    strong_layer = Sdf.Layer.CreateNew(str(strong_path))
    strong_layer.subLayerPaths = ["../weak/weak.usda"]
    strong = Usd.Stage.Open(strong_layer)
    strong_attr = strong.OverridePrim("/World").CreateAttribute(
        "user:file",
        Sdf.ValueTypeNames.Asset,
        custom=True,
    )
    strong_attr.SetDocumentation("A stronger declaration without a value")

    fragment = serialize_composed_property_spec_fields(
        strong,
        strong_attr.GetPath(),
        SDF_SPEC_KIND_ATTRIBUTE,
        ["default"],
    )
    target = Usd.Stage.CreateInMemory()
    apply_spec_delta(
        target,
        {
            "k": "set_sdf_spec_fields",
            "prim": "/World",
            "spec_path": str(strong_attr.GetPath()),
            "spec_kind": SDF_SPEC_KIND_ATTRIBUTE,
            "fields": ["default"],
            "fragment": fragment,
            "removed": False,
        },
    )

    value = target.GetAttributeAtPath(strong_attr.GetPath()).Get()
    assert value.path == _usd_path(weak_path.parent / "textures" / "albedo.tx")


def test_composed_asset_projection_flattens_referenced_property(tmp_path):
    asset_path = tmp_path / "asset" / "asset.usda"
    asset_path.parent.mkdir()
    asset = Usd.Stage.CreateNew(str(asset_path))
    model = asset.DefinePrim("/Model", "Xform")
    model.CreateAttribute("user:file", Sdf.ValueTypeNames.Asset, custom=True).Set(
        Sdf.AssetPath("./textures/albedo.tx")
    )
    asset.SetDefaultPrim(model)
    asset.GetRootLayer().Save()

    shot_path = tmp_path / "shot" / "shot.usda"
    shot_path.parent.mkdir()
    shot = Usd.Stage.CreateNew(str(shot_path))
    shot.OverridePrim("/World").GetReferences().AddReference("../asset/asset.usda")
    prop = shot.GetAttributeAtPath("/World.user:file")

    fragment = serialize_composed_property_spec_fields(
        shot,
        prop.GetPath(),
        SDF_SPEC_KIND_ATTRIBUTE,
        ["default"],
    )
    target = Usd.Stage.CreateInMemory()
    apply_spec_delta(
        target,
        {
            "k": "set_sdf_spec_fields",
            "prim": "/World",
            "spec_path": str(prop.GetPath()),
            "spec_kind": SDF_SPEC_KIND_ATTRIBUTE,
            "fields": ["default"],
            "fragment": fragment,
            "removed": False,
        },
    )

    value = target.GetAttributeAtPath(prop.GetPath()).Get()
    assert value.path == _usd_path(asset_path.parent / "textures" / "albedo.tx")


def test_composed_asset_projection_evaluates_expression_at_its_source_anchor(tmp_path):
    source_path = tmp_path / "scene" / "scene.usda"
    source_path.parent.mkdir()
    source = Usd.Stage.CreateNew(str(source_path))
    source.GetRootLayer().expressionVariables = {"FILE": "./textures/albedo.tx"}
    prop = source.DefinePrim("/World", "Xform").CreateAttribute(
        "user:file",
        Sdf.ValueTypeNames.Asset,
        custom=True,
    )
    prop.Set(Sdf.AssetPath('`"${FILE}"`'))

    fragment = serialize_composed_property_spec_fields(
        source,
        prop.GetPath(),
        SDF_SPEC_KIND_ATTRIBUTE,
        ["default"],
    )
    target = Usd.Stage.CreateInMemory()
    apply_spec_delta(
        target,
        {
            "k": "set_sdf_spec_fields",
            "prim": "/World",
            "spec_path": str(prop.GetPath()),
            "spec_kind": SDF_SPEC_KIND_ATTRIBUTE,
            "fields": ["default"],
            "fragment": fragment,
            "removed": False,
        },
    )

    value = target.GetAttributeAtPath(prop.GetPath()).Get()
    assert value.path == _usd_path(source_path.parent / "textures" / "albedo.tx")


def test_composed_asset_projection_uses_referenced_layer_stack_expression_variables(
    tmp_path,
):
    asset_path = tmp_path / "asset" / "asset.usda"
    asset_path.parent.mkdir()
    asset = Usd.Stage.CreateNew(str(asset_path))
    asset.GetRootLayer().expressionVariables = {"FILE": "./textures/albedo.tx"}
    model = asset.DefinePrim("/Model", "Xform")
    model.CreateAttribute("user:file", Sdf.ValueTypeNames.Asset, custom=True).Set(
        Sdf.AssetPath('`"${FILE}"`')
    )
    asset.SetDefaultPrim(model)
    asset.GetRootLayer().Save()

    shot_path = tmp_path / "shot" / "shot.usda"
    shot_path.parent.mkdir()
    shot = Usd.Stage.CreateNew(str(shot_path))
    shot.OverridePrim("/World").GetReferences().AddReference("../asset/asset.usda")
    prop = shot.GetAttributeAtPath("/World.user:file")

    fragment = serialize_composed_property_spec_fields(
        shot,
        prop.GetPath(),
        SDF_SPEC_KIND_ATTRIBUTE,
        ["default"],
    )
    target = Usd.Stage.CreateInMemory()
    apply_spec_delta(
        target,
        {
            "k": "set_sdf_spec_fields",
            "prim": "/World",
            "spec_path": str(prop.GetPath()),
            "spec_kind": SDF_SPEC_KIND_ATTRIBUTE,
            "fields": ["default"],
            "fragment": fragment,
            "removed": False,
        },
    )

    value = target.GetAttributeAtPath(prop.GetPath()).Get()
    assert value.path == _usd_path(asset_path.parent / "textures" / "albedo.tx")


def test_composed_asset_projection_preserves_dictionary_key_anchors(tmp_path):
    asset_path = tmp_path / "asset" / "asset.usda"
    asset_path.parent.mkdir()
    asset = Usd.Stage.CreateNew(str(asset_path))
    model = asset.DefinePrim("/Model", "Xform")
    asset_attr = model.CreateAttribute(
        "user:value",
        Sdf.ValueTypeNames.Int,
        custom=True,
    )
    asset_attr.Set(1)
    asset_attr.SetCustomDataByKey("weak", Sdf.AssetPath("./textures/weak.tx"))
    asset.SetDefaultPrim(model)
    asset.GetRootLayer().Save()

    shot_path = tmp_path / "shot" / "shot.usda"
    shot_path.parent.mkdir()
    shot = Usd.Stage.CreateNew(str(shot_path))
    shot.OverridePrim("/World").GetReferences().AddReference("../asset/asset.usda")
    prop = shot.GetAttributeAtPath("/World.user:value")
    prop.SetCustomDataByKey("strong", Sdf.AssetPath("./textures/strong.tx"))

    fragment = serialize_composed_property_spec_fields(
        shot,
        prop.GetPath(),
        SDF_SPEC_KIND_ATTRIBUTE,
        ["customData"],
    )
    target = Usd.Stage.CreateInMemory()
    apply_spec_delta(
        target,
        {
            "k": "set_sdf_spec_fields",
            "prim": "/World",
            "spec_path": str(prop.GetPath()),
            "spec_kind": SDF_SPEC_KIND_ATTRIBUTE,
            "fields": ["customData"],
            "fragment": fragment,
            "removed": False,
        },
    )

    custom_data = target.GetAttributeAtPath(prop.GetPath()).GetCustomData()
    assert custom_data["weak"].path == _usd_path(asset_path.parent / "textures" / "weak.tx")
    assert custom_data["strong"].path == _usd_path(shot_path.parent / "textures" / "strong.tx")


def test_reference_custom_data_asset_values_use_reference_layer_anchor(tmp_path):
    source_path = tmp_path / "source" / "scene.usda"
    source_path.parent.mkdir()
    layer = Sdf.Layer.CreateNew(str(source_path))
    spec = Sdf.CreatePrimInLayer(layer, "/World")
    spec.referenceList.prependedItems = [
        Sdf.Reference(
            "./model.usda",
            customData={"preview": Sdf.AssetPath("./textures/preview.png")},
        )
    ]

    event_state = read_arc_state(
        layer,
        "/World",
        "referenceList",
        absolute_asset_paths=True,
    )

    entry = event_state["entries"][0]
    assert entry["asset_path"] == _usd_path(source_path.parent / "model.usda")
    custom_data = deserialize_reference_custom_data(entry["custom_data_fragment"])
    assert custom_data["preview"].path == _usd_path(
        source_path.parent / "textures" / "preview.png"
    )


def test_sdf_fragment_preserves_reference_state_and_empty_asset_array_slots(tmp_path):
    source_path = tmp_path / "source" / "scene.usda"
    source_path.parent.mkdir()
    layer = Sdf.Layer.CreateNew(str(source_path))
    spec = Sdf.CreatePrimInLayer(layer, "/World")
    spec.referenceList.prependedItems = [
        Sdf.Reference(
            "./model.usda",
            "/Model",
            Sdf.LayerOffset(7.0, 2.0),
            customData={
                "previews": Sdf.AssetPathArray([Sdf.AssetPath("./preview.png"), Sdf.AssetPath()])
            },
        )
    ]

    fragment = serialize_spec_fields(
        layer,
        "/World",
        SDF_SPEC_KIND_PRIM,
        ["references"],
    )
    transported = Sdf.Layer.CreateAnonymous("transported")
    assert transported.ImportFromString(fragment)
    reference = transported.GetPrimAtPath("/World").referenceList.prependedItems[0]

    assert reference.assetPath == _usd_path(source_path.parent / "model.usda")
    assert reference.primPath == Sdf.Path("/Model")
    assert reference.layerOffset == Sdf.LayerOffset(7.0, 2.0)
    assert [value.path for value in reference.customData["previews"]] == [
        _usd_path(source_path.parent / "preview.png"),
        "",
    ]
