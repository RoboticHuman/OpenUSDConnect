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
    SetGprimAttrs,
    SetMaterialBinding,
    SetPayload,
    SetReference,
    SetShaderConnection,
    SetShaderInput,
    SetVariantSelections,
    SetVisibility,
    SetXformMatrices,
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
        "set_xform_matrices",
        SetXformMatrices(
            k="set_xform_matrices",
            prim="/World/Sphere",
            local_m=[1.0, 0.0, 0.0, 0.0,
                     0.0, 1.0, 0.0, 0.0,
                     0.0, 0.0, 1.0, 0.0,
                     0.0, 0.0, 0.0, 1.0],
            world_m=[1.0, 0.0, 0.0, 0.0,
                     0.0, 1.0, 0.0, 0.0,
                     0.0, 0.0, 1.0, 0.0,
                     0.0, 0.0, 0.0, 1.0],
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
        "set_shader_input",
        SetShaderInput(
            k="set_shader_input",
            prim="/World/Mat/Surface",
            shader_id="UsdPreviewSurface",
            inputs={"metallic": 0.75, "roughness": 0.4},
            input_types={"metallic": "Float", "roughness": "Float"},
        ),
    ),
    (
        "set_shader_connection",
        SetShaderConnection(
            k="set_shader_connection",
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
    assert not missing, (
        f"Event kinds in EVENT_KEYS lack a TypedDict + example: {sorted(missing)}"
    )


class TestRoundtrip:
    """Each TypedDict-shaped event survives encode → decode with its discriminator intact."""

    def test_discriminator_preserved(self):
        for kind, ev in _CASES:
            out = _roundtrip(ev)
            assert out["k"] == kind, f"{kind}: round-trip lost discriminator → {out['k']!r}"

    def test_prim_preserved(self):
        for kind, ev in _CASES:
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
        ev = next(e for k, e in _CASES if k == "set_shader_input")
        out = _roundtrip(ev)
        assert out["shader_id"] == "UsdPreviewSurface"
        assert out["inputs"]["metallic"] == 0.75
        assert out["input_types"]["metallic"] == "Float"

    def test_set_shader_connection_preserved(self):
        ev = next(e for k, e in _CASES if k == "set_shader_connection")
        out = _roundtrip(ev)
        conn = out["connections"]["inputs:diffuseColor"]
        assert conn["source_prim"] == "/World/Mat/Tex"
        assert conn["source_attr"] == "outputs:rgb"
        assert out["disconnections"] == ["inputs:roughness"]
