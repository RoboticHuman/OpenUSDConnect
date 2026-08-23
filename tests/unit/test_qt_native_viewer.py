"""Headless checks for the Qt example's non-USD adapter boundary."""

from pxr import Sdf, Usd

from examples.qt_native_viewer.native_scene import NativeScene, NativeSceneAdapter
from openusdconnect import DCCAdapter
from openusdconnect.composed_projection import ComposedChangeProjection, ComposedProjectionState
from openusdconnect.event_apply import apply_events


def _apply_to_layer(stage: Usd.Stage, layer: Sdf.Layer, events: list[dict]) -> None:
    with Usd.EditContext(stage, Usd.EditTarget(layer)):
        apply_events(stage, events)


def _create_xform(x: float) -> list[dict]:
    return [
        {"k": "ensure_prim", "prim": "/World/Cube", "typeName": "Cube"},
        {"k": "ensure_xform_ops", "prim": "/World/Cube"},
        {
            "k": "set_xform_trs",
            "prim": "/World/Cube",
            "fields": ["t"],
            "t": [x, 0.0, 0.0],
        },
    ]


def test_native_scene_adapter_builds_application_owned_scene():
    scene = NativeScene()
    delivered = []
    adapter = NativeSceneAdapter(scene, lambda events: delivered.extend(events))

    events = [
        {"k": "ensure_prim", "prim": "/World/Cube", "typeName": "Cube"},
        {"k": "ensure_xform_ops", "prim": "/World/Cube"},
        {
            "k": "set_xform_trs",
            "prim": "/World/Cube",
            "fields": ["t", "s"],
            "t": [2.0, 3.0, 0.0],
            "s": [1.5, 1.0, 1.0],
        },
        {
            "k": "set_gprim_attrs",
            "prim": "/World/Cube",
            "attrs": {"displayColor": [[0.1, 0.4, 0.9]]},
        },
    ]

    assert adapter.targets_stage() is None
    assert type(adapter).__mro__[1] is DCCAdapter
    assert adapter.apply_events(events) == len(events)
    assert delivered == events
    assert scene.objects["/World/Cube"].type_name == "Cube"
    assert scene.objects["/World/Cube"].translation == [2.0, 3.0, 0.0]
    assert scene.objects["/World/Cube"].scale == [1.5, 1.0, 1.0]
    assert scene.objects["/World/Cube"].attributes["displayColor"] == [[0.1, 0.4, 0.9]]


def test_native_scene_adapter_reset_clears_application_state():
    scene = NativeScene()
    adapter = NativeSceneAdapter(scene)
    adapter.apply_events([{"k": "ensure_prim", "prim": "/World/Cube", "typeName": "Cube"}])

    adapter.reset()

    assert scene.objects == {}


