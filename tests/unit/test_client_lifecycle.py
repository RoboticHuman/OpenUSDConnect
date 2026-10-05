"""Host-facing lifecycle guarantees shared by the high-level clients."""

import threading
from types import SimpleNamespace

import pytest
from pxr import Sdf, Usd

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
from openusdconnect.protocol_constants import LayerMode
from tests.helpers import RecordingObserver, connect_client, embedded_server, recorded_hellos


@pytest.fixture(scope="module")
def managed_server():
    with embedded_server() as runtime:
        yield runtime


@pytest.fixture(scope="module")
def shared_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("shared") / "root.usda"
    Sdf.Layer.CreateNew(str(root)).Save()
    with embedded_server(base_usd_path=str(root), layer_mode=LayerMode.SHARED_STAGE) as runtime:
        yield runtime


@pytest.fixture
def ports(managed_server, shared_server):
    """Live server ports by client kind; publishers connect nowhere in these tests."""
    return {
        ManagedClient: managed_server.server_address[1],
        SharedStageClient: shared_server.server_address[1],
        UsdPublisher: 1,
    }


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


def test_each_phase_outranks_the_phases_after_it():
    flags = [
        "closed", "recovery_required", "rejected", "parked", "replaying", "ready", "connecting",
    ]
    for index, flag in enumerate(flags):
        state = {name: position >= index for position, name in enumerate(flags)}
        assert _client_lifecycle.compute_phase(**state) is client_types.ClientPhase(flag)
    offline = _client_lifecycle.compute_phase(**dict.fromkeys(flags, False))
    assert offline is client_types.ClientPhase.OFFLINE


@pytest.mark.parametrize("kind", [ManagedClient, SharedStageClient, UsdReceiver, UsdPublisher])
def test_every_client_reports_the_same_lifecycle_phases(kind, tmp_path):
    stage = Usd.Stage.CreateNew(str(tmp_path / "scene.usda"))
    client = kind(stage, app_name="phases", port=1, persist_token=False)
    try:
        assert client.status.phase is client_types.ClientPhase.OFFLINE
        assert not client.status.connected
        client.start()
        assert client.status.phase is client_types.ClientPhase.CONNECTING
    finally:
        client.close()
    assert client.status.phase is client_types.ClientPhase.CLOSED
    with pytest.raises(RuntimeError, match=f"{kind.__name__} is closed"):
        client.update()


def test_waits_raise_when_nothing_will_reconnect():
    publisher = UsdPublisher(
        Usd.Stage.CreateInMemory(), app_name="paused", port=1, persist_token=False,
    )
    receiver = UsdReceiver(
        Usd.Stage.CreateInMemory(), app_name="one-shot", port=1, persist_token=False,
        reconnect=False,
    )
    try:
        publisher.start()
        publisher.disconnect()
        receiver.start()
        receiver.receiver.join(timeout=5)
        for client in (publisher, receiver):
            assert client.status.phase is client_types.ClientPhase.OFFLINE
            with pytest.raises(ConnectionError, match="offline"):
                client.wait_until_ready(timeout=5)
        with pytest.raises(ConnectionError, match="offline"):
            publisher.submit_and_wait(timeout=5)
    finally:
        publisher.close()
        receiver.close()


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
    callback("queued")
    notifications.close()
    callback("late")
    notifications.drain()
    assert [value for value, _thread in received] == ["first", "next-tick", "queued"]


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


def test_credential_reads_storage_only_while_no_token_is_known(monkeypatch):
    stored = [None]
    reads = []

    def load_token(host, port):
        reads.append((host, port))
        return stored[0]

    monkeypatch.setattr(_client_utils, "load_token", load_token)
    credential = _client_utils.ClientCredential("test-host", 7200, None, True)
    assert credential.current() is None
    stored[0] = "stored"
    assert credential.current() == "stored"
    reads.clear()
    assert credential.current() == "stored"
    assert _client_utils.ClientCredential("test-host", 7200, None, False).current() is None
    assert reads == []


def test_credential_keeps_a_token_issued_while_storage_loads(monkeypatch):
    monkeypatch.setattr(_client_utils, "save_token", lambda host, port, token: None)
    monkeypatch.setattr(_client_utils, "load_token", lambda host, port: None)
    credential = _client_utils.ClientCredential("localhost", 1, None, True)
    loading, release = threading.Event(), threading.Event()

    def slow_load(host, port):
        loading.set()
        assert release.wait(5)
        return "old-stored-token"

    monkeypatch.setattr(_client_utils, "load_token", slow_load)
    reader = threading.Thread(target=credential.current)
    reader.start()
    assert loading.wait(5)
    issuer = threading.Thread(target=credential.issued, args=("issued-during-load",))
    issuer.start()
    release.set()
    reader.join(5)
    issuer.join(5)
    assert credential.current() == "issued-during-load"


def test_sender_takes_its_token_from_the_provider_on_every_attempt(monkeypatch, managed_server):
    hellos = recorded_hellos(monkeypatch)
    tokens = iter(["first", "second"])
    sender = sender_module.EventSender(
        "127.0.0.1", managed_server.server_address[1], client_id="token-attempts",
        token="stale", token_provider=lambda: next(tokens),
    )
    for expected in ("first", "second"):
        assert sender.connect(timeout=5)
        assert sender.token == expected
        sender.disconnect()
    assert [hello["token"] for hello in hellos] == ["first", "second"]


