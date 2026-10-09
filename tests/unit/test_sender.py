"""EventSender's wrapper contract, exercised against a live server."""

import gc
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from contextlib import nullcontext as does_not_raise

import pytest
from pxr import Sdf, Usd, UsdGeom

from openusdconnect._client_utils import ClientCredential
from openusdconnect.checkpoints import MirrorCheckpoint
from openusdconnect.codec import message_to_dict
from openusdconnect.protocol import make_claim_playback, make_playback_control
from openusdconnect.protocol_constants import LayerMode
from openusdconnect.recovery import RejectionDisposition
from openusdconnect.sdf_spec_delta import serialize_spec_fields
from openusdconnect.sender import EventSender, TransactionRejectedError
from openusdconnect.server import connection
from tests.helpers import embedded_server, ensure_prim_event, recorded_hellos, wait_until

METADATA = {"timeCodesPerSecond": 24.0, "upAxis": "Z"}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """A managed server that requires tokens and whose base authors stage metadata."""
    base = tmp_path_factory.mktemp("sender") / "base.usda"
    stage = Usd.Stage.CreateNew(str(base))
    stage.SetTimeCodesPerSecond(METADATA["timeCodesPerSecond"])
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    stage.GetRootLayer().Save()
    with embedded_server(base_usd_path=str(base), require_token=True) as runtime:
        yield runtime


@pytest.fixture(scope="module")
def shared_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("sender-shared") / "root.usda"
    Sdf.Layer.CreateNew(str(root)).Save()
    with embedded_server(base_usd_path=str(root), layer_mode=LayerMode.SHARED_STAGE) as runtime:
        yield runtime


@pytest.fixture(scope="module")
def limited_server():
    """Admits one transaction per connection at once, then five per second."""
    with embedded_server(txn_rate=5.0, txn_burst=1) as runtime:
        yield runtime


@pytest.fixture
def senders():
    """Build senders with fresh identities that are disconnected when the test ends."""
    created = []

    def make(port, **options):
        sender = EventSender("127.0.0.1", port, **{"client_id": uuid.uuid4().hex, **options})
        created.append(sender)
        return sender

    yield make
    for sender in created:
        sender.disconnect()


@contextmanager
def silent_listener():
    """A port that completes TCP connections and never answers a Hello."""
    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.settimeout(5)
        yield listener


def _port(runtime) -> int:
    return runtime.server_address[1]


def _committed_through(runtime, sender) -> int:
    return runtime.sync_server.producer_committed_through(sender.client_id, sender.session_id)


def _spec_event(path):
    source = Sdf.Layer.CreateAnonymous()
    Sdf.CreatePrimInLayer(source, path).specifier = Sdf.SpecifierDef
    return {
        "k": "set_sdf_spec_fields",
        "prim": path,
        "spec_path": path,
        "spec_kind": "prim",
        "fields": ["specifier"],
        "fragment": serialize_spec_fields(
            source,
            path,
            "prim",
            ["specifier"],
            stabilize_asset_paths=False,
        ),
        "removed": False,
    }


@pytest.mark.parametrize(
    ("handshake_timeout", "outcome"), [(0, pytest.raises(ValueError)), (1, does_not_raise())]
)
def test_invalid_settings_raise_value_error(handshake_timeout, outcome):
    with outcome:
        EventSender("127.0.0.1", 7300, client_id="client", handshake_timeout=handshake_timeout)


