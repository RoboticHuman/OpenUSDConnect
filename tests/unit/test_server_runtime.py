"""Ownership and failure-path tests for the embedded server lifecycle."""

import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import openusdconnect.server as server_api
import openusdconnect.server.cli as cli
import openusdconnect.server.runtime as runtime_module
from openusdconnect.server.config import ServerConfig, VfsConfig
from openusdconnect.server.runtime import ServerRuntime, start_server


@pytest.fixture
def config(tmp_path):
    return ServerConfig(port=0, log_path=str(tmp_path / "events.db"), preflight_plugins=False)


def assert_released(runtime):
    assert not runtime.running
    if runtime._thread is not None:
        assert not runtime._thread.is_alive()
    if runtime.server is not None:
        assert runtime.server.socket.fileno() == -1
    state = runtime.sync_server
    thread = getattr(state, "_broadcast_thread", None)
    if thread is not None:
        assert not thread.is_alive()
    compactor = getattr(state, "_compactor", None)
    if compactor is not None:
        assert not compactor.running
    coordinator = getattr(state, "_transactions", None)
    if coordinator is not None:
        assert not coordinator.running
    journal = getattr(state, "_journal", None)
    if journal is not None:
        assert not journal.running
    if getattr(state, "store", None) is not None:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            state.store.get_count()


def test_public_and_backward_imports():
    assert cli.ServerConfig is server_api.ServerConfig is ServerConfig
    assert cli.VfsConfig is server_api.VfsConfig is VfsConfig
    assert server_api.ServerRuntime is ServerRuntime
    assert server_api.start_server is start_server
    assert server_api.run_server is cli.run_server


def test_context_manager_closes_clients_workers_and_store(config, monkeypatch):
    def forbidden(*args):
        pytest.fail("embedded runtime must not install signal handlers")

    monkeypatch.setattr(signal, "signal", forbidden)
    started = time.monotonic()
    with ServerRuntime(config) as runtime:
        assert runtime.running
        assert runtime.start() is runtime
        assert runtime.server_address[1] != 0
        assert not runtime.wait(timeout=0)
        client = socket.create_connection(runtime.server_address, timeout=2)
        # Ensure this is an accepted, idle pre-handshake client.
        deadline = threading.Event()
        for _ in range(200):
            with runtime.server._requests_lock:
                if runtime.server._requests:
                    break
            deadline.wait(0.01)
        else:
            pytest.fail("client was not accepted")
    try:
        assert client.recv(1) == b""
    finally:
        client.close()
    assert_released(runtime)
    assert time.monotonic() - started < 5
    runtime.stop()
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        runtime.start()


def test_context_exception_and_export_failure_still_clean_up(config, monkeypatch):
    config.export_diff = "unused.usda"
    runtime = ServerRuntime(config)
    with pytest.raises(ValueError, match="body"):
        with runtime:

            def fail_export(path):
                raise OSError("export failed")

            monkeypatch.setattr(runtime.sync_server, "export_edit_layer", fail_export)
            raise ValueError("body")
    assert_released(runtime)


def test_start_stop_from_background_thread(config):
    errors = []

    def run():
        try:
            runtime = start_server(config)
            runtime.stop()
            assert_released(runtime)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors


@pytest.mark.parametrize("failure", ["compact", "tcp", "vfs", "prewarm", "dashboard"])
def test_partial_service_startup_cleanup(config, monkeypatch, failure):
    import openusdconnect.server.vfs as vfs_module

    events = []

    def fail(*args, **kwargs):
        raise RuntimeError(failure)

    class Dashboard:
        def __init__(self, *args):
            pass

        def start(self):
            fail()

        def stop(self):
            events.append("dashboard.stop")

    class Provider:
        live_name = "scene.live.usda"

        def __init__(self, *args, **kwargs):
            pass

        def prewarm(self, **kwargs):
            if failure == "prewarm":
                fail()

    monkeypatch.setattr(vfs_module, "VirtualStageFileSet", Provider)
    monkeypatch.setattr(
        vfs_module,
        "run_vfs_server",
        fail
        if failure == "vfs"
        else lambda *a, **kw: SimpleNamespace(stop=lambda: events.append("vfs.stop")),
    )
    if failure == "compact":
        config.compact = True
        monkeypatch.setattr(runtime_module.UsdSyncServer, "compact_log", fail)
    elif failure == "tcp":
        monkeypatch.setattr(runtime_module, "ThreadedTCPServer", fail)
    else:
        config.vfs = VfsConfig(port=7280)
    if failure == "dashboard":
        import integrations.dashboard as dashboard

        config.dashboard_port = 8080

        def start_dashboard(*args):
            handle = Dashboard(*args)
            try:
                handle.start()
            except BaseException:
                handle.stop()
                raise

        monkeypatch.setattr(dashboard, "run_dashboard", start_dashboard)
    runtime = ServerRuntime(config)
    with pytest.raises(RuntimeError, match=failure):
        runtime.start()
    assert_released(runtime)
    if failure in ("prewarm", "dashboard"):
        assert events.count("vfs.stop") == 1
    if failure == "dashboard":
        assert events.count("dashboard.stop") == 1
    runtime.stop()


