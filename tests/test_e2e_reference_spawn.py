"""End-to-end test: server + emitter/receiver for set_reference events.

Verifies the full pipeline: emitter sends ensure_prim + set_reference,
receiver gets the events, and applying them to a USD stage produces a
valid reference arc with the expected composed prim hierarchy.

No Blender required — uses UsdStageAdapter for USD-level verification.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time

import pytest

try:
    from pxr import Usd, UsdGeom, Gf, Sdf
    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")

from openusdconnect.protocol import make_hello, make_txn, make_quit
from openusdconnect.transport import send_line
from openusdconnect.receiver import ReceiverThread
from openusdconnect.adapters import UsdStageAdapter
from openusdconnect.event_apply import apply_events


def _free_port():
    """Find a free TCP port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host, port, timeout=10):
    """Wait until a TCP port accepts connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    raise TimeoutError(f"Port {port} not ready after {timeout}s")


def _create_test_assets(tmp_dir):
    """Create base scene and reference asset USD files.

    Returns (base_path, asset_path).
    """
    # Base scene with /World Xform
    base_path = os.path.join(tmp_dir, "base_scene.usda")
    base_stage = Usd.Stage.CreateNew(base_path)
    UsdGeom.SetStageUpAxis(base_stage, UsdGeom.Tokens.y)
    UsdGeom.Xform.Define(base_stage, "/World")
    base_stage.Save()

    # Asset with a Cube mesh prim
    asset_path = os.path.join(tmp_dir, "asset.usda")
    asset_stage = Usd.Stage.CreateNew(asset_path)
    UsdGeom.SetStageUpAxis(asset_stage, UsdGeom.Tokens.y)
    UsdGeom.Xform.Define(asset_stage, "/Model")
    cube = UsdGeom.Cube.Define(asset_stage, "/Model/Geom")
    cube.GetSizeAttr().Set(2.0)
    asset_stage.Save()

    return base_path, asset_path


class TestE2EReferenceSpawn:
    """End-to-end test: server + TCP clients for reference spawning."""

    def test_reference_through_server(self):
        """Emitter sends ensure_prim + set_reference through server; receiver gets them."""
        port = _free_port()
        tmp_dir = tempfile.mkdtemp()
        base_path, asset_path = _create_test_assets(tmp_dir)

        # Start server with a temp log to avoid replaying old events
        log_path = os.path.join(tmp_dir, "events.db")
        server_proc = subprocess.Popen(
            [sys.executable, "-m", "openusdconnect.server",
             "--port", str(port), "--base", base_path, "--log", log_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            _wait_for_port("127.0.0.1", port)

            # Start receiver
            receiver = ReceiverThread(host="127.0.0.1", port=port, sync_from=1)
            receiver.start()
            time.sleep(0.3)  # let receiver connect

            # Emitter: connect and send events
            emitter_sock = socket.create_connection(("127.0.0.1", port))
            send_line(emitter_sock, make_hello("emitter"))
            time.sleep(0.1)

            events = [
                {"k": "ensure_prim", "prim": "/World/Chair", "typeName": "Xform"},
                {"k": "set_reference", "prim": "/World/Chair",
                 "asset_path": asset_path, "prim_path": "/Model"},
            ]
            send_line(emitter_sock, make_txn("test-emitter", events))
            time.sleep(0.5)

            # Drain receiver queue
            lines = receiver.drain_queue()

            # Parse received events
            received_events = []
            for raw_line in lines:
                msg = json.loads(raw_line)
                if msg.get("type") == "event":
                    received_events.append(msg["event"])

            # Verify we received both events
            event_kinds = [e["k"] for e in received_events]
            assert "ensure_prim" in event_kinds, f"Missing ensure_prim in {event_kinds}"
            assert "set_reference" in event_kinds, f"Missing set_reference in {event_kinds}"

            # Verify set_reference payload
            ref_ev = [e for e in received_events if e["k"] == "set_reference"][0]
            assert ref_ev["prim"] == "/World/Chair"
            assert ref_ev["asset_path"] == asset_path
            assert ref_ev.get("prim_path") == "/Model"

            # Apply events to a fresh USD stage and verify reference resolves
            verify_stage = Usd.Stage.CreateInMemory()
            verify_stage.DefinePrim("/World", "Xform")
            apply_events(verify_stage, received_events)

            chair = verify_stage.GetPrimAtPath("/World/Chair")
            assert chair.IsValid(), "Chair prim not created"
            assert chair.HasAuthoredReferences(), "Chair prim has no references"

            # Verify composed hierarchy: /Model/Geom should appear as /World/Chair/Geom
            geom = verify_stage.GetPrimAtPath("/World/Chair/Geom")
            assert geom.IsValid(), "Composed child prim /World/Chair/Geom not found"
            assert geom.GetTypeName() == "Cube", f"Expected Cube, got {geom.GetTypeName()}"

            # Cleanup
            send_line(emitter_sock, make_quit())
            emitter_sock.close()
            receiver.stop()
            receiver.join(timeout=2)

        finally:
            server_proc.terminate()
            server_proc.wait(timeout=5)

    def test_reference_applied_to_usd_stage_adapter(self):
        """UsdStageAdapter.set_reference creates valid reference arcs."""
        tmp_dir = tempfile.mkdtemp()
        _, asset_path = _create_test_assets(tmp_dir)

        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/World", "Xform")
        adapter = UsdStageAdapter(stage)

        adapter.ensure_prim("/World/Furniture", "Xform")
        adapter.set_reference("/World/Furniture", asset_path, "/Model")

        furniture = stage.GetPrimAtPath("/World/Furniture")
        assert furniture.IsValid()
        assert furniture.HasAuthoredReferences()

        # Verify composed child
        geom = stage.GetPrimAtPath("/World/Furniture/Geom")
        assert geom.IsValid()
        assert geom.GetTypeName() == "Cube"

    def test_reference_without_prim_path_ref(self):
        """set_reference without prim_path_ref references the whole asset."""
        tmp_dir = tempfile.mkdtemp()
        _, asset_path = _create_test_assets(tmp_dir)

        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/World", "Xform")
        adapter = UsdStageAdapter(stage)

        adapter.ensure_prim("/World/FullAsset", "Xform")
        adapter.set_reference("/World/FullAsset", asset_path)

        prim = stage.GetPrimAtPath("/World/FullAsset")
        assert prim.IsValid()
        assert prim.HasAuthoredReferences()
