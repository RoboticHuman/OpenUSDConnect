"""Asset integration tests — pytest wrappers that launch Blender.

Each test starts a server, runs a Blender script, and checks for SUCCESS
in the output. Skipped by default; enable with --asset-tests flag.

Usage:
    uv run pytest tests/integration/asset_tests/ --asset-tests -v
"""

import os
import subprocess
import sys
import time

import pytest

from tests.helpers import run_blender, start_server, stop_server

SCRIPTS_DIR = os.path.dirname(__file__)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPTS_DIR)))


@pytest.fixture(scope="session", autouse=True)
def _build_addon():
    """Rebuild the addon zip before running asset tests."""
    result = subprocess.run(
        [sys.executable, "scripts/build_blender_addon.py"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Addon build failed:\n{result.stderr}"


def _run_asset_test(blender_exe, tmp_path, script_name, port, timeout=90):
    """Run an asset test script in Blender and assert SUCCESS."""
    script = os.path.join(SCRIPTS_DIR, script_name)
    server = start_server(tmp_path, port)
    try:
        r = run_blender(blender_exe, script, port, timeout=timeout,
                        background=False)
        print(f"\n=== {script_name} stdout ===")
        print(r.stdout[-2000:] if len(r.stdout) > 2000 else r.stdout)
        if r.stderr:
            print("=== stderr ===")
            print(r.stderr[-500:])
        assert "SUCCESS" in r.stdout, (
            f"{script_name} did not print SUCCESS.\n"
            f"Last output: {r.stdout[-500:]}"
        )
    finally:
        stop_server(server)


def test_bishop_materialx(blender_exe, tmp_path):
    """Bishop: MaterialX multi-node network with texture connections."""
    _run_asset_test(blender_exe, tmp_path, "test_bishop.py", 7210)


def test_remote_reference_descendant_edit(blender_exe, tmp_path):
    """A local edit on remotely referenced geometry remains an Sdf override."""
    _run_asset_test(
        blender_exe,
        tmp_path,
        "test_remote_descendant_edit.py",
        7219,
    )


def test_teapot_variants(blender_exe, tmp_path):
    """Teapot: variant switching with interleaved live editing."""
    _run_asset_test(blender_exe, tmp_path, "test_teapot_variants.py", 7211,
                    timeout=120)


def test_two_teapots_identity(blender_exe, tmp_path):
    """Two Teapots: path-based material identity separation."""
    _run_asset_test(blender_exe, tmp_path, "test_two_teapots.py", 7212)


def test_vehicles_multi_binding(blender_exe, tmp_path):
    """Vehicles 4WD: multiple material bindings per asset."""
    _run_asset_test(blender_exe, tmp_path, "test_vehicles.py", 7213)


def test_camera_scene(blender_exe, tmp_path):
    """Camera scene: UsdGeomCamera replicates as a Blender CAMERA object
    and can be promoted to the active scene camera."""
    _run_asset_test(blender_exe, tmp_path, "test_camera_scene.py", 7214)


def test_headless_time_samples_to_blender(blender_exe, tmp_path):
    """Headless EventSender emits time-sampled SetXformTrs events; Blender
    receives them via the addon. Verifies the protocol layer is animation-
    aware end-to-end (events round-trip via the server's event log, the
    receiver-side mirror USD stage gets the time samples) and documents
    the Q1 gap (BlenderAdapter ignores the ``time`` field and the sphere
    ends at the latest-sample's static pose with no F-curves).
    """
    port = 7216
    observer_script = os.path.join(
        SCRIPTS_DIR, "test_headless_time_samples_to_blender.py",
    )
    server = start_server(tmp_path, port)
    try:
        # 1) Send time-sampled events BEFORE Blender connects.
        #    They land in the event log; Blender's receiver replays
        #    them from seq=1 on connect — no startup race.
        from openusdconnect.protocol_constants import (
            K_ENSURE_PRIM,
            K_ENSURE_XFORM_OPS,
            K_SET_XFORM_TRS,
        )
        from openusdconnect.sender import EventSender

        sender = EventSender(
            host="127.0.0.1",
            port=port,
            client_id="headless-emit",
            role="emitter",
            origin="headless-origin",
        )
        assert sender.connect(), "Headless EventSender failed to connect"
        try:
            events = [
                {"k": K_ENSURE_PRIM, "prim": "/World", "typeName": "Xform"},
                {"k": K_ENSURE_PRIM, "prim": "/World/AnimSphere",
                 "typeName": "Sphere"},
                {"k": K_ENSURE_XFORM_OPS, "prim": "/World/AnimSphere"},
                # Time-sampled translate keyframes — these go to USD as
                # xformOp:translate.Set(value, Usd.TimeCode(t))
                {"k": K_SET_XFORM_TRS, "prim": "/World/AnimSphere",
                 "fields": ["t"], "t": [0.0, 0.0, 0.0], "time": 1.0},
                {"k": K_SET_XFORM_TRS, "prim": "/World/AnimSphere",
                 "fields": ["t"], "t": [10.0, 0.0, 0.0], "time": 12.0},
                {"k": K_SET_XFORM_TRS, "prim": "/World/AnimSphere",
                 "fields": ["t"], "t": [20.0, 0.0, 0.0], "time": 24.0},
            ]
            ok = sender.send_events(events)
            assert ok, "send_events returned False"
        finally:
            sender.disconnect()

        # 2) Now start Blender — it replays the log on receiver connect.
        r = run_blender(
            blender_exe, observer_script, port, timeout=120, background=False,
        )
        print("\n=== Observer Blender stdout ===")
        print(r.stdout[-3000:] if len(r.stdout) > 3000 else r.stdout)
        if r.stderr:
            print("=== stderr ===")
            print(r.stderr[-500:])
        assert "SUCCESS" in r.stdout, (
            f"Observer Blender did not print SUCCESS.\n"
            f"Last output: {r.stdout[-1000:]}"
        )
    finally:
        stop_server(server)


def test_chair_backlog_replay(blender_exe, tmp_path):
    """Chair material inheritance after a SINGLE full backlog replay
    (sync_from=1), with no second resync.

    The scene is authored to the server BEFORE Blender connects, with the wood
    material bound only on the parent Xform and the binding authored before the
    child cubes exist. The receiver replays it all in one full sync; the native
    child cubes must inherit the wood material after that single replay.
    """
    from openusdconnect.protocol_constants import (
        K_ENSURE_PRIM,
        K_ENSURE_XFORM_OPS,
        K_SET_CONNECTABLE_CONNECTION,
        K_SET_CONNECTABLE_INPUT,
        K_SET_MATERIAL_BINDING,
        K_SET_XFORM_TRS,
    )
    from openusdconnect.sender import EventSender

    port = 7217
    server = start_server(tmp_path, port)
    try:
        sender = EventSender(
            host="127.0.0.1", port=port, client_id="chair-emit",
            role="emitter", origin="chair-origin",
        )
        assert sender.connect(), "chair EventSender failed to connect"
        try:
            def _cube(p, t, s):
                return [
                    {"k": K_ENSURE_PRIM, "prim": p, "typeName": "Cube"},
                    {"k": K_ENSURE_XFORM_OPS, "prim": p},
                    {"k": K_SET_XFORM_TRS, "prim": p, "fields": ["t", "s"], "t": t, "s": s},
                ]

            wood_network = [
                {"k": K_ENSURE_PRIM, "prim": "/World/Looks", "typeName": "Scope"},
                {"k": K_ENSURE_PRIM, "prim": "/World/Looks/Wood", "typeName": "Material"},
                {"k": K_ENSURE_PRIM, "prim": "/World/Looks/Wood/Surface", "typeName": "Shader"},
                {
                    "k": K_SET_CONNECTABLE_INPUT,
                    "prim": "/World/Looks/Wood/Surface",
                    "info_id": "UsdPreviewSurface",
                    "inputs": {
                        "diffuseColor": [0.4, 0.26, 0.13],
                        "metallic": 0.0,
                        "roughness": 0.6,
                    },
                    "input_types": {
                        "diffuseColor": "color3f",
                        "metallic": "float",
                        "roughness": "float",
                    },
                },
                {"k": K_SET_CONNECTABLE_CONNECTION, "prim": "/World/Looks/Wood",
                 "connections": {"outputs:surface": {
                     "source_prim": "/World/Looks/Wood/Surface", "source_attr": "outputs:surface"}},
                 "disconnections": []},
            ]
            # The parent-Xform binding is authored BEFORE the child cubes, so at
            # binding-apply time the chair Empty has no children to propagate to.
            # The cubes must still inherit the material once they are created.
            events = [
                {"k": K_ENSURE_PRIM, "prim": "/World", "typeName": "Xform"},
                {"k": K_ENSURE_PRIM, "prim": "/World/Chair", "typeName": "Xform"},
                {"k": K_ENSURE_XFORM_OPS, "prim": "/World/Chair"},
                {
                    "k": K_SET_XFORM_TRS,
                    "prim": "/World/Chair",
                    "fields": ["t"],
                    "t": [0.0, 0.0, 0.0],
                },
                *wood_network,
                {"k": K_SET_MATERIAL_BINDING, "prim": "/World/Chair",
                 "material_path": "/World/Looks/Wood"},
                *_cube("/World/Chair/Seat", [0.0, 0.42, 0.0], [0.46, 0.06, 0.46]),
                *_cube("/World/Chair/LegFL", [-0.4, 0.2, -0.4], [0.05, 0.4, 0.05]),
                *_cube("/World/Chair/Backrest", [0.0, 0.9, -0.4], [0.46, 0.4, 0.05]),
            ]
            assert sender.send_events(events), "send_events returned False"
        finally:
            sender.disconnect()

        r = run_blender(blender_exe, os.path.join(SCRIPTS_DIR, "test_chair_replay.py"),
                        port, timeout=90, background=False)
        print("\n=== test_chair_replay.py stdout ===")
        print(r.stdout[-2000:] if len(r.stdout) > 2000 else r.stdout)
        if r.stderr:
            print("=== stderr ===")
            print(r.stderr[-500:])
        assert "SUCCESS" in r.stdout, (
            f"chair backlog replay did not print SUCCESS.\nLast output: {r.stdout[-800:]}"
        )
    finally:
        stop_server(server)


def _verify_material_zoo_stage_receiver(base_path, port, event_count):
    """Exercise the network/stage path used by the usdview integration."""
    from pxr import Usd, UsdShade

    from openusdconnect.adapters import UsdStageAdapter
    from openusdconnect.dispatcher import EventDispatcher
    from openusdconnect.receiver import ReceiverThread

    stage = Usd.Stage.Open(base_path)
    assert stage is not None, f"Could not open Material Zoo base stage: {base_path}"
    stage.SetEditTarget(stage.GetSessionLayer())
    receiver = ReceiverThread(
        host="127.0.0.1",
        port=port,
        sync_from=1,
        reconnect=False,
        client_id="material-zoo-stage-receiver",
        origin="material-zoo-stage-receiver",
        layered_replay=True,
    )
    receiver.start()
    dispatcher = EventDispatcher(receiver=receiver, adapter=UsdStageAdapter(stage))
    try:
        deadline = time.monotonic() + 30.0
        while dispatcher.last_seq < event_count and time.monotonic() < deadline:
            dispatcher.drain_and_apply()
            time.sleep(0.02)

        assert dispatcher.last_seq >= event_count, (
            f"stage receiver stopped at sequence {dispatcher.last_seq}/{event_count}"
        )
        key_paths = (
            "/World/Chair/Seat",
            "/World/WoodBall2",
            "/World/OpenPBRTest",
            "/World/Bishop",
            "/World/Looks/Wood",
            "/World/Looks/WoodUV",
        )
        missing = [path for path in key_paths if not stage.GetPrimAtPath(path)]
        assert not missing, f"stage receiver is missing Material Zoo prims: {missing}"

        def _bound_material(prim_path):
            material, _relationship = UsdShade.MaterialBindingAPI(
                stage.GetPrimAtPath(prim_path)
            ).ComputeBoundMaterial()
            return str(material.GetPath()) if material else ""

        assert _bound_material("/World/Chair/Seat") == "/World/Looks/Wood"
        assert _bound_material("/World/WoodBall2") == "/World/Looks/WoodUV"
        sphere_translate = stage.GetPrimAtPath("/World/Sphere").GetAttribute(
            "xformOp:translate"
        ).Get()
        assert tuple(sphere_translate) == (0.0, 1.5, 0.0)
    finally:
        receiver.stop()
        receiver.join(timeout=2.0)
        dispatcher.close()


def test_material_zoo_backlog_replay(blender_exe, tmp_path, free_port):
    """The committed Material Zoo fixture replays onto a base-scene Blender.

    This is the network/DCC companion to tests/visual/test_material_zoo.py:
    the same semantic event fixture is first sequenced by a real server, then
    consumed from seq=1 by the packaged Blender addon after it imports only
    test_scene.usda.
    """
    from integrations.visualtest.replay import load_events
    from openusdconnect.sender import EventSender

    port = free_port
    base_path = os.path.join(PROJECT_ROOT, "test_scene.usda")
    fixture_path = os.path.join(
        PROJECT_ROOT,
        "tests",
        "visual",
        "fixtures",
        "material_zoo.jsonl",
    )
    events = load_events(
        fixture_path,
        subst={"{REPO}": PROJECT_ROOT.replace("\\", "/")},
    )
    server = start_server(tmp_path, port, base_path=base_path)
    try:
        sender = EventSender(
            host="127.0.0.1",
            port=port,
            client_id="material-zoo-fixture",
            role="emitter",
            origin="material-zoo-fixture",
        )
        assert sender.connect(), "Material Zoo EventSender failed to connect"
        try:
            assert sender.send_events(events), "Material Zoo fixture transaction failed"
        finally:
            sender.disconnect()

        _verify_material_zoo_stage_receiver(base_path, port, len(events))

        script = os.path.join(SCRIPTS_DIR, "test_material_zoo_replay.py")
        result = run_blender(
            blender_exe,
            script,
            port,
            ["--expected-seq", str(len(events))],
            timeout=180,
            background=False,
        )
        print("\n=== test_material_zoo_replay.py stdout ===")
        print(result.stdout[-5000:] if len(result.stdout) > 5000 else result.stdout)
        if result.stderr:
            print("=== stderr ===")
            print(result.stderr[-1000:])
        assert result.returncode == 0, (
            f"Material Zoo Blender process exited with {result.returncode}.\n"
            f"Last stderr: {result.stderr[-1500:]}"
        )
        assert "SUCCESS" in result.stdout, (
            "Material Zoo backlog replay did not print SUCCESS.\n"
            f"Last output: {result.stdout[-1500:]}"
        )
    finally:
        stop_server(server)


def test_two_blender_playback(blender_exe, tmp_path):
    """Two Blender processes share a server; one claims playback and
    scrubs through frames, the other follows. Asserts SUCCESS in both.

    Exercises Fix 1 (leader's frame_change → PlaybackControl) and the
    receive-side path (PlaybackState → scene.frame_set on the follower).
    Fix 2's wire-traffic-cleanliness effect is observable via the
    server log but isn't asserted directly here.
    """
    port = 7215
    leader_script = os.path.join(SCRIPTS_DIR, "test_playback_leader.py")
    follower_script = os.path.join(SCRIPTS_DIR, "test_playback_follower.py")
    server = start_server(tmp_path, port)
    try:
        env = os.environ.copy()
        env["BLENDER_USER_RESOURCES"] = os.path.join(
            PROJECT_ROOT, ".blender", "user_data",
        )

        def _spawn(script, client_id):
            # Same as the other asset tests: run with UI (not --background)
            # so bpy.app.timers fire reliably. Blender quits itself via
            # bpy.ops.wm.quit_blender() in TestHarness.done().
            # USD_CONNECT_CLIENT_ID forces a distinct client_id per
            # process — same-machine STABLE_CLIENT_IDs would otherwise
            # collide and the follower would think it's the leader.
            proc_env = dict(env)
            proc_env["USD_CONNECT_CLIENT_ID"] = client_id
            return subprocess.Popen(
                [
                    blender_exe,
                    "--python", script,
                    "--", "--port", str(port),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=proc_env,
            )

        leader = _spawn(leader_script, "test-playback-leader")
        # Stagger to give the leader a head start on addon install /
        # connect — same-port concurrent connects on Windows are flaky
        # otherwise. The leader's _run() sleeps 4 s before it claims
        # playback, well after the follower has connected.
        import time as _time
        _time.sleep(2.0)
        follower = _spawn(follower_script, "test-playback-follower")

        leader_out, leader_err = leader.communicate(timeout=120)
        follower_out, follower_err = follower.communicate(timeout=120)

        print("\n=== Leader stdout ===")
        print(leader_out[-2000:] if len(leader_out) > 2000 else leader_out)
        if leader_err:
            print("=== Leader stderr ===")
            print(leader_err[-500:])
        print("\n=== Follower stdout ===")
        print(follower_out[-2000:] if len(follower_out) > 2000 else follower_out)
        if follower_err:
            print("=== Follower stderr ===")
            print(follower_err[-500:])

        assert "SUCCESS" in leader_out, (
            f"Leader did not print SUCCESS.\nLast output: {leader_out[-500:]}"
        )
        assert "SUCCESS" in follower_out, (
            f"Follower did not print SUCCESS.\nLast output: {follower_out[-500:]}"
        )
    finally:
        stop_server(server)





