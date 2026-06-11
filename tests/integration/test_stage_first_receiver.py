"""Integration test for the Blender receiver's stage-first architecture.

Verifies the full flow: server → receiver → stage commit (atomic) → adapter.
Uses a real server (subprocess), real ReceiverThread, and MockAdapter.
No Blender required — headless, runs in CI.
"""

import socket
import time

import pytest
from pxr import Usd, UsdGeom

from openusdconnect.adapters import MockAdapter
from openusdconnect.codec import message_to_dict
from openusdconnect.event_apply import apply_events, atomic_apply
from openusdconnect.protocol import (
    make_hello,
    make_txn,
)
from openusdconnect.protocol_constants import (
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_SET_XFORM_TRS,
    MSG_EVENT,
    MSG_RESYNC,
)
from openusdconnect.receiver import ReceiverThread
from openusdconnect.transport import recv_msg, send_msg
from tests.helpers import start_server, stop_server

PORT = 7291  # Unique port to avoid conflicts with other tests


@pytest.fixture
def server(tmp_path):
    """Start a real TCP server subprocess."""
    proc = start_server(tmp_path, PORT)
    yield proc
    stop_server(proc)


def _send_events_to_server(events, port=PORT):
    """Connect as emitter, complete the handshake, and send a batch of events.

    Mirrors the production EventSender contract: read hello_ok before
    sending, and retry on a dropped connection. Reading hello_ok also
    drains the receive buffer, so the close is an orderly FIN; closing
    with unread data sends RST, which discards a txn the loaded server
    has not pulled out of its receive buffer yet.
    """
    for attempt in range(3):
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            send_msg(sock, make_hello("emitter", client_id="test-emitter", origin="test"))
            reply = message_to_dict(recv_msg(sock))
            assert reply.get("type") == "hello_ok", f"handshake failed: {reply}"
            send_msg(sock, make_txn("test-emitter", events))
            return
        except ConnectionError:
            if attempt == 2:
                raise
            time.sleep(0.3)
        finally:
            sock.close()


def _receive_events(min_events=1, timeout=30.0):
    """Connect as receiver and drain until the log replay arrives.

    Polls for *min_events* parsed events instead of sleeping a fixed
    interval: under machine load the server's startup, txn processing,
    and replay can outlast any constant, which surfaces as spuriously
    empty drains. The deadline is a ceiling, not a wait; healthy runs
    return as soon as the events land. Fails here, at the wait, rather
    than letting a short drain confuse downstream assertions.
    """
    rt = ReceiverThread(
        host="127.0.0.1", port=PORT, sync_from=1, client_id="test-receiver", origin="test-recv"
    )
    rt.start()
    lines = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lines.extend(rt.drain_queue())
        if len(_parse_events_from_bufs(lines)) >= min_events:
            break
        time.sleep(0.05)
    rt.stop()
    got = len(_parse_events_from_bufs(lines))
    assert got >= min_events, f"receiver drained {got}/{min_events} events within {timeout}s"
    return lines


def _parse_events_from_bufs(bufs):
    """Replicate the receiver's parse phase — decode FB buffers."""
    events = []
    for raw_buf in bufs:
        msg = message_to_dict(raw_buf)
        if msg.get("type") == MSG_RESYNC:
            events.clear()
            continue
        if msg.get("type") == MSG_EVENT:
            ev = msg.get("event", {})
            if ev:
                events.append(ev)
    return events


class TestStageFirstIntegration:
    """End-to-end: server → network → receiver → stage commit → adapter."""

    def test_full_pipeline_commit_then_adapter(self, server):
        """Events flow through server, commit to stage, then dispatch to adapter."""
        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/World", "Xform")
        adapter = MockAdapter()

        events = [
            {"k": K_ENSURE_PRIM, "prim": "/World/Cube", "typeName": "Xform"},
            {"k": K_ENSURE_XFORM_OPS, "prim": "/World/Cube"},
            {"k": K_SET_XFORM_TRS, "prim": "/World/Cube", "fields": ["t"], "t": [1.0, 2.0, 3.0]},
        ]

        # Emitter sends events to server
        _send_events_to_server(events)

        # Receiver collects from server
        lines = _receive_events(min_events=3)
        assert len(lines) > 0

        parsed = _parse_events_from_bufs(lines)
        assert len(parsed) == 3

        # Stage-first: commit to stage atomically
        with atomic_apply(stage):
            apply_events(stage, parsed)

        # Stage committed — dispatch to adapter
        for ev in parsed:
            adapter.apply_event(ev)

        # Verify both stage and adapter
        prim = stage.GetPrimAtPath("/World/Cube")
        assert prim.IsValid()
        xf = UsdGeom.Xformable(prim)
        ops = {op.GetAttr().GetName(): op for op in xf.GetOrderedXformOps()}
        t = ops["xformOp:translate"].Get()
        assert (t[0], t[1], t[2]) == (1.0, 2.0, 3.0)

        assert "/World/Cube" in adapter._prims
        assert adapter.get_trs("/World/Cube")["t"] == [1.0, 2.0, 3.0]

    def test_stage_failure_blocks_adapter(self, server):
        """If stage commit fails, adapter is never touched."""
        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/World", "Xform")
        adapter = MockAdapter()

        events = [
            {"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Xform"},
            {"k": K_ENSURE_PRIM, "prim": "/World/B", "typeName": "Xform"},
        ]

        _send_events_to_server(events)
        lines = _receive_events(min_events=2)
        parsed = _parse_events_from_bufs(lines)

        adapter_called = False
        with pytest.raises(RuntimeError):
            with atomic_apply(stage):
                apply_events(stage, parsed)
                raise RuntimeError("corrupt event data")

            # This should never run
            adapter_called = True

        assert not adapter_called
        assert not stage.GetPrimAtPath("/World/A").IsValid()
        assert not stage.GetPrimAtPath("/World/B").IsValid()
        assert "/World/A" not in adapter._prims

    def test_rollback_preserves_existing_state(self, server):
        """Failed batch doesn't corrupt pre-existing stage state."""
        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/World", "Xform")

        # Pre-existing state
        apply_events(
            stage,
            [
                {"k": K_ENSURE_PRIM, "prim": "/World/Old", "typeName": "Xform"},
                {"k": K_ENSURE_XFORM_OPS, "prim": "/World/Old"},
                {
                    "k": K_SET_XFORM_TRS,
                    "prim": "/World/Old",
                    "fields": ["t"],
                    "t": [10.0, 20.0, 30.0],
                },
            ],
        )

        events = [
            {"k": K_ENSURE_PRIM, "prim": "/World/New", "typeName": "Xform"},
        ]
        _send_events_to_server(events)
        lines = _receive_events()
        parsed = _parse_events_from_bufs(lines)

        with pytest.raises(RuntimeError):
            with atomic_apply(stage):
                apply_events(stage, parsed)
                # Verify New exists mid-transaction
                assert stage.GetPrimAtPath("/World/New").IsValid()
                raise RuntimeError("fail")

        # New rolled back, Old preserved
        assert not stage.GetPrimAtPath("/World/New").IsValid()
        assert stage.GetPrimAtPath("/World/Old").IsValid()
        xf = UsdGeom.Xformable(stage.GetPrimAtPath("/World/Old"))
        ops = {op.GetAttr().GetName(): op for op in xf.GetOrderedXformOps()}
        t = ops["xformOp:translate"].Get()
        assert (t[0], t[1], t[2]) == (10.0, 20.0, 30.0)
