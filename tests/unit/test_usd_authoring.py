"""Public authoring helpers work without protocol event construction."""

import numpy as np
import pytest
from pxr import Gf, Sdf, Usd, UsdShade, Vt

from openusdconnect import event_apply
from openusdconnect.codec import encode_message, message_to_dict
from openusdconnect.connectable_attrs import input_attr, output_attr
from openusdconnect.usd_authoring import (
    resolve_shader_port_type,
    set_connectable_input_value,
)


@pytest.mark.parametrize("schema", ["Shader", "NodeGraph", "Material", "SphereLight"])
def test_authors_connectable_inputs_on_current_edit_target(schema):
    stage = Usd.Stage.CreateInMemory()
    prim = stage.DefinePrim("/Target", schema)
    stage.SetEditTarget(stage.GetSessionLayer())
    connectable = UsdShade.ConnectableAPI(prim)

    set_connectable_input_value(connectable, "weight", 1, "float")
    set_connectable_input_value(connectable, "weight", 0.5, "float", Usd.TimeCode(12))

    inp = connectable.GetInput("weight")
    assert inp.Get() == 1.0
    assert inp.Get(Usd.TimeCode(12)) == 0.5
    assert inp.GetAttr().GetTimeSamples() == [12.0]
    assert stage.GetSessionLayer().GetAttributeAtPath("/Target.inputs:weight")
    assert not stage.GetRootLayer().GetAttributeAtPath("/Target.inputs:weight")


@pytest.mark.parametrize(
    ("type_name", "value", "expected"),
    [
        ("color3f", [0.25, 0.5, 1], Gf.Vec3f(0.25, 0.5, 1)),
        ("double3", [1, 2, 3], Gf.Vec3d(1, 2, 3)),
        ("texCoord2f", [0.25, 0.5], Gf.Vec2f(0.25, 0.5)),
        ("double2", [1, 2], Gf.Vec2d(1, 2)),
        ("color4f", [1, 2, 3, 4], Gf.Vec4f(1, 2, 3, 4)),
        ("double4", [1, 2, 3, 4], Gf.Vec4d(1, 2, 3, 4)),
        ("matrix2d", [1, 0, 0, 1], Gf.Matrix2d(1)),
        ("matrix3d", [1, 0, 0, 0, 1, 0, 0, 0, 1], Gf.Matrix3d(1)),
        ("matrix4d", [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], Gf.Matrix4d(1)),
        ("int[]", [1, 2], Vt.IntArray([1, 2])),
        ("float[]", [0.25, 0.5], Vt.FloatArray([0.25, 0.5])),
        ("token[]", ["a", "b"], Vt.TokenArray(["a", "b"])),
        ("string[]", ["a", "b"], Vt.StringArray(["a", "b"])),
        ("float", 2, 2.0),
        ("int", 2.0, 2),
    ],
)
def test_converts_python_values(type_name, value, expected):
    stage = Usd.Stage.CreateInMemory()
    connectable = UsdShade.Shader.Define(stage, "/Shader").ConnectableAPI()
    set_connectable_input_value(connectable, "value", value, type_name)
    inp = connectable.GetInput("value")
    assert inp.GetTypeName() == Sdf.ValueTypeNames.Find(type_name)
    assert inp.Get() == expected