def test_settings_read_back_and_state_starts_empty():
    sender = EventSender(
        "127.0.0.1",
        7300,
        client_id="client",
        origin="origin",
        department="layout",
        token="token",
        handshake_timeout=2.5,
        session_id="session",
        max_pending_transactions=7,
    )
    assert (sender.host, sender.port, sender.client_id, sender.role) == (
        "127.0.0.1",
        7300,
        "client",
        "emitter",
    )
    assert (sender.origin, sender.department, sender.token) == ("origin", "layout", "token")
    assert (sender.handshake_timeout, sender.max_pending_transactions) == (2.5, 7)
    assert sender.session_id == "session"
    assert sender.layer_mode is LayerMode.MANAGED
    with pytest.raises(AttributeError):
        sender.host = "elsewhere"
    sender.token = "assigned"
    assert sender.token == "assigned"
    assert len(EventSender("127.0.0.1", 7300, client_id="client").session_id) == 32

    assert not sender.connected and not sender.is_connected
    assert sender.layer_mode_active is LayerMode.MANAGED
    assert sender.stage_metadata == {}
    assert not sender.auth_rejected and not sender.hello_rejected
    assert sender.rejection_reason == ""
    assert (sender.pending_transaction_count, sender.pending_event_count) == (0, 0)
    assert (sender.acknowledged_transaction_count, sender.acknowledged_event_count) == (0, 0)
    assert sender.drain_acknowledged_event_count() == 0
    assert sender.acknowledged_checkpoint is None
    assert sender.transaction_failure is None and sender.transaction_error == ""
    assert not sender.recovery_required and sender.recovery_disposition is None
    assert sender.recovery_incident is None and sender.recovery_artifact is None
    assert sender.cancel_connect()
    sender.disconnect()
    # Nothing is pending, so there is nothing to wait for.
    assert sender.flush(timeout=None)


def test_calls_that_need_a_connection_or_a_failure_refuse_without_one():
    sender = EventSender("127.0.0.1", 7300, client_id="client")
    event = ensure_prim_event("/A")
    assert not sender.send_events([])
    assert not sender.send_events([event])
    assert not sender.send_message(make_claim_playback("client"))
    assert not sender.claim_playback(1.0)
    assert not sender.send_playback_control("play")
    assert not sender.connect(timeout=0)
    with pytest.raises(ValueError, match="transform fields"):
        sender.send_events([{"k": "set_xform_trs", "prim": "/World/X", "fields": ["bogus"]}])
    with pytest.raises(ValueError, match="must not be empty"):
        sender.repair_rejected_transaction([])
    with pytest.raises(RuntimeError, match="no rejected transaction"):
        sender.repair_rejected_transaction([event])
    with pytest.raises(ValueError, match="1-128 characters"):
        sender.abandon_rejected_session(session_id="s" * 129)
    with pytest.raises(RuntimeError, match="no rejected producer session"):
        sender.abandon_rejected_session()
    assert sender.pending_transaction_count == 0


def test_handshake_presents_the_identity_and_reads_state_through(
    senders, server, monkeypatch, caplog
):
    hellos = recorded_hellos(monkeypatch)
    calls = []

    def record(name):
        def callback(value):
            calls.append((name, value, threading.get_ident()))
            if name == "token":
                raise RuntimeError("injected callback failure")

        return callback

    sender = senders(
        _port(server),
        origin="origin",
        department="layout",
        session_id="identity-session",
        on_token_issued=record("token"),
        on_stage_metadata=record("metadata"),
    )
    assert sender.connect(timeout=5)
    wait_until(lambda: len(calls) == 2)
    assert [(name, value) for name, value, _thread in calls] == [
        ("token", sender.token),
        ("metadata", METADATA),
    ]
    assert sender.token
    assert threading.get_ident() not in {thread for _name, _value, thread in calls}
    assert "EventSender: on_token_issued callback failed" in caplog.text
    assert sender.connected and sender.stage_metadata == METADATA
    assert sender.layer_mode_active is LayerMode.MANAGED

    assert [hello["role"] for hello in hellos] == ["emitter"]
    hello = hellos[0]
    assert (hello["client_id"], hello["origin"], hello["department"]) == (
        sender.client_id,
        "origin",
        "layout",
    )
    assert hello["producer_session_id"] == "identity-session" and "token" not in hello
    state = server.sync_server

    def registered():
        with state.clients_lock:
            return any(
                (info.role, info.client_id) == ("emitter", sender.client_id)
                for info in state.clients.values()
            )

    wait_until(registered)


def test_token_provider_supplies_each_attempt_and_the_issued_token_is_presented(
    senders, server, monkeypatch, caplog
):
    hellos = recorded_hellos(monkeypatch)
    credential = ClientCredential("127.0.0.1", 0, None, persist=False)
    sender = senders(
        _port(server),
        token_provider=credential.current,
        on_token_issued=credential.issued,
    )
    assert sender.connect(timeout=5)
    wait_until(lambda: credential.token is not None)
    assert sender.token == credential.token
    sender.disconnect()
    assert sender.connect(timeout=5), sender.rejection_reason
    assert not sender.auth_rejected
    assert [hello.get("token") for hello in hellos] == [None, credential.token]

    def fail():
        raise RuntimeError("injected provider failure")

    failing = senders(_port(server), token_provider=fail)
    assert not failing.connect(timeout=5)
    assert "EventSender: token provider failed" in caplog.text
    assert len(hellos) == 2


