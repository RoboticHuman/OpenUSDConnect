"""EventReceiver's wrapper contract, exercised against a live server."""

import gc
import socket
import threading
import time
import uuid
from contextlib import nullcontext as does_not_raise

import pytest
from pxr import Usd, UsdGeom

from openusdconnect import _client_backend
from openusdconnect._client_utils import ClientCredential
from openusdconnect.codec import HelloRejectionCode, message_to_dict
from openusdconnect.protocol_constants import (
    MSG_PLAYBACK_CLAIMED,
    MSG_PLAYBACK_REJECTED,
    MSG_PLAYBACK_STATE,
    LayerMode,
)
from openusdconnect.receiver import EventReceiver
from tests.helpers import client_registered, embedded_server, ensure_prim_event, wait_until

FAST = {"reconnect_base_delay": 0.01, "reconnect_max_delay": 0.04}
METADATA = {"timeCodesPerSecond": 24.0, "upAxis": "Z"}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """A server that requires tokens and whose base authors stage metadata."""
    base = tmp_path_factory.mktemp("receiver") / "base.usda"
    stage = Usd.Stage.CreateNew(str(base))
    stage.SetTimeCodesPerSecond(METADATA["timeCodesPerSecond"])
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    stage.GetRootLayer().Save()
    with embedded_server(base_usd_path=str(base), require_token=True) as runtime:
        yield runtime


@pytest.fixture
def receivers(server):
    """Build receivers of the server that are closed when the test ends."""
    created = []

    def make(**options):
        receiver = EventReceiver(
            **{
                "host": "127.0.0.1",
                "port": server.server_address[1],
                "client_id": uuid.uuid4().hex,
                **FAST,
                **options,
            }
        )
        created.append(receiver)
        return receiver

    yield make
    for receiver in created:
        assert receiver.close(5)


def _commit(server, count):
    """Commit new prims, which receivers that connect later replay, and return the head."""
    state = server.sync_server
    state._commit_events([ensure_prim_event(f"/P{uuid.uuid4().hex}") for _ in range(count)])
    return state.store.get_max_seq()


def _messages(frames):
    return [message_to_dict(frame) for frame in frames]


@pytest.mark.parametrize(
    ("max_queue", "outcome"), [(0, pytest.raises(ValueError)), (1, does_not_raise())]
)
def test_invalid_settings_raise_value_error(max_queue, outcome):
    with outcome:
        EventReceiver(max_queue=max_queue)


def test_consumer_calls_reject_invalid_arguments():
    receiver = EventReceiver()
    for limit in (0, True):
        with pytest.raises(ValueError, match="max_messages"):
            receiver.drain_queue(max_messages=limit)
    with pytest.raises(ValueError, match="at least 1"):
        receiver.request_replay_from(0)


def test_settings_read_back_and_state_starts_empty():
    receiver = EventReceiver(
        port=7300, sync_from=5, reconnect=False, max_queue=7, socket_timeout=2.5
    )
    assert (receiver.port, receiver.sync_from) == (7300, 5)
    assert (receiver.max_queue, receiver.socket_timeout) == (7, 2.5)
    assert receiver.layered_replay and receiver.layer_mode is LayerMode.MANAGED
    assert not receiver.reconnect
    receiver.reconnect = True
    assert receiver.reconnect

    assert not receiver.connected and not receiver.synchronized
    assert receiver.last_seq == 4
    assert receiver.queued_message_count == 0 and len(receiver.drain_queue()) == 0
    assert receiver.stage_metadata == {}
    assert not receiver.auth_rejected and not receiver.hello_rejected
    assert receiver.rejection_code == HelloRejectionCode.Unspecified
    assert receiver.connection_error is None

    shared = EventReceiver(layer_mode="shared_stage", layered_replay=False)
    assert shared.layer_mode is LayerMode.SHARED_STAGE and not shared.layered_replay


def test_close_stops_the_thread_and_reports_whether_it_exited(receivers):
    assert EventReceiver().close(0), "a receiver that never started has no thread"
    entered, release = threading.Event(), threading.Event()

    def hold(_token):
        entered.set()
        assert release.wait(5)

    receiver = receivers(on_token_issued=hold)
    assert not receiver.running and not receiver.stopped
    started = time.monotonic()
    assert not receiver.wait_connected(5)
    assert not receiver.wait_synchronized(5)
    assert time.monotonic() - started < 1, "a receiver that never started was waited on"

    receiver.start()
    assert receiver.running and not receiver.stopped
    with pytest.raises(RuntimeError, match="started once"):
        receiver.start()
    # The callback holds the thread, so it cannot exit yet.
    assert entered.wait(5)
    assert receiver.connected
    assert not receiver.close(timeout=0)
    assert receiver.running

    release.set()
    started = time.monotonic()
    assert receiver.close()
    assert time.monotonic() - started < 2, "close waited for the read timeout"
    assert receiver.stopped and not receiver.running and not receiver.connected
    assert receiver.close(0)
    with pytest.raises(RuntimeError, match="started once"):
        receiver.start()

    with receivers() as receiver:
        receiver.start()
        assert receiver.wait_connected(5)
    assert receiver.stopped and not receiver.connected


