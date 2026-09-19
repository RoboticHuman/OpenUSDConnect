"""E2E: the MCP session authors over real TCP; mirror + other clients reflect it.

Exercises the full networked path (EventSender -> server -> ReceiverThread ->
UsdStageAdapter mirror), the read-after-write drain, ancestor auto-create, and
fan-out to an independent client. Headless, no DCC.
"""

import socket
import threading
import time

import pytest
from pxr import Usd, UsdGeom

from integrations.mcp import discovery, introspection
from integrations.mcp.config import McpConfig
from integrations.mcp.errors import ToolError
from integrations.mcp.session import ConnectionSession
from integrations.mcp.validation import validate_and_prepare
from openusdconnect.adapters import UsdStageAdapter
from openusdconnect.dispatcher import EventDispatcher
from openusdconnect.receiver import ReceiverThread
from openusdconnect.sender import EventSender
from tests.helpers import start_server, stop_server


def test_foreign_commit_cannot_confirm_blocked_own_commit(tmp_path, monkeypatch):
    from openusdconnect.server import UsdSyncServer
    from openusdconnect.server.connection import ConnectionHandler, ThreadedTCPServer

    state = UsdSyncServer(log_path=str(tmp_path / "checkpoint.db"))
    tcp = ThreadedTCPServer(("127.0.0.1", 0), ConnectionHandler, state, max_workers=8)
    serving = threading.Thread(target=tcp.serve_forever, daemon=True)
    serving.start()
    session = ConnectionSession(McpConfig(port=tcp.server_address[1], client_id="owner"))
    foreign = EventSender("127.0.0.1", tcp.server_address[1], client_id="foreign")
    observed_foreign = threading.Event()
    release_own = threading.Event()
    apply = state._apply_validated_txn

    def gated_apply(events, *args, **kwargs):
        if any(event.get("prim") == "/Own" for event in events):
            assert release_own.wait(5)
        return apply(events, *args, **kwargs)

    monkeypatch.setattr(state, "_apply_validated_txn", gated_apply)

    def release_after_foreign():
        if observed_foreign.wait(5):
            time.sleep(0.05)
        release_own.set()

    release_thread = threading.Thread(target=release_after_foreign, daemon=True)
    try:
        session.connect()
        assert foreign.connect()
        send = session.sender.send_events

        def send_after_foreign(events):
            assert foreign.send_events([
                {"k": "ensure_prim", "prim": "/Foreign", "typeName": "Xform"},
            ])
            assert foreign.flush(5)
            return send(events)

        drain = session.receiver.update

        def observe():
            result = drain()
            if session.mirror_stage.GetPrimAtPath("/Foreign"):
                observed_foreign.set()
            return result

        monkeypatch.setattr(session.sender, "send_events", send_after_foreign)
        monkeypatch.setattr(session.receiver, "update", observe)
        release_thread.start()
        result = session.send([{"k": "ensure_prim", "prim": "/Own", "typeName": "Xform"}])
        assert result["mirror_synced"]
        assert observed_foreign.is_set()
        assert session.mirror_stage.GetPrimAtPath("/Own")
        # Cache-only invalidation must not strand the receiver in an old sequence domain.
        state.bump_snapshot_epoch("test-cache-invalidation")
        assert session.send([
            {"k": "ensure_prim", "prim": "/AfterCache", "typeName": "Xform"},
        ])["mirror_synced"]
        for reset, path in [(state.compact_log, "/AfterCompact"), (state.purge, "/AfterPurge")]:
            reset()
            assert session.send([
                {"k": "ensure_prim", "prim": path, "typeName": "Xform"},
            ])["mirror_synced"]
            assert session.mirror_stage.GetPrimAtPath(path)
    finally:
        release_own.set()
        if release_thread.ident is not None:
            release_thread.join(6)
        session.disconnect()
        foreign.disconnect()
        tcp.shutdown()
        tcp.server_close()
        serving.join(5)
        state.shutdown()
        state.store.close()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def server(tmp_path):
    port = _free_port()
    proc = start_server(tmp_path, port)
    yield port
    stop_server(proc)


