"""Integration tests: emitter Blender -> server -> receiver Blender.

Starts a real server, runs Blender instances as emitter and receiver.
Skipped if Blender is not configured (see conftest.py for options).
"""

import json
import os
import subprocess
import sys
import time

TESTS_DIR = os.path.dirname(__file__)
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
EMITTER_SCRIPT = os.path.join(TESTS_DIR, "blender_emitter_script.py")
RECEIVER_SCRIPT = os.path.join(TESTS_DIR, "blender_receiver_script.py")
AUTOTRACK_EMITTER_SCRIPT = os.path.join(TESTS_DIR, "blender_autotrack_emitter_script.py")
AUTOTRACK_RECEIVER_SCRIPT = os.path.join(TESTS_DIR, "blender_autotrack_receiver_script.py")
AUTOTRACK_PROPS_EMITTER_SCRIPT = os.path.join(TESTS_DIR, "blender_autotrack_props_emitter_script.py")
ROLEFLIP_EMITTER_SCRIPT = os.path.join(TESTS_DIR, "blender_roleflip_emitter_script.py")
ROLEFLIP_RECEIVER_SCRIPT = os.path.join(TESTS_DIR, "blender_roleflip_receiver_script.py")
ROLEFLIP_VERIFIER_SCRIPT = os.path.join(TESTS_DIR, "blender_roleflip_verifier_script.py")
TEST_SCENE_USD = os.path.join(PROJECT_ROOT, "test_scene.usda")


def _start_server(tmp_path, port):
    """Start the sync server and return the subprocess."""
    db_path = str(tmp_path / f"events_{port}.db")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "openusdconnect.server",
            "--port", str(port),
            "--log", db_path,
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(1.5)
    assert proc.poll() is None, "Server exited early"
    return proc


def _run_blender(blender_exe, script, port, extra_args=None):
    """Run a Blender script and return the subprocess result."""
    cmd = [
        blender_exe, "--background",
        "--python", script,
        "--", "--port", str(port),
    ]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def _check_results(results_path, label):
    """Read results JSON and assert no failures."""
    assert os.path.isfile(results_path), f"{label}: results file not written"
    with open(results_path) as f:
        results = json.load(f)
    print(f"\n=== {label} Results ===")
    for k, v in results.items():
        print(f"  {k}: {v}")
    failures = {k: v for k, v in results.items() if v.startswith("FAIL")}
    assert not failures, f"{label} failures: {failures}"
    return results


def test_emitter_server_receiver_integration(blender_exe, tmp_path):
    """Full pipeline: Blender emitter -> server -> Blender receiver."""
    port = 7299
    results_path = str(tmp_path / "receiver_results.json")
    server = _start_server(tmp_path, port)

    try:
        # Emitter
        r = _run_blender(blender_exe, EMITTER_SCRIPT, port)
        print("=== Emitter stdout ===")
        print(r.stdout)
        assert r.returncode == 0, f"Emitter failed:\n{r.stdout}\n{r.stderr}"

        # Receiver
        r = _run_blender(blender_exe, RECEIVER_SCRIPT, port,
                         ["--out", results_path])
        print("=== Receiver stdout ===")
        print(r.stdout)
        assert r.returncode == 0, f"Receiver failed:\n{r.stdout}\n{r.stderr}"

        _check_results(results_path, "Integration")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


def test_autotrack_emitter_to_receiver(blender_exe, tmp_path):
    """Auto-tracking: bpy.ops create -> depsgraph -> _NetworkEmitter -> server -> receiver.

    Tests the real addon capture path with auto_track=True.
    """
    port = 7298
    results_path = str(tmp_path / "autotrack_results.json")
    server = _start_server(tmp_path, port)

    try:
        # Emitter with auto-tracking
        r = _run_blender(blender_exe, AUTOTRACK_EMITTER_SCRIPT, port)
        print("=== AutoTrack Emitter stdout ===")
        print(r.stdout)
        assert r.returncode == 0, f"Emitter failed:\n{r.stdout}\n{r.stderr}"

        # Receiver
        r = _run_blender(blender_exe, AUTOTRACK_RECEIVER_SCRIPT, port,
                         ["--out", results_path])
        print("=== AutoTrack Receiver stdout ===")
        print(r.stdout)
        assert r.returncode == 0, f"Receiver failed:\n{r.stdout}\n{r.stderr}"

        _check_results(results_path, "AutoTrack")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


