"""Semantic validation raises actionable ToolErrors and auto-creates ancestors."""

from __future__ import annotations

import pytest
from pxr import Usd

from integrations.mcp.errors import ToolError
from integrations.mcp.validation import validate_and_prepare


def _stage_with(*prim_paths):
    stage = Usd.Stage.CreateInMemory()
    for p in prim_paths:
        stage.DefinePrim(p, "Xform")
    return stage


def test_good_batch_passes():
    events = [
        {"k": "ensure_prim", "prim": "/World", "typeName": "Xform"},
        {"k": "ensure_prim", "prim": "/World/Mesh", "typeName": "Mesh"},
        {"k": "set_gprim_attrs", "prim": "/World/Mesh", "attrs": {"points": [[0, 0, 0]]}},
    ]
    prepared, warnings = validate_and_prepare(events, stage=Usd.Stage.CreateInMemory())
    assert [e["prim"] for e in prepared] == ["/World", "/World/Mesh", "/World/Mesh"]
    assert warnings == []


def test_invalid_prim_path_raises():
    with pytest.raises(ToolError) as ei:
        validate_and_prepare([{"k": "ensure_prim", "prim": "World/x", "typeName": "Xform"}])
    assert ei.value.field == "prim"


def test_unknown_type_name_raises():
    with pytest.raises(ToolError) as ei:
        validate_and_prepare([{"k": "ensure_prim", "prim": "/W", "typeName": "Bogus"}])
    assert ei.value.code == "unknown_type"


def test_abstract_type_raises():
    # Imageable is a typed but abstract (non-concrete) schema.
    with pytest.raises(ToolError) as ei:
        validate_and_prepare([{"k": "ensure_prim", "prim": "/W", "typeName": "Imageable"}])
    assert ei.value.code in ("abstract_type", "unknown_type")


def test_unknown_api_schema_raises():
    with pytest.raises(ToolError) as ei:
        validate_and_prepare(
            [{"k": "ensure_prim", "prim": "/W", "typeName": "Xform", "api_schemas": ["BogusAPI"]}]
        )
    assert ei.value.code == "unknown_api_schema"


def test_multi_apply_requires_instance():
    with pytest.raises(ToolError) as ei:
        validate_and_prepare(
            [
                {
                    "k": "ensure_prim",
                    "prim": "/W",
                    "typeName": "Xform",
                    "api_schemas": ["CollectionAPI"],
                }
            ]
        )
    assert ei.value.code == "api_schema_instance"


def test_collection_api_with_instance_ok():
    prepared, _ = validate_and_prepare(
        [
            {
                "k": "ensure_prim",
                "prim": "/W",
                "typeName": "Xform",
                "api_schemas": ["CollectionAPI:render"],
            }
        ]
    )
    assert prepared[0]["api_schemas"] == ["CollectionAPI:render"]


def test_bad_input_type_raises():
    ev = {
        "k": "set_connectable_input",
        "prim": "/M/S",
        "info_id": "UsdPreviewSurface",
        "inputs": {"roughness": 0.5},
        "input_types": {"roughness": "Float"},  # wrong case, must be 'float'
    }
    with pytest.raises(ToolError) as ei:
        validate_and_prepare([ev])
    assert ei.value.code == "unknown_sdf_type"


def test_bad_primvar_type_raises():
    ev = {
        "k": "set_gprim_attrs",
        "prim": "/M",
        "attrs": {"primvars:st": [[0, 0]]},
        "primvar_meta": {"primvars:st": {"typeName": "TexCoord2fArray"}},  # must be 'texCoord2f[]'
    }
    with pytest.raises(ToolError) as ei:
        validate_and_prepare([ev])
    assert ei.value.code == "unknown_sdf_type"


def test_dangling_connection_source_raises_with_stage():
    ev = {
        "k": "set_connectable_connection",
        "prim": "/M",
        "connections": {
            "outputs:surface": {"source_prim": "/M/Surf", "source_attr": "outputs:surface"}
        },
    }
    with pytest.raises(ToolError) as ei:
        validate_and_prepare([ev], stage=Usd.Stage.CreateInMemory())
    assert ei.value.code == "missing_source"


def test_connection_source_created_in_batch_ok():
    events = [
        {"k": "ensure_prim", "prim": "/M", "typeName": "Material"},
        {"k": "ensure_prim", "prim": "/M/Surf", "typeName": "Shader"},
        {
            "k": "set_connectable_connection",
            "prim": "/M",
            "connections": {
                "outputs:surface": {"source_prim": "/M/Surf", "source_attr": "outputs:surface"}
            },
        },
    ]
    prepared, _ = validate_and_prepare(events, stage=Usd.Stage.CreateInMemory())
    assert any(e["k"] == "set_connectable_connection" for e in prepared)


def test_ancestors_auto_created():
    events = [{"k": "ensure_prim", "prim": "/World/Group/Mesh", "typeName": "Mesh"}]
    prepared, _ = validate_and_prepare(events, stage=Usd.Stage.CreateInMemory())
    paths = [e["prim"] for e in prepared]
    assert paths == ["/World", "/World/Group", "/World/Group/Mesh"]
    assert all(e["typeName"] == "Xform" for e in prepared[:2])


def test_existing_ancestors_not_recreated():
    stage = _stage_with("/World")
    events = [{"k": "ensure_prim", "prim": "/World/Mesh", "typeName": "Mesh"}]
    prepared, _ = validate_and_prepare(events, stage=stage)
    assert [e["prim"] for e in prepared] == ["/World/Mesh"]


def test_stage_metadata_requires_a_field():
    with pytest.raises(ToolError):
        validate_and_prepare([{"k": "set_stage_metadata"}])


def test_stage_metadata_bad_up_axis():
    with pytest.raises(ToolError) as ei:
        validate_and_prepare([{"k": "set_stage_metadata", "upAxis": "Q"}])
    assert ei.value.field == "upAxis"


def test_info_id_warning_when_node_unknown():
    ev = {
        "k": "set_connectable_input",
        "prim": "/M/S",
        "info_id": "ND_not_a_real_node",
        "inputs": {},
        "input_types": {},
    }
    _, warnings = validate_and_prepare([ev], node_exists=lambda i: False)
    assert warnings and "ND_not_a_real_node" in warnings[0]
