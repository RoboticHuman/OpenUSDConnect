"""Host-facing lifecycle guarantees shared by the high-level clients."""

import threading
from types import SimpleNamespace

import pytest
from pxr import Usd

from openusdconnect import (
    ManagedClient,
    SharedStageClient,
    TransactionFailure,
    TransactionRejectedError,
    UsdPublisher,
    UsdReceiver,
    _client_lifecycle,
    _client_utils,
    client_types,
)
from openusdconnect import sender as sender_module
from openusdconnect.client_observer import StageMetadata
from tests.helpers import RecordingObserver


def test_wait_until_ready_returns_false_only_when_startup_expires():
    client = SimpleNamespace(
        start=lambda: None,
        update=lambda: None,
        status=SimpleNamespace(phase=client_types.ClientPhase.REPLAYING, failure=None),
    )
    assert not _client_lifecycle.wait_until_ready(client, timeout=0)


@pytest.mark.parametrize(
    ("phase", "auth_rejected", "failure", "expected"),
    [
        ("rejected", True, None, PermissionError),
        ("rejected", False, None, ConnectionError),
        (
            "recovery_required", False, TransactionFailure(1, 0, "rejected"),
            TransactionRejectedError,
        ),
        ("recovery_required", False, None, RuntimeError),
        ("parked", False, None, RuntimeError),
        ("closed", False, None, RuntimeError),
    ],
)
def test_blocked_states_raise_instead_of_timing_out(phase, auth_rejected, failure, expected):
    status = SimpleNamespace(
        phase=client_types.ClientPhase(phase), failure=failure, reason="",
        auth_rejected=auth_rejected,
    )
    with pytest.raises(expected):
        _client_lifecycle.raise_if_blocked(SimpleNamespace(), status)


def test_queued_notifications_run_on_update_thread_and_bound_each_drain():
    notifications = _client_lifecycle.ClientCallbackQueue()
    received = []

    def observe(value):
        received.append((value, threading.get_ident()))
        if value == "first":
            callback("next-tick")

    callback = notifications.wrap(observe)
    worker = threading.Thread(target=lambda: callback("first"))
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert received == []
    notifications.drain()
    assert received == [("first", threading.get_ident())]
    notifications.drain()
    assert received == [("first", threading.get_ident()), ("next-tick", threading.get_ident())]
    callback("discarded")
    notifications.close()
    callback("late")
    notifications.drain()
    assert len(received) == 2


def test_queued_notification_failure_propagates_and_keeps_later_notifications():
    notifications = _client_lifecycle.ClientCallbackQueue()
    received = []

    def fail(value):
        raise RuntimeError("observer failed")

    notifications.wrap(fail)(None)
    notifications.wrap(received.append)("next")
    with pytest.raises(RuntimeError, match="observer failed"):
        notifications.drain()
    assert received == []
    notifications.drain()
    assert received == ["next"]


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
    ("receiver_token", "persist", "expected"),
    [
        ("issued", True, "issued"),
        ("issued", False, "issued"),
        (None, True, "stored"),
        (None, False, None),
    ],
)
def test_sender_token_provider_prefers_receiver_then_storage(
    monkeypatch, receiver_token, persist, expected,
):
    reads = []

    def load_token(host, port):
        reads.append((host, port))
        return "stored"

    monkeypatch.setattr(_client_utils, "load_token", load_token)
    provide = _client_lifecycle.sender_token_provider(
        lambda: receiver_token, host="test-host", port=7200, persist_token=persist,
    )
    assert provide() == expected
    assert reads == ([("test-host", 7200)] if expected == "stored" else [])


@pytest.mark.parametrize(
    ("configured", "expected"), [("configured", "configured"), (None, "provided")],
)
def test_sender_asks_provider_only_for_missing_credentials(configured, expected):
    calls = []

    def provide():
        calls.append(True)
        return "provided"

    sender = sender_module.EventSender(
        "localhost", 1, client_id="token-fill", token=configured, token_provider=provide,
    )
    sender._fill_missing_token()
    assert sender.token == expected
    assert calls == ([] if configured else [True])