def _connect(port):
    session = ConnectionSession(McpConfig(port=port, client_id="mcp-test"))
    session.connect()
    return session


def _author(session, events):
    """Mirror what the usd_send_events tool does (validate + send)."""
    prepared, warnings = validate_and_prepare(
        events,
        stage=session.mirror_stage,
        auto_create_ancestors=True,
        node_exists=discovery.node_exists,
    )
    result = session.send(prepared)
    return result, prepared


def _drain_other(disp, target_seq, timeout=5.0):
    deadline = time.monotonic() + timeout
    while disp.last_seq < target_seq and time.monotonic() < deadline:
        if disp.drain_and_apply() == 0:
            time.sleep(0.02)


def test_reconnect_and_disconnect_join_mirror_threads(server):
    session = _connect(server)
    first = session.receiver.receiver
    try:
        assert first.is_alive()
        session.sender.disconnect()
        session.connect()
        second = session.receiver.receiver
        assert second is not first
        assert not first.is_alive()
        assert second.is_alive()
        session.disconnect()
        assert not second.is_alive()
        session.disconnect()
    finally:
        session.disconnect()


def test_mesh_roundtrip_and_fanout(server):
    session = _connect(server)
    other = None
    try:
        other_stage = Usd.Stage.CreateInMemory()
        other = ReceiverThread(
            host="127.0.0.1", port=server, sync_from=1, client_id="other", origin="other-recv"
        )
        other.start()
        other_disp = EventDispatcher(receiver=other, adapter=UsdStageAdapter(other_stage))

        events = [
            {"k": "ensure_prim", "prim": "/World", "typeName": "Xform"},
            {"k": "ensure_prim", "prim": "/World/Mesh", "typeName": "Mesh"},
            {
                "k": "set_gprim_attrs",
                "prim": "/World/Mesh",
                "attrs": {
                    "points": [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]],
                    "faceVertexCounts": [4],
                    "faceVertexIndices": [0, 1, 2, 3],
                },
            },
            {"k": "ensure_xform_ops", "prim": "/World/Mesh"},
            {"k": "set_xform_trs", "prim": "/World/Mesh", "fields": ["t"], "t": [5, 0, 0]},
        ]
        result, _ = _author(session, events)
        assert result["sent"] and result["mirror_synced"]

        # MCP mirror reflects the write
        m = session.mirror_stage.GetPrimAtPath("/World/Mesh")
        assert m.IsValid() and m.GetTypeName() == "Mesh"
        info = introspection.get_prim(session.mirror_stage, "/World/Mesh")
        assert info["xform"]["t"] == pytest.approx([5.0, 0.0, 0.0])

        # Independent client receives the same scene (server fan-out)
        _drain_other(other_disp, result["last_seq"])
        om = other_stage.GetPrimAtPath("/World/Mesh")
        assert om.IsValid() and om.GetTypeName() == "Mesh"
        assert UsdGeom.Mesh(om).GetPointsAttr().Get() is not None
    finally:
        if other is not None:
            other.stop()
        session.disconnect()


def test_ancestors_auto_created_over_the_wire(server):
    session = _connect(server)
    try:
        result, prepared = _author(
            session, [{"k": "ensure_prim", "prim": "/World/Group/Leaf", "typeName": "Sphere"}]
        )
        assert result["mirror_synced"]
        # validation prepended /World and /World/Group
        assert [e["prim"] for e in prepared] == ["/World", "/World/Group", "/World/Group/Leaf"]
        for path in ("/World", "/World/Group", "/World/Group/Leaf"):
            assert session.mirror_stage.GetPrimAtPath(path).IsValid()
    finally:
        session.disconnect()


def test_connect_returns_with_existing_replay_applied(server):
    author = _connect(server)
    try:
        result, _prepared = _author(
            author,
            [{"k": "ensure_prim", "prim": "/World/Existing", "typeName": "Xform"}],
        )
        assert result["mirror_synced"]
    finally:
        author.disconnect()

    reader = _connect(server)
    try:
        assert reader.status()["mirror_synchronized"] is True
        assert reader.mirror_stage.GetPrimAtPath("/World/Existing").IsValid()
    finally:
        reader.disconnect()


