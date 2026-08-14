"""Tests for openusdconnect.protocol — validation helpers, message construction."""

import pytest

from openusdconnect.codec import encode_message, message_to_dict
from openusdconnect.connectable_attrs import (
    SIDE_INPUT,
    SIDE_OUTPUT,
    ConnectableAttr,
    input_attr,
    output_attr,
    split_qualified_attr,
)
from openusdconnect.protocol import (
    make_hello,
    make_quit,
    make_replay_complete,
    make_txn,
)
from openusdconnect.protocol_constants import (
    COLLABORATION_LAYER_KINDS,
    EVENT_KEYS,
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_SET_GPRIM_ATTRS,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_SDF_SPEC_FIELDS,
    K_SET_STAGE_METADATA,
    K_SET_SUBLAYERS,
    K_SET_VISIBILITY,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
    MSG_HELLO,
    MSG_QUIT,
    MSG_REPLAY_COMPLETE,
    MSG_TXN,
    PROTOCOL_VERSION,
    SESSION_LAYER_KINDS,
    STAGE_RUNTIME_KINDS,
    LayerMode,
)
from openusdconnect.protocol_validation import (
    clamp_fields,
    is_quat_valid,
    is_vec3_valid,
    validate_event,
    validate_event_or_raise,
    validate_events,
)


class TestValidationHelpers:
    def test_is_quat_valid_good(self):
        assert is_quat_valid([1.0, 0.0, 0.0, 0.0])
        assert is_quat_valid([0, 0, 0, 1])  # ints are ok

    def test_is_quat_valid_bad(self):
        assert not is_quat_valid([1.0, 0.0, 0.0])  # too short
        assert not is_quat_valid([1.0, 0.0, 0.0, 0.0, 0.0])  # too long
        assert not is_quat_valid("not a list")
        assert not is_quat_valid([1.0, "x", 0.0, 0.0])

    def test_is_vec3_valid_good(self):
        assert is_vec3_valid([1.0, 2.0, 3.0])
        assert is_vec3_valid([0, 0, 0])

    def test_is_vec3_valid_bad(self):
        assert not is_vec3_valid([1.0, 2.0])
        assert not is_vec3_valid(None)

    def test_clamp_fields(self):
        assert clamp_fields(["t", "r", "s"]) == ["t", "r", "s"]
        assert clamp_fields(["t", "bogus", "s"]) == ["t", "s"]
        assert clamp_fields([]) == []


class TestConnectableAttrHelpers:
    def test_builds_qualified_input_and_output_names(self):
        assert input_attr("diffuseColor").qualified_name == "inputs:diffuseColor"
        assert output_attr("surface").qualified_name == "outputs:surface"

    def test_parses_qualified_names(self):
        inp = ConnectableAttr.from_qualified_name("inputs:tint")
        out = ConnectableAttr.from_qualified_name("outputs:rgb")

        assert inp == ConnectableAttr(SIDE_INPUT, "tint")
        assert out == ConnectableAttr(SIDE_OUTPUT, "rgb")
        assert inp.is_input
        assert out.is_output

    def test_rejects_unqualified_or_empty_base_names(self):
        assert ConnectableAttr.from_qualified_name("diffuseColor") is None
        assert ConnectableAttr.from_qualified_name("inputs:") is None
        assert split_qualified_attr("outputs:") == ("", "")


