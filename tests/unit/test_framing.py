"""Tests for length-prefixed binary framing."""

import io
import struct

import pytest

from openusdconnect.framing import (
    MAX_MESSAGE_SIZE,
    IncompleteRead,
    MessageTooLarge,
    frame_batch,
    recv_framed_rfile,
    send_framed_wfile,
)


class TestSendRecvRfile:
    def test_roundtrip(self):
        payload = b"hello flatbuffers"
        buf = io.BytesIO()
        send_framed_wfile(buf, payload)
        buf.seek(0)
        result = recv_framed_rfile(buf)
        assert result == payload

    def test_empty_payload(self):
        buf = io.BytesIO()
        send_framed_wfile(buf, b"")
        buf.seek(0)
        result = recv_framed_rfile(buf)
        assert result == b""

    def test_large_payload(self):
        payload = b"x" * 65536
        buf = io.BytesIO()
        send_framed_wfile(buf, payload)
        buf.seek(0)
        result = recv_framed_rfile(buf)
        assert result == payload

    def test_multiple_messages(self):
        buf = io.BytesIO()
        payloads = [b"msg1", b"msg2", b"msg3"]
        for p in payloads:
            send_framed_wfile(buf, p)
        buf.seek(0)
        results = [recv_framed_rfile(buf) for _ in payloads]
        assert results == payloads

    def test_message_too_large(self):
        # Forge a header claiming a huge payload
        buf = io.BytesIO(struct.pack(">I", MAX_MESSAGE_SIZE + 1) + b"\x00")
        with pytest.raises(MessageTooLarge):
            recv_framed_rfile(buf)

    def test_custom_max_size(self):
        buf = io.BytesIO(struct.pack(">I", 100) + b"\x00" * 100)
        with pytest.raises(MessageTooLarge):
            recv_framed_rfile(buf, max_size=50)

    def test_incomplete_header(self):
        buf = io.BytesIO(b"\x00\x00")  # only 2 bytes, need 4
        with pytest.raises(IncompleteRead):
            recv_framed_rfile(buf)

    def test_incomplete_payload(self):
        # Header says 100 bytes but only 10 available
        buf = io.BytesIO(struct.pack(">I", 100) + b"\x00" * 10)
        with pytest.raises(IncompleteRead):
            recv_framed_rfile(buf)

    def test_empty_stream(self):
        buf = io.BytesIO(b"")
        with pytest.raises(IncompleteRead):
            recv_framed_rfile(buf)


class TestFrameBatch:
    def test_single(self):
        batch = frame_batch([b"one"])
        buf = io.BytesIO(batch)
        assert recv_framed_rfile(buf) == b"one"

    def test_multiple(self):
        payloads = [b"alpha", b"beta", b"gamma"]
        batch = frame_batch(payloads)
        buf = io.BytesIO(batch)
        results = [recv_framed_rfile(buf) for _ in payloads]
        assert results == payloads

    def test_empty_list(self):
        assert frame_batch([]) == b""

    def test_batch_byte_layout(self):
        """Verify the exact wire layout of a batch."""
        batch = frame_batch([b"AB", b"C"])
        # First message: 4-byte header (len=2) + b"AB"
        # Second message: 4-byte header (len=1) + b"C"
        expected = struct.pack(">I", 2) + b"AB" + struct.pack(">I", 1) + b"C"
        assert batch == expected