def test_state_constructor_failure_cleans_partial_workers_and_store(config, monkeypatch):
    import openusdconnect.server.state as state_module

    stores = []
    states = []
    store_class = state_module.SqliteEventStore

    def make_store(*args, **kwargs):
        store = store_class(*args, **kwargs)
        stores.append(store)
        return store

    def fail(self):
        states.append(self)
        raise RuntimeError("replay failed")

    config.durability = "realtime"
    config.compact_interval = 60
    monkeypatch.setattr(state_module, "SqliteEventStore", make_store)
    monkeypatch.setattr(runtime_module.UsdSyncServer, "_replay_log_into_stage", fail)
    runtime = ServerRuntime(config)
    with pytest.raises(RuntimeError, match="replay failed"):
        runtime.start()
    assert_released(runtime)
    assert len(stores) == len(states) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        stores[0].get_count()
    assert not states[0]._compactor.running
    assert not states[0]._journal.running
    thread = states[0]._broadcast_thread
    assert thread is None or thread.ident is None


def test_failed_state_preserves_caller_owned_store(config, monkeypatch):
    from openusdconnect.server.state import SqliteEventStore, UsdSyncServer

    def fail(self):
        raise RuntimeError("replay failed")

    monkeypatch.setattr(UsdSyncServer, "_replay_log_into_stage", fail)
    store = SqliteEventStore(config.log_path)
    try:
        with pytest.raises(RuntimeError, match="replay failed"):
            UsdSyncServer(event_store=store)
        assert store.get_count() == 0
    finally:
        store.close()


def test_worker_start_failure_stops_started_workers(config, monkeypatch):
    import openusdconnect.server.state as state_module

    states = []
    original = state_module.PeriodicCompactor.start
    original_initialize = state_module.UsdSyncServer.__init__

    def initialize(self, **kwargs):
        states.append(self)
        original_initialize(self, **kwargs)

    def fail(self):
        original(self)
        raise RuntimeError("worker startup failed")

    config.durability = "realtime"
    config.compact_interval = 60
    monkeypatch.setattr(state_module.UsdSyncServer, "__init__", initialize)
    monkeypatch.setattr(state_module.PeriodicCompactor, "start", fail)
    with pytest.raises(RuntimeError, match="worker startup failed"):
        start_server(config)
    state = states[0]
    assert not state._compactor.running
    assert not state._journal.running
    assert not state._broadcast_thread.is_alive()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        state.store.get_count()
    state.shutdown()


def test_bind_failure_releases_state_and_store(config):
    with socket.socket() as occupied:
        occupied.bind((config.host, 0))
        occupied.listen()
        config.port = occupied.getsockname()[1]
        runtime = ServerRuntime(config)
        with pytest.raises(OSError):
            runtime.start()
        assert_released(runtime)


def test_cleanup_continues_after_service_stop_failure(config):
    runtime = start_server(config)
    events = []

    def fail():
        events.append("vfs")
        raise RuntimeError("stop failed")

    runtime.vfs_handle = SimpleNamespace(stop=fail)
    runtime.dashboard_handle = SimpleNamespace(stop=lambda: events.append("dashboard"))
    runtime.stop()
    runtime.stop()
    assert events == ["vfs", "dashboard"]
    assert_released(runtime)


def test_cli_blocks_until_signal_and_restores_handler(config, monkeypatch):
    previous = signal.getsignal(signal.SIGTERM)
    observed = []

    class Runtime:
        def __init__(self, config):
            pass

        def __enter__(self):
            return self

        def wait(self, timeout):
            handler = signal.getsignal(signal.SIGTERM)
            assert handler != previous
            observed.append("wait")
            handler(signal.SIGTERM, None)
            return False

        def stop(self):
            pass

        def __exit__(self, *args):
            self.stop()

    monkeypatch.setattr(cli, "ServerRuntime", Runtime)
    cli.run_server(config)
    assert observed == ["wait"]
    assert signal.getsignal(signal.SIGTERM) == previous


@pytest.mark.parametrize("occupied", [False, True])
def test_real_dashboard_start_stop_in_isolated_process(tmp_path, occupied):
    pytest.importorskip("nicegui")
    script = """
import socket
import sys
import threading
import urllib.request
from openusdconnect.server import ServerConfig, ServerRuntime

occupied = sys.argv[2] == "True"
with socket.socket() as listener:
    listener.bind(("0.0.0.0", 0))
    listener.listen()
    port = listener.getsockname()[1] if occupied else 0
    runtime = ServerRuntime(ServerConfig(
        port=0, dashboard_port=port, log_path=sys.argv[1], preflight_plugins=False
    ))
    try:
        if occupied:
            try:
                runtime.start()
            except RuntimeError as exc:
                assert "Dashboard startup failed" in str(exc)
            else:
                raise AssertionError("dashboard accepted an occupied port")
        else:
            with runtime:
                port = runtime.dashboard_handle.server.servers[0].sockets[0].getsockname()[1]
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5) as r:
                    assert r.status == 200
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
                    assert r.status == 200
        if not occupied:
            assert not runtime.dashboard_handle.thread.is_alive()
        assert not any(t.name == "OpenUSDConnect_Dashboard" for t in threading.enumerate())
        assert runtime.server.socket.fileno() == -1
    finally:
        runtime.stop()
    if not occupied:
        with ServerRuntime(ServerConfig(
            port=0, dashboard_port=0, log_path=sys.argv[1], preflight_plugins=False
        )) as second:
            second.sync_server.get_event_count = lambda: 123
            port = second.dashboard_handle.server.servers[0].sockets[0].getsockname()[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5) as r:
                assert r.status == 200
                import json
                assert json.load(r)["event_count"] == 123
            from nicegui import app
            assert sum(getattr(route, "path", None) == "/api/status" for route in app.routes) == 1
        assert not second.dashboard_handle.thread.is_alive()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "dashboard.db"), str(occupied)],
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("failure", ["none", "setup", "bind"])
@pytest.mark.parametrize("visit", [False, True])
def test_dashboard_releases_page_registry_and_state_in_isolated_process(tmp_path, failure, visit):
    pytest.importorskip("nicegui")
    script = """
