"""Tests for codec.decode_messages — wire stream batch decode."""

from __future__ import annotations

from openusdconnect.codec import decode_messages, encode_message


def _event(seq: int, prim: str = "/World/Cube") -> bytes:
    return encode_message(
        {
            "type": "event",
            "seq": seq,
            "event": {"k": "ensure_prim", "prim": prim, "typeName": "Xform"},
        }
    )


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

    def test_threads_last_seq_in_and_out(self):
        first = decode_messages([_event(1, "/A"), _event(2, "/B")])
        second = decode_messages([_event(2, "/B-Dup"), _event(3, "/C")], last_seq=first.last_seq)

        assert [ev["prim"] for ev in second.received] == ["/C"]
        assert second.last_seq == 3

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

    def test_per_message_decode_errors_are_captured_not_raised(self):
        result = decode_messages([b"not-a-flatbuffer", _event(1, "/A")])

        assert len(result.errors) == 1
        assert [ev["prim"] for ev in result.received] == ["/A"]

    def test_ignores_unrecognized_message_types(self):
        # A bare event without a wrapping "type" field is silently dropped.
        result = decode_messages([encode_message({"type": "hello", "role": "emitter"})])
        assert result.received == []
        assert result.resync_requested is False