def test_native_scene_adapter_dispatches_the_public_integration_contract():
    scene = NativeScene()
    adapter = NativeSceneAdapter(scene)
    events = [
        {"k": "set_stage_metadata", "upAxis": "Y", "metersPerUnit": 0.01},
        {"k": "ensure_prim", "prim": "/World/Asset", "typeName": "Cube"},
        {"k": "ensure_xform_ops", "prim": "/World/Asset"},
        {
            "k": "set_xform_trs",
            "prim": "/World/Asset",
            "fields": ["t"],
            "t": [1.0, 2.0, 3.0],
            "time": 24.0,
        },
        {
            "k": "set_visibility",
            "prim": "/World/Asset",
            "visible": False,
            "time": 24.0,
        },
        {
            "k": "set_gprim_attrs",
            "prim": "/World/Asset",
            "attrs": {"size": 2.0},
            "time": 24.0,
        },
        {
            "k": "set_reference",
            "prim": "/World/Asset",
            "refs": [{"asset_path": "./asset.usda", "prim_path": "/Model"}],
        },
        {
            "k": "set_payload",
            "prim": "/World/Asset",
            "payloads": [{"asset_path": "./detail.usda"}],
        },
        {"k": "load_payload", "prim": "/World/Asset"},
        {"k": "unload_payload", "prim": "/World/Asset"},
        {
            "k": "set_variant_selections",
            "prim": "/World/Asset",
            "selections": {"look": "red"},
        },
        {"k": "set_instanceable", "prim": "/World/Asset", "instanceable": True},
        {"k": "ensure_prim", "prim": "/World/Looks/Red", "typeName": "Material"},
        {
            "k": "ensure_prim",
            "prim": "/World/Looks/Red/Surface",
            "typeName": "Shader",
        },
        {
            "k": "set_connectable_input",
            "prim": "/World/Looks/Red/Surface",
            "info_id": "UsdPreviewSurface",
            "inputs": {"roughness": 0.25},
            "input_types": {"roughness": "float"},
        },
        {
            "k": "set_connectable_connection",
            "prim": "/World/Looks/Red",
            "connections": {
                "outputs:surface": {
                    "source_prim": "/World/Looks/Red/Surface",
                    "source_attr": "outputs:surface",
                }
            },
        },
        {
            "k": "set_material_binding",
            "prim": "/World/Asset",
            "material_path": "/World/Looks/Red",
            "material_purpose": "preview",
        },
        {
            "k": "ensure_prim",
            "prim": "/World/Crowd",
            "typeName": "PointInstancer",
        },
        {
            "k": "set_point_instancer",
            "prim": "/World/Crowd",
            "fields": ["prototypes", "positions", "proto_indices"],
            "prototypes": ["/World/Prototype"],
            "positions": [[0.0, 1.0, 0.0]],
            "proto_indices": [0],
        },
        {"k": "ensure_prim", "prim": "/World/Old", "typeName": "Xform"},
        {"k": "ensure_prim", "prim": "/World/Old/Child", "typeName": "Sphere"},
        {"k": "rename_prim", "prim": "/World/Old", "new_name": "Renamed"},
        {"k": "deactivate_prim", "prim": "/World/Renamed", "active": False},
        {"k": "delete_prim", "prim": "/World/Renamed"},
    ]

    assert adapter.apply_events(events) == len(events)

    asset = scene.objects["/World/Asset"]
    assert scene.stage_metadata == {"metersPerUnit": 0.01, "upAxis": "Y"}
    assert asset.transform_samples[24.0]["t"] == [1.0, 2.0, 3.0]
    assert asset.visibility_samples[24.0] is False
    assert asset.attribute_samples[24.0]["size"] == 2.0
    assert asset.references[0]["asset_path"] == "./asset.usda"
    assert asset.payloads == [{"asset_path": "./detail.usda"}]
    assert asset.payload_loaded is False
    assert asset.variant_selections == {"look": "red"}
    assert asset.instanceable is True
    assert asset.material_bindings["preview"] == "/World/Looks/Red"
    shader = scene.objects["/World/Looks/Red/Surface"]
    assert shader.shader_id == "UsdPreviewSurface"
    assert shader.connectable_inputs == {"roughness": 0.25}
    material = scene.objects["/World/Looks/Red"]
    assert material.type_name == "Material"
    assert material.connectable_connections["outputs:surface"]["source_prim"] == (
        "/World/Looks/Red/Surface"
    )
    assert scene.objects["/World/Crowd"].point_instancer == {
        "prototypes": ["/World/Prototype"],
        "proto_indices": [0],
        "positions": [[0.0, 1.0, 0.0]],
    }
    assert "/World/Old" not in scene.objects
    assert "/World/Renamed" not in scene.objects
    assert "/World/Renamed/Child" not in scene.objects


def test_native_scene_example_receives_composed_values_not_weaker_opinions():
    root = Sdf.Layer.CreateAnonymous("qt-example-root.usda")
    strong = Sdf.Layer.CreateAnonymous("animation.usda")
    weak = Sdf.Layer.CreateAnonymous("layout.usda")
    root.subLayerPaths = [strong.identifier, weak.identifier]
    stage = Usd.Stage.Open(root)
    state = ComposedProjectionState(stage)
    scene = NativeScene()
    adapter = NativeSceneAdapter(scene)

    weak_initial = _create_xform(-4.0)
    strong_initial = _create_xform(4.0)
    with ComposedChangeProjection(stage, weak_initial + strong_initial, state=state) as projection:
        _apply_to_layer(stage, weak, weak_initial)
        _apply_to_layer(stage, strong, strong_initial)
        adapter.apply_events(projection.build_events())
        projection.commit()
    assert scene.objects["/World/Cube"].translation == [4.0, 0.0, 0.0]

    masked_edit = [
        {
            "k": "set_xform_trs",
            "prim": "/World/Cube",
            "fields": ["t"],
            "t": [-1.0, 0.0, 0.0],
        }
    ]
    with ComposedChangeProjection(stage, masked_edit, state=state) as projection:
        _apply_to_layer(stage, weak, masked_edit)
        assert projection.build_events() == []
        projection.commit()
    assert scene.objects["/World/Cube"].translation == [4.0, 0.0, 0.0]

    reveal_weaker = [{"k": "delete_prim", "prim": "/World/Cube"}]
    with ComposedChangeProjection(stage, reveal_weaker, state=state) as projection:
        _apply_to_layer(stage, strong, reveal_weaker)
        adapter.apply_events(projection.build_events())
        projection.commit()
    assert scene.objects["/World/Cube"].translation == [-1.0, 0.0, 0.0]