@pytest.mark.parametrize("kind", [ManagedClient, SharedStageClient])
@pytest.mark.parametrize("issuer", ["_sender", "_receiver"])
@pytest.mark.parametrize("failure", [None, "persistence", "observer"])
def test_issued_token_updates_both_connections_before_callbacks(
    kind, issuer, failure, tmp_path, monkeypatch,
):
    calls = []

    def record(name, token):
        calls.append((name, token, client._sender.token, client._receiver.token))
        if failure == name:
            raise RuntimeError(f"injected {name} failure")

    monkeypatch.setattr(
        _client_utils, "save_token", lambda host, port, token: record("persistence", token),
    )
    stage = Usd.Stage.CreateNew(str(tmp_path / "scene.usda"))
    observer = RecordingObserver(on_call=lambda name, token: record("observer", token))
    client = kind(
        stage, app_name="shared-credentials", token="configured", persist_token=True,
        observer=observer,
    )
    try:
        callback = getattr(client, issuer)._on_token_issued
        if failure == "persistence":
            with pytest.raises(RuntimeError, match="injected persistence failure"):
                callback("replacement")
        else:
            callback("replacement")
        assert client._sender.token == client._receiver.token == "replacement"
        # The host observer is queued; credentials are shared and persisted first.
        assert calls == [("persistence", "replacement", "replacement", "replacement")]

        if failure != "persistence":
            client._started = True
            if failure == "observer":
                with pytest.raises(RuntimeError, match="injected observer failure"):
                    client._callbacks.drain()
            else:
                client._callbacks.drain()
            assert calls[-1] == ("observer", "replacement", "replacement", "replacement")
    finally:
        client.close()


def test_token_issued_while_provider_reads_storage_is_not_overwritten():
    def provide():
        sender.token = "issued-during-load"
        return "old-stored-token"

    sender = sender_module.EventSender(
        "localhost", 1, client_id="token-race", token_provider=provide,
    )
    sender._fill_missing_token()
    assert sender.token == "issued-during-load"


@pytest.mark.parametrize("kind", [ManagedClient, SharedStageClient, UsdPublisher])
def test_disconnected_update_does_no_token_io(kind, tmp_path, monkeypatch):
    reads = []
    monkeypatch.setattr(_client_utils, "load_token", lambda host, port: reads.append(1))
    requests = []
    monkeypatch.setattr(
        sender_module.EventSender, "request_connect",
        lambda self, timeout=2.0: requests.append(True) or True,
    )
    stage = Usd.Stage.CreateNew(str(tmp_path / "scene.usda"))
    client = kind(stage, app_name="token-io", port=1, persist_token=True)
    try:
        client._started = True
        if kind is not UsdPublisher:
            client._receiver.connected = True
            client._receiver.layered_replay_active = True
        if kind is SharedStageClient:
            client._graph._ready = True
        reads.clear()
        for _ in range(20):
            client.update()
        assert requests
        assert reads == []
    finally:
        client.close()


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


def test_receiver_exposes_identity_and_current_delivery_sequence():
    client = UsdReceiver(
        Usd.Stage.CreateInMemory(), app_name="viewer", client_id="viewer-id", persist_token=False
    )
    try:
        assert client.client_id == "viewer-id"
    finally:
        client.close()


@pytest.mark.parametrize(
    ("phase", "sender_connected", "edit_target_is_shared", "expected"),
    [
        ("ready", True, True, True),
        ("ready", True, None, True),
        ("ready", True, False, False),
        ("ready", None, None, False),
        ("replaying", True, True, False),
        ("recovery_required", True, True, False),
    ],
)
def test_can_author_combines_readiness_role_and_edit_target(
    phase, sender_connected, edit_target_is_shared, expected,
):
    status = client_types.ClientStatus(
        phase=client_types.ClientPhase(phase),
        connected=True,
        synchronized=True,
        sender_connected=sender_connected,
        edit_target_is_shared=edit_target_is_shared,
    )
    assert status.can_author is expected


@pytest.mark.parametrize("kind", [ManagedClient, SharedStageClient, UsdReceiver, UsdPublisher])
def test_every_client_delivers_notifications_on_the_draining_thread(kind, tmp_path):
    observer = RecordingObserver()
    stage = Usd.Stage.CreateNew(str(tmp_path / "scene.usda"))
    client = kind(stage, app_name="notifications", persist_token=False, observer=observer)
    endpoint = client._sender if kind is UsdPublisher else client._receiver
    try:
        worker = threading.Thread(target=lambda: (
            endpoint._on_token_issued("issued"),
            endpoint._on_stage_metadata({"upAxis": "Y"}),
        ))
        worker.start()
        worker.join(timeout=1)
        assert observer.calls == []
        client._callbacks.drain()
        assert observer.calls == [
            ("token_issued", "issued", threading.get_ident()),
            ("stage_metadata", StageMetadata(up_axis="Y"), threading.get_ident()),
        ]
    finally:
        client.close()