def test_autotrack_deferred_props(blender_exe, tmp_path):
    """Auto-tracked objects get usd_prim_path/usd_type_name via deferred timer.

    Verifies the _deferred_set_props fix: custom properties are written via
    bpy.app.timers (by-name lookup) instead of directly inside depsgraph
    callbacks where writes are discarded by Blender.

    Emitter creates objects, triggers depsgraph, fires deferred timers,
    then verifies custom properties. Receiver verifies events arrived.
    """
    port = 7297
    emitter_results = str(tmp_path / "emitter_props_results.json")
    receiver_results = str(tmp_path / "receiver_props_results.json")
    server = _start_server(tmp_path, port)

    try:
        # Emitter: auto-track + verify deferred custom props
        r = _run_blender(blender_exe, AUTOTRACK_PROPS_EMITTER_SCRIPT, port,
                         ["--out", emitter_results])
        print("=== AutoTrack Props Emitter stdout ===")
        print(r.stdout)
        assert r.returncode == 0, f"Emitter failed:\n{r.stdout}\n{r.stderr}"
        _check_results(emitter_results, "AutoTrack Props (emitter)")

        # Receiver: verify events were received and applied correctly
        r = _run_blender(blender_exe, AUTOTRACK_RECEIVER_SCRIPT, port,
                         ["--out", receiver_results])
        print("=== AutoTrack Props Receiver stdout ===")
        print(r.stdout)
        assert r.returncode == 0, f"Receiver failed:\n{r.stdout}\n{r.stderr}"
        _check_results(receiver_results, "AutoTrack Props (receiver)")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


def test_roleflip_no_axis_flip(blender_exe, tmp_path):
    """Role-flip test: Y-up USD import → emit → receive → flip roles → verify no axis flip.

    This is the key integration test for the flipped-up-axis bug.
    Three phases run sequentially against the same server:

    Phase 1 (Emitter A): Import Y-up test_scene.usda into Z-up Blender
        (creates non-identity MPI from coordinate conversion), move Cube
        to (3,5,7), emit events.

    Phase 2 (Instance B): Import same scene, receive Phase 1 events
        (ensure_xform_ops resets MPI), verify position, then flip to emitter
        and move Cube to (10,11,12).

    Phase 3 (Verifier): Import same scene, receive ALL events, verify Cube
        ends up at (10,11,12) with no axis flip.

    Exercises all three fixes:
    - Fix 1: world-preserving MPI reset (Phase 2 receive)
    - Fix 2: batch-scoped feedback guard (Phase 2 flip)
    - Fix 3: ancestor event emission (parent /World emitted before children)
    """
    port = 7296
    scene = TEST_SCENE_USD
    assert os.path.isfile(scene), f"Test scene not found: {scene}"

    phase2_results = str(tmp_path / "roleflip_phase2.json")
    phase3_results = str(tmp_path / "roleflip_phase3.json")
    server = _start_server(tmp_path, port)

    try:
        # Phase 1: Emitter A imports and moves Cube
        r = _run_blender(blender_exe, ROLEFLIP_EMITTER_SCRIPT, port,
                         ["--scene", scene])
        print("=== RoleFlip Phase 1 (Emitter A) stdout ===")
        print(r.stdout)
        if r.stderr:
            print("=== stderr ===")
            print(r.stderr)
        assert r.returncode == 0, f"Phase 1 failed:\n{r.stdout}\n{r.stderr}"

        # Phase 2: Instance B receives, verifies, flips, moves
        r = _run_blender(blender_exe, ROLEFLIP_RECEIVER_SCRIPT, port,
                         ["--scene", scene, "--out", phase2_results])
        print("=== RoleFlip Phase 2 (Instance B) stdout ===")
        print(r.stdout)
        if r.stderr:
            print("=== stderr ===")
            print(r.stderr)
        assert r.returncode == 0, f"Phase 2 failed:\n{r.stdout}\n{r.stderr}"
        _check_results(phase2_results, "RoleFlip Phase 2")

        # Phase 3: Verifier receives all events, checks final position
        r = _run_blender(blender_exe, ROLEFLIP_VERIFIER_SCRIPT, port,
                         ["--scene", scene, "--out", phase3_results])
        print("=== RoleFlip Phase 3 (Verifier) stdout ===")
        print(r.stdout)
        if r.stderr:
            print("=== stderr ===")
            print(r.stderr)
        assert r.returncode == 0, f"Phase 3 failed:\n{r.stdout}\n{r.stderr}"
        _check_results(phase3_results, "RoleFlip Phase 3")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
