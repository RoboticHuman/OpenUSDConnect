"""Integration tests: emitter Blender -> server -> receiver Blender.

Starts a real server, runs Blender instances as emitter and receiver.
Skipped if Blender is not configured (see conftest.py for options).
"""

import os

from tests.helpers import read_results, run_blender, start_server, stop_server

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "scripts")
EMITTER_SCRIPT = os.path.join(SCRIPTS_DIR, "blender_emitter_script.py")
RECEIVER_SCRIPT = os.path.join(SCRIPTS_DIR, "blender_receiver_script.py")


def test_emitter_server_receiver_integration(blender_exe, tmp_path, free_port):
    """Full pipeline: Blender emitter -> server -> Blender receiver."""
    port = free_port
    results_path = str(tmp_path / "receiver_results.json")
    server = start_server(tmp_path, port)

    try:
        # Emitter
        r = run_blender(blender_exe, EMITTER_SCRIPT, port, timeout=30)
        print("=== Emitter stdout ===")
        print(r.stdout)
        assert r.returncode == 0, f"Emitter failed:\n{r.stdout}\n{r.stderr}"

        # Receiver
        r = run_blender(blender_exe, RECEIVER_SCRIPT, port, ["--out", results_path], timeout=30)
        print("=== Receiver stdout ===")
        print(r.stdout)
        assert r.returncode == 0, f"Receiver failed:\n{r.stdout}\n{r.stderr}"

        results = read_results(results_path, "Integration")
        failures = {k: v for k, v in results.items() if v.startswith("FAIL")}
        assert not failures, f"Integration failures: {failures}"
    finally:
        stop_server(server)
