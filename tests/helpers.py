"""Shared test helpers for integration tests."""

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

from openusdconnect.client_observer import ClientObserver
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
def serving(state, port=0):
    """Serve *state* on a loopback port; leaving closes the listener and its connections."""
    from openusdconnect.server.connection import ConnectionHandler, ThreadedTCPServer

    tcp = ThreadedTCPServer(("127.0.0.1", port), ConnectionHandler, state, max_workers=8)
    # Shutdown waits for the next poll.
    thread = threading.Thread(target=tcp.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield tcp.server_address[1]
    finally:
        tcp.shutdown()
        tcp.server_close()
        thread.join(5)
        assert not thread.is_alive()


@contextmanager
def server_state():
    """An isolated server state with an in-memory event log."""
    from openusdconnect.server.state import UsdSyncServer

    state = UsdSyncServer(log_path=":memory:", txn_batch_size=1)
    try:
        yield state
    finally:
        state.shutdown()
        state.store.close()


@contextmanager
def in_process_server():
    """Run an isolated TCP server on an ephemeral port."""
    with server_state() as state, serving(state) as port:
        yield state, port


def recorded_hellos(monkeypatch):
    """Record every hello the in-process server decodes, in arrival order."""
    from openusdconnect.server import connection

    hellos = []
    decode = connection.decode_hello

    def record(table):
        hellos.append(decode(table))
        return hellos[-1]

    monkeypatch.setattr(connection, "decode_hello", record)
    return hellos


@contextmanager
def embedded_server(**config):
    """Run a ``ServerRuntime`` on an ephemeral loopback port with an in-memory event log."""
    from openusdconnect.server import ServerConfig, ServerRuntime

    runtime = ServerRuntime(
        ServerConfig(
            host="127.0.0.1", port=0, log_path=":memory:", preflight_plugins=False, **config
        )
    )
    try:
        with runtime:
            yield runtime
    finally:
        if runtime.sync_server is not None and runtime.sync_server.token_store is not None:
            runtime.sync_server.token_store.close()


def client_registered(runtime, client_id):
    """Whether the server of *runtime* holds a connection from *client_id*."""
    state = runtime.sync_server
    with state.clients_lock:
        return any(info.client_id == client_id for info in state.clients.values())


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


def connect_client(client, *, synchronized=True):
    """Start a high-level client and complete its receiver's handshake with the server.

    ``synchronized`` also applies the server's replay the way ``update()`` does,
    without publishing local edits.
    """
    client.start()
    receiver = client._receiver
    assert receiver.wait_connected(5), receiver.connection_error
    if synchronized:
        apply = client.update if client._sender is None else client._apply_queued
        wait_until(lambda: apply() is not None and receiver.synchronized)


class PeerTraffic:
    """Records a peer producer commits, each queued by *receiver* when ``arrive`` returns."""

    def __init__(self, state, receiver, make_event, *, layer_key=""):
        self._state = state
        self._receiver = receiver
        self._make_event = make_event
        self._layer_key = layer_key
        self._txn_id = 0

    def arrive(self, count):
        queued = self._receiver.queued_message_count + count
        for _ in range(count):
            self._txn_id += 1
            self._state.process_idempotent_txn(
                [self._make_event(f"/Peer{self._txn_id}")],
                session_id="peer",
                txn_id=self._txn_id,
                client_id="peer",
                layer_key=self._layer_key,
            )
        wait_until(lambda: self._receiver.queued_message_count == queued)


class RecordingObserver(ClientObserver):
    """Records (method, value, thread id) for every observer call."""

    def __init__(self, on_call=None):
        self.calls = []
        self._on_call = on_call

    def _record(self, name, value):
        self.calls.append((name, value, threading.get_ident()))
        if self._on_call is not None:
            self._on_call(name, value)

    def on_applied(self, batch):
        self._record("applied", batch)

    def on_resync(self):
        self._record("resync", None)

    def on_stage_metadata(self, metadata):
        self._record("stage_metadata", metadata)

    def on_playback_state(self, state):
        self._record("playback_state", state)

    def on_playback_claim(self, result):
        self._record("playback_claim", result)

    def on_token_issued(self, token):
        self._record("token_issued", token)
