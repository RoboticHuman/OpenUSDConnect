"""Tests for codec.decode_messages — wire stream batch decode."""

from __future__ import annotations

from openusdconnect.codec import (
    SequenceGapError,
    decode_messages,
    encode_message,
    message_to_dict,
)


def _event(seq: int, prim: str = "/World/Cube") -> bytes:
    return encode_message(
        {
            "type": "event",
            "seq": seq,
            "event": {"k": "ensure_prim", "prim": prim, "typeName": "Xform"},
        }
    )


def test_broadcast_event_layer_routing_roundtrip():
    buf = encode_message(
        {
            "type": "event",
            "seq": 7,
            "event": {"k": "ensure_prim", "prim": "/A", "typeName": "Xform"},
            "layer_key": "department:fx",
        }
    )
    decoded = message_to_dict(buf)
    assert decoded["layer_key"] == "department:fx"

    without = message_to_dict(_event(8))
    assert "layer_key" not in without


def test_layer_stack_state_roundtrip():
    message = {
        "type": "layer_stack_state",
        "generation": "server-process",
        "revision": 4,
        "layers": [
            {
                "layer_key": "department:animation",
                "label": "Animation",
                "muted": False,
            },
            {
                "layer_key": "department:layout",
                "label": "Layout",
                "muted": True,
            },
            {
                "layer_key": "default",
                "label": "Default",
                "muted": True,
            },
        ],
    }
    assert message_to_dict(encode_message(message)) == message


class TestDecodeMessages:
    def test_extracts_events_and_deduplicates_sequence(self):
        result = decode_messages(
            [
                _event(1, "/World/A"),
                _event(1, "/World/A-Duplicate"),
                _event(2, "/World/B"),
            ]
        )

        assert [ev["prim"] for ev in result.received] == ["/World/A", "/World/B"]
        assert result.last_seq == 2

    def test_preserves_routing_envelopes_on_request(self):
        routed = encode_message(
            {
                "type": "event",
                "seq": 3,
                "event": {
                    "k": "ensure_prim",
                    "prim": "/World/A",
                    "typeName": "Xform",
                },
                "layer_key": "department:animation",
                "origin": "blender-a",
                "client_id": "artist-a",
                "client": "127.0.0.1:1234",
            }
        )
        result = decode_messages([routed], preserve_envelopes=True)

        assert len(result.received_records) == 1
        record = result.received_records[0]
        assert record.seq == 3
        assert record.event is result.received[0]
        assert record.layer_key == "department:animation"
        assert record.origin == "blender-a"
        assert record.client_id == "artist-a"
        assert record.client == "127.0.0.1:1234"

    def test_collects_layer_stack_control_state(self):
        state = {
            "type": "layer_stack_state",
            "generation": "server-process",
            "revision": 2,
            "layers": [
                {
                    "layer_key": "department:animation",
                    "label": "Animation",
                    "muted": False,
                },
                {
                    "layer_key": "default",
                    "label": "Default",
                    "muted": False,
                },
            ],
        }
        result = decode_messages([encode_message(state)])

        assert result.received == []
        assert result.layer_stack_states == [state]

    def test_threads_last_seq_in_and_out(self):
        first = decode_messages([_event(1, "/A"), _event(2, "/B")])
        second = decode_messages([_event(2, "/B-Dup"), _event(3, "/C")], last_seq=first.last_seq)

        assert [ev["prim"] for ev in second.received] == ["/C"]
        assert second.last_seq == 3

    def test_lossless_decode_stops_before_forward_gap(self):
        result = decode_messages(
            [_event(2, "/TooNew"), _event(1, "/Missing")],
            require_contiguous=True,
        )

        assert result.received == []
        assert result.last_seq == 0
        assert len(result.errors) == 1
        assert isinstance(result.errors[0], SequenceGapError)
        assert (result.errors[0].expected, result.errors[0].received) == (1, 2)

    def test_flat_decode_allows_origin_suppression_gaps(self):
        result = decode_messages([_event(2, "/Visible")])

        assert [event["prim"] for event in result.received] == ["/Visible"]
        assert result.last_seq == 2
        assert result.errors == []

    def test_handles_resync_and_rate_limit_messages(self):
        result = decode_messages(
            [
                encode_message({"type": "resync"}),
                encode_message({"type": "rate_limited", "retry_after": 0.25}),
                _event(1, "/World/AfterResync"),
            ],
            last_seq=99,
        )

        assert result.resync_requested is True
        assert result.rate_limited_retry_after == 0.25
        assert [ev["prim"] for ev in result.received] == ["/World/AfterResync"]
        assert result.last_seq == 1

    def test_can_clear_pre_resync_events_for_dcc_replay(self):
        result = decode_messages(
            [
                _event(1, "/World/BeforeResync"),
                encode_message({"type": "resync"}),
                _event(1, "/World/AfterResync"),
            ],
            clear_on_resync=True,
        )

        assert result.resync_requested is True
        assert [ev["prim"] for ev in result.received] == ["/World/AfterResync"]

    def test_keeps_pre_resync_events_when_clear_disabled(self):
        result = decode_messages(
            [
                _event(1, "/World/BeforeResync"),
                encode_message({"type": "resync"}),
                _event(1, "/World/AfterResync"),
            ],
        )

        assert result.resync_requested is True
        assert [ev["prim"] for ev in result.received] == [
            "/World/BeforeResync",
            "/World/AfterResync",
        ]

    def test_decode_error_stops_at_last_valid_sequence(self):
        result = decode_messages(
            [
                _event(1, "/A"),
                b"not-a-flatbuffer",
                _event(2, "/B"),
            ]
        )

        assert len(result.errors) == 1
        assert [ev["prim"] for ev in result.received] == ["/A"]
        assert result.last_seq == 1

    def test_ignores_unrecognized_message_types(self):
        # A bare event without a wrapping "type" field is silently dropped.
        result = decode_messages([encode_message({"type": "hello", "role": "emitter"})])
        assert result.received == []
        assert result.resync_requested is False
