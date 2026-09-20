"""Host-facing lifecycle guarantees shared by the high-level clients."""

import threading
from types import SimpleNamespace

import pytest
from pxr import Usd

from openusdconnect import (
    ManagedClient,
    SharedStageClient,
    UsdPublisher,
    UsdReceiver,
    _client_lifecycle,
    _client_utils,
    client_types,
)
from openusdconnect import sender as sender_module


def test_public_status_types_keep_compatibility_identity():
    from openusdconnect import ClientPhase, ClientStatus, SyncUpdate

    for name, value in (
        ("ClientPhase", ClientPhase),
        ("ClientStatus", ClientStatus),
        ("SyncUpdate", SyncUpdate),
    ):
        assert value is getattr(client_types, name)
        assert value is getattr(_client_utils, name)


@pytest.mark.parametrize(
    ("sender_token", "receiver", "persist", "expected"),
    [
        ("expired", SimpleNamespace(token="issued"), False, "issued"),
        ("expired", SimpleNamespace(token="issued"), True, "issued"),
        (None, SimpleNamespace(token="issued"), True, "issued"),
        ("configured", SimpleNamespace(token=None), True, "configured"),
        ("configured", None, True, "configured"),
        (None, SimpleNamespace(token=None), True, "stored"),
        (None, None, True, "stored"),
        (None, SimpleNamespace(token=None), False, None),
        (None, None, False, None),
    ],
)
def test_sender_token_prefers_receiver_then_existing_token_then_persistence(
    monkeypatch, sender_token, receiver, persist, expected,
):
    reads = []

    def load_token(host, port):
        reads.append((host, port))
        return "stored"

    monkeypatch.setattr(_client_utils, "load_token", load_token)
    sender = SimpleNamespace(token=sender_token)
    _client_lifecycle.prepare_sender_token(
        sender, receiver, host="test-host", port=7200, persist_token=persist,
    )
    assert sender.token == expected
    assert reads == ([("test-host", 7200)] if expected == "stored" else [])


@pytest.mark.parametrize("kind", [ManagedClient, SharedStageClient])
def test_update_schedules_handshake_without_waiting_or_touching_stage_in_worker(
    kind, tmp_path, monkeypatch
):
    stage = Usd.Stage.CreateNew(str(tmp_path / "scene.usda"))
    client = kind(stage, app_name="background-connect-test", persist_token=False)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    owner = threading.get_ident()
    threads = []

    def connect(*args, **kwargs):
        threads.append(threading.get_ident())
        entered.set()
        try:
            assert release.wait(5)
            raise OSError("injected connection failure")
        finally:
            finished.set()

    monkeypatch.setattr(sender_module.socket, "create_connection", connect)
    client._started = True
    client._receiver.connected = True
    client._receiver.layered_replay_active = True
    if isinstance(client, SharedStageClient):
        client._graph._ready = True
    try:
        result = client.update()
        assert entered.wait(2)
        assert not finished.is_set()
        assert result.submitted_events == 0
        assert len(threads) == 1
        assert threads[0] != owner
        client.update()
        assert len(threads) == 1
    finally:
        release.set()
        client.close()
        if entered.is_set():
            assert finished.wait(2)


@pytest.mark.parametrize("kind", [ManagedClient, UsdPublisher])
def test_flush_shares_timeout_between_reconnect_and_acknowledgement(kind, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(_client_lifecycle.time, "monotonic", lambda: clock[0])
    client = kind(Usd.Stage.CreateInMemory(), app_name="flush-budget", persist_token=False)
    calls = []

    def connect(timeout=None):
        calls.append(("connect", timeout))
        clock[0] += 0.75
        return True

    def flush(timeout=None):
        calls.append(("flush", timeout))
        return True

    monkeypatch.setattr(client._sender, "connect", connect)
    monkeypatch.setattr(client._sender, "flush", flush)
    client._transform_coalescing = SimpleNamespace(buffering=True, force=lambda emitter: [])
    if isinstance(client, ManagedClient):
        client._receiver.connected = True
        client._receiver._synchronized_event.set()
    try:
        assert client.flush(timeout=1.0)
        assert calls == [("connect", 1.0), ("flush", 0.25)]
    finally:
        client.close()


def test_publisher_accepts_host_owned_token_and_metadata_callbacks():
    tokens, metadata = [], []
    with UsdPublisher(
        Usd.Stage.CreateInMemory(),
        app_name="host-credentials",
        client_id="host-id",
        persist_token=False,
        on_token_issued=tokens.append,
        on_stage_metadata=metadata.append,
    ) as client:
        client.sender._on_token_issued("issued-token")
        client.sender._on_stage_metadata({"metersPerUnit": 0.01})
        assert client.client_id == "host-id"
    assert tokens == ["issued-token"]
    assert metadata == [{"metersPerUnit": 0.01}]


def test_receiver_exposes_identity_and_current_delivery_sequence():
    client = UsdReceiver(
        Usd.Stage.CreateInMemory(), app_name="viewer", client_id="viewer-id", persist_token=False
    )
    try:
        assert client.client_id == "viewer-id"
        assert client.applying_seq == client.dispatcher.applying_seq
    finally:
        client.close()
