"""Round-trip tests for the FlatBuffers codec.

Tests encode_message → decode via zero-copy API and message_to_dict for
all message types and all 17 event kinds.
"""

import flatbuffers
import numpy as np
import pytest

from openusdconnect.codec import (
    AttrValueType,
    PayloadType,
    decode_envelope,
    encode_message,
    is_ping,
    message_to_dict,
    payload_type,
    resolve_event,
    resolve_payload,
)
from openusdconnect.generated import messages_generated as _fb

# ===================================================================
# Helper
# ===================================================================


def _roundtrip(msg_dict):
    """Encode dict → FlatBuffers bytes → decode back to dict."""
    buf = encode_message(msg_dict)
    return message_to_dict(buf), buf


# ===================================================================
# Message-level tests
# ===================================================================


class TestSchemaVersion:
    def test_version_set(self):
        from openusdconnect.codec import SCHEMA_VERSION

        buf = encode_message({"type": "ping"})
        env = decode_envelope(buf)
        assert env.SchemaVersion() == SCHEMA_VERSION

    def test_version_is_two(self):
        from openusdconnect.codec import SCHEMA_VERSION

        assert SCHEMA_VERSION == 2

    @pytest.mark.parametrize("version", [0, 1])
    def test_incompatible_version_is_rejected(self, version):
        builder = flatbuffers.Builder(32)
        _fb.PingStart(builder)
        ping = _fb.PingEnd(builder)
        _fb.EnvelopeStart(builder)
        _fb.EnvelopeAddPayloadType(builder, _fb.Payload.Ping)
        _fb.EnvelopeAddPayload(builder, ping)
        _fb.EnvelopeAddSchemaVersion(builder, version)
        envelope = _fb.EnvelopeEnd(builder)
        builder.Finish(envelope)

        with pytest.raises(
            ValueError,
            match=rf"unsupported schema version {version}",
        ):
            decode_envelope(bytes(builder.Output()))


class TestPing:
    def test_encode_decode(self):
        buf = encode_message({"type": "ping"})
        assert is_ping(buf)
        assert payload_type(buf) == PayloadType.Ping
        assert message_to_dict(buf) == {"type": "ping"}

    def test_not_ping(self):
        buf = encode_message({"type": "quit"})
        assert not is_ping(buf)


class TestHello:
    def test_full(self):
        msg = {
            "type": "hello",
            "role": "emitter",
            "protocol_version": 2,
            "sync_from": 42,
            "client_id": "c1",
            "origin": "blender-1",
            "department": "anim",
            "token": "tok-abc",
        }
        d, _ = _roundtrip(msg)
        assert d == msg

    def test_minimal(self):
        msg = {"type": "hello", "role": "receiver", "protocol_version": 1}
        d, _ = _roundtrip(msg)
        assert d["role"] == "receiver"
        assert d["protocol_version"] == 1

    def test_zero_copy_access(self):
        msg = {
            "type": "hello",
            "role": "emitter",
            "protocol_version": 2,
            "client_id": "c1",
            "origin": "o1",
        }
        buf = encode_message(msg)
        env = decode_envelope(buf)
        mt, hello = resolve_payload(env)
        assert mt == "hello"
        # FlatBuffers returns bytes for strings
        assert hello.Role() in (b"emitter", "emitter")
        assert hello.ProtocolVersion() == 2


class TestHelloOk:
    def test_with_token(self):
        d, _ = _roundtrip({"type": "hello_ok", "token": "new-tok"})
        assert d == {"type": "hello_ok", "token": "new-tok"}

    def test_without_token(self):
        d, _ = _roundtrip({"type": "hello_ok"})
        assert d == {"type": "hello_ok"}


class TestAuthRejected:
    def test_roundtrip(self):
        d, _ = _roundtrip({"type": "auth_rejected", "reason": "bad token"})
        assert d["reason"] == "bad token"


class TestResyncCompactQuit:
    @pytest.mark.parametrize("msg_type", ["resync", "compact", "quit"])
    def test_empty_messages(self, msg_type):
        d, _ = _roundtrip({"type": msg_type})
        assert d == {"type": msg_type}