import asyncio
import gc
import socket
import sys
import urllib.request
import weakref
from unittest.mock import patch

from nicegui import app, ui
from nicegui.client import Client
import integrations.dashboard as dashboard
from integrations.dashboard import pages
from openusdconnect.server import ServerConfig, ServerRuntime

@ui.page("/")
def unrelated_before():
    pass

@ui.page("/unrelated")
def unrelated_page():
    ui.label("Unrelated page")

def unrelated_disconnect():
    pass

app.on_disconnect(unrelated_disconnect)
expected_handlers = list(app._disconnect_handlers)
expected_delete_handlers = list(app._delete_handlers)
expected_clients = dict(Client.instances)
expected_pages = dict(Client.page_routes)
original_setup = pages.setup_pages
refs = []
failure = sys.argv[2]
visit = sys.argv[3] == "True"
deletions = []

def on_delete(client):
    deletions.append((client.id, asyncio.get_running_loop().is_running(), app.is_started))

def fetch(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
        assert response.status == 200

def setup(srv, **kwargs):
    refs.extend((weakref.ref(srv), weakref.ref(srv.stage)))
    original_setup(srv, **kwargs)
    if failure == "setup":
        raise RuntimeError("partial page setup failed")

for cycle in range(3):
    # Later cycles must still work after either startup failure.
    if cycle:
        failure = "none"
    routes_before = list(app.routes)
    owned_ids = set()
    lifespan_before = None
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 0))
        listener.listen()
        port = listener.getsockname()[1] if failure == "bind" else 0
        runtime = ServerRuntime(ServerConfig(
            port=0, dashboard_port=port, log_path=sys.argv[1], preflight_plugins=False
        ))
        try:
            with patch.object(pages, "setup_pages", setup):
                if failure == "none":
                    runtime.start()
                    lifespan_before = runtime.dashboard_handle._lifespan_before
                    assert len(Client.page_routes) == len(expected_pages) + 1
                    if visit:
                        listener_socket = runtime.dashboard_handle.server.servers[0].sockets[0]
                        port = listener_socket.getsockname()[1]
                        # Keep unrelated clients from both this run and previous runs alive.
                        fetch(port, "/unrelated")
                        expected_clients = dict(Client.instances)
                        fetch(port, "/")
                        fetch(port, "/")
                        owned_ids = set(Client.instances) - expected_clients.keys()
                        assert len(owned_ids) == 2
                        assert len(runtime.sync_server._event_listeners) == 2
                        for client_id in owned_ids:
                            Client.instances[client_id].on_delete(on_delete)
                    # A late, unrelated entry must not be removed by snapshot restoration.
                    def unrelated_during():
                        pass
                    Client.page_routes[unrelated_during] = "/unrelated"
                    expected_pages[unrelated_during] = "/unrelated"
                else:
                    try:
                        runtime.start()
                    except RuntimeError as exc:
                        expected = ("partial page setup failed" if failure == "setup"
                                    else "Dashboard startup failed")
                        assert expected in str(exc), str(exc)
                    else:
                        raise AssertionError("dashboard startup unexpectedly succeeded")
        finally:
            runtime.stop()
            runtime.stop()
        assert app.routes == routes_before
        if lifespan_before is not None:
            assert dashboard._dashboard_app.router.lifespan_context is lifespan_before
        assert Client.page_routes == expected_pages
        assert Client.instances == expected_clients
        assert app._disconnect_handlers == expected_handlers
        assert app._delete_handlers == expected_delete_handlers
        assert not runtime.sync_server._event_listeners
        assert {entry[0] for entry in deletions} == owned_ids
        assert all(running and started for _, running, started in deletions)
        deletions.clear()
        assert runtime.server.socket.fileno() == -1
        if runtime.dashboard_handle is not None:
            assert not runtime.dashboard_handle.thread.is_alive()
        del runtime
    gc.collect()
    assert len(refs) == 2 * (cycle + 1)
    assert all(ref() is None for ref in refs), "stopped dashboard retained server state/stage"
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "dashboard.db"), failure, str(visit)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
