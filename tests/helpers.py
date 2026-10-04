"""Shared test helpers for integration tests."""

import errno
import json
import os
import socket
import subprocess
import sys
import threading
import time
import weakref
from contextlib import contextmanager

import pytest

from openusdconnect import _native_client as native
from openusdconnect import receiver as receiver_module
from openusdconnect.client_observer import ClientObserver
from openusdconnect.codec import encode_message, message_to_dict
from openusdconnect.framing import IncompleteRead, recv_framed
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


def framed(message):
    """One server message as a receiver reads it from its socket."""
    return native.encode_frame(encode_message(message))


def sent_messages(connection):
    """Every message a receiver sent on a scripted connection."""
    return [message_to_dict(frame) for frame in native.FrameDecoder().feed(connection.sent)]


class ScriptedPeer:
    """The server's end of one scripted receiver connection."""

    def __init__(self, connection):
        self.connection = connection
        assert connection.wait_idle(), "the receiver did not send its hello"
        self.hello = sent_messages(connection)[0]

    def send(self, *messages):
        """Deliver each message and wait until the receiver handled it."""
        for message in messages:
            assert self.connection.deliver(framed(message))
            assert self.connection.wait_idle(), f"the receiver closed on {message['type']}"

    def send_closing(self, message):
        """Deliver a message the receiver answers by closing the connection."""
        assert self.connection.deliver(framed(message))
        assert self.connection.wait_closed()

    def accept_hello(self, receiver, **fields):
        """Accept the hello with the receiver's own replay and layer mode."""
        hello_ok = {
            "type": "hello_ok",
            "server_instance": "scripted",
            "replay_identity": True,
            "replay_epoch": 0,
            "layer_mode": receiver.layer_mode.value,
            "layered_replay": receiver.layered_replay,
            **fields,
        }
        self.send(hello_ok)
        assert receiver.connected

    def synchronize(self, receiver, *, epoch=0):
        """Complete the replay at the received head and mark it applied."""
        self.send({"type": "replay_complete", "head_seq": receiver.last_seq, "epoch": epoch})
        assert receiver.mark_replay_applied()

    def time_out(self, count=1):
        """Make the receiver's next reads wait out its socket timeout."""
        for _ in range(count):
            assert self.connection.deliver_timeout()
            assert self.connection.wait_idle()

    def close(self):
        """Close the connection as the server."""
        self.connection.close()
        assert self.connection.wait_closed()


_SCRIPTED_SERVERS = weakref.WeakKeyDictionary()


class ScriptedServer:
    """Plays the server for the receivers started through it, without sockets.

    Every connection attempt waits until the test accepts or refuses it.
    """

    def __init__(self):
        self.sockets = native.ScriptedSocketFactory()
        self.peer = None

    def start(self, target):
        """Start a receiver, or a high-level client's receiver, on scripted sockets."""
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(receiver_module, "_SOCKET_FACTORY", self.sockets)
            target.start()
        _SCRIPTED_SERVERS[getattr(target, "_receiver", target)] = self

    def accept(self):
        connection = self.sockets.accept()
        assert connection is not None, "the receiver made no connection attempt"
        self.peer = ScriptedPeer(connection)
        return self.peer

    def refuse(self, error=errno.ECONNREFUSED):
        assert self.sockets.refuse(error), "the receiver made no connection attempt"

    def connect(self, receiver, *, synchronized=False):
        """Accept the next attempt and complete the receiver's handshake."""
        peer = self.accept()
        peer.accept_hello(receiver)
        if synchronized:
            peer.synchronize(receiver)
        return peer

    def ready(self, receiver):
        """Leave the receiver connected with its replay applied."""
        if not receiver.connected:
            self.connect(receiver)
        if not receiver.synchronized:
            self.peer.synchronize(receiver)


def scripted_start(target):
    server = ScriptedServer()
    server.start(target)
    return server


def handshake(target, *, synchronized=False):
    """Start a receiver or client whose scripted server accepted its hello."""
    receiver = getattr(target, "_receiver", target)
    return scripted_start(target).connect(receiver, synchronized=synchronized)


def force_handshake(client, *, synchronized=False):
    """Start a high-level client past its receiver handshake and layer graph."""
    if client._receiver is None:
        client.start()
        return None
    peer = handshake(client, synchronized=synchronized)
    graph = getattr(client, "_graph", None)
    if graph is not None:
        graph._ready = True
    return peer


class PeerTraffic:
    """Queues messages that peers keep sending and the consumer applies as no-ops."""

    def __init__(self, peer, *, queued=0):
        self._peer = peer
        self.arrive(queued)

    def arrive(self, count):
        self._peer.send(*[{"type": "compact"}] * count)


class ServerRelay:
    """Relays a scripted receiver connection to a real server, one message at a time.

    The receiver handles each message before the next is read, so callers
    observe it at exact message boundaries.
    """

    def __init__(self, peer, port):
        self.hello = peer.hello
        self.received = []
        self.held = threading.Event()
        self._connection = peer.connection
        self._resume = threading.Event()
        self._server = socket.create_connection(("127.0.0.1", port), timeout=30)
        self._server.sendall(peer.connection.sent)

    def next(self):
        """Relay the next server message; ``None`` once either side closed."""
        try:
            payload = recv_framed(self._server)
        except (IncompleteRead, OSError):
            return None
        delivered = self._connection.deliver(native.encode_frame(payload))
        if not (delivered and self._connection.wait_idle()):
            return None
        message = message_to_dict(payload)
        self.received.append(message)
        return message

    def pump(self, until):
        """Relay until a message matches ``until``; ``None`` if a side closed first."""
        while (message := self.next()) is not None:
            if until(message):
                return message
        return None

    def run(self, hold_after=None):
        """Relay until closed, pausing after the first message matching ``hold_after``."""
        while (message := self.next()) is not None:
            if hold_after is not None and hold_after(message):
                hold_after = None
                self.held.set()
                self._resume.wait()

    def release(self):
        self._resume.set()

    def close(self):
        self._resume.set()
        # The server answers the shutdown by closing, which ends a blocked read.
        self._server.shutdown(socket.SHUT_RDWR)
        self._server.close()
        self._connection.close()


def relay_connection(receiver, port=None):
    """Accept the receiver's next connection attempt and relay it to a server.

    The receiver starts on scripted sockets on first use, so between relays
    its connection attempts wait and the test decides when the server sees it.
    """
    server = _SCRIPTED_SERVERS.get(receiver) or scripted_start(receiver)
    return ServerRelay(server.accept(), receiver.port if port is None else port)


@contextmanager
def receiver_connection(receiver, port=None, *, hold_after=None):
    """Relay one connection in the background and close it before returning."""
    relay = relay_connection(receiver, port)
    worker = threading.Thread(target=relay.run, args=(hold_after,), daemon=True)
    worker.start()
    try:
        wait_until(lambda: receiver.connected)
        yield relay
    finally:
        relay.close()
        worker.join(5)
        assert not worker.is_alive()
        wait_until(lambda: not receiver.connected)


def mcp_session_with_receiver(port):
    """Build an MCP mirror whose connections the test relays one at a time."""
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
    scripted_start(session.receiver)
    return session


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
