"""Tests for openusdconnect.protocol — validation helpers, message construction."""

from openusdconnect.protocol import (
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_RENAME_PRIM,
    K_SET_GPRIM_ATTRS,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_VISIBILITY,
    K_SET_XFORM_MATRICES,
    K_SET_XFORM_TRS,
    MSG_HELLO,
    MSG_QUIT,
    MSG_TXN,
    PROTOCOL_VERSION,
    clamp_fields,
    is_mat16_valid,
    is_quat_valid,
    is_vec3_valid,
    make_hello,
    make_quit,
    make_txn,
    validate_event,
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

    def test_is_mat16_valid_good(self):
        assert is_mat16_valid([float(i) for i in range(16)])

    def test_is_mat16_valid_bad(self):
        assert not is_mat16_valid([1.0] * 15)
        assert not is_mat16_valid([1.0] * 17)

    def test_clamp_fields(self):
        assert clamp_fields(["t", "r", "s"]) == ["t", "r", "s"]
        assert clamp_fields(["t", "bogus", "s"]) == ["t", "s"]
        assert clamp_fields([]) == []


class TestMessageConstruction:
    def test_make_hello_emitter(self):
        msg = make_hello("emitter")
        assert msg["type"] == MSG_HELLO
        assert msg["role"] == "emitter"
        assert msg["protocol_version"] == PROTOCOL_VERSION
        assert "sync_from" not in msg

    def test_make_hello_receiver_with_sync(self):
        msg = make_hello("receiver", sync_from=5)
        assert msg["sync_from"] == 5

    def test_make_txn(self):
        events = [{"k": K_ENSURE_PRIM, "prim": "/World/Foo"}]
        msg = make_txn("client-1", events)
        assert msg["type"] == MSG_TXN
        assert msg["client_id"] == "client-1"
        assert msg["events"] == events

    def test_make_quit(self):
        msg = make_quit()
        assert msg["type"] == MSG_QUIT


class TestValidateEvent:
    def test_ensure_prim_valid(self):
        assert validate_event({"k": K_ENSURE_PRIM, "prim": "/World/Sphere", "typeName": "Xform"})

    def test_ensure_prim_missing_prim(self):
        assert not validate_event({"k": K_ENSURE_PRIM})

    def test_unknown_key(self):
        assert not validate_event({"k": "unknown_key", "prim": "/Foo"})

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

    def test_set_xform_matrices_valid(self):
        ev = {
            "k": K_SET_XFORM_MATRICES,
            "prim": "/World/Sphere",
            "local_m": [float(i) for i in range(16)],
            "world_m": [float(i) for i in range(16)],
        }
        assert validate_event(ev)

    def test_set_xform_matrices_bad(self):
        ev = {
            "k": K_SET_XFORM_MATRICES,
            "prim": "/World/Sphere",
            "local_m": [1.0] * 15,
            "world_m": [1.0] * 16,
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
        assert not validate_event(
            {"k": K_SET_REFERENCE, "prim": "/World/Chair", "refs": "bad"}
        )

    def test_set_reference_empty_refs_clears(self):
        assert validate_event(
            {"k": K_SET_REFERENCE, "prim": "/World/Chair", "refs": []}
        )

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
        assert not validate_event(
            {"k": K_SET_REFERENCE, "prim": "/World/Chair", "refs": ["bad"]}
        )

    def test_set_reference_internal_ref_valid(self):
        """Same-file reference with only prim_path is valid."""
        assert validate_event(
            {"k": K_SET_REFERENCE, "prim": "/World/Chair", "refs": [{"prim_path": "/X"}]}
        )

    def test_set_reference_entry_missing_both(self):
        """Entry with neither asset_path nor prim_path is invalid."""
        assert not validate_event(
            {"k": K_SET_REFERENCE, "prim": "/World/Chair", "refs": [{}]}
        )

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

    def test_set_payload_missing_payloads(self):
        assert not validate_event({"k": K_SET_PAYLOAD, "prim": "/World/Asset"})

    def test_set_payload_empty_clears(self):
        assert validate_event(
            {"k": K_SET_PAYLOAD, "prim": "/World/Asset", "payloads": []}
        )

    def test_set_payload_internal_valid(self):
        assert validate_event(
            {"k": K_SET_PAYLOAD, "prim": "/World/Asset", "payloads": [{"prim_path": "/X"}]}
        )

    def test_set_payload_entry_missing_both(self):
        assert not validate_event(
            {"k": K_SET_PAYLOAD, "prim": "/World/Asset", "payloads": [{}]}
        )

    def test_set_payload_entry_empty_asset_path(self):
        assert not validate_event(
            {"k": K_SET_PAYLOAD, "prim": "/World/Asset", "payloads": [{"asset_path": ""}]}
        )
