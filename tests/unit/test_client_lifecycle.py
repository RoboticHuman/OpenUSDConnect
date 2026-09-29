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


def test_wait_until_ready_pumps_replay_before_success(monkeypatch):
    phases = iter([client_types.ClientPhase.REPLAYING, client_types.ClientPhase.READY])
    client = SimpleNamespace(start=lambda: None)
    ticks = []

    def update():
        ticks.append(threading.get_ident())
        client.status = SimpleNamespace(phase=next(phases), failure=None)

    client.update = update
    monkeypatch.setattr(_client_lifecycle.time, "sleep", lambda seconds: None)
    assert _client_lifecycle.wait_until_ready(client, timeout=1)
    assert ticks == [threading.get_ident()] * 2


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
@pytest.mark.parametrize("helper", ["wait_until_ready", "submit_and_wait"])
def test_blocking_helpers_raise_for_states_updates_cannot_resolve(
    helper, phase, auth_rejected, failure, expected,
):
    client = SimpleNamespace(
        start=lambda: None,
        update=lambda: None,
        auth_rejected=auth_rejected,
        status=SimpleNamespace(
            phase=client_types.ClientPhase(phase), failure=failure, reason="",
        ),
    )
    with pytest.raises(expected):
        getattr(_client_lifecycle, helper)(client, timeout=0)


def test_submit_and_wait_does_not_mistake_empty_outbox_for_finished_edits(monkeypatch):
    states = iter([
        (client_types.ClientPhase.REPLAYING, True, 0),
        (client_types.ClientPhase.READY, True, 0),
        (client_types.ClientPhase.READY, False, 2),
        (client_types.ClientPhase.READY, False, 0),
    ])
    client = SimpleNamespace(start=lambda: None)
    ticks = []
    flushes = []

    def update():
        phase, unsent, pending = next(states)
        ticks.append(phase)
        client.status = SimpleNamespace(phase=phase, failure=None)
        client.has_unsent_changes = unsent
        client.pending_event_count = pending

    def flush(timeout):
        flushes.append(timeout)
        return client.pending_event_count == 0

    client.update = update
    client.flush = flush
    monkeypatch.setattr(_client_lifecycle.time, "sleep", lambda seconds: None)
    assert _client_lifecycle.submit_and_wait(client, timeout=1)
    assert len(ticks) == 4
    assert flushes == [0, 0, 0]


def test_submit_and_wait_times_out_with_unsent_work():
    client = SimpleNamespace(
        start=lambda: None,
        update=lambda: None,
        flush=lambda timeout: True,
        status=SimpleNamespace(phase=client_types.ClientPhase.READY, failure=None),
        has_unsent_changes=True,
        pending_event_count=0,
    )
    assert not _client_lifecycle.submit_and_wait(client, timeout=0)


def test_submit_and_wait_surfaces_transaction_rejection():
    from openusdconnect import TransactionFailure, TransactionRejectedError

    failure = TransactionFailure(1, 0, "rejected edit")
    client = SimpleNamespace(
        start=lambda: None,
        update=lambda: None,
        status=SimpleNamespace(phase=client_types.ClientPhase.RECOVERY_REQUIRED, failure=failure),
    )
    with pytest.raises(TransactionRejectedError) as exc:
        _client_lifecycle.submit_and_wait(client, timeout=0)
    assert exc.value.failure is failure


def test_queued_notifications_run_on_update_thread_and_bound_each_drain():
    notifications = _client_lifecycle.ClientCallbackQueue(True)
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
    notifications = _client_lifecycle.ClientCallbackQueue(True)
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


@pytest.mark.parametrize("kind", [ManagedClient, SharedStageClient])
def test_client_queues_host_notifications_but_shares_credentials_immediately(kind, tmp_path):
    stage = Usd.Stage.CreateNew(str(tmp_path / "scene.usda"))
    calls = []
    client = kind(
        stage,
        app_name="queued-callbacks",
        persist_token=False,
        callbacks_on_update=True,
        on_token_issued=lambda value: calls.append(("token", value, threading.get_ident())),
        on_stage_metadata=lambda value: calls.append(("metadata", value, threading.get_ident())),
    )
    try:
        client._started = True

        def receive():
            client._receiver._on_token_issued("issued")
            client._receiver._on_stage_metadata({"up_axis": "Y"})

        worker = threading.Thread(target=receive)
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert client._sender.token == client._receiver.token == "issued"
        assert calls == []
        client.update()
        assert calls == [
            ("token", "issued", threading.get_ident()),
            ("metadata", {"up_axis": "Y"}, threading.get_ident()),
        ]
    finally:
        client.close()


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
        ("current", SimpleNamespace(token="stale"), False, "current"),
        ("current", SimpleNamespace(token="stale"), True, "current"),
        (None, SimpleNamespace(token="issued"), True, "issued"),
        ("configured", SimpleNamespace(token=None), True, "configured"),
        ("configured", None, True, "configured"),
        (None, SimpleNamespace(token=None), True, "stored"),
        (None, None, True, "stored"),
        (None, SimpleNamespace(token=None), False, None),
        (None, None, False, None),
    ],
)
def test_sender_token_preparation_only_fills_missing_credentials(
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
    client = kind(
        stage, app_name="shared-credentials", token="configured", persist_token=True,
        callbacks_on_update=False,
        on_token_issued=lambda token: record("observer", token),
    )
    try:
        callback = getattr(client, issuer)._on_token_issued
        if failure is None:
            callback("replacement")
        else:
            with pytest.raises(RuntimeError, match=f"injected {failure} failure"):
                callback("replacement")

        assert client._sender.token == client._receiver.token == "replacement"
        expected = ["persistence"] if failure == "persistence" else ["persistence", "observer"]
        assert calls == [(name, "replacement", "replacement", "replacement") for name in expected]
    finally:
        client.close()


def test_token_issued_while_loading_credentials_is_not_overwritten(monkeypatch):
    sender = SimpleNamespace(token=None)

    def load_token(host, port):
        sender.token = "issued-during-load"
        return "old-stored-token"

    monkeypatch.setattr(_client_utils, "load_token", load_token)
    _client_lifecycle.prepare_sender_token(
        sender, None, host="test-host", port=7200, persist_token=True,
    )
    assert sender.token == "issued-during-load"


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


@pytest.mark.parametrize("callbacks_on_update", [False, True])
def test_publisher_accepts_host_owned_token_and_metadata_callbacks(callbacks_on_update):
    tokens, metadata = [], []
    with UsdPublisher(
        Usd.Stage.CreateInMemory(),
        app_name="host-credentials",
        client_id="host-id",
        persist_token=False,
        callbacks_on_update=callbacks_on_update,
        on_token_issued=tokens.append,
        on_stage_metadata=metadata.append,
    ) as client:
        client.sender._on_token_issued("issued-token")
        client.sender._on_stage_metadata({"metersPerUnit": 0.01})
        assert client.client_id == "host-id"
        if callbacks_on_update:
            assert tokens == metadata == []
            client._callbacks.drain()
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


def test_receiver_delivers_transport_notifications_during_update():
    calls = []
    client = UsdReceiver(
        Usd.Stage.CreateInMemory(),
        app_name="queued-receiver",
        persist_token=False,
        reconnect=False,
        on_stage_metadata=lambda value: calls.append((value, threading.get_ident())),
    )
    try:
        client._started = True
        worker = threading.Thread(target=lambda: client._receiver._on_stage_metadata({"up": "Z"}))
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert calls == []
        assert client.update().applied_events == 0
        assert calls == [({"up": "Z"}, threading.get_ident())]
    finally:
        client.close()