def test_close_from_a_callback_returns_without_waiting_for_its_own_thread(receivers):
    results = []

    def close_on_token(_token):
        started = time.monotonic()
        results.append(receiver.close())
        results.append(time.monotonic() - started)

    receiver = receivers(on_token_issued=close_on_token)
    receiver.start()
    wait_until(lambda: len(results) == 2)
    closed, waited = results
    assert closed is False and waited < 1
    assert receiver.close(5)
    assert receiver.stopped


def test_collecting_a_receiver_stops_its_connection(server):
    receiver = EventReceiver("127.0.0.1", server.server_address[1], client_id=uuid.uuid4().hex)
    client_id = receiver.client_id
    receiver.start()
    assert receiver.wait_connected(5)
    wait_until(lambda: client_registered(server, client_id))
    del receiver
    gc.collect()
    wait_until(lambda: not client_registered(server, client_id))


def test_close_before_start_ends_without_connecting(receivers, server):
    receiver = receivers()
    assert receiver.close()
    receiver.start()
    wait_until(lambda: receiver.stopped)
    assert not receiver.running
    assert not server.sync_server.token_store.has_token(receiver.client_id)


def test_handshake_state_reads_through_after_connecting(receivers, server):
    state = server.sync_server
    head = _commit(server, 2)
    receiver = receivers(origin="origin", department="layout")
    receiver.start()
    assert receiver.wait_connected(5)

    assert receiver.layered_replay_active
    assert receiver.layer_mode_active is LayerMode.MANAGED
    assert receiver.stage_metadata == METADATA
    with state.clients_lock:
        clients = [
            (info.role, info.client_id, info.origin, info.department)
            for info in state.clients.values()
        ]
    assert ("receiver", receiver.client_id, "origin", "layout") in clients
    wait_until(lambda: receiver.last_seq == head)
    assert not receiver.synchronized and receiver.server_instance == ""


def test_token_provider_supplies_each_attempt_and_the_issued_token_is_presented(receivers):
    credential = ClientCredential("127.0.0.1", 0, None, persist=False)
    presented = []

    def provide():
        presented.append(credential.current())
        return presented[-1]

    receiver = receivers(token_provider=provide, on_token_issued=credential.issued)
    receiver.start()
    assert receiver.wait_connected(5)
    issued = credential.token
    assert issued and receiver.token == issued

    receiver.request_replay_from(1)
    assert receiver.wait_connected(5), "the server rejected the issued token"
    assert not receiver.auth_rejected
    assert presented == [None, issued]


def test_token_provider_failure_is_the_connection_error(receivers, caplog):
    def fail():
        raise RuntimeError("injected provider failure")

    receiver = receivers(token_provider=fail, reconnect=False)
    receiver.start()
    wait_until(lambda: receiver.stopped)
    assert not receiver.connected
    assert isinstance(receiver.connection_error, RuntimeError)
    assert "EventReceiver: token provider failed" in caplog.text


def test_legacy_callbacks_receive_message_dicts_once_on_the_connection_thread(
    receivers, server, caplog
):
    state = server.sync_server
    calls = []

    def record(name):
        def callback(value):
            calls.append((name, value, threading.get_ident()))
            if name == "token":
                raise RuntimeError("injected callback failure")

        return callback

    receiver = receivers(
        on_token_issued=record("token"),
        on_stage_metadata=record("metadata"),
        on_playback_state=record("state"),
        on_playback_claimed=record("claimed"),
        on_playback_rejected=record("rejected"),
    )
    receiver.start()
    assert receiver.wait_connected(5)
    # The server sends its playback state after every accepted hello.
    wait_until(lambda: len(calls) == 3)
    claimed = {"type": MSG_PLAYBACK_CLAIMED, "leader_client_id": "leader"}
    rejected = {
        "type": MSG_PLAYBACK_REJECTED,
        "reason": "already led",
        "current_leader_client_id": "leader",
    }
    state.broadcast_message(claimed)
    state.broadcast_message(rejected)
    wait_until(lambda: len(calls) == 5)

    assert [(name, value) for name, value, _thread in calls] == [
        ("token", receiver.token),
        ("metadata", METADATA),
        ("state", {"type": MSG_PLAYBACK_STATE, **state.get_playback_state()}),
        ("claimed", claimed),
        ("rejected", rejected),
    ]
    assert threading.get_ident() not in {thread for _name, _value, thread in calls}
    assert "EventReceiver: on_token_issued callback failed" in caplog.text
    assert receiver.connected


