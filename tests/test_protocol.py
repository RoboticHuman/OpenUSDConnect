"""Tests for openusdconnect.protocol — validation helpers, message construction."""

from openusdconnect.protocol import (
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
        assert msg["type"] == "hello"
        assert msg["role"] == "emitter"
        assert msg["protocol_version"] == PROTOCOL_VERSION
        assert "sync_from" not in msg

    def test_make_hello_receiver_with_sync(self):
        msg = make_hello("receiver", sync_from=5)
        assert msg["sync_from"] == 5

    def test_make_txn(self):
        events = [{"k": "ensure_prim", "prim": "/World/Foo"}]
        msg = make_txn("client-1", events)
        assert msg["type"] == "txn"
        assert msg["client_id"] == "client-1"
        assert msg["events"] == events

    def test_make_quit(self):
        msg = make_quit()
        assert msg["type"] == "quit"


class TestValidateEvent:
    def test_ensure_prim_valid(self):
        assert validate_event({"k": "ensure_prim", "prim": "/World/Sphere", "typeName": "Xform"})

    def test_ensure_prim_missing_prim(self):
        assert not validate_event({"k": "ensure_prim"})

    def test_unknown_key(self):
        assert not validate_event({"k": "unknown_key", "prim": "/Foo"})

    def test_set_xform_trs_valid(self):
        ev = {
            "k": "set_xform_trs",
            "prim": "/World/Sphere",
            "fields": ["t", "r", "s"],
            "t": [1.0, 2.0, 3.0],
            "r": [1.0, 0.0, 0.0, 0.0],
            "s": [1.0, 1.0, 1.0],
        }
        assert validate_event(ev)

    def test_set_xform_trs_partial_fields(self):
        ev = {
            "k": "set_xform_trs",
            "prim": "/World/Sphere",
            "fields": ["t"],
            "t": [1.0, 2.0, 3.0],
        }
        assert validate_event(ev)

    def test_set_xform_trs_bad_rotation(self):
        ev = {
            "k": "set_xform_trs",
            "prim": "/World/Sphere",
            "fields": ["r"],
            "r": [1.0, 0.0, 0.0],  # only 3 elements
        }
        assert not validate_event(ev)

    def test_set_xform_trs_invalid_field(self):
        ev = {
            "k": "set_xform_trs",
            "prim": "/World/Sphere",
            "fields": ["x"],  # invalid
        }
        assert not validate_event(ev)

    def test_set_xform_matrices_valid(self):
        ev = {
            "k": "set_xform_matrices",
            "prim": "/World/Sphere",
            "local_m": [float(i) for i in range(16)],
            "world_m": [float(i) for i in range(16)],
        }
        assert validate_event(ev)

    def test_set_xform_matrices_bad(self):
        ev = {
            "k": "set_xform_matrices",
            "prim": "/World/Sphere",
            "local_m": [1.0] * 15,
            "world_m": [1.0] * 16,
        }
        assert not validate_event(ev)

    def test_delete_prim_valid(self):
        assert validate_event({"k": "delete_prim", "prim": "/World/Sphere"})

    def test_deactivate_prim_valid(self):
        assert validate_event({"k": "deactivate_prim", "prim": "/World/Sphere", "active": False})
        assert validate_event({"k": "deactivate_prim", "prim": "/World/Sphere", "active": True})

    def test_deactivate_prim_missing_active(self):
        assert not validate_event({"k": "deactivate_prim", "prim": "/World/Sphere"})

    def test_deactivate_prim_active_not_bool(self):
        assert not validate_event({"k": "deactivate_prim", "prim": "/World/Sphere", "active": 0})

    def test_rename_prim_valid(self):
        assert validate_event({"k": "rename_prim", "prim": "/World/OldName", "new_name": "NewName"})

    def test_rename_prim_missing_new_name(self):
        assert not validate_event({"k": "rename_prim", "prim": "/World/OldName"})

    def test_rename_prim_empty_new_name(self):
        assert not validate_event({"k": "rename_prim", "prim": "/World/OldName", "new_name": ""})

    # --- set_visibility ---
    def test_set_visibility_valid(self):
        assert validate_event({"k": "set_visibility", "prim": "/World/Sphere", "visible": False})
        assert validate_event({"k": "set_visibility", "prim": "/World/Sphere", "visible": True})

    def test_set_visibility_missing_visible(self):
        assert not validate_event({"k": "set_visibility", "prim": "/World/Sphere"})

    def test_set_visibility_not_bool(self):
        assert not validate_event({"k": "set_visibility", "prim": "/World/Sphere", "visible": 0})

    # --- set_gprim_attrs ---
    def test_set_gprim_attrs_valid(self):
        assert validate_event(
            {
                "k": "set_gprim_attrs",
                "prim": "/World/Sphere/Geom",
                "attrs": {"radius": 2.0},
            }
        )

    def test_set_gprim_attrs_multiple(self):
        assert validate_event(
            {
                "k": "set_gprim_attrs",
                "prim": "/World/Cone/Geom",
                "attrs": {"height": 1.4, "radius": 0.6},
            }
        )

    def test_set_gprim_attrs_missing_attrs(self):
        assert not validate_event({"k": "set_gprim_attrs", "prim": "/World/Sphere/Geom"})

    def test_set_gprim_attrs_not_dict(self):
        assert not validate_event(
            {
                "k": "set_gprim_attrs",
                "prim": "/World/Sphere/Geom",
                "attrs": [1, 2],
            }
        )

    def test_set_gprim_attrs_non_string_key(self):
        assert not validate_event(
            {
                "k": "set_gprim_attrs",
                "prim": "/World/Sphere/Geom",
                "attrs": {1: 2.0},
            }
        )

    # --- set_reference ---
    def test_set_reference_valid(self):
        assert validate_event(
            {
                "k": "set_reference",
                "prim": "/World/Chair",
                "asset_path": "./assets/chair.usd",
            }
        )

    def test_set_reference_with_prim_path(self):
        assert validate_event(
            {
                "k": "set_reference",
                "prim": "/World/Chair",
                "asset_path": "./assets/chair.usd",
                "prim_path": "/Chair",
            }
        )

    def test_set_reference_missing_asset_path(self):
        assert not validate_event({"k": "set_reference", "prim": "/World/Chair"})

    def test_set_reference_empty_asset_path(self):
        assert not validate_event({"k": "set_reference", "prim": "/World/Chair", "asset_path": ""})

    def test_set_reference_bad_prim_path(self):
        assert not validate_event(
            {
                "k": "set_reference",
                "prim": "/World/Chair",
                "asset_path": "a.usd",
                "prim_path": "no_slash",
            }
        )
