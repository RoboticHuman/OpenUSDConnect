"""Shared test helpers for integration tests."""

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

from openusdconnect.protocol_constants import (
    K_SET_REFERENCE,
    K_SET_XFORM_TRS,
)

TESTS_DIR = os.path.dirname(__file__)
PROJECT_ROOT = os.path.dirname(TESTS_DIR)


class ReceiverStub:
    """Receiver metadata and cleanup contract established by a successful hello."""

    _client_id: str | None = None
    _origin: str | None = None
    _layered_replay = False

    def release_receiver_replay_reservation(self):
        pass


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


def start_server(tmp_path, port, *, base_path=None):
    """Start the sync server and return the subprocess.

    ``base_path`` configures the authoritative base stage used by receivers.
    Most integration tests build their scene entirely from events and leave it
    unset; stage-first replay tests pass the same base imported by the DCC.
    """
    db_path = str(tmp_path / f"events_{port}.db")
    command = [
        sys.executable,
        "-m",
        "openusdconnect.server",
        "--port",
        str(port),
        "--event-log",
        db_path,
    ]
    if base_path is not None:
        command.extend(["--base", os.fspath(base_path)])
    proc = subprocess.Popen(
        command,
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


def run_blender(blender_exe, script, port, extra_args=None, timeout=60, background=True):
    """Run a Blender script and return the subprocess result."""
    cmd = [blender_exe]
    if background:
        cmd.append("--background")
    cmd.extend(
        [
            "--python",
            script,
            "--",
            "--port",
            str(port),
        ]
    )
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
    rows = conn.execute("SELECT seq, event_bin FROM events ORDER BY seq").fetchall()
    conn.close()
    from openusdconnect.codec import message_to_dict

    print(f"\n=== Server Event Log ({len(rows)} events) ===")
    for seq, event_bin in rows:
        record = message_to_dict(event_bin)
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


def ensure_prim_event(path):
    return {"k": "ensure_prim", "prim": path, "typeName": "Xform"}


@contextmanager
def in_process_server():
    """Run an isolated TCP server on an ephemeral port."""
    from openusdconnect.server.connection import ConnectionHandler, ThreadedTCPServer
    from openusdconnect.server.state import UsdSyncServer

    state = UsdSyncServer(log_path=":memory:", txn_batch_size=1)
    tcp = ThreadedTCPServer(("127.0.0.1", 0), ConnectionHandler, state, max_workers=8)
    thread = threading.Thread(target=tcp.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, tcp.server_address[1]
    finally:
        tcp.shutdown()
        tcp.server_close()
        thread.join(5)
        state.shutdown()
        state.store.close()
        assert not thread.is_alive()


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


@contextmanager
def receiver_connection(receiver):
    """Run one connection attempt and close its socket before returning."""
    thread = threading.Thread(target=receiver._connect_and_recv, daemon=True)
    thread.start()
    try:
        wait_until(lambda: receiver.connected)
        yield
    finally:
        receiver._close_socket()
        thread.join(5)
        assert not thread.is_alive()
        receiver.connected = False


def mcp_session_with_receiver(port):
    """Build an MCP mirror whose connection timing is controlled by the test."""
    from pxr import Usd

    from integrations.mcp.config import McpConfig
    from integrations.mcp.session import ConnectionSession
    from openusdconnect.usd_client import UsdReceiver

    session = ConnectionSession(McpConfig(read_after_write_timeout_s=1))
    session.mirror_stage = Usd.Stage.CreateInMemory()
    session.receiver = UsdReceiver(
        session.mirror_stage,
        app_name="replay-identity-test",
        host="127.0.0.1",
        port=port,
        persist_token=False,
    )
    # These tests drive one connection attempt directly to control reconnect timing.
    session.receiver._started = True
    return session
