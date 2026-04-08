"""Tests for transport helpers (send_msg / recv_msg)."""

import socket

from openusdconnect.codec import message_to_dict
from openusdconnect.framing import recv_framed
from openusdconnect.transport import send_msg


class TestSendMsg:
    def test_sends_framed_flatbuffers(self):
        a, b = socket.socketpair()
        try:
            send_msg(a, {"type": "hello", "role": "emitter", "protocol_version": 1})
            buf = recv_framed(b)
            parsed = message_to_dict(buf)
            assert parsed["type"] == "hello"
            assert parsed["role"] == "emitter"
        finally:
            a.close()
            b.close()

    def test_roundtrip_event(self):
        a, b = socket.socketpair()
        try:
            msg = {
                "type": "event", "seq": 42,
                "event": {"k": "set_visibility", "prim": "/World/X", "visible": True},
            }
            send_msg(a, msg)
            buf = recv_framed(b)
            parsed = message_to_dict(buf)
            assert parsed["seq"] == 42
            assert parsed["event"]["visible"] is True
        finally:
            a.close()
            b.close()
