"""Tests for openusdconnect.protocol — validation helpers, message construction."""

import pytest
from openusdconnect.protocol import (
    is_quat_valid,
    is_vec3_valid,
    is_mat16_valid,
    clamp_fields,
    make_hello,
    make_txn,
    make_quit,
    validate_event,
    PROTOCOL_VERSION,
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
