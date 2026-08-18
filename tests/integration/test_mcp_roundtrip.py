"""E2E: the MCP session authors over real TCP; mirror + other clients reflect it.

Exercises the full networked path (EventSender -> server -> ReceiverThread ->
UsdStageAdapter mirror), the read-after-write drain, ancestor auto-create, and
fan-out to an independent client. Headless, no DCC.
"""

import socket
import time

import pytest
from pxr import Usd, UsdGeom

from integrations.mcp import discovery, introspection
from integrations.mcp.config import McpConfig
from integrations.mcp.session import ConnectionSession
from integrations.mcp.validation import validate_and_prepare
from openusdconnect.adapters import UsdStageAdapter
from openusdconnect.dispatcher import EventDispatcher
from openusdconnect.receiver import ReceiverThread
from openusdconnect.sender import EventSender
from tests.helpers import start_server, stop_server


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