def test_a_given_queue_takes_the_notifications_and_snapshot_reads_the_status(receivers, server):
    with pytest.raises(ValueError, match="on_playback_state"):
        EventReceiver(notifications=_client_backend.NotificationQueue(), on_playback_state=print)
    head = _commit(server, 1)
    notifications = _client_backend.NotificationQueue()
    issued = []
    receiver = receivers(
        notifications=notifications,
        on_token_issued=lambda token: issued.append((token, threading.get_ident())),
    )
    receiver.start()
    wait_until(lambda: issued and receiver.last_seq == head)
    [(token, thread)] = issued
    assert token == receiver.token and thread != threading.get_ident()
    kinds = [type(notification).__name__ for notification in notifications.drain()]
    assert kinds[:3] == ["TokenIssued", "StageMetadata", "Connected"]

    snapshot = receiver.snapshot()
    assert (snapshot.connected, snapshot.last_sequence, snapshot.layered_replay_active) == (
        receiver.connected,
        receiver.last_seq,
        receiver.layered_replay_active,
    )


def test_replay_drains_in_batches_and_is_ready_once_marked_applied(receivers, server):
    state = server.sync_server
    head = _commit(server, 3)
    receiver = receivers()
    receiver.start()
    wait_until(lambda: receiver.last_seq == head)
    waited = []
    waiter = threading.Thread(target=lambda: waited.append(receiver.wait_synchronized(5)))
    waiter.start()

    generation = receiver.generation
    frames = list(receiver.drain_queue(max_messages=2))
    assert len(frames) == 2 and receiver.queued_message_count == head - 1
    assert not receiver.mark_replay_applied()
    frames.extend(receiver.drain_queue())
    messages = _messages(frames)
    assert [message["type"] for message in messages] == ["layer_stack_state"] + ["event"] * head
    assert [message["seq"] for message in messages[1:]] == list(range(1, head + 1))
    assert receiver.mark_applied_through(generation, head)
    # The completion marker follows the last replayed event.
    wait_until(receiver.mark_replay_applied)

    waiter.join(5)
    assert waited == [True]
    assert receiver.synchronized and receiver.wait_synchronized(0)
    assert receiver.replay_head_seq == head
    assert receiver.replay_epoch == state.get_replay_token()[0]
    assert receiver.server_instance == state.server_instance


def test_replay_request_reconnects_and_replays_from_the_requested_sequence(receivers, server):
    head = _commit(server, 2)
    receiver = receivers(reconnect=False)
    receiver.start()
    wait_until(lambda: receiver.last_seq == head)
    marker = receiver.freeze_marker()
    assert not receiver.drained_through(marker)

    # Reconnecting after the request shows the setting reached the connection.
    receiver.reconnect = True
    receiver.request_replay_from(1)
    assert receiver.drained_through(marker)
    assert receiver.queued_message_count == 0 and receiver.last_seq == 0
    assert receiver.wait_connected(5)
    wait_until(lambda: receiver.last_seq == head)

    generation = receiver.generation
    messages = _messages(receiver.drain_queue())
    assert [message["type"] for message in messages] == (
        ["resync", "layer_stack_state"] + ["event"] * head
    )
    receiver.reset_applied_progress()
    assert receiver.mark_applied_through(generation, head)
    wait_until(receiver.mark_replay_applied)
    assert receiver.synchronized


def test_refused_connection_is_the_connection_error(receivers):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    receiver = receivers(port=port, reconnect=False)
    receiver.start()
    waited = []
    waiter = threading.Thread(target=lambda: waited.append(receiver.wait_connected(10)))
    waiter.start()
    wait_until(lambda: receiver.stopped, timeout=10)
    waiter.join(5)
    assert waited == [False]
    assert isinstance(receiver.connection_error, ConnectionRefusedError)


@pytest.mark.parametrize("rejection", ["auth", "hello"])
def test_rejection_is_reported_and_stops_reconnecting(receivers, server, rejection):
    if rejection == "auth":
        client_id = uuid.uuid4().hex
        server.sync_server.token_store.issue(client_id)
        receiver = receivers(client_id=client_id, token="not-issued")
        expected = (True, False, HelloRejectionCode.Unspecified, "invalid or missing token")
    else:
        receiver = receivers(layered_replay=False, layer_mode=LayerMode.SHARED_STAGE)
        expected = (
            False,
            True,
            HelloRejectionCode.LayerModeMismatch,
            "server uses 'managed' layer mode, client requested 'shared_stage'",
        )
    receiver.start()
    wait_until(lambda: receiver.stopped)
    assert not receiver.running and not receiver.connected
    assert (
        receiver.auth_rejected,
        receiver.hello_rejected,
        receiver.rejection_code,
        receiver.rejection_reason,
    ) == expected
