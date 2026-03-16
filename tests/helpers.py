"""Shared test helpers for integration tests."""

import json
import os
import subprocess
import sys
import time

from openusdconnect.protocol import K_SET_REFERENCE, K_SET_XFORM_TRS

TESTS_DIR = os.path.dirname(__file__)
PROJECT_ROOT = os.path.dirname(TESTS_DIR)


def _wait_for_server(port, timeout=5):
    """Poll until the server accepts TCP connections."""
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.1)
            s.close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"Server not ready on port {port} after {timeout}s")


def start_server(tmp_path, port):
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
    _wait_for_server(port)
    assert proc.poll() is None, "Server exited early"
    return proc


def stop_server(proc):
    """Terminate server process, kill if it doesn't stop."""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_blender(blender_exe, script, port, extra_args=None, timeout=60):
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
    # Isolate Blender user data to repo-local directory (not system AppData)
    env = os.environ.copy()
    env["BLENDER_USER_RESOURCES"] = os.path.join(PROJECT_ROOT, ".blender", "user_data")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def dump_server_log(tmp_path, port):
    """Print server event log from SQLite for debugging."""
    import sqlite3

    db_path = str(tmp_path / f"events_{port}.db")
    if not os.path.isfile(db_path):
        print("[ServerLog] No database found")
        return
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT seq, event FROM events ORDER BY seq").fetchall()
    conn.close()
    print(f"\n=== Server Event Log ({len(rows)} events) ===")
    for seq, event_json in rows:
        record = json.loads(event_json)
        ev = record.get("event", record)
        k = ev.get("k", "?")
        prim = ev.get("prim", "?")
        extra = ""
        if k == K_SET_REFERENCE:
            extra = f" refs={ev.get('refs')}"
        elif k == K_SET_XFORM_TRS:
            extra = f" fields={ev.get('fields')}"
        print(f"  seq={seq}: {k} {prim}{extra}")


def read_results(results_path, label):
    """Read results JSON and return the dict. Prints everything for debugging."""
    assert os.path.isfile(results_path), f"{label}: results file not written"
    with open(results_path) as f:
        results = json.load(f)
    print(f"\n=== {label} Results ===")
    for k, v in results.items():
        print(f"  {k}: {v}")
    return results
