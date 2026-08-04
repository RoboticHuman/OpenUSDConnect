"""Lock the TypedDicts in openusdconnect.events to the wire schema.

Each event kind is constructed via its TypedDict and round-tripped through
the codec; the decoded event must carry the discriminator and the fields
the producer set. Coverage of every kind in ``EVENT_KEYS`` is asserted so
a new constant cannot land without a matching TypedDict.
"""

from __future__ import annotations

from openusdconnect.codec import encode_message, message_to_dict
from openusdconnect.events import (
    DeactivatePrim,
    DeletePrim,
    EnsurePrim,
    EnsureXformOps,
    Event,
    LoadPayload,
    RenamePrim,
    ReplaceSdfLayerContent,
    SetConnectableConnection,
    SetConnectableInput,
    SetGprimAttrs,
    SetInstanceable,
    SetMaterialBinding,
    SetPayload,
    SetPointInstancer,
    SetReference,
    SetSdfSpecFields,
    SetStageMetadata,
    SetSublayers,
    SetVariantSelections,
    SetVisibility,
    SetXformTRS,
    UnloadPayload,
)
from openusdconnect.protocol_constants import EVENT_KEYS

# One representative example per kind. The (kind, value) tuples drive both
# the round-trip test and the coverage assertion below.
_CASES: list[tuple[str, Event]] = [
    (
        "ensure_prim",
        EnsurePrim(k="ensure_prim", prim="/World/Sphere", typeName="Sphere"),
    ),
    (
        "ensure_xform_ops",
        EnsureXformOps(k="ensure_xform_ops", prim="/World/Sphere"),
    ),
    (
        "set_xform_trs",
        SetXformTRS(
            k="set_xform_trs",
            prim="/World/Sphere",
            fields=["t", "r", "s"],
            t=[1.0, 2.0, 3.0],
            r=[1.0, 0.0, 0.0, 0.0],
            s=[1.0, 1.0, 1.0],
        ),
    ),
    (
        "delete_prim",
        DeletePrim(k="delete_prim", prim="/World/Cube"),
    ),
    (
        "deactivate_prim",
        DeactivatePrim(k="deactivate_prim", prim="/World/Cone", active=False),
    ),
    (
        "rename_prim",
        RenamePrim(k="rename_prim", prim="/World/Cube", new_name="BigCube"),
    ),
    (
        "set_visibility",
        SetVisibility(k="set_visibility", prim="/World/Hidden", visible=False),
    ),
    (
        "set_gprim_attrs",
        SetGprimAttrs(
            k="set_gprim_attrs",
            prim="/World/Mesh",
            attrs={"radius": 2.5, "purpose": "render"},
            primvar_meta={
                "primvars:st": {"typeName": "float2[]", "interpolation": "vertex"},
            },
            attr_interp={"normals": "vertex"},
        ),
    ),
    (
        "set_reference",
        SetReference(
            k="set_reference",
            prim="/World/Furniture",
            refs=[{"asset_path": "model.usda", "prim_path": "/Model"}],
        ),
    ),
    (
        "set_payload",
        SetPayload(
            k="set_payload",
            prim="/World/Furniture",
            payloads=[{"asset_path": "model.usda", "prim_path": "/Model"}],
        ),
    ),
    (
        "load_payload",
        LoadPayload(k="load_payload", prim="/World/Furniture"),
    ),
    (
        "unload_payload",
        UnloadPayload(k="unload_payload", prim="/World/Furniture"),
    ),
    (
        "set_variant_selections",
        SetVariantSelections(
            k="set_variant_selections",
            prim="/World/Sphere",
            selections={"size": "large"},
        ),
    ),
    (
        "set_material_binding",
        SetMaterialBinding(
            k="set_material_binding",
            prim="/World/Mesh",
            material_path="/World/Mat",
        ),
    ),
    (
        "set_connectable_input",
        SetConnectableInput(
            k="set_connectable_input",
            prim="/World/Mat/Surface",
            info_id="UsdPreviewSurface",
            inputs={"metallic": 0.75, "roughness": 0.4},
            input_types={"metallic": "Float", "roughness": "Float"},
        ),
    ),
    (
        "set_connectable_connection",
        SetConnectableConnection(
            k="set_connectable_connection",
            prim="/World/Mat/Surface",
            connections={
                "inputs:diffuseColor": {
                    "source_prim": "/World/Mat/Tex",
                    "source_attr": "outputs:rgb",
                },
            },
            disconnections=["inputs:roughness"],
        ),
    ),
    (
        "set_instanceable",
        SetInstanceable(k="set_instanceable", prim="/World/Tree_01", instanceable=True),
    ),
    (
        "set_point_instancer",
        SetPointInstancer(
            k="set_point_instancer",
            prim="/World/Forest",
            fields=["prototypes", "proto_indices", "positions", "orientations", "ids"],
            prototypes=["/World/Protos/Pine", "/World/Protos/Oak"],
            proto_indices=[0, 1, 0],
            positions=[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]],
            orientations=[
                [1.0, 0.0, 0.0, 0.0],
                [0.5, 0.5, 0.5, 0.5],
                [1.0, 0.0, 0.0, 0.0],
            ],
            ids=[1099511627776, 1099511627777, 1099511627778],
        ),
    ),
    (
        "set_stage_metadata",
        SetStageMetadata(
            k="set_stage_metadata",
            timeCodesPerSecond=24.0,
            framesPerSecond=30.0,
            startTimeCode=0.0,
            endTimeCode=240.0,
            metersPerUnit=0.01,
            upAxis="Y",
        ),
    ),
    (
        "set_sdf_spec_fields",
        SetSdfSpecFields(
            k="set_sdf_spec_fields",
            prim="/World/Thing",
            spec_path="/World/Thing.userProperties:value",
            spec_kind="attribute",
            fields=["default"],
            fragment='#usda 1.0\n\nover "World" {}\n',
            removed=False,
        ),
    ),
    (
        "replace_sdf_layer_content",
        ReplaceSdfLayerContent(
            k="replace_sdf_layer_content",
            prim="/",
            fragment="#usda 1.0\n",
        ),
    ),
    (
        "set_sublayers",
        SetSublayers(
            k="set_sublayers",
            prim="/",
            generation="graph-1",
            revision=2,
            sublayers=[
                {
                    "authored_path": "./asset.usda",
                    "offset": 7.0,
                    "scale": 2.0,
                    "layer_key": "layer:asset",
                }
            ],
        ),
    ),
]