class TestMessageConstruction:
    def test_make_hello_rejects_unknown_role(self):
        with pytest.raises(ValueError, match="emitter.*receiver"):
            make_hello("observer")

    def test_make_hello_emitter(self):
        msg = make_hello("emitter", producer_session_id="producer-1")
        assert msg["type"] == MSG_HELLO
        assert msg["role"] == "emitter"
        assert msg["protocol_version"] == PROTOCOL_VERSION
        assert "sync_from" not in msg
        assert msg["producer_session_id"] == "producer-1"

    def test_make_hello_receiver_with_sync(self):
        msg = make_hello("receiver", sync_from=5)
        assert msg["sync_from"] == 5
        assert msg["layered_replay"] is True

        flat = make_hello("receiver", layered_replay=False)
        assert "layered_replay" not in flat

    def test_hello_ok_layered_replay_roundtrip(self):
        msg = {"type": "hello_ok", "layered_replay": True}
        assert message_to_dict(encode_message(msg)) == msg

    def test_shared_stage_hello_uses_a_separate_capability(self):
        msg = make_hello("receiver", layer_mode=LayerMode.SHARED_STAGE)
        assert msg["layer_mode"] == "shared_stage"
        assert "layered_replay" not in msg

        for kwargs in (
            {"department": "animation"},
            {"layered_replay": True},
        ):
            with pytest.raises(ValueError):
                make_hello("receiver", layer_mode=LayerMode.SHARED_STAGE, **kwargs)

    def test_make_txn(self):
        events = [{"k": K_ENSURE_PRIM, "prim": "/World/Foo", "typeName": "Xform"}]
        msg = make_txn(events)
        assert msg["type"] == MSG_TXN
        assert msg["events"] == events

        targeted = make_txn(events, layer_key="layer:root")
        assert targeted["layer_key"] == "layer:root"

        identified = make_txn(events, txn_id=42)
        assert identified["txn_id"] == 42

    def test_make_quit(self):
        msg = make_quit()
        assert msg["type"] == MSG_QUIT

    def test_make_replay_complete(self):
        msg = make_replay_complete(42, 3)
        assert msg == {
            "type": MSG_REPLAY_COMPLETE,
            "head_seq": 42,
            "epoch": 3,
        }
        with pytest.raises(ValueError, match="cannot be negative"):
            make_replay_complete(-1, 0)


def test_every_event_kind_has_one_target_domain():
    domains = (
        COLLABORATION_LAYER_KINDS,
        SESSION_LAYER_KINDS,
        STAGE_RUNTIME_KINDS,
    )

    assert set().union(*domains) == EVENT_KEYS
    assert all(
        not domains[left] & domains[right]
        for left in range(len(domains))
        for right in range(left + 1, len(domains))
    )
    assert K_ENSURE_PRIM in COLLABORATION_LAYER_KINDS
    assert K_SET_STAGE_METADATA in SESSION_LAYER_KINDS
    assert {K_LOAD_PAYLOAD, K_UNLOAD_PAYLOAD} <= STAGE_RUNTIME_KINDS