def test_send_rejects_while_initial_replay_is_incomplete(server):
    author = ConnectionSession(
        McpConfig(port=server, client_id="replay-author", read_after_write_timeout_s=10.0)
    )
    try:
        author.connect()
        events = [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}]
        events.extend(
            {"k": "ensure_prim", "prim": f"/World/P{index}", "typeName": "Xform"}
            for index in range(2_000)
        )
        assert author.send(events)["mirror_synced"]
    finally:
        author.disconnect()

    reader = ConnectionSession(
        McpConfig(port=server, client_id="replay-reader", read_after_write_timeout_s=1e-9)
    )
    try:
        status = reader.connect()
        assert status["mirror_synchronized"] is False

        # Keep send() from winning a race with the remaining replay. The
        # behavior under test is the unsynchronized guard, not replay speed.
        reader.config.read_after_write_timeout_s = 1e-9

        with pytest.raises(ToolError) as error:
            reader.send([{"k": "ensure_prim", "prim": "/World/TooSoon", "typeName": "Xform"}])

        assert error.value.code == "mirror_not_ready"
    finally:
        reader.disconnect()


def test_changes_since_tracks_own_and_foreign_edits(server):
    session = _connect(server)
    emitter = None
    try:
        result, _ = _author(
            session,
            [
                {"k": "ensure_prim", "prim": "/World", "typeName": "Xform"},
                {"k": "ensure_prim", "prim": "/World/A", "typeName": "Sphere"},
            ],
        )
        assert result["mirror_synced"]

        # own edits surface in the diff
        first = session.changes_since(0)
        assert "/World/A" in {c["prim"] for c in first["changes"]}
        cursor = first["last_seq"]
        # nothing new past the cursor
        assert session.changes_since(cursor)["count"] == 0

        # a foreign client's edit appears, scoped to just the new prim
        emitter = EventSender("127.0.0.1", server, client_id="foreign", origin="foreign-emit")
        assert emitter.connect()
        emitter.send_events([{"k": "ensure_prim", "prim": "/World/B", "typeName": "Cube"}])

        deadline = time.monotonic() + 5.0
        new_paths: set = set()
        while time.monotonic() < deadline:
            new_paths = {c["prim"] for c in session.changes_since(cursor)["changes"]}
            if "/World/B" in new_paths:
                break
            time.sleep(0.05)
        assert "/World/B" in new_paths
        assert "/World/A" not in new_paths  # diff is scoped past the cursor
    finally:
        if emitter is not None:
            emitter.disconnect()
        session.disconnect()


def test_playback_status_observes_claim_and_control(server):
    session = _connect(server)
    try:
        session.claim_playback()
        deadline = time.monotonic() + 5.0
        st = {}
        while time.monotonic() < deadline:
            st = session.playback_status()
            if st.get("is_leader"):
                break
            time.sleep(0.05)
        assert st.get("observed") is True
        assert st.get("is_leader") is True
        assert st.get("leader_client_id") == "mcp-test"

        session.playback_control("set_time", time_code=9.0)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            st = session.playback_status()
            if st.get("time") == pytest.approx(9.0):
                break
            time.sleep(0.05)
        assert st.get("time") == pytest.approx(9.0)
    finally:
        session.disconnect()


def test_foreign_edit_visible_to_mcp(server):
    session = _connect(server)
    emitter = EventSender("127.0.0.1", server, client_id="foreign", origin="foreign-emit")
    assert emitter.connect()
    try:
        emitter.send_events(
            [
                {"k": "ensure_prim", "prim": "/World", "typeName": "Xform"},
                {"k": "ensure_prim", "prim": "/World/Foreign", "typeName": "Sphere"},
            ]
        )
        emitter.disconnect()

        deadline = time.monotonic() + 5.0
        seen = False
        while time.monotonic() < deadline:
            stage = session.require_mirror()  # pumps the dispatcher
            if stage.GetPrimAtPath("/World/Foreign").IsValid():
                seen = True
                break
            time.sleep(0.05)
        assert seen
        listing = introspection.list_prims(session.mirror_stage)
        assert any(p["path"] == "/World/Foreign" for p in listing["prims"])
    finally:
        session.disconnect()