class TestRateLimited:
    def test_roundtrip(self):
        d, _ = _roundtrip({"type": "rate_limited", "retry_after": 1.5})
        assert abs(d["retry_after"] - 1.5) < 0.01


class TestProposalCreated:
    def test_roundtrip(self):
        d, _ = _roundtrip({"type": "proposal_created", "proposal_id": "p-123"})
        assert d["proposal_id"] == "p-123"


class TestTxnProposalId:
    def test_roundtrip_with_proposal_id(self):
        msg = {"type": "txn", "client_id": "c1", "events": [], "proposal_id": "prop-abc"}
        d, _ = _roundtrip(msg)
        assert d["proposal_id"] == "prop-abc"

    def test_absent_when_not_set(self):
        d, _ = _roundtrip({"type": "txn", "client_id": "c1", "events": []})
        assert "proposal_id" not in d

    def test_zero_copy_accessor(self):
        # The connection handler reads txn.ProposalId() to route proposal edits.
        buf = encode_message(
            {"type": "txn", "client_id": "c1", "events": [], "proposal_id": "prop-xyz"}
        )
        _, txn = resolve_payload(decode_envelope(buf))
        assert txn.ProposalId() in (b"prop-xyz", "prop-xyz")


# ===================================================================
# Event-level tests — all 17 kinds
# ===================================================================


def _txn_roundtrip(event_dict):
    """Wrap an event in a txn, encode, decode, return the event dict."""
    msg = {"type": "txn", "client_id": "test", "events": [event_dict]}
    d, buf = _roundtrip(msg)
    return d["events"][0]


def _txn_zerocopy(event_dict):
    """Wrap event in txn, encode, return (kind, typed_fb_object) via zero-copy."""
    msg = {"type": "txn", "client_id": "test", "events": [event_dict]}
    buf = encode_message(msg)
    env = decode_envelope(buf)
    _, txn = resolve_payload(env)
    ew = txn.Events(0)
    return resolve_event(ew)


class TestEnsurePrim:
    def test_roundtrip(self):
        ev = {"k": "ensure_prim", "prim": "/World/Sphere", "typeName": "Xform"}
        d = _txn_roundtrip(ev)
        assert d == ev

    def test_missing_typename_rejected(self):
        ev = {"k": "ensure_prim", "prim": "/World/Scope"}
        with pytest.raises(KeyError):
            _txn_roundtrip(ev)


class TestEnsureXformOps:
    def test_roundtrip(self):
        ev = {"k": "ensure_xform_ops", "prim": "/World/Cube"}
        assert _txn_roundtrip(ev) == ev


class TestSetXformTrs:
    def test_full_trs(self):
        ev = {
            "k": "set_xform_trs",
            "prim": "/World/Cube",
            "fields": ["t", "r", "s"],
            "t": [1.0, 2.0, 3.0],
            "r": [1.0, 0.0, 0.0, 0.0],
            "s": [2.0, 2.0, 2.0],
        }
        d = _txn_roundtrip(ev)
        assert d["fields"] == ["t", "r", "s"]
        assert d["t"] == pytest.approx([1, 2, 3])
        assert d["r"] == pytest.approx([1, 0, 0, 0])
        assert d["s"] == pytest.approx([2, 2, 2])

    def test_partial_translate_only(self):
        ev = {
            "k": "set_xform_trs",
            "prim": "/World/Cube",
            "fields": ["t"],
            "t": [5.0, 0.0, 0.0],
        }
        d = _txn_roundtrip(ev)
        assert d["fields"] == ["t"]
        assert "r" not in d
        assert "s" not in d

    def test_zero_copy_access(self):
        ev = {
            "k": "set_xform_trs",
            "prim": "/World/Cube",
            "fields": ["t", "r", "s"],
            "t": [1.0, 2.0, 3.0],
            "r": [1.0, 0.0, 0.0, 0.0],
            "s": [1.0, 1.0, 1.0],
        }
        kind, trs = _txn_zerocopy(ev)
        assert kind == "set_xform_trs"
        assert trs.TLength() == 3
        assert trs.T(0) == pytest.approx(1.0)
        np_t = trs.TAsNumpy()
        assert np_t.dtype == np.float32
        assert list(np_t) == pytest.approx([1, 2, 3])


