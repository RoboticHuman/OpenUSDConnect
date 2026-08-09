"""Shared helpers for asset integration tests.

Each test script runs inside Blender (not pytest). These helpers provide
reusable setup, event sending, and assertion functions.

Usage in a test script:
    from helpers import TestHarness
    harness = TestHarness("BISHOP", server_port=7202)
    harness.setup()  # install addon, connect emitter + receiver
    harness.send_reference("/World/Bishop", BISHOP_ASSET, "/Bishop")
    harness.wait(5.0)
    harness.check_material("M_Bishop_B", path_contains="Bishop", min_nodes=5)
    harness.check_connection("M_Bishop_B", "Base Color", linked=True)
    harness.check_texture("M_Bishop_B", "diffuse2", loaded=True)
    harness.check_binding("Geom_Render", "M_Bishop_B")
    harness.done()
"""

import os
import socket as _socket
import sys

PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", ".."),
)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import bpy  # noqa: E402


def _parse_port():
    """Extract --port from Blender's -- passthrough args."""
    import sys as _sys
    args = _sys.argv
    if "--" in args:
        after = args[args.index("--") + 1:]
        for i, a in enumerate(after):
            if a == "--port" and i + 1 < len(after):
                return int(after[i + 1])
    return 7202  # default for manual runs