@pytest.mark.parametrize("failure", [None, "persistence", "observer"])
def test_issued_token_is_persisted_before_notifying_and_used_by_both_roles(
    failure, tmp_path, monkeypatch,
):
    calls = []

    def record(name, token):
        calls.append((name, token))
        if failure == name:
            raise RuntimeError(f"injected {name} failure")

    monkeypatch.setattr(
        _client_utils, "save_token", lambda host, port, token: record("persistence", token),
    )
    stage = Usd.Stage.CreateNew(str(tmp_path / "scene.usda"))
    observer = RecordingObserver(on_call=lambda name, token: record("observer", token))
    client = ManagedClient(
        stage, app_name="shared-credentials", token="configured", persist_token=True,
        observer=observer,
    )
    try:
        callback = client._sender._on_token_issued
        if failure == "persistence":
            with pytest.raises(RuntimeError, match="injected persistence failure"):
                callback("replacement")
        else:
            callback("replacement")
        # The host observer is queued; the token is adopted and persisted first.
        assert calls == [("persistence", "replacement")]
        for endpoint in (client._sender, client._receiver):
            assert endpoint._token_provider() == "replacement"

        if failure != "persistence":
            if failure == "observer":
                with pytest.raises(RuntimeError, match="injected observer failure"):
                    client._callbacks.drain()
            else:
                client._callbacks.drain()
            assert calls[-1] == ("observer", "replacement")
    finally:
        client.close()


@pytest.mark.parametrize("kind", [ManagedClient, SharedStageClient, UsdPublisher])
def test_disconnected_update_does_no_token_io(kind, tmp_path, monkeypatch, ports):
    reads = []
    monkeypatch.setattr(_client_utils, "load_token", lambda host, port: reads.append(1))
    requests = []
    monkeypatch.setattr(
        sender_module.EventSender, "request_connect",
        lambda self, timeout=2.0: requests.append(True) or True,
    )
    stage = Usd.Stage.CreateNew(str(tmp_path / "scene.usda"))
    client = kind(stage, app_name="token-io", port=ports[kind], persist_token=True)
    try:
        if kind is UsdPublisher:
            client.start()
        else:
            connect_client(client)
        reads.clear()
        for _ in range(20):
            client.update()
        assert requests
        assert reads == []
    finally:
        client.close()


@pytest.mark.parametrize("kind", [ManagedClient, SharedStageClient])
def test_update_schedules_handshake_without_waiting_or_touching_stage_in_worker(
    kind, tmp_path, monkeypatch, ports
):
    stage = Usd.Stage.CreateNew(str(tmp_path / "scene.usda"))
    client = kind(
        stage, app_name="background-connect-test", port=ports[kind], persist_token=False,
    )
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    owner = threading.get_ident()
    threads = []

    def provide():
        # The handshake waits here, after the connection opened.
        threads.append(threading.get_ident())
        entered.set()
        try:
            assert release.wait(5)
            raise OSError("injected connection failure")
        finally:
            finished.set()

    try:
        connect_client(client)
        monkeypatch.setattr(client._sender, "_token_provider", provide)
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
def test_flush_shares_timeout_between_reconnect_and_acknowledgement(kind, monkeypatch, ports):
    clock = [10.0]
    calls = []

    def connect(timeout=None):
        calls.append(("connect", timeout))
        clock[0] += 0.75
        return True

    def flush(timeout=None):
        calls.append(("flush", timeout))
        return True

    client = kind(
        Usd.Stage.CreateInMemory(), app_name="flush-budget", port=ports[kind],
        persist_token=False,
    )
    try:
        if isinstance(client, ManagedClient):
            connect_client(client)
        else:
            monkeypatch.setattr(client, "_is_synchronized", lambda: True)
        monkeypatch.setattr(_client_lifecycle.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(client._sender, "connect", connect)
        monkeypatch.setattr(client._sender, "flush", flush)
        client._transform_coalescing = SimpleNamespace(buffering=True, force=lambda emitter: [])
        assert client.flush(timeout=1.0)
        assert calls == [("connect", 1.0), ("flush", 0.25)]
    finally:
        client.close()


@pytest.mark.parametrize(
    ("phase", "sender_connected", "edit_target_is_published", "expected"),
    [
        ("ready", True, None, True),
        ("ready", True, False, False),
        ("ready", None, None, False),
        ("replaying", True, True, False),
    ],
)
def test_can_author_combines_readiness_role_and_edit_target(
    phase, sender_connected, edit_target_is_published, expected,
):
    status = client_types.ClientStatus(
        phase=client_types.ClientPhase(phase),
        connected=True,
        synchronized=True,
        sender_connected=sender_connected,
        edit_target_is_published=edit_target_is_published,
    )
    assert status.can_author is expected


@pytest.mark.parametrize("kind", [ManagedClient, SharedStageClient, UsdReceiver, UsdPublisher])
def test_close_delivers_notifications_queued_by_network_threads(kind, tmp_path):
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
    finally:
        client.close()
    # A host that stores tokens itself must receive one issued just before close.
    assert observer.calls == [
        ("token_issued", "issued", threading.get_ident()),
        ("stage_metadata", StageMetadata(up_axis="Y"), threading.get_ident()),
    ]
