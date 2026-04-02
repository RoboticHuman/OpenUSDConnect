"""Tests for transport helpers (send_line)."""

import json
import socket

from openusdconnect.transport import send_line


class TestSendLine:
    def test_sends_json_newline(self):
        a, b = socket.socketpair()
        try:
            send_line(a, {"type": "hello", "role": "emitter"})
            data = b.recv(4096)
            assert data.endswith(b"\n")
            parsed = json.loads(data.decode("utf-8").strip())
            assert parsed == {"type": "hello", "role": "emitter"}
        finally:
            a.close()
            b.close()

    def test_sends_unicode(self):
        a, b = socket.socketpair()
        try:
            send_line(a, {"name": "test\u2603"})
            data = b.recv(4096)
            parsed = json.loads(data.decode("utf-8").strip())
            assert parsed["name"] == "test\u2603"
        finally:
            a.close()
            b.close()