def test_concurrent_submissions_commit_in_transaction_order(senders, server):
    sender = senders(_port(server))
    assert sender.connect(timeout=5)
    barrier = threading.Barrier(4)
    accepted = []

    def submit(worker):
        barrier.wait(timeout=5)
        for item in range(25):
            accepted.append(sender.send_events([ensure_prim_event(f"/Order{worker}_{item}")]))

    workers = [threading.Thread(target=submit, args=(index,)) for index in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    assert accepted == [True] * 100
    # The server refuses an ID out of order, so success proves the order.
    assert sender.flush(timeout=5)
    assert sender.transaction_failure is None
    assert _committed_through(server, sender) == 100
    assert (sender.pending_transaction_count, sender.pending_event_count) == (0, 0)
    assert (sender.acknowledged_transaction_count, sender.acknowledged_event_count) == (100, 100)
    assert sender.drain_acknowledged_event_count() == 100
    assert sender.drain_acknowledged_event_count() == 0


def test_a_transaction_above_the_frame_limit_is_refused(senders, server):
    sender = senders(_port(server))
    assert sender.connect(timeout=5)
    oversized = {
        "k": "set_connectable_input",
        "prim": "/World/Shader",
        "info_id": "",
        "inputs": {"large": "x" * (16 * 1024 * 1024)},
    }
    assert not sender.send_events([oversized])
    assert sender.pending_transaction_count == 0 and sender.connected


def test_acknowledged_checkpoint_names_the_servers_replay_position(senders, server):
    state = server.sync_server
    sender = senders(_port(server))
    assert sender.connect(timeout=5)
    assert sender.acknowledged_checkpoint is None
    assert sender.send_events([ensure_prim_event(f"/Checkpoint{uuid.uuid4().hex}")])
    assert sender.flush(timeout=5)
    epoch, head = state.get_replay_token()
    assert sender.acknowledged_checkpoint == MirrorCheckpoint(state.server_instance, epoch, head)
    # A Hello acknowledges no mirror position.
    sender.disconnect()
    assert sender.connect(timeout=5)
    assert sender.acknowledged_checkpoint is None


def test_flush_waits_out_the_rate_limit_and_replays_the_outbox(
    senders, limited_server, monkeypatch
):
    hellos = recorded_hellos(monkeypatch)
    sender = senders(_port(limited_server))
    assert sender.connect(timeout=5)
    started = time.monotonic()
    for name in ("First", "Second"):
        assert sender.send_events([ensure_prim_event(f"/{name}{uuid.uuid4().hex}")])
    assert sender.flush(timeout=5)
    # The server refused the second, so it was replayed on a second connection
    # once the retry window passed.
    assert time.monotonic() - started >= 0.1
    assert len(hellos) == 2
    assert _committed_through(limited_server, sender) == 2
    assert sender.transaction_failure is None


@pytest.mark.parametrize("cancel", ["cancel_connect", "disconnect"])
def test_cancelled_handshake_leaves_the_session_healthy(senders, server, monkeypatch, cancel):
    state = server.sync_server
    entered, release = threading.Event(), threading.Event()
    authenticate = state.authenticate

    def held(*args):
        entered.set()
        assert release.wait(5)
        return authenticate(*args)

    # The token is issued up front, since the held answer would issue it unseen.
    client_id = uuid.uuid4().hex
    sender = senders(_port(server), client_id=client_id, token=state.token_store.issue(client_id))
    monkeypatch.setattr(state, "authenticate", held)
    try:
        assert sender.request_connect()
        # The server has the Hello and holds its answer.
        assert entered.wait(5)
        assert not sender.request_connect()
        result = getattr(sender, cancel)()
        if cancel == "cancel_connect":
            assert result is False
            wait_until(sender.cancel_connect)
        assert not sender.connected
    finally:
        release.set()
    monkeypatch.setattr(state, "authenticate", authenticate)

    assert sender.connect(timeout=5)
    assert sender.send_events([ensure_prim_event(f"/After{uuid.uuid4().hex}")])
    assert sender.flush(timeout=5)
    assert sender.transaction_failure is None
    assert _committed_through(server, sender) == 1


def test_request_connect_makes_one_bounded_attempt():
    with silent_listener() as listener:
        sender = EventSender(*listener.getsockname(), client_id="bounded")
        started = time.monotonic()
        assert sender.request_connect(timeout=0.05)
        assert not sender.request_connect()
        peer, _ = listener.accept()
        with peer:
            peer.settimeout(5)
            assert message_to_dict(peer.recv(4096)[4:])["type"] == "hello"
            # The attempt ends at its deadline and closes the connection.
            assert peer.recv(1) == b""
        assert time.monotonic() - started < 1
        assert not sender.connected and not sender.auth_rejected and not sender.hello_rejected
        # Cancelling ends the backoff that the failed request started.
        wait_until(sender.cancel_connect)
        assert sender.request_connect()
        sender.disconnect()


def test_connect_never_waits_past_its_timeout_or_the_handshake_timeout():
    with silent_listener() as listener:
        sender = EventSender(*listener.getsockname(), client_id="slow", handshake_timeout=0.2)
        for timeout, budget in ((0.05, 0.05), (5, 0.2)):
            started = time.monotonic()
            assert not sender.connect(timeout=timeout)
            assert budget <= time.monotonic() - started < budget + 1
        started = time.monotonic()
        assert not sender.connect(timeout=0)
        assert time.monotonic() - started < 0.5


@pytest.mark.parametrize("rejection", ["auth", "hello", "empty"])
def test_rejection_is_reported_until_an_explicit_connect(senders, server, monkeypatch, rejection):
    if rejection == "auth":
        client_id = uuid.uuid4().hex
        issued = server.sync_server.token_store.issue(client_id)
        sender = senders(_port(server), client_id=client_id, token="not-issued")
        expected = (True, False, "invalid or missing token")
    else:
        if rejection == "empty":
            reject = connection.ConnectionHandler._reject_hello
            monkeypatch.setattr(
                connection.ConnectionHandler,
                "_reject_hello",
                lambda handler, code, _reason: reject(handler, code, ""),
            )
        sender = senders(_port(server), layer_mode=LayerMode.SHARED_STAGE)
        reason = (
            "connection rejected"
            if rejection == "empty"
            else "server uses 'managed' layer mode, client requested 'shared_stage'"
        )
        expected = (False, True, reason)
    assert not sender.connect(timeout=5)
    assert (sender.auth_rejected, sender.hello_rejected, sender.rejection_reason) == expected
    assert not sender.request_connect()
    assert not sender.connected
    if rejection == "auth":
        sender.token = issued
        assert sender.connect(timeout=5)
        assert not sender.auth_rejected and sender.rejection_reason == ""


def test_hello_highwater_contradiction_requires_abandoning_the_session(senders, server):
    first = senders(_port(server), session_id="shared-session")
    assert first.connect(timeout=5)
    assert first.send_events([ensure_prim_event(f"/Committed{uuid.uuid4().hex}")])
    assert first.flush(timeout=5)
    first.disconnect()

    # A second outbox claims the same producer session.
    second = senders(
        _port(server),
        client_id=first.client_id,
        session_id="shared-session",
        token=first.token,
    )
    assert not second.connect(timeout=5)
    failure = second.transaction_failure
    assert (failure.txn_id, failure.code_name) == (1, "unexpected_id")
    assert failure.reason == "server producer highwater 1 is ahead of local transaction 0"
    assert second.rejection_reason == failure.reason
    assert not second.auth_rejected and not second.hello_rejected
    assert second.recovery_required and second.acknowledged_checkpoint is None
    assert second.recovery_disposition is RejectionDisposition.SESSION_FATAL
    artifact = second.recovery_artifact
    assert artifact.producer_session_id == "shared-session" and artifact.transactions == ()
    assert second.recovery_incident.incident_id == "shared-session:1"
    with pytest.raises(TransactionRejectedError) as caught:
        second.flush(timeout=0)
    assert caught.value.failure is second.transaction_failure
    assert not second.connect(timeout=5)
    with pytest.raises(RuntimeError, match="unexpected_id is session_fatal, not recoverable"):
        second.repair_rejected_transaction([ensure_prim_event("/Repaired")])
    with pytest.raises(ValueError, match="must differ"):
        second.abandon_rejected_session(session_id="shared-session")

    assert second.abandon_rejected_session(session_id="fresh-session") is artifact
    assert second.session_id == "fresh-session"
    assert not second.recovery_required and second.recovery_artifact is None
    assert second.rejection_reason == ""
    assert second.connect(timeout=5)
    assert second.send_events([ensure_prim_event(f"/Fresh{uuid.uuid4().hex}")])
    assert second.flush(timeout=5)
    assert _committed_through(server, second) == 1


def test_recoverable_rejection_is_repaired_at_the_same_id(senders, shared_server):
    state = shared_server.sync_server
    root_key = state.shared_layer_graph.root_layer_key
    sender = senders(_port(shared_server), layer_mode=LayerMode.SHARED_STAGE)
    assert sender.connect(timeout=5)
    assert sender.send_events([_spec_event("/Stale")], layer_key="unmapped")
    assert sender.send_events([_spec_event("/Later")], layer_key=root_key)
    with pytest.raises(TransactionRejectedError, match="stale_layer_graph") as caught:
        sender.flush(timeout=5)

    failure = sender.transaction_failure
    assert caught.value.failure is failure and failure.txn_id == 1
    assert sender.transaction_error == str(failure)
    assert sender.recovery_disposition is RejectionDisposition.RECOVERABLE_CONFLICT
    assert sender.recovery_required and not sender.connected
    assert sender.pending_transaction_count == 2
    artifact = sender.recovery_artifact
    assert artifact is sender.recovery_artifact
    assert artifact.producer_session_id == sender.session_id
    assert [transaction.txn_id for transaction in artifact.transactions] == [1, 2]
    assert artifact.layer_keys == ("unmapped", root_key)
    quarantined = [message_to_dict(transaction.payload) for transaction in artifact.transactions]
    assert [(message["txn_id"], message["layer_key"]) for message in quarantined] == [
        (1, "unmapped"),
        (2, root_key),
    ]
    incident = sender.recovery_incident
    assert incident.incident_id == f"{sender.session_id}:1"
    assert (incident.transaction_ids, incident.event_count) == ((1, 2), 2)
    assert not sender.send_events([_spec_event("/Refused")], layer_key=root_key)
    assert not sender.connect(timeout=5)

    assert sender.repair_rejected_transaction([_spec_event("/Repaired")], layer_key=root_key) == 1
    assert sender.transaction_failure is None and sender.recovery_artifact is None
    assert sender.connect(timeout=5)
    assert sender.flush(timeout=5)
    assert _committed_through(shared_server, sender) == 2
    assert state.stage.GetPrimAtPath("/Repaired") and state.stage.GetPrimAtPath("/Later")
    assert not state.stage.GetPrimAtPath("/Stale")


def test_playback_messages_follow_the_connection(senders, server):
    state = server.sync_server
    sender = senders(_port(server))
    assert not sender.claim_playback(2.0)
    assert sender.connect(timeout=5)
    assert sender.claim_playback(2.0)
    wait_until(lambda: state.get_playback_state()["leader_client_id"] == sender.client_id)
    assert sender.send_playback_control("play")
    wait_until(lambda: state.get_playback_state()["playing"])
    assert sender.send_message(make_playback_control("pause"))
    wait_until(lambda: not state.get_playback_state()["playing"])
    sender.disconnect()
    assert not sender.send_playback_control("pause")
    wait_until(lambda: state.get_playback_state()["leader_client_id"] != sender.client_id)


def test_collecting_a_sender_stops_its_connection(server):
    state = server.sync_server
    sender = EventSender("127.0.0.1", _port(server), client_id=uuid.uuid4().hex)
    client_id = sender.client_id
    assert sender.connect(timeout=5)

    def connected():
        with state.clients_lock:
            return any(info.client_id == client_id for info in state.clients.values())

    wait_until(connected)
    del sender
    gc.collect()
    wait_until(lambda: not connected())
