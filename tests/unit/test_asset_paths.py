from __future__ import annotations

from pxr import Sdf, Usd, UsdShade, UsdVol

from openusdconnect.asset_paths import (
    repair_missing_duplicate_asset_paths,
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


def _layer_with_asset_path(identifier: str):
    layer = Sdf.Layer.CreateAnonymous("asset-path")
    stage = Usd.Stage.Open(layer)
    stage.DefinePrim("/Look").CreateAttribute(
        "file", Sdf.ValueTypeNames.Asset, custom=True
    ).Set(Sdf.AssetPath(identifier))
    return layer


def test_duplicate_asset_path_repair_preserves_valid_intentional_directory(tmp_path):
    texture = tmp_path / "textures" / "textures" / "map.png"
    texture.parent.mkdir(parents=True)
    texture.write_bytes(b"image")
    identifier = _usd_path(texture)
    layer = _layer_with_asset_path(identifier)

    assert repair_missing_duplicate_asset_paths(layer) == 0
    assert layer.GetAttributeAtPath("/Look.file").default.path == identifier


def test_duplicate_asset_path_repair_preserves_unresolved_and_package_paths(tmp_path):
    unresolved = _usd_path(tmp_path / "textures" / "textures" / "missing.png")
    layer = _layer_with_asset_path(unresolved)
    assert repair_missing_duplicate_asset_paths(layer) == 0
    assert layer.GetAttributeAtPath("/Look.file").default.path == unresolved

    package = f"{_usd_path(tmp_path / 'archive.usdz')}[textures/textures/map.png]"
    layer = _layer_with_asset_path(package)
    assert repair_missing_duplicate_asset_paths(layer) == 0
    assert layer.GetAttributeAtPath("/Look.file").default.path == package


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