class TestHarness:
    """Lightweight test runner for Blender asset integration tests."""

    def __init__(self, label, server_host="127.0.0.1", server_port=None):
        self.label = label
        self.host = server_host
        self.port = server_port or _parse_port()
        self.base_usd = os.path.join(PROJECT_ROOT, "test_scene.usda")
        self.addon_zip = os.path.join(PROJECT_ROOT, "dist", "usd_connect_blender.zip")
        self.passed = 0
        self.failed = 0
        self.errors = []
        self._session_id = f"asset-test-{label}"
        self._txn_id = 0

    def log(self, msg):
        print(f"[{self.label}] {msg}", flush=True)

    def setup(self):
        """Install addon, connect emitter + receiver."""
        self.log("Installing addon...")
        bpy.ops.preferences.addon_install(filepath=self.addon_zip, overwrite=True)
        bpy.ops.preferences.addon_enable(module="usd_connect")

        scene = bpy.context.scene
        scene.usd_connect_base_usd_path = self.base_usd
        scene.usd_connect_auto_track = False
        scene.usd_connect_emit_host = self.host
        scene.usd_connect_emit_port = self.port
        try:
            bpy.ops.usd_connect.connect_emitter()
        except Exception as e:
            self.log(f"  Emitter: {e}")
        scene.usd_connect_recv_host = self.host
        scene.usd_connect_recv_port = self.port
        scene.usd_connect_recv_last_seq = 0
        try:
            bpy.ops.usd_connect.start_receiver()
        except Exception as e:
            self.log(f"  Receiver: {e}")
        self.log("Setup done.")

    def send_reference(self, prim_path, asset_path, prim_path_ref=""):
        """Send ensure_prim + set_reference events."""
        asset_path = os.path.abspath(asset_path).replace("\\", "/")
        self._send([
            {"k": "ensure_prim", "prim": prim_path, "typeName": "Xform"},
            {"k": "set_reference", "prim": prim_path,
             "refs": [{"asset_path": asset_path, "prim_path": prim_path_ref}]},
        ])
        self.log(f"Sent set_reference {prim_path} -> {os.path.basename(asset_path)}")

    def send_payload(self, prim_path, asset_path, prim_path_ref=""):
        """Send ensure_prim + set_payload + load_payload events."""
        asset_path = os.path.abspath(asset_path).replace("\\", "/")
        self._send([
            {"k": "ensure_prim", "prim": prim_path, "typeName": "Xform"},
            {"k": "ensure_xform_ops", "prim": prim_path},
            {"k": "set_xform_trs", "prim": prim_path,
             "fields": ["t", "r", "s"], "t": [0, 0, 0],
             "r": [1, 0, 0, 0], "s": [1, 1, 1]},
            {"k": "set_payload", "prim": prim_path,
             "payloads": [{"asset_path": asset_path, "prim_path": prim_path_ref}]},
            {"k": "load_payload", "prim": prim_path},
        ])
        self.log(f"Sent set_payload {prim_path} -> {os.path.basename(asset_path)}")

    def send_variant(self, prim_path, selections):
        """Send set_variant_selections event."""
        self._send([
            {"k": "set_variant_selections", "prim": prim_path,
             "selections": selections},
        ])
        self.log(f"Sent variant {prim_path} -> {selections}")

    # ------------------------------------------------------------------
    # Assertions
    # ------------------------------------------------------------------

    def check_material(self, name_contains, *, path_contains=None,
                       min_nodes=0, base_color_linked=None):
        """Assert a material exists with expected properties."""
        mat = self._find_material(name_contains)
        if not mat:
            self._fail(f"Material containing '{name_contains}' not found")
            return
        if path_contains:
            mp = mat.get("usd_material_path", "")
            if path_contains not in mp:
                self._fail(
                    f"{mat.name}: usd_material_path '{mp}' missing '{path_contains}'",
                )
                return
        if min_nodes > 0 and mat.node_tree:
            count = len(mat.node_tree.nodes)
            if count < min_nodes:
                self._fail(f"{mat.name}: {count} nodes < {min_nodes} expected")
                return
        if base_color_linked is not None and mat.node_tree:
            for n in mat.node_tree.nodes:
                if n.type == "BSDF_PRINCIPLED" and "Base Color" in n.inputs:
                    actual = n.inputs["Base Color"].is_linked
                    if actual != base_color_linked:
                        self._fail(
                            f"{mat.name}: Base Color linked={actual}, expected {base_color_linked}",
                        )
                        return
                    break
        self._pass(f"Material '{mat.name}' OK")

    def check_texture(self, mat_name_contains, node_name_contains, *, loaded=True):
        """Assert a texture node exists with image loaded."""
        mat = self._find_material(mat_name_contains)
        if not mat or not mat.node_tree:
            self._fail(f"Material '{mat_name_contains}' not found for texture check")
            return
        for n in mat.node_tree.nodes:
            if n.type == "TEX_IMAGE" and node_name_contains in n.name:
                has_image = n.image is not None
                if loaded and not has_image:
                    self._fail(f"{mat.name}/{n.name}: image NOT loaded")
                    return
                if loaded and has_image:
                    w, h = n.image.size
                    if w == 0 or h == 0:
                        self._fail(f"{mat.name}/{n.name}: image size=({w},{h})")
                        return
                status = "loaded" if has_image else "none"
                self._pass(f"Texture '{n.name}' in '{mat.name}': image={status}")
                return
        self._fail(f"No TEX_IMAGE node containing '{node_name_contains}' in '{mat_name_contains}'")

    def check_connection(self, mat_name_contains, input_name, *,
                         linked=True, from_type=None):
        """Assert a BSDF input is connected (optionally check source type)."""
        mat = self._find_material(mat_name_contains)
        if not mat or not mat.node_tree:
            self._fail(f"Material '{mat_name_contains}' not found for connection check")
            return
        for n in mat.node_tree.nodes:
            if n.type == "BSDF_PRINCIPLED" and input_name in n.inputs:
                inp = n.inputs[input_name]
                if inp.is_linked != linked:
                    self._fail(
                        f"{mat.name}.{input_name}: linked={inp.is_linked}, expected {linked}",
                    )
                    return
                if from_type and inp.is_linked:
                    src = inp.links[0].from_node
                    if src.type != from_type:
                        self._fail(
                            f"{mat.name}.{input_name}: from {src.type}, expected {from_type}",
                        )
                        return
                self._pass(f"{mat.name}.{input_name}: linked={inp.is_linked}")
                return
        self._fail(f"No BSDF with '{input_name}' in '{mat_name_contains}'")

    def check_binding(self, obj_name_contains, mat_name_contains):
        """Assert an object has a specific material assigned."""
        for obj in bpy.data.objects:
            if obj_name_contains not in obj.name:
                continue
            if not obj.data or not hasattr(obj.data, "materials"):
                continue
            mats = [m.name for m in obj.data.materials if m]
            for m in mats:
                if mat_name_contains in m:
                    self._pass(f"{obj.name} -> {m}")
                    return
            self._fail(f"{obj.name} has {mats}, expected '{mat_name_contains}'")
            return
        self._fail(f"No object containing '{obj_name_contains}'")

    def check_shader_maps_seeded(self, path_contains):
        """Assert shader input maps were seeded for a path."""
        from usd_connect.capture import _state
        if not _state.author:
            self._fail("Author is None — emitter not running")
            return
        maps = [k for k in _state.author._shader_input_maps if path_contains in k]
        if maps:
            self._pass(f"Shader maps seeded: {len(maps)} entries for '{path_contains}'")
        else:
            self._fail(f"No shader input maps for '{path_contains}'")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def done(self):
        """Print summary and quit Blender."""
        total = self.passed + self.failed
        self.log(f"{'=' * 40}")
        self.log(f"Results: {self.passed}/{total} passed, {self.failed} failed")
        if self.errors:
            for err in self.errors:
                self.log(f"  FAIL: {err}")
        if self.failed == 0:
            self.log("SUCCESS")
        else:
            self.log("FAILED")
        self.log(f"{'=' * 40}")
        bpy.ops.wm.quit_blender()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _send(self, events):
        from openusdconnect.protocol import make_hello
        from openusdconnect.transport import send_line
        s = _socket.create_connection((self.host, self.port), timeout=5)
        send_line(
            s,
            make_hello(
                "emitter",
                client_id="asset_test",
                producer_session_id=self._session_id,
            ),
        )
        # Read hello_ok before sending txn: the server won't process
        # further messages until hello_ok is sent.
        s.settimeout(5)
        s.recv(4096)
        self._txn_id += 1
        send_line(s, {
            "type": "txn", "events": events, "txn_id": self._txn_id,
        })
        # Ensure the server fully reads and processes the txn before this
        # connection tears down. A bare close() can RST the socket and drop
        # the unprocessed txn; half-closing then draining to EOF waits for the
        # server to consume it and close its end, which also serializes
        # teardown so the next same-client_id connect does not race this one.
        s.shutdown(_socket.SHUT_WR)
        try:
            while s.recv(4096):
                pass
        except OSError:
            pass
        s.close()

    def _find_material(self, name_contains):
        for m in bpy.data.materials:
            if name_contains in m.name:
                return m
        return None

    def _pass(self, msg):
        self.passed += 1
        self.log(f"  PASS: {msg}")

    def _fail(self, msg):
        self.failed += 1
        self.errors.append(msg)
        self.log(f"  FAIL: {msg}")
