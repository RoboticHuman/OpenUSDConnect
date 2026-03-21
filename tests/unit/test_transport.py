"""Tests for transport helpers (send_line, recv_lines, recv_lines_from_file)."""

import io
import json
import socket

from openusdconnect.transport import recv_lines, recv_lines_from_file, send_line


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


class TestRecvLines:
    def test_single_message(self):
        a, b = socket.socketpair()
        try:
            a.sendall(b'{"k":"hello"}\n')
            a.close()
            msgs = list(recv_lines(b))
            assert len(msgs) == 1
            assert msgs[0] == {"k": "hello"}
        finally:
            b.close()

    def test_multiple_messages(self):
        a, b = socket.socketpair()
        try:
            a.sendall(b'{"seq":1}\n{"seq":2}\n{"seq":3}\n')
            a.close()
            msgs = list(recv_lines(b))
            assert len(msgs) == 3
            assert [m["seq"] for m in msgs] == [1, 2, 3]
        finally:
            b.close()

    def test_empty_connection(self):
        a, b = socket.socketpair()
        try:
            a.close()
            msgs = list(recv_lines(b))
            assert msgs == []
        finally:
            b.close()

    def test_partial_then_complete(self):
        """Data arriving in chunks that split a line."""
        a, b = socket.socketpair()
        try:
            a.sendall(b'{"k":')
            a.sendall(b'"test"}\n')
            a.close()
            msgs = list(recv_lines(b))
            assert len(msgs) == 1
            assert msgs[0] == {"k": "test"}
        finally:
            b.close()


class TestRecvLinesFromFile:
    def test_single_line(self):
        f = io.StringIO('{"type":"event"}\n')
        msgs = list(recv_lines_from_file(f))
        assert msgs == [{"type": "event"}]

    def test_multiple_lines(self):
        f = io.StringIO('{"a":1}\n{"a":2}\n')
        msgs = list(recv_lines_from_file(f))
        assert len(msgs) == 2

    def test_blank_lines_skipped(self):
        f = io.StringIO('{"a":1}\n\n\n{"a":2}\n')
        msgs = list(recv_lines_from_file(f))
        assert len(msgs) == 2

    def test_empty_file(self):
        f = io.StringIO("")
        msgs = list(recv_lines_from_file(f))
        assert msgs == []
