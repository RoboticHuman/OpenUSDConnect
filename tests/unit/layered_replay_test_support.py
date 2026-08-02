"""Shared message builders for layered replay unit tests."""

from __future__ import annotations

from pxr import Sdf

from openusdconnect.codec import encode_message

_STRONG_LAYER = "collaboration:strong"
_WEAK_LAYER = "review:overrides"
_BASE_LAYER = "base"


def _state(
    revision: int,
    layers: list[tuple[str, str, bool]],
    *,
    generation: str = "server-a",
) -> dict:
    return {
        "type": "layer_stack_state",
        "generation": generation,
        "revision": revision,
        "layers": [
            {
                "layer_key": layer_key,
                "label": label,
                "muted": muted,
            }
            for layer_key, label, muted in layers
        ],
    }


class _LayeredQueue:
    layered_replay = True
    layered_replay_active = True

    def __init__(self, messages):
        self.messages = list(messages)
        self.replay_requests = []

    def drain_queue(self):
        messages = self.messages
        self.messages = []
        return messages

    def request_replay_from(self, seq_start):
        self.replay_requests.append(seq_start)


def _property_event(seq: int, layer_key: str, value: int) -> bytes:
    fragment = Sdf.Layer.CreateAnonymous("property-fragment")
    prim = Sdf.CreatePrimInLayer(fragment, "/World/Thing")
    attr = Sdf.AttributeSpec(
        prim,
        "userProperties:value",
        Sdf.ValueTypeNames.Int,
    )
    attr.default = value
    return encode_message(
        {
            "type": "event",
            "seq": seq,
            "layer_key": layer_key,
            "event": {
                "k": "set_sdf_spec_fields",
                "prim": "/World/Thing",
                "spec_path": "/World/Thing.userProperties:value",
                "spec_kind": "attribute",
                "fields": ["default"],
                "fragment": fragment.ExportToString(),
                "removed": False,
            },
        }
    )


def _event(seq: int, layer_key: str, event: dict) -> bytes:
    return encode_message(
        {
            "type": "event",
            "seq": seq,
            "layer_key": layer_key,
            "event": event,
        }
    )


def _xform_events(
    seq: int,
    layer_key: str,
    value: float,
    prim_path: str = "/World/Thing",
) -> list[bytes]:
    return [
        _event(
            seq,
            layer_key,
            {
                "k": "ensure_prim",
                "prim": prim_path,
                "typeName": "Xform",
            },
        ),
        _event(
            seq + 1,
            layer_key,
            {"k": "ensure_xform_ops", "prim": prim_path},
        ),
        _event(
            seq + 2,
            layer_key,
            {
                "k": "set_xform_trs",
                "prim": prim_path,
                "fields": ["t"],
                "t": [value, 0.0, 0.0],
            },
        ),
    ]


def _clear_translate_event(seq: int, layer_key: str) -> bytes:
    return _event(
        seq,
        layer_key,
        {
            "k": "set_sdf_spec_fields",
            "prim": "/World/Thing",
            "spec_path": "/World/Thing.xformOp:translate",
            "spec_kind": "attribute",
            "fields": ["default"],
            "fragment": "",
            "removed": True,
        },
    )


def _clear_property_event(seq: int, layer_key: str, property_name: str) -> bytes:
    return _event(
        seq,
        layer_key,
        {
            "k": "set_sdf_spec_fields",
            "prim": "/World/Thing",
            "spec_path": f"/World/Thing.{property_name}",
            "spec_kind": "attribute",
            "fields": ["default"],
            "fragment": "",
            "removed": True,
        },
    )


def _clear_prim_field_event(seq: int, layer_key: str, field: str) -> bytes:
    fragment = Sdf.Layer.CreateAnonymous("prim-field-fragment")
    Sdf.CreatePrimInLayer(fragment, "/World/Thing")
    return _event(
        seq,
        layer_key,
        {
            "k": "set_sdf_spec_fields",
            "prim": "/World/Thing",
            "spec_path": "/World/Thing",
            "spec_kind": "prim",
            "fields": [field],
            "fragment": fragment.ExportToString(),
            "removed": False,
        },
    )
