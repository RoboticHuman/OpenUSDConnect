"""Integration tests for reference child duplication.

Verifies that when the emitter sends events for objects that are children
of a referenced asset, the receiver does not create duplicate objects.

Requires Blender (see conftest.py for how to configure the path).
"""

import os
import subprocess
import sys

from tests.helpers import (
    PROJECT_ROOT,
    dump_server_log,
    read_results,
    run_blender,
    start_server,
    stop_server,
)

TESTS_DIR = os.path.dirname(__file__)
REF_EMITTER_SCRIPT = os.path.join(TESTS_DIR, "ref_emitter_script.py")
REF_RECEIVER_SCRIPT = os.path.join(TESTS_DIR, "ref_receiver_script.py")
REF_LOOPBACK_SCRIPT = os.path.join(TESTS_DIR, "ref_loopback_script.py")
REF_MANUAL_SCRIPT = os.path.join(TESTS_DIR, "ref_manual_then_move_script.py")
TEST_ASSET = os.path.join(PROJECT_ROOT, "test_asset.usda")


def _run_emitter(port):
    """Run the ref emitter script and assert success."""
    result = subprocess.run(
        [
            sys.executable,
            REF_EMITTER_SCRIPT,
            "--port",
            str(port),
            "--asset-path",
            TEST_ASSET,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    print("=== Emitter stdout ===")
    print(result.stdout)
    if result.stderr:
        print("=== Emitter stderr ===")
        print(result.stderr)
    assert result.returncode == 0, (
        f"Emitter failed:\n{result.stdout}\n{result.stderr}"
    )


def _assert_ref_results(results, label, check_trs=True):
    """Common assertions for reference duplication tests."""
    assert results.get("no_duplicates") == "PASS", (
        f"{label}: duplicate prim paths: {results.get('object_inventory')}"
    )
    assert results.get("no_blender_suffixes") == "PASS", (
        f"{label}: Blender-suffixed names: {results.get('no_blender_suffixes')}"
    )
    if check_trs:
        assert results.get("chair_trs") == "PASS", (
            f"{label}: Chair TRS: {results.get('chair_trs')}"
        )
        if "children_trs" in results:
            assert results.get("children_trs") == "PASS", (
                f"{label}: Children TRS: {results.get('children_trs')}"
            )


def test_ref_no_duplicates(blender_exe, tmp_path):
    """Reference children: emitter sends ensure_prim for children of a
    referenced asset.  Receiver must not create duplicate Blender objects."""
    port = 7297
    results_path = str(tmp_path / "ref_receiver_results.json")
    server = start_server(tmp_path, port)

    try:
        _run_emitter(port)

        recv_result = run_blender(
            blender_exe,
            REF_RECEIVER_SCRIPT,
            port,
            ["--out", results_path, "--asset-root", PROJECT_ROOT],
        )
        print("=== Receiver stdout ===")
        print(recv_result.stdout)

        dump_server_log(tmp_path, port)
        results = read_results(results_path, "RefNoDup")
        _assert_ref_results(results, "RefNoDup")
    finally:
        stop_server(server)


def test_ref_reset_no_duplicates(blender_exe, tmp_path):
    """Same scenario but receiver runs twice (simulating seq=0 reset).

    Each Blender invocation starts with a fresh scene.  The second receiver
    gets all events replayed from the start, including set_reference.
    Both runs must produce no duplicates.
    """
    port = 7296
    results1_path = str(tmp_path / "ref_reset_results1.json")
    results2_path = str(tmp_path / "ref_reset_results2.json")
    server = start_server(tmp_path, port)

    try:
        _run_emitter(port)

        r1 = run_blender(
            blender_exe,
            REF_RECEIVER_SCRIPT,
            port,
            ["--out", results1_path, "--asset-root", PROJECT_ROOT],
        )
        print("=== Receiver 1 stdout ===")
        print(r1.stdout)
        results1 = read_results(results1_path, "Reset-Run1")

        r2 = run_blender(
            blender_exe,
            REF_RECEIVER_SCRIPT,
            port,
            ["--out", results2_path, "--asset-root", PROJECT_ROOT],
        )
        print("=== Receiver 2 stdout ===")
        print(r2.stdout)

        dump_server_log(tmp_path, port)
        results2 = read_results(results2_path, "Reset-Run2")

        for label, results in [("Run1", results1), ("Run2", results2)]:
            _assert_ref_results(results, label, check_trs=False)
    finally:
        stop_server(server)


def _create_test_scene(tmp_path):
    """Create a test_scene.usda with /World/Chair referencing test_asset.usda."""
    asset_path = TEST_ASSET.replace("\\", "/")
    scene_content = (
        '#usda 1.0\n'
        '(\n'
        '    defaultPrim = "World"\n'
        '    upAxis = "Y"\n'
        ')\n'
        '\n'
        'def Xform "World"\n'
        '{\n'
        '    def Xform "Chair" (\n'
        f'        references = @{asset_path}@</Model>\n'
        '    )\n'
        '    {\n'
        '        double3 xformOp:translate = (0, 0, 0)\n'
        '        token[] xformOpOrder = ["xformOp:translate"]\n'
        '    }\n'
        '}\n'
    )
    scene_path = str(tmp_path / "test_scene.usda")
    with open(scene_path, "w") as f:
        f.write(scene_content)
    return scene_path


def test_ref_loopback_no_duplicates(blender_exe, tmp_path):
    """Loopback: Blender already has the scene imported when events arrive."""
    port = 7295
    results_path = str(tmp_path / "ref_loopback_results.json")
    scene_path = _create_test_scene(tmp_path)
    server = start_server(tmp_path, port)

    try:
        _run_emitter(port)

        r = run_blender(
            blender_exe,
            REF_LOOPBACK_SCRIPT,
            port,
            ["--out", results_path, "--scene", scene_path],
        )
        print("=== Loopback stdout ===")
        print(r.stdout)

        dump_server_log(tmp_path, port)
        results = read_results(results_path, "Loopback")
        _assert_ref_results(results, "Loopback")
    finally:
        stop_server(server)


def test_ref_manual_then_move(blender_exe, tmp_path):
    """User's exact workflow: manual set_reference via CLI, then move Chair."""
    port = 7294
    results_path = str(tmp_path / "ref_manual_results.json")
    server = start_server(tmp_path, port)

    try:
        r = run_blender(
            blender_exe,
            REF_MANUAL_SCRIPT,
            port,
            ["--out", results_path, "--asset-path", TEST_ASSET],
        )
        print("=== ManualThenMove stdout ===")
        print(r.stdout)

        dump_server_log(tmp_path, port)
        results = read_results(results_path, "ManualThenMove")
        _assert_ref_results(results, "ManualThenMove")
    finally:
        stop_server(server)
