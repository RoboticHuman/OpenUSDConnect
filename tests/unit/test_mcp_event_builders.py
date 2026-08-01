"""Each registry builder emits a correct event dict for its kind."""

from __future__ import annotations

import math

import pytest

from integrations.mcp.registry import TOOL_TABLE, euler_to_quat_wxyz
from openusdconnect.protocol_constants import (
    K_SET_STAGE_METADATA,
    POINT_INSTANCER_FIELDS,
)
from openusdconnect.protocol_validation import validate_event

# Representative args per kind, also the inputs the consistency loop validates.
SAMPLE_ARGS: dict[str, dict] = {
    "ensure_prim": {"prim": "/World/Mesh", "type_name": "Mesh"},
    "ensure_xform_ops": {"prim": "/World/Mesh"},
    "set_xform_trs": {"prim": "/World/Mesh", "t": [1.0, 2.0, 3.0]},
    "delete_prim": {"prim": "/World/Mesh"},
    "deactivate_prim": {"prim": "/World/Mesh", "active": False},
    "rename_prim": {"prim": "/World/Mesh", "new_name": "Mesh2"},
    "set_visibility": {"prim": "/World/Mesh", "visible": False},
    "set_gprim_attrs": {"prim": "/World/Mesh", "attrs": {"points": [[0, 0, 0]]}},
    "set_reference": {
        "prim": "/World/A",
        "refs": [{"asset_path": "/a.usda", "prim_path": "/Root"}],
    },
    "set_payload": {"prim": "/World/A", "payloads": [{"asset_path": "/a.usda"}]},
    "load_payload": {"prim": "/World/A"},
    "unload_payload": {"prim": "/World/A"},
    "set_variant_selections": {"prim": "/World/A", "selections": {"size": "large"}},
    "set_material_binding": {"prim": "/World/Mesh", "material_path": "/World/Looks/M"},
    "set_connectable_input": {
        "prim": "/World/Looks/M/Surf",
        "info_id": "UsdPreviewSurface",
        "inputs": {"roughness": 0.5},
        "input_types": {"roughness": "float"},
    },
    "set_connectable_connection": {
        "prim": "/World/Looks/M",
        "connections": {
            "outputs:surface": {
                "source_prim": "/World/Looks/M/Surf",
                "source_attr": "outputs:surface",
            }
        },
    },
    "set_stage_metadata": {"upAxis": "Y", "metersPerUnit": 0.01},
    "set_instanceable": {"prim": "/World/A", "instanceable": True},
    "set_point_instancer": {
        "prim": "/World/PI",
        "prototypes": ["/World/PI/proto0"],
        "positions": [[0, 0, 0], [1, 0, 0]],
        "proto_indices": [0, 0],
    },
    "set_sdf_spec_fields": {
        "prim": "/World/A",
        "spec_path": "/World/A.userProperties:weight",
        "spec_kind": "attribute",
        "fields": ["default"],
        "fragment": (
            '#usda 1.0\n\nover "World" { over "A" { custom double userProperties:weight = 1 } }\n'
        ),
    },
}


def test_sample_args_cover_all_kinds():
    assert set(SAMPLE_ARGS) == set(TOOL_TABLE)


@pytest.mark.parametrize("kind", sorted(TOOL_TABLE))
def test_build_emits_matching_kind_and_passes_shape(kind):
    ev = TOOL_TABLE[kind].build(**SAMPLE_ARGS[kind])
    assert ev["k"] == kind
    if kind != K_SET_STAGE_METADATA:
        # set_stage_metadata carries no "prim", which validate_event requires.
        assert validate_event(ev), f"{kind}: {ev}"


def test_set_stage_metadata_has_no_prim():
    ev = TOOL_TABLE["set_stage_metadata"].build(upAxis="Y", metersPerUnit=0.01)
    assert ev == {"k": "set_stage_metadata", "upAxis": "Y", "metersPerUnit": 0.01}
    assert "prim" not in ev


def test_set_xform_trs_derives_fields_and_keeps_quaternion():
    ev = TOOL_TABLE["set_xform_trs"].build("/p", t=[1, 2, 3], s=[2, 2, 2])
    assert ev["fields"] == ["t", "s"]
    assert "r" not in ev
    ev2 = TOOL_TABLE["set_xform_trs"].build("/p", r=[1, 0, 0, 0])
    assert ev2["fields"] == ["r"] and ev2["r"] == [1.0, 0.0, 0.0, 0.0]


def test_set_xform_trs_euler_converts_to_quaternion():
    ev = TOOL_TABLE["set_xform_trs"].build("/p", rotate_euler=[90, 0, 0])
    assert ev["fields"] == ["r"]
    w, x, y, z = ev["r"]
    h = math.sqrt(0.5)
    assert (w, x, y, z) == pytest.approx((h, h, 0.0, 0.0), abs=1e-6)


def test_euler_to_quat_identity_and_z():
    assert euler_to_quat_wxyz([0, 0, 0]) == pytest.approx([1, 0, 0, 0], abs=1e-6)
    h = math.sqrt(0.5)
    assert euler_to_quat_wxyz([0, 0, 90]) == pytest.approx([h, 0, 0, h], abs=1e-6)


def test_point_instancer_fields_in_canonical_order():
    ev = TOOL_TABLE["set_point_instancer"].build(
        "/World/PI",
        ids=[1, 2],
        positions=[[0, 0, 0], [1, 0, 0]],
        prototypes=["/World/PI/p0"],
    )
    # fields must follow POINT_INSTANCER_FIELDS order regardless of kwarg order
    assert ev["fields"] == [f for f in POINT_INSTANCER_FIELDS if f in ev["fields"]]
    assert ev["fields"] == ["prototypes", "positions", "ids"]


def test_set_connectable_input_defaults_input_types():
    ev = TOOL_TABLE["set_connectable_input"].build("/p", info_id="", inputs={"intensity": 9.0})
    assert ev["input_types"] == {}
    assert ev["info_id"] == ""