@pytest.mark.parametrize(("type_name", "value", "expected"), [
    ("float[]", [0.25, 0.5], Vt.FloatArray([0.25, 0.5])),
    ("int[]", [1, 2], Vt.IntArray([1, 2])),
    ("float[]", [], Vt.FloatArray()),
    ("int[]", [], Vt.IntArray()),
    ("color3f", [0.25, 0.5, 1], Gf.Vec3f(0.25, 0.5, 1)),
    ("matrix4d", [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], Gf.Matrix4d(1)),
])
def test_buffer_view_inputs_roundtrip_and_apply(type_name, value, expected):
    event = {"k": "set_connectable_input", "prim": "/Shader", "info_id": "TestShader",
             "inputs": {"value": value}, "input_types": {"value": type_name}, "time": 12}
    wire = encode_message({"type": "event", "seq": 1, "event": event})
    decoded = message_to_dict(wire, numpy_arrays=True)["event"]
    array = decoded["inputs"]["value"]
    assert isinstance(array, np.ndarray)
    assert not array.flags.owndata
    if value:
        assert np.shares_memory(array, np.frombuffer(wire, dtype=np.uint8))
    assert message_to_dict(wire)["event"]["inputs"]["value"] == value
    assert message_to_dict(encode_message({"type": "event", "seq": 2, "event": decoded}))[
        "event"
    ] == event
    stage = Usd.Stage.CreateInMemory()
    shader = UsdShade.Shader.Define(stage, "/Shader")
    event_apply.apply_events(stage, [decoded])
    inp = shader.GetInput("value")
    assert inp.GetTypeName() == Sdf.ValueTypeNames.Find(type_name)
    assert inp.Get(Usd.TimeCode(12)) == expected
    assert inp.GetAttr().GetTimeSamples() == [12.0]


def test_asset_paths_can_be_set_and_cleared():
    stage = Usd.Stage.CreateInMemory()
    connectable = UsdShade.Shader.Define(stage, "/Shader").ConnectableAPI()
    set_connectable_input_value(connectable, "file", "texture.exr", "asset")
    assert connectable.GetInput("file").Get().path == "texture.exr"
    set_connectable_input_value(connectable, "file", "", "asset")
    assert connectable.GetInput("file").Get() == Sdf.AssetPath()


def test_unknown_type_is_noop_and_existing_input_type_is_preserved():
    stage = Usd.Stage.CreateInMemory()
    connectable = UsdShade.Shader.Define(stage, "/Shader").ConnectableAPI()
    set_connectable_input_value(connectable, "missing", 1, "not-a-type")
    assert not connectable.GetInput("missing")
    connectable.CreateInput("existing", Sdf.ValueTypeNames.Double).Set(0.5)
    set_connectable_input_value(connectable, "existing", 1, "float")
    assert connectable.GetInput("existing").GetTypeName() == Sdf.ValueTypeNames.Double
    assert connectable.GetInput("existing").Get() == 1.0
    set_connectable_input_value(connectable, "existing", 2, "not-a-type")
    assert connectable.GetInput("existing").Get() == 1.0


def test_resolves_sdr_ports_on_untyped_override_without_authoring():
    stage = Usd.Stage.CreateInMemory()
    prim = stage.OverridePrim("/Shader")
    prim.CreateAttribute("info:id", Sdf.ValueTypeNames.Token).Set("UsdPreviewSurface")
    before = stage.GetRootLayer().ExportToString()
    assert resolve_shader_port_type(prim, input_attr("diffuseColor")) == Sdf.ValueTypeNames.Color3f
    assert resolve_shader_port_type(prim, output_attr("surface")) == Sdf.ValueTypeNames.Token
    assert resolve_shader_port_type(prim, input_attr("missing")) is None
    assert resolve_shader_port_type(prim, output_attr("missing")) is None
    assert stage.GetRootLayer().ExportToString() == before
    assert not prim.GetTypeName()


def test_unresolvable_shader_returns_none():
    stage = Usd.Stage.CreateInMemory()
    assert resolve_shader_port_type(stage.GetPrimAtPath("/Missing"), input_attr("x")) is None
    prim = stage.OverridePrim("/Shader")
    assert resolve_shader_port_type(prim, input_attr("x")) is None
    prim.CreateAttribute("info:id", Sdf.ValueTypeNames.Token).Set("UnregisteredTestShader")
    assert resolve_shader_port_type(prim, input_attr("x")) is None


def test_private_event_helpers_remain_identity_aliases():
    assert event_apply._set_connectable_input_value is set_connectable_input_value
    assert event_apply._resolve_shader_port_type is resolve_shader_port_type