def _roundtrip(ev: Event) -> dict:
    """Wrap ev in a txn, encode, decode, return the decoded event dict."""
    buf = encode_message({"type": "txn", "client_id": "test", "events": [ev]})
    decoded = message_to_dict(buf)
    assert decoded["events"], "txn round-tripped with empty events list"
    return decoded["events"][0]


def test_all_event_kinds_have_typeddicts():
    """Every K_* in EVENT_KEYS must have a TypedDict + example in _CASES."""
    covered = {kind for kind, _ in _CASES}
    missing = EVENT_KEYS - covered
    assert not missing, f"Event kinds in EVENT_KEYS lack a TypedDict + example: {sorted(missing)}"


class TestRoundtrip:
    """Each TypedDict-shaped event survives encode → decode with its discriminator intact."""

    def test_discriminator_preserved(self):
        for kind, ev in _CASES:
            out = _roundtrip(ev)
            assert out["k"] == kind, f"{kind}: round-trip lost discriminator → {out['k']!r}"

    def test_prim_preserved(self):
        for kind, ev in _CASES:
            # Stage-level events have no ``prim`` field by design.
            if "prim" not in ev:
                continue
            out = _roundtrip(ev)
            assert out["prim"] == ev["prim"], f"{kind}: prim path mangled"

    def test_set_xform_trs_fields_preserved(self):
        ev = next(e for k, e in _CASES if k == "set_xform_trs")
        out = _roundtrip(ev)
        assert out["t"] == [1.0, 2.0, 3.0]
        assert out["r"] == [1.0, 0.0, 0.0, 0.0]
        assert out["s"] == [1.0, 1.0, 1.0]

    def test_deactivate_prim_active_flag_preserved(self):
        ev = next(e for k, e in _CASES if k == "deactivate_prim")
        out = _roundtrip(ev)
        assert out["active"] is False

    def test_rename_prim_new_name_preserved(self):
        ev = next(e for k, e in _CASES if k == "rename_prim")
        out = _roundtrip(ev)
        assert out["new_name"] == "BigCube"

    def test_set_reference_arc_entry_preserved(self):
        ev = next(e for k, e in _CASES if k == "set_reference")
        out = _roundtrip(ev)
        assert out["refs"][0]["asset_path"] == "model.usda"
        assert out["refs"][0]["prim_path"] == "/Model"

    def test_set_variant_selections_preserved(self):
        ev = next(e for k, e in _CASES if k == "set_variant_selections")
        out = _roundtrip(ev)
        assert out["selections"] == {"size": "large"}

    def test_set_shader_input_preserved(self):
        ev = next(e for k, e in _CASES if k == "set_connectable_input")
        out = _roundtrip(ev)
        assert out["info_id"] == "UsdPreviewSurface"
        assert out["inputs"]["metallic"] == 0.75
        assert out["input_types"]["metallic"] == "Float"

    def test_set_shader_connection_preserved(self):
        ev = next(e for k, e in _CASES if k == "set_connectable_connection")
        out = _roundtrip(ev)
        conn = out["connections"]["inputs:diffuseColor"]
        assert conn["source_prim"] == "/World/Mat/Tex"
        assert conn["source_attr"] == "outputs:rgb"
        assert out["disconnections"] == ["inputs:roughness"]