class TestDeletePrim:
    def test_roundtrip(self):
        ev = {"k": "delete_prim", "prim": "/World/Gone"}
        assert _txn_roundtrip(ev) == ev


class TestDeactivatePrim:
    def test_roundtrip(self):
        ev = {"k": "deactivate_prim", "prim": "/World/X", "active": False}
        d = _txn_roundtrip(ev)
        assert d["active"] is False

    def test_active_true(self):
        """active=True must survive round-trip (verifies schema default change)."""
        ev = {"k": "deactivate_prim", "prim": "/World/X", "active": True}
        d = _txn_roundtrip(ev)
        assert d["active"] is True


class TestRenamePrim:
    def test_roundtrip(self):
        ev = {"k": "rename_prim", "prim": "/World/Old", "new_name": "New"}
        assert _txn_roundtrip(ev) == ev


class TestSetVisibility:
    def test_roundtrip(self):
        ev = {"k": "set_visibility", "prim": "/World/X", "visible": False}
        d = _txn_roundtrip(ev)
        assert d["visible"] is False


class TestSetGprimAttrs:
    def test_scalar_attrs(self):
        ev = {
            "k": "set_gprim_attrs",
            "prim": "/World/Mesh/Geom",
            "attrs": {"radius": 2.5, "subdivisionScheme": "catmullClark"},
        }
        d = _txn_roundtrip(ev)
        assert d["attrs"]["radius"] == pytest.approx(2.5)
        assert d["attrs"]["subdivisionScheme"] == "catmullClark"

    def test_float_array(self):
        ev = {
            "k": "set_gprim_attrs",
            "prim": "/World/Mesh/Geom",
            "attrs": {"extent": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]},
        }
        d = _txn_roundtrip(ev)
        assert d["attrs"]["extent"] == pytest.approx([1, 2, 3, 4, 5, 6])

    def test_int_array(self):
        ev = {
            "k": "set_gprim_attrs",
            "prim": "/World/Mesh/Geom",
            "attrs": {"faceVertexCounts": [3, 3, 4, 4]},
        }
        d = _txn_roundtrip(ev)
        assert d["attrs"]["faceVertexCounts"] == [3, 3, 4, 4]

    def test_nested_vec3_array(self):
        """Vec3fArray encoded as flattened float array with stride=3."""
        points = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]]
        ev = {
            "k": "set_gprim_attrs",
            "prim": "/World/Mesh/Geom",
            "attrs": {"points": points},
        }
        d = _txn_roundtrip(ev)
        result = d["attrs"]["points"]
        assert len(result) == 3
        for got, expected in zip(result, points, strict=True):
            assert got == pytest.approx(expected)

    def test_zero_copy_numpy_access(self):
        """Verify zero-copy numpy access to large float arrays."""
        points = [[float(i), float(i + 1), float(i + 2)] for i in range(0, 3000, 3)]
        ev = {
            "k": "set_gprim_attrs",
            "prim": "/World/Mesh/Geom",
            "attrs": {"points": points},
        }
        kind, sg = _txn_zerocopy(ev)
        assert kind == "set_gprim_attrs"
        na = sg.Attrs(0)
        av = na.Value()
        assert av.ValueType() == AttrValueType.FloatArray
        assert av.Stride() == 3
        np_arr = av.FloatArrayAsNumpy()
        assert np_arr.dtype == np.float32
        assert len(np_arr) == 3000
        # Reshape using stride
        reshaped = np_arr.reshape(-1, av.Stride())
        assert reshaped.shape == (1000, 3)
        assert reshaped[0].tolist() == pytest.approx([0, 1, 2])

    def test_primvar_meta(self):
        ev = {
            "k": "set_gprim_attrs",
            "prim": "/World/Mesh/Geom",
            "attrs": {"primvars:st": [[0.0, 1.0], [1.0, 0.0]]},
            "primvar_meta": {
                "primvars:st": {"typeName": "texCoord2f[]", "interpolation": "faceVarying"}
            },
        }
        d = _txn_roundtrip(ev)
        assert d["primvar_meta"]["primvars:st"]["typeName"] == "texCoord2f[]"
        assert d["primvar_meta"]["primvars:st"]["interpolation"] == "faceVarying"

    def test_attr_interp(self):
        ev = {
            "k": "set_gprim_attrs",
            "prim": "/World/Mesh/Geom",
            "attrs": {"normals": [[0.0, 1.0, 0.0]]},
            "attr_interp": {"normals": "faceVarying"},
        }
        d = _txn_roundtrip(ev)
        assert d["attr_interp"]["normals"] == "faceVarying"

    def test_bool_attr(self):
        ev = {
            "k": "set_gprim_attrs",
            "prim": "/World/Mesh/Geom",
            "attrs": {"doubleSided": True},
        }
        d = _txn_roundtrip(ev)
        assert d["attrs"]["doubleSided"] is True