class TestValidateEvent:
    @pytest.mark.parametrize("prim", ["relative", "/", "/World{look=red}/Mesh"])
    def test_semantic_events_require_absolute_non_variant_prim_paths(self, prim):
        assert not validate_event({"k": K_DELETE_PRIM, "prim": prim})

    def test_stage_metadata_does_not_require_a_prim(self):
        assert validate_event({"k": K_SET_STAGE_METADATA, "metersPerUnit": 1.0})
        assert not validate_event({"k": K_SET_STAGE_METADATA})
        assert not validate_event({"k": K_SET_STAGE_METADATA, "upAxis": "X"})

    def test_nonfinite_transform_values_are_rejected(self):
        assert not validate_event(
            {
                "k": K_SET_XFORM_TRS,
                "prim": "/World/X",
                "fields": ["t"],
                "t": [float("nan"), 0.0, 0.0],
            }
        )

    def test_transform_fields_must_exactly_match_payload(self):
        assert not validate_event(
            {
                "k": K_SET_XFORM_TRS,
                "prim": "/World/X",
                "fields": ["t"],
                "t": [0.0, 0.0, 0.0],
                "s": [1.0, 1.0, 1.0],
            }
        )

    def test_point_instancer_fields_are_validated(self):
        event = {
            "k": "set_point_instancer",
            "prim": "/World/Instances",
            "fields": ["positions"],
            "positions": [[0.0, 0.0, 0.0]],
        }
        assert validate_event(event)
        event["fields"] = ["not_a_point_instancer_field"]
        assert not validate_event(event)

    def test_mode_validation_uses_event_kind_classification(self):
        event = {"k": K_SET_VISIBILITY, "prim": "/World/X", "visible": True}
        with pytest.raises(ValueError, match="shared_stage"):
            validate_event_or_raise(event, layer_mode=LayerMode.SHARED_STAGE)

    def test_transaction_validation_identifies_event_index(self):
        with pytest.raises(ValueError, match="event 1:.*transform fields"):
            validate_events(
                [
                    {"k": K_DELETE_PRIM, "prim": "/World/Valid"},
                    {
                        "k": K_SET_XFORM_TRS,
                        "prim": "/World/Bad",
                        "fields": ["bogus"],
                    },
                ]
            )

    def test_ensure_prim_valid(self):
        assert validate_event({"k": K_ENSURE_PRIM, "prim": "/World/Sphere", "typeName": "Xform"})

    def test_ensure_prim_missing_prim(self):
        assert not validate_event({"k": K_ENSURE_PRIM})

    def test_ensure_prim_missing_typename(self):
        assert not validate_event({"k": K_ENSURE_PRIM, "prim": "/World/Sphere"})

    def test_unknown_key(self):
        assert not validate_event({"k": "unknown_key", "prim": "/Foo"})

    def test_sdf_layer_fields_exclude_sublayer_topology(self):
        event = {
            "k": K_SET_SDF_SPEC_FIELDS,
            "prim": "/",
            "spec_path": "/",
            "spec_kind": "layer",
            "fields": ["documentation"],
            "fragment": "#usda 1.0\n",
            "removed": False,
        }
        assert validate_event(event)
        for field in ("subLayers", "subLayerOffsets"):
            event["fields"] = [field]
            assert not validate_event(event)

    def test_set_sublayers_validates_complete_ordered_state(self):
        event = {
            "k": K_SET_SUBLAYERS,
            "prim": "/",
            "generation": "graph-1",
            "revision": 0,
            "sublayers": [
                {
                    "authored_path": "./asset.usda",
                    "offset": 7.0,
                    "scale": -2.0,
                }
            ],
        }
        assert validate_event(event)
        event["sublayers"].append(dict(event["sublayers"][0]))
        assert not validate_event(event)

    def test_set_xform_trs_valid(self):
        ev = {
            "k": K_SET_XFORM_TRS,
            "prim": "/World/Sphere",
            "fields": ["t", "r", "s"],
            "t": [1.0, 2.0, 3.0],
            "r": [1.0, 0.0, 0.0, 0.0],
            "s": [1.0, 1.0, 1.0],
        }
        assert validate_event(ev)

    def test_set_xform_trs_partial_fields(self):
        ev = {
            "k": K_SET_XFORM_TRS,
            "prim": "/World/Sphere",
            "fields": ["t"],
            "t": [1.0, 2.0, 3.0],
        }
        assert validate_event(ev)

    def test_set_xform_trs_bad_rotation(self):
        ev = {
            "k": K_SET_XFORM_TRS,
            "prim": "/World/Sphere",
            "fields": ["r"],
            "r": [1.0, 0.0, 0.0],  # only 3 elements
        }
        assert not validate_event(ev)

    def test_set_xform_trs_invalid_field(self):
        ev = {
            "k": K_SET_XFORM_TRS,
            "prim": "/World/Sphere",
            "fields": ["x"],  # invalid
        }
        assert not validate_event(ev)

    def test_delete_prim_valid(self):
        assert validate_event({"k": K_DELETE_PRIM, "prim": "/World/Sphere"})

    def test_deactivate_prim_valid(self):
        assert validate_event({"k": K_DEACTIVATE_PRIM, "prim": "/World/Sphere", "active": False})
        assert validate_event({"k": K_DEACTIVATE_PRIM, "prim": "/World/Sphere", "active": True})

    def test_deactivate_prim_missing_active(self):
        assert not validate_event({"k": K_DEACTIVATE_PRIM, "prim": "/World/Sphere"})

    def test_deactivate_prim_active_not_bool(self):
        assert not validate_event({"k": K_DEACTIVATE_PRIM, "prim": "/World/Sphere", "active": 0})

    def test_rename_prim_valid(self):
        assert validate_event({"k": K_RENAME_PRIM, "prim": "/World/OldName", "new_name": "NewName"})

    def test_rename_prim_missing_new_name(self):
        assert not validate_event({"k": K_RENAME_PRIM, "prim": "/World/OldName"})

    def test_rename_prim_empty_new_name(self):
        assert not validate_event({"k": K_RENAME_PRIM, "prim": "/World/OldName", "new_name": ""})

    # --- set_visibility ---
    def test_set_visibility_valid(self):
        assert validate_event({"k": K_SET_VISIBILITY, "prim": "/World/Sphere", "visible": False})
        assert validate_event({"k": K_SET_VISIBILITY, "prim": "/World/Sphere", "visible": True})

    def test_set_visibility_missing_visible(self):
        assert not validate_event({"k": K_SET_VISIBILITY, "prim": "/World/Sphere"})

    def test_set_visibility_not_bool(self):
        assert not validate_event({"k": K_SET_VISIBILITY, "prim": "/World/Sphere", "visible": 0})

    # --- set_gprim_attrs ---
    def test_set_gprim_attrs_valid(self):
        assert validate_event(
            {
                "k": K_SET_GPRIM_ATTRS,
                "prim": "/World/Sphere/Geom",
                "attrs": {"radius": 2.0},
            }
        )

    def test_set_gprim_attrs_multiple(self):
        assert validate_event(
            {
                "k": K_SET_GPRIM_ATTRS,
                "prim": "/World/Cone/Geom",
                "attrs": {"height": 1.4, "radius": 0.6},
            }
        )

    def test_set_gprim_attrs_missing_attrs(self):
        assert not validate_event({"k": K_SET_GPRIM_ATTRS, "prim": "/World/Sphere/Geom"})

    def test_set_gprim_attrs_not_dict(self):
        assert not validate_event(
            {
                "k": K_SET_GPRIM_ATTRS,
                "prim": "/World/Sphere/Geom",
                "attrs": [1, 2],
            }
        )

    def test_set_gprim_attrs_non_string_key(self):
        assert not validate_event(
            {
                "k": K_SET_GPRIM_ATTRS,
                "prim": "/World/Sphere/Geom",
                "attrs": {1: 2.0},
            }
        )

    # --- set_reference ---
    def test_set_reference_valid(self):
        assert validate_event(
            {
                "k": K_SET_REFERENCE,
                "prim": "/World/Chair",
                "refs": [{"asset_path": "./assets/chair.usd"}],
            }
        )

    def test_set_reference_with_prim_path(self):
        assert validate_event(
            {
                "k": K_SET_REFERENCE,
                "prim": "/World/Chair",
                "refs": [{"asset_path": "./assets/chair.usd", "prim_path": "/Chair"}],
            }
        )

    def test_set_reference_missing_refs(self):
        assert not validate_event({"k": K_SET_REFERENCE, "prim": "/World/Chair"})

    def test_set_reference_refs_not_list(self):
        assert not validate_event({"k": K_SET_REFERENCE, "prim": "/World/Chair", "refs": "bad"})

    def test_set_reference_empty_refs_clears(self):
        assert validate_event({"k": K_SET_REFERENCE, "prim": "/World/Chair", "refs": []})

    def test_set_reference_multiple_refs(self):
        assert validate_event(
            {
                "k": K_SET_REFERENCE,
                "prim": "/World/Chair",
                "refs": [
                    {"asset_path": "./chair.usd", "prim_path": "/Model"},
                    {"asset_path": "./material.usd"},
                ],
            }
        )

    def test_set_reference_entry_not_dict(self):
        assert not validate_event({"k": K_SET_REFERENCE, "prim": "/World/Chair", "refs": ["bad"]})

    def test_set_reference_internal_ref_valid(self):
        """Same-file reference with only prim_path is valid."""
        assert validate_event(
            {"k": K_SET_REFERENCE, "prim": "/World/Chair", "refs": [{"prim_path": "/X"}]}
        )

    def test_set_reference_entry_missing_both(self):
        """Entry with neither asset_path nor prim_path is invalid."""
        assert not validate_event({"k": K_SET_REFERENCE, "prim": "/World/Chair", "refs": [{}]})

    def test_set_reference_entry_empty_asset_path(self):
        assert not validate_event(
            {"k": K_SET_REFERENCE, "prim": "/World/Chair", "refs": [{"asset_path": ""}]}
        )

    def test_set_reference_entry_bad_prim_path(self):
        assert not validate_event(
            {
                "k": K_SET_REFERENCE,
                "prim": "/World/Chair",
                "refs": [{"asset_path": "a.usd", "prim_path": "no_slash"}],
            }
        )

    def test_set_reference_rich_list_op(self):
        assert validate_event(
            {
                "k": K_SET_REFERENCE,
                "prim": "/World/Chair",
                "refs": [
                    {
                        "asset_path": "chair.usd",
                        "list_position": "appended",
                        "layer_offset": 4.0,
                        "layer_scale": 0.5,
                        "custom_data_fragment": "#usda 1.0\n",
                    }
                ],
                "list_op_authored": True,
                "list_op_explicit": False,
            }
        )

    def test_set_reference_explicit_empty(self):
        assert validate_event(
            {
                "k": K_SET_REFERENCE,
                "prim": "/World/Chair",
                "refs": [],
                "list_op_authored": True,
                "list_op_explicit": True,
            }
        )

    def test_set_reference_rejects_position_mismatched_with_mode(self):
        assert not validate_event(
            {
                "k": K_SET_REFERENCE,
                "prim": "/World/Chair",
                "refs": [
                    {
                        "asset_path": "chair.usd",
                        "list_position": "explicit",
                    }
                ],
                "list_op_authored": True,
                "list_op_explicit": False,
            }
        )

    def test_set_reference_rejects_entries_on_unauthored_opinion(self):
        assert not validate_event(
            {
                "k": K_SET_REFERENCE,
                "prim": "/World/Chair",
                "refs": [{"asset_path": "chair.usd"}],
                "list_op_authored": False,
            }
        )

    def test_set_reference_rejects_nonfinite_offset(self):
        assert not validate_event(
            {
                "k": K_SET_REFERENCE,
                "prim": "/World/Chair",
                "refs": [
                    {
                        "asset_path": "chair.usd",
                        "layer_offset": float("nan"),
                    }
                ],
            }
        )

    # --- set_payload ---
    def test_set_payload_valid(self):
        assert validate_event(
            {
                "k": K_SET_PAYLOAD,
                "prim": "/World/Asset",
                "payloads": [{"asset_path": "./payload.usda"}],
            }
        )

    def test_set_payload_with_prim_path(self):
        assert validate_event(
            {
                "k": K_SET_PAYLOAD,
                "prim": "/World/Asset",
                "payloads": [{"asset_path": "./payload.usda", "prim_path": "/Model"}],
            }
        )

    def test_set_payload_rejects_reference_custom_data(self):
        assert not validate_event(
            {
                "k": K_SET_PAYLOAD,
                "prim": "/World/Asset",
                "payloads": [
                    {
                        "asset_path": "./payload.usda",
                        "custom_data_fragment": "#usda 1.0\n",
                    }
                ],
            }
        )

    def test_set_payload_missing_payloads(self):
        assert not validate_event({"k": K_SET_PAYLOAD, "prim": "/World/Asset"})

    def test_set_payload_empty_clears(self):
        assert validate_event({"k": K_SET_PAYLOAD, "prim": "/World/Asset", "payloads": []})

    def test_set_payload_internal_valid(self):
        assert validate_event(
            {"k": K_SET_PAYLOAD, "prim": "/World/Asset", "payloads": [{"prim_path": "/X"}]}
        )

    def test_set_payload_entry_missing_both(self):
        assert not validate_event({"k": K_SET_PAYLOAD, "prim": "/World/Asset", "payloads": [{}]})

    def test_set_payload_entry_empty_asset_path(self):
        assert not validate_event(
            {"k": K_SET_PAYLOAD, "prim": "/World/Asset", "payloads": [{"asset_path": ""}]}
        )

    def test_set_payload_entry_not_dict(self):
        assert not validate_event({"k": K_SET_PAYLOAD, "prim": "/World/Asset", "payloads": ["bad"]})

    def test_set_payload_entry_bad_prim_path(self):
        assert not validate_event(
            {
                "k": K_SET_PAYLOAD,
                "prim": "/World/Asset",
                "payloads": [{"asset_path": "a.usd", "prim_path": "no_slash"}],
            }
        )

    # --- load_payload / unload_payload ---
    def test_load_payload_valid(self):
        assert validate_event({"k": "load_payload", "prim": "/World/Asset"})

    def test_unload_payload_valid(self):
        assert validate_event({"k": "unload_payload", "prim": "/World/Asset"})

    # --- set_variant_selections ---
    def test_set_variant_selections_valid(self):
        assert validate_event(
            {"k": "set_variant_selections", "prim": "/World/Car", "selections": {"wheels": "sport"}}
        )

    def test_set_variant_selections_multiple(self):
        assert validate_event(
            {
                "k": "set_variant_selections",
                "prim": "/World/Car",
                "selections": {"wheels": "sport", "color": "red"},
            }
        )

    def test_set_variant_selections_empty_clears(self):
        assert validate_event(
            {"k": "set_variant_selections", "prim": "/World/Car", "selections": {}}
        )

    def test_set_variant_selections_missing_key(self):
        assert not validate_event({"k": "set_variant_selections", "prim": "/World/Car"})

    def test_set_variant_selections_not_dict(self):
        assert not validate_event(
            {"k": "set_variant_selections", "prim": "/World/Car", "selections": ["bad"]}
        )

    def test_set_variant_selections_non_string_key(self):
        assert not validate_event(
            {"k": "set_variant_selections", "prim": "/World/Car", "selections": {1: "a"}}
        )

    def test_set_variant_selections_non_string_value(self):
        assert not validate_event(
            {"k": "set_variant_selections", "prim": "/World/Car", "selections": {"wheels": 42}}
        )

    def test_set_shader_input_with_info_id_valid(self):
        assert validate_event(
            {
                "k": "set_connectable_input",
                "prim": "/Mat/PBR",
                "info_id": "UsdPreviewSurface",
                "inputs": {"roughness": 0.5},
                "input_types": {"roughness": "float"},
            }
        )

    def test_set_shader_input_empty_info_id_valid(self):
        """Empty info_id is permitted for NodeGraph/Material container
        prims that have no info:id but carry interface inputs."""
        assert validate_event(
            {
                "k": "set_connectable_input",
                "prim": "/Mat/NG_Inner",
                "info_id": "",
                "inputs": {"tint": [1.0, 0.5, 0.25]},
                "input_types": {"tint": "color3f"},
            }
        )

    def test_set_shader_input_missing_info_id_invalid(self):
        """Missing info_id (None) is still rejected — only an explicit
        empty string is the valid 'no info:id' signal."""
        assert not validate_event(
            {"k": "set_connectable_input", "prim": "/Mat/PBR", "inputs": {}, "input_types": {}}
        )

    def test_set_shader_input_non_string_info_id_invalid(self):
        assert not validate_event(
            {
                "k": "set_connectable_input",
                "prim": "/Mat/PBR",
                "info_id": 42,
                "inputs": {},
                "input_types": {},
            }
        )
