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


def _start_server(tmp_path, port):
    """Start the sync server and return the subprocess."""
    db_path = str(tmp_path / f"events_{port}.db")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "openusdconnect.server",
            "--port",
            str(port),
            "--log",
            db_path,
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
        blender_exe,
        "--background",
        "--python",
        script,
        "--",
        "--port",
        str(port),
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
        r = _run_blender(blender_exe, RECEIVER_SCRIPT, port, ["--out", results_path])
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