class TestSetReference:
    def test_roundtrip(self):
        ev = {
            "k": "set_reference",
            "prim": "/World/Chair",
            "refs": [
                {"asset_path": "./chair.usd", "prim_path": "/Model"},
                {"asset_path": "./table.usd"},
            ],
        }
        d = _txn_roundtrip(ev)
        assert d["refs"][0] == {"asset_path": "./chair.usd", "prim_path": "/Model"}
        assert d["refs"][1] == {"asset_path": "./table.usd"}
        assert d["list_op_authored"] is True
        assert d["list_op_explicit"] is False

    def test_rich_list_op_roundtrip(self):
        fragment = "#usda 1.0\n(\n    customLayerData = { int rank = 7 }\n)\n"
        ev = {
            "k": "set_reference",
            "prim": "/World/Asset",
            "refs": [
                {
                    "asset_path": "./added.usd",
                    "prim_path": "/Model",
                    "list_position": "added",
                    "layer_offset": 7.0,
                    "layer_scale": 2.0,
                    "custom_data_fragment": fragment,
                },
                {
                    "asset_path": "./deleted.usd",
                    "list_position": "deleted",
                },
                {
                    "prim_path": "/Internal",
                    "list_position": "ordered",
                },
            ],
            "list_op_authored": True,
            "list_op_explicit": False,
        }

        assert _txn_roundtrip(ev) == ev

    def test_explicit_empty_roundtrip(self):
        ev = {
            "k": "set_reference",
            "prim": "/World/Asset",
            "refs": [],
            "list_op_authored": True,
            "list_op_explicit": True,
        }

        assert _txn_roundtrip(ev) == ev


class TestSetPayload:
    def test_roundtrip(self):
        ev = {
            "k": "set_payload",
            "prim": "/World/Asset",
            "payloads": [{"asset_path": "./heavy.usda", "prim_path": "/Root"}],
        }
        d = _txn_roundtrip(ev)
        assert d["payloads"][0]["asset_path"] == "./heavy.usda"
        assert d["list_op_authored"] is True
        assert d["list_op_explicit"] is False

    def test_offset_and_list_position_roundtrip(self):
        ev = {
            "k": "set_payload",
            "prim": "/World/Asset",
            "payloads": [
                {
                    "asset_path": "./heavy.usda",
                    "prim_path": "/Root",
                    "list_position": "appended",
                    "layer_offset": -3.5,
                    "layer_scale": 0.5,
                }
            ],
            "list_op_authored": True,
            "list_op_explicit": False,
        }

        assert _txn_roundtrip(ev) == ev


class TestLoadUnloadPayload:
    def test_load(self):
        ev = {"k": "load_payload", "prim": "/World/Asset"}
        assert _txn_roundtrip(ev) == ev

    def test_unload(self):
        ev = {"k": "unload_payload", "prim": "/World/Asset"}
        assert _txn_roundtrip(ev) == ev


class TestSetVariantSelections:
    def test_roundtrip(self):
        ev = {
            "k": "set_variant_selections",
            "prim": "/World/Car",
            "selections": {"wheels": "wide", "color": "red"},
        }
        d = _txn_roundtrip(ev)
        assert d["selections"] == {"wheels": "wide", "color": "red"}


class TestSetMaterialBinding:
    def test_roundtrip(self):
        ev = {
            "k": "set_material_binding",
            "prim": "/World/Mesh",
            "material_path": "/World/Materials/Wood",
        }
        assert _txn_roundtrip(ev) == ev


class TestSetConnectableInput:
    def test_roundtrip(self):
        ev = {
            "k": "set_connectable_input",
            "prim": "/World/Mat/Shader",
            "info_id": "UsdPreviewSurface",
            "inputs": {"metallic": 0.8, "diffuseColor": [0.5, 0.5, 0.5]},
            "input_types": {"metallic": "float", "diffuseColor": "color3f"},
        }
        d = _txn_roundtrip(ev)
        assert d["inputs"]["metallic"] == pytest.approx(0.8)
        assert d["inputs"]["diffuseColor"] == pytest.approx([0.5, 0.5, 0.5])
        assert d["input_types"]["metallic"] == "float"

    def test_falsy_bool(self):
        """bool=False must round-trip as False, not 0.0."""
        ev = {
            "k": "set_connectable_input",
            "prim": "/S",
            "info_id": "X",
            "inputs": {"flag": False},
            "input_types": {"flag": "bool"},
        }
        d = _txn_roundtrip(ev)
        assert d["inputs"]["flag"] is False

    def test_zero_int(self):
        """int=0 must round-trip as int 0, not float 0.0."""
        ev = {
            "k": "set_connectable_input",
            "prim": "/S",
            "info_id": "X",
            "inputs": {"count": 0},
            "input_types": {"count": "int"},
        }
        d = _txn_roundtrip(ev)
        assert d["inputs"]["count"] == 0
        assert isinstance(d["inputs"]["count"], int)

    def test_zero_float(self):
        """float=0.0 must round-trip correctly."""
        ev = {
            "k": "set_connectable_input",
            "prim": "/S",
            "info_id": "X",
            "inputs": {"metallic": 0.0},
            "input_types": {"metallic": "float"},
        }
        d = _txn_roundtrip(ev)
        assert d["inputs"]["metallic"] == 0.0

    def test_empty_string(self):
        """Empty string must round-trip as '', not None or 0.0."""
        ev = {
            "k": "set_connectable_input",
            "prim": "/S",
            "info_id": "X",
            "inputs": {"file": ""},
            "input_types": {"file": "asset"},
        }
        d = _txn_roundtrip(ev)
        assert d["inputs"]["file"] == ""


class TestSetConnectableConnection:
    def test_roundtrip(self):
        ev = {
            "k": "set_connectable_connection",
            "prim": "/World/Mat/Shader",
            "connections": {
                "inputs:diffuseColor": {
                    "source_prim": "/World/Mat/Tex",
                    "source_attr": "outputs:rgb",
                },
            },
            "disconnections": ["inputs:opacity"],
        }
        d = _txn_roundtrip(ev)
        conn = d["connections"]["inputs:diffuseColor"]
        assert conn["source_prim"] == "/World/Mat/Tex"
        assert conn["source_attr"] == "outputs:rgb"
        assert d["disconnections"] == ["inputs:opacity"]

    def test_roundtrip_output_side(self):
        """Material/NodeGraph output port connections ride the same wire
        shape — only the namespace prefix on local_attr differs."""
        ev = {
            "k": "set_connectable_connection",
            "prim": "/World/Mat",
            "connections": {
                "outputs:surface": {
                    "source_prim": "/World/Mat/PBR",
                    "source_attr": "outputs:surface",
                },
            },
        }
        d = _txn_roundtrip(ev)
        conn = d["connections"]["outputs:surface"]
        assert conn["source_prim"] == "/World/Mat/PBR"
        assert conn["source_attr"] == "outputs:surface"


# ===================================================================
# Broadcast event (wraps an event with seq + origin)
# ===================================================================


class TestBroadcastEvent:
    def test_roundtrip(self):
        msg = {
            "type": "event",
            "seq": 42,
            "event": {"k": "set_visibility", "prim": "/World/X", "visible": True},
            "origin": "blender-1",
        }
        d, _ = _roundtrip(msg)
        assert d["seq"] == 42
        assert d["event"]["k"] == "set_visibility"
        assert d["origin"] == "blender-1"


# ===================================================================
# CreateProposal
# ===================================================================


class TestCreateProposal:
    def test_roundtrip(self):
        msg = {
            "type": "create_proposal",
            "target_department": "lighting",
            "events": [{"k": "set_visibility", "prim": "/World/X", "visible": False}],
            "description": "hide for lighting pass",
        }
        d, _ = _roundtrip(msg)
        assert d["target_department"] == "lighting"
        assert d["events"][0]["visible"] is False
        assert d["description"] == "hide for lighting pass"


# ===================================================================
# Multi-event transaction
# ===================================================================


class TestMultiEventTxn:
    def test_multiple_events(self):
        msg = {
            "type": "txn",
            "client_id": "c1",
            "events": [
                {"k": "ensure_prim", "prim": "/World/New", "typeName": "Xform"},
                {"k": "ensure_xform_ops", "prim": "/World/New"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/New",
                    "fields": ["t"],
                    "t": [1.0, 0.0, 0.0],
                },
                {"k": "set_visibility", "prim": "/World/New", "visible": True},
            ],
        }
        d, _ = _roundtrip(msg)
        assert len(d["events"]) == 4
        assert d["events"][0]["k"] == "ensure_prim"
        assert d["events"][2]["t"] == pytest.approx([1, 0, 0])


# ===================================================================
# Schema ↔ protocol sync guards
# ===================================================================


class TestSchemaProtocolSync:
    """Fail if the FlatBuffers schema and protocol.py / codec.py drift apart."""

    @staticmethod
    def _enum_values(cls):
        """Extract non-zero enum values from a FlatBuffers enum class."""
        return {
            v
            for k, v in vars(cls).items()
            if isinstance(v, int) and v != 0 and not k.startswith("_")
        }

    def test_event_kinds_cover_schema(self):
        """Every FB EventPayload variant has a codec entry and K_* constant."""
        from openusdconnect import events
        from openusdconnect.codec import EventPayloadType

        fb_tags = self._enum_values(EventPayloadType)
        registered_tags = {spec.fb_tag for spec in events.all_specs() if spec.fb_tag is not None}

        assert fb_tags == registered_tags, (
            f"Schema/registry mismatch — "
            f"in schema but not registered: {fb_tags - registered_tags}, "
            f"registered but not in schema: {registered_tags - fb_tags}"
        )

    def test_message_types_cover_schema(self):
        """Every FB Payload variant has a codec entry and MSG_* constant."""
        from openusdconnect.codec import _PAYLOAD_TO_MSG_TYPE, PayloadType

        fb_tags = self._enum_values(PayloadType)
        codec_tags = set(_PAYLOAD_TO_MSG_TYPE.keys())

        assert fb_tags == codec_tags, (
            f"Schema/codec mismatch — "
            f"in schema but not codec: {fb_tags - codec_tags}, "
            f"in codec but not schema: {codec_tags - fb_tags}"
        )


class TestConnectableInputDeclaredTypeWire:
    """Numeric payload slots follow the declared type, not the emitting
    language's literal type: a JSON 1 / [1, 1, 1] for float-typed inputs
    travels in the float slots, so typed receivers never see int payloads
    for float inputs."""

    def test_integral_literals_encode_as_floats(self):
        ev = {
            "k": "set_connectable_input",
            "prim": "/World/Looks/Chrome/Surface",
            "info_id": "UsdPreviewSurface",
            "inputs": {"diffuseColor": [1, 1, 1], "metallic": 1, "roughness": 0},
            "input_types": {
                "diffuseColor": "color3f",
                "metallic": "float",
                "roughness": "float",
            },
        }
        d = _txn_roundtrip(ev)
        assert d["inputs"]["diffuseColor"] == [1.0, 1.0, 1.0]
        assert all(isinstance(v, float) for v in d["inputs"]["diffuseColor"])
        assert isinstance(d["inputs"]["metallic"], float)
        assert isinstance(d["inputs"]["roughness"], float)

    def test_int_declared_inputs_stay_int(self):
        ev = {
            "k": "set_connectable_input",
            "prim": "/World/Looks/M/S",
            "info_id": "MyShader",
            "inputs": {"samples": 4, "indices": [1, 2, 3]},
            "input_types": {"samples": "int", "indices": "int[]"},
        }
        d = _txn_roundtrip(ev)
        assert d["inputs"]["samples"] == 4
        assert isinstance(d["inputs"]["samples"], int)
        assert d["inputs"]["indices"] == [1, 2, 3]
        assert all(isinstance(v, int) for v in d["inputs"]["indices"])
