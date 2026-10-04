"""Tests for ReceiverThread over scripted sockets, plus its real TCP transport."""

import errno
import logging
import socket
import threading
import time

import pytest
from pxr import Usd

from openusdconnect._client_utils import ClientCredential
from openusdconnect.adapters import UsdStageAdapter
from openusdconnect.codec import HelloRejectionCode, encode_message, message_to_dict
from openusdconnect.dispatcher import EventDispatcher
from openusdconnect.framing import recv_framed, send_framed
from openusdconnect.protocol_constants import LayerMode
from openusdconnect.receiver import ReceiverThread
from tests.helpers import framed, handshake, scripted_start, wait_until

FAST = {"reconnect_base_delay": 0.01, "reconnect_max_delay": 0.04}


def _event(seq, path="/World/X", **fields):
    event = {"k": "ensure_prim", "prim": path, "typeName": "Xform"}
    return {"type": "event", "seq": seq, "event": event, **fields}


def _replay_complete(head, epoch=1):
    return {"type": "replay_complete", "head_seq": head, "epoch": epoch}


def _sequences(frames):
    return [message_to_dict(frame)["seq"] for frame in frames]


def _claim(hello):
    """The prefix a hello claims: (instance, epoch), or None without a claim."""
    if "replay_server_instance" not in hello:
        return None
    return hello["replay_server_instance"], hello.get("replay_epoch")


@pytest.fixture
def receivers():
    """Build receivers that are stopped and joined when the test ends."""
    created = []

    def make(**options):
        receiver = ReceiverThread(**options)
        created.append(receiver)
        return receiver

    yield make
    for receiver in created:
        receiver.stop()
        receiver.join(5)
        assert not receiver.is_alive()


class TestHandshake:
    def test_hello_carries_the_configuration(self, receivers):
        rt = receivers(
            sync_from=5,
            client_id="client",
            origin="origin",
            department="layout",
            token="token-1",
        )
        hello = scripted_start(rt).accept().hello
        assert hello["type"] == "hello"
        assert hello["role"] == "receiver"
        assert hello["sync_from"] == 5
        assert hello["layered_replay"] is True
        assert (hello["client_id"], hello["origin"]) == ("client", "origin")
        assert hello["department"] == "layout"
        assert hello["token"] == "token-1"
        assert _claim(hello) is None

    def test_anonymous_receiver_omits_its_identity(self, receivers):
        hello = scripted_start(receivers()).accept().hello
        assert "client_id" not in hello and "origin" not in hello and "token" not in hello

    def test_negotiates_layered_replay(self, receivers):
        rt = receivers()
        handshake(rt)
        assert rt.connected and not rt.synchronized
        assert rt.layered_replay_active is True
        assert rt.layer_mode_active is LayerMode.MANAGED

    def test_explicit_flat_replay_accepts_unlayered_handshake(self, receivers):
        rt = receivers(layered_replay=False)
        peer = scripted_start(rt).accept()
        assert "layered_replay" not in peer.hello
        peer.send({"type": "hello_ok"})
        assert rt.connected
        assert not rt.layered_replay_active
        assert not rt.hello_rejected

    def test_shared_stage_mode_is_negotiated(self, receivers):
        rt = receivers(layered_replay=False, layer_mode=LayerMode.SHARED_STAGE)
        peer = scripted_start(rt).accept()
        assert peer.hello["layer_mode"] == "shared_stage"
        peer.accept_hello(rt)
        assert rt.layer_mode_active is LayerMode.SHARED_STAGE

    @pytest.mark.parametrize(
        ("hello_ok", "code"),
        [
            ({"type": "hello_ok"}, HelloRejectionCode.LayeredReplayRequired),
            (
                {"type": "hello_ok", "layered_replay": True, "layer_mode": "shared_stage"},
                HelloRejectionCode.LayerModeMismatch,
            ),
        ],
    )
    def test_unnegotiated_capability_rejects_the_handshake(self, receivers, hello_ok, code):
        rt = receivers(reconnect=True, **FAST)
        server = scripted_start(rt)
        server.accept().send_closing(hello_ok)
        rt.join(5)
        assert not rt.is_alive() and rt.stopped
        assert not rt.connected
        assert rt.hello_rejected and not rt.auth_rejected
        assert rt.rejection_code == code
        assert server.sockets.attempts == 1

    def test_hello_rejection_stops_reconnects(self, receivers):
        rt = receivers(reconnect=True, **FAST)
        server = scripted_start(rt)
        server.accept().send_closing(
            {
                "type": "hello_rejected",
                "code": HelloRejectionCode.LayeredReplayRequired,
                "reason": "layered replay is required",
            }
        )
        rt.join(5)
        assert not rt.is_alive()
        assert not rt.connected
        assert not rt.auth_rejected
        assert rt.hello_rejected
        assert rt.rejection_code == HelloRejectionCode.LayeredReplayRequired
        assert rt.rejection_reason == "layered replay is required"
        assert server.sockets.attempts == 1

    def test_auth_rejection_stores_reason(self, receivers):
        rt = receivers(reconnect=True, **FAST)
        assert not rt.auth_rejected and rt.rejection_reason == ""
        scripted_start(rt).accept().send_closing(
            {"type": "auth_rejected", "reason": "invalid token"},
        )
        rt.join(5)
        assert not rt.is_alive()
        assert rt.auth_rejected
        assert not rt.hello_rejected
        assert rt.rejection_code == HelloRejectionCode.Unspecified
        assert rt.rejection_reason == "invalid token"

    def test_issued_token_is_presented_by_the_next_connection(self, receivers):
        rt = receivers(**FAST)
        server = scripted_start(rt)
        first = server.accept()
        assert "token" not in first.hello
        first.send({"type": "hello_ok", "layered_replay": True, "token": "issued"})
        assert rt.token == "issued"
        first.close()
        assert server.accept().hello["token"] == "issued"

    def test_credential_stores_the_issued_token_before_the_next_connection(self, receivers):
        credential = ClientCredential("127.0.0.1", 7200, None, persist=False)
        rt = receivers(**FAST, **credential.endpoint_kwargs())
        server = scripted_start(rt)
        first = server.accept()
        first.send({"type": "hello_ok", "layered_replay": True, "token": "issued"})
        assert credential.token == "issued"
        first.close()
        assert server.accept().hello["token"] == "issued"

    def test_token_callback_failure_does_not_abort_handshake(self, receivers, caplog):
        def fail(_token):
            raise RuntimeError("injected callback failure")

        rt = receivers(on_token_issued=fail)
        peer = scripted_start(rt).accept()
        peer.send({"type": "hello_ok", "layered_replay": True, "token": "issued-token"})
        assert rt.wait_connected(timeout=1)
        assert rt.token == "issued-token"
        assert "on_token_issued callback failed" in caplog.text

    def test_token_provider_failure_abandons_only_that_attempt(self, receivers):
        tokens = iter([RuntimeError("injected provider failure"), "later"])

        def provide():
            token = next(tokens)
            if isinstance(token, Exception):
                raise token
            return token

        rt = receivers(token_provider=provide, **FAST)
        server = scripted_start(rt)
        abandoned = server.sockets.accept()
        assert abandoned.wait_closed()
        assert abandoned.sent == b""
        assert isinstance(rt.connection_error, RuntimeError)
        assert server.accept().hello["token"] == "later"
        assert rt.token == "later"
        assert rt.connection_error is None

    def test_control_callbacks_receive_message_dicts_on_the_receiver_thread(self, receivers):
        calls = []
        playback = [
            {
                "type": "playback_state",
                "time": 12.5,
                "playing": True,
                "rate": 2.0,
                "leader_client_id": "leader",
            },
            {"type": "playback_claimed", "leader_client_id": "me"},
            {"type": "playback_rejected", "reason": "already led", "current_leader_client_id": "x"},
        ]

        def record(name):
            return lambda value: calls.append((name, value, threading.get_ident()))

        rt = receivers(
            on_stage_metadata=record("metadata"),
            on_playback_state=record("state"),
            on_playback_claimed=record("claimed"),
            on_playback_rejected=record("rejected"),
        )
        assert rt.stage_metadata == {}
        peer = scripted_start(rt).accept()
        metadata = {"timeCodesPerSecond": 24.0, "upAxis": "Z"}
        peer.accept_hello(rt, stage_metadata=metadata)
        peer.send(*playback)
        assert rt.stage_metadata == metadata
        assert [(name, value) for name, value, _thread in calls] == [
            ("metadata", metadata),
            *zip(
                ("state", "claimed", "rejected"),
                (message_to_dict(encode_message(message)) for message in playback),
                strict=True,
            ),
        ]
        assert {thread for _name, _value, thread in calls} != {threading.get_ident()}
        assert rt.queued_message_count == 0


class TestConsumer:
    def test_bounded_drain_preserves_suffix_and_replay_watermark(self, receivers):
        rt = receivers(reconnect=False)
        peer = handshake(rt)
        peer.send(_event(1), _event(2), _event(3), _replay_complete(3, epoch=7))

        assert _sequences(rt.drain_queue(max_messages=2)) == [1, 2]
        assert rt.queued_message_count == 1
        assert not rt.mark_replay_applied()

        assert _sequences(rt.drain_queue(max_messages=2)) == [3]
        assert rt.mark_replay_applied()
        assert rt.synchronized
        assert rt.replay_head_seq == 3
        assert rt.replay_epoch == 7

    @pytest.mark.parametrize("limit", [0, -1, True, 1.5])
    def test_bounded_drain_rejects_invalid_limit(self, limit):
        with pytest.raises(ValueError, match="max_messages"):
            ReceiverThread(reconnect=False).drain_queue(max_messages=limit)

    def test_drain_is_empty_before_and_after_connecting(self, receivers):
        rt = receivers(reconnect=False)
        assert len(rt.drain_queue()) == 0
        handshake(rt)
        assert len(rt.drain_queue()) == 0

    def test_receives_and_drains(self, receivers):
        rt = receivers(reconnect=False)
        handshake(rt).send(_event(1), {"type": "ping"}, _event(2))
        assert rt.last_seq == 2
        assert _sequences(rt.drain_queue()) == [1, 2]
        assert len(rt.drain_queue()) == 0

    def test_replay_ready_only_after_preceding_frames_are_drained_and_applied(self, receivers):
        rt = receivers(reconnect=False)
        handshake(rt).send(_event(1), _replay_complete(1, epoch=4))
        assert not rt.synchronized
        assert not rt.wait_synchronized(timeout=0.01)
        assert [message_to_dict(raw)["type"] for raw in rt.drain_queue()] == ["event"]
        assert rt.mark_replay_applied()
        assert rt.synchronized
        assert rt.replay_head_seq == 1
        assert rt.replay_epoch == 4

    def test_wait_synchronized_wakes_when_the_replay_is_marked(self, receivers):
        rt = receivers(reconnect=False)
        handshake(rt).send(_replay_complete(0))
        waited = []
        waiter = threading.Thread(target=lambda: waited.append(rt.wait_synchronized(timeout=5)))
        waiter.start()
        assert rt.mark_replay_applied()
        waiter.join(2)
        assert waited == [True]

    def test_replay_request_discards_the_queue_and_reconnects_from_it(self, receivers, caplog):
        caplog.set_level(logging.WARNING, logger="openusdconnect.receiver")
        rt = receivers(**FAST)
        server = scripted_start(rt)
        first = server.connect(rt)
        first.send(_event(1), _event(2))
        marker = rt.freeze_marker()
        assert not rt.drained_through(marker)

        rt.request_replay_from(2)
        assert rt.last_seq == 1
        assert rt.queued_message_count == 0
        assert rt.drained_through(marker)
        assert first.connection.wait_closed()

        second = server.connect(rt)
        assert second.hello["sync_from"] == 2
        second.send(_event(2))
        assert rt.last_seq == 2
        assert not caplog.records, "a requested replay is not a connection failure"

    def test_replay_request_rejects_sequences_below_one(self):
        with pytest.raises(ValueError, match="at least 1"):
            ReceiverThread().request_replay_from(0)


class TestLifecycle:
    def test_thread_api_before_and_after_running(self, receivers):
        rt = receivers(reconnect=False)
        assert not rt.stopped and not rt.is_alive() and rt.ident is None
        rt.join(timeout=1)
        scripted_start(rt)
        assert rt.is_alive() and not rt.stopped
        assert isinstance(rt.ident, int)
        with pytest.raises(RuntimeError, match="started once"):
            rt.start()
        rt.stop()
        rt.join(timeout=5)
        assert rt.stopped and not rt.is_alive()

    def test_stop_before_start_never_connects(self, receivers):
        rt = receivers()
        rt.stop()
        server = scripted_start(rt)
        rt.join(timeout=5)
        assert rt.stopped
        assert server.sockets.attempts == 0

    def test_terminal_transport_failure_wakes_connection_waiter(self, receivers):
        rt = receivers(host="127.0.0.1", port=1, reconnect=False)
        server = scripted_start(rt)
        waited = []
        waiter = threading.Thread(target=lambda: waited.append(rt.wait_connected(timeout=5)))
        waiter.start()
        server.refuse(errno.ECONNREFUSED)
        waiter.join(2)
        assert waited == [False]
        rt.join(timeout=5)
        assert not rt.is_alive()
        assert isinstance(rt.connection_error, ConnectionRefusedError)

    def test_stop_on_server_close(self, receivers):
        rt = receivers(reconnect=False)
        handshake(rt).close()
        rt.join(timeout=5)
        assert not rt.is_alive()
        assert not rt.connected

    def test_reconnects_after_server_close(self, receivers):
        rt = receivers(**FAST)
        server = scripted_start(rt)
        server.connect(rt).close()
        assert not rt.connected
        server.connect(rt)
        assert rt.connected

    def test_reconnect_clears_ready_until_the_new_replay_marker_is_applied(self, receivers):
        rt = receivers(**FAST)
        server = scripted_start(rt)
        server.connect(rt, synchronized=True).close()
        assert not rt.synchronized
        peer = server.connect(rt)
        assert rt.connected and not rt.synchronized
        peer.synchronize(rt)
        assert rt.synchronized

    def test_reconnect_uses_last_seq(self, receivers):
        rt = receivers(sync_from=10, **FAST)
        server = scripted_start(rt)
        first = server.connect(rt)
        first.send(_event(10))
        first.close()
        assert server.accept().hello["sync_from"] == 11

    def test_resync_rewinds_the_reconnect_cursor(self, receivers):
        rt = receivers(sync_from=10, **FAST)
        server = scripted_start(rt)
        first = server.connect(rt)
        first.send(_event(10), {"type": "resync"}, _event(1))
        assert rt.last_seq == 1
        first.close()
        assert server.accept().hello["sync_from"] == 2

    def test_in_place_resync_clears_ready_until_new_watermark_is_applied(self, receivers):
        rt = receivers(reconnect=False)
        peer = handshake(rt, synchronized=True)
        peer.send({"type": "resync"}, _event(1), _replay_complete(1, epoch=2))
        assert not rt.synchronized
        queued = rt.drain_queue()
        assert [message_to_dict(raw)["type"] for raw in queued] == ["resync", "event"]
        assert rt.mark_replay_applied()
        assert rt.synchronized
        assert rt.replay_head_seq == 1
        assert rt.replay_epoch == 2

    def test_backoff_doubles_and_resets_after_a_connected_session(self, receivers, caplog):
        caplog.set_level(logging.INFO, logger="openusdconnect.receiver")
        rt = receivers(reconnect_base_delay=0.01, reconnect_max_delay=0.08)
        server = scripted_start(rt)
        server.refuse()
        server.refuse()
        server.connect(rt).close()
        server.accept()
        delays = [
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("reconnecting in")
        ]
        assert delays == ["reconnecting in 10 ms", "reconnecting in 20 ms", "reconnecting in 10 ms"]

    def test_no_reconnect_when_disabled(self, receivers):
        rt = receivers(reconnect=False)
        server = scripted_start(rt)
        server.connect(rt).close()
        rt.join(timeout=5)
        assert not rt.is_alive()
        assert server.sockets.attempts == 1

    def test_reconnect_toggle_applies_when_the_connection_ends(self, receivers):
        rt = receivers(reconnect=False, **FAST)
        server = scripted_start(rt)
        first = server.connect(rt)
        rt.reconnect = True
        rt.request_replay_from(1)
        assert first.connection.wait_closed()
        second = server.connect(rt)
        assert second.hello["sync_from"] == 1
        rt.reconnect = False
        assert not rt.reconnect
        second.close()
        rt.join(timeout=5)
        assert not rt.is_alive()


class TestReadTimeouts:
    def test_timeouts_below_the_limit_keep_the_connection(self, receivers):
        rt = receivers(reconnect=False)
        peer = handshake(rt)
        peer.time_out(9)
        assert rt.connected
        peer.send(_event(1))
        assert rt.queued_message_count == 1

    def test_received_bytes_reset_the_timeout_count(self, receivers):
        rt = receivers(reconnect=False)
        peer = handshake(rt)
        peer.time_out(9)
        peer.send({"type": "ping"})
        peer.time_out(9)
        assert rt.connected

    def test_consecutive_timeouts_end_the_connection(self, receivers):
        rt = receivers(reconnect=False)
        peer = handshake(rt)
        peer.time_out(9)
        assert peer.connection.deliver_timeout()
        assert peer.connection.wait_closed()
        rt.join(timeout=5)
        assert not rt.is_alive()


class TestBoundedQueue:
    def test_overflow_resumes_from_the_queue_after_it_drains(self, receivers):
        # The drain wait lasts at most the maximum reconnect delay.
        rt = receivers(max_queue=3, reconnect_base_delay=0.01, reconnect_max_delay=30)
        server = scripted_start(rt)
        peer = server.connect(rt)
        peer.send(_event(1), _event(2), _event(3))
        peer.send_closing(_event(4))
        assert not rt.connected
        assert rt.queued_message_count == 3
        assert server.sockets.accept(timeout=0.2) is None, "reconnected before the drain"
        assert _sequences(rt.drain_queue()) == [1, 2, 3]
        assert server.accept().hello["sync_from"] == 4

    def test_overflow_stops_when_reconnect_is_disabled(self, receivers):
        rt = receivers(reconnect=False, max_queue=3)
        peer = handshake(rt)
        peer.send(_event(1), _event(2), _event(3))
        peer.send_closing(_event(4))
        rt.join(timeout=5)
        assert not rt.is_alive()


class TestReplayIdentity:
    def test_received_prefix_identity_is_not_published_until_applied(self, receivers):
        rt = receivers(**FAST)
        server = scripted_start(rt)
        first = server.accept()
        first.accept_hello(rt, server_instance="old", replay_epoch=None)
        first.send(_replay_complete(0, epoch=2))
        assert rt.server_instance == ""
        assert rt.mark_replay_applied()
        assert rt.server_instance == "old"
        first.close()

        second = server.accept()
        assert _claim(second.hello) == ("old", 2)
        second.accept_hello(rt, server_instance="new", replay_epoch=None)
        assert rt.server_instance == "old"
        assert not rt.synchronized
        second.send({"type": "resync"}, _replay_complete(0, epoch=0))
        assert not rt.mark_replay_applied()
        rt.drain_queue()
        assert rt.mark_replay_applied()
        assert rt.server_instance == "new"
        assert rt.replay_epoch == 0

    def test_interrupted_reset_claims_an_unknown_prefix(self, receivers):
        rt = receivers(**FAST)
        server = scripted_start(rt)
        first = server.accept()
        first.accept_hello(rt, replay_epoch=None)
        first.send(_replay_complete(0, epoch=3))
        assert rt.mark_replay_applied()
        first.send({"type": "resync"})
        assert not rt.synchronized
        first.close()
        assert _claim(server.accept().hello) == ("", None)
        assert not rt.mark_replay_applied()

    def test_explicit_full_replay_queues_reset_before_colliding_events(self, receivers):
        rt = receivers(**FAST)
        stage = Usd.Stage.CreateInMemory()
        dispatcher = EventDispatcher(receiver=rt, adapter=UsdStageAdapter(stage))
        stack = {"type": "layer_stack_state", "layers": [{"layer_key": "shared"}]}
        server = scripted_start(rt)
        first = server.connect(rt)
        first.send(stack, _event(1, "/Old", layer_key="shared"))
        dispatcher.drain_and_apply()
        assert stage.GetPrimAtPath("/Old")
        first.close()

        second = server.accept()
        assert second.hello["sync_from"] == 2
        rt.request_replay_from(1)
        assert second.connection.wait_closed()
        third = server.connect(rt)
        assert third.hello["sync_from"] == 1
        third.send(stack, _event(1, "/Own", layer_key="shared"), _replay_complete(1, epoch=0))
        dispatcher.drain_and_apply()
        assert stage.GetPrimAtPath("/Own")
        assert not stage.GetPrimAtPath("/Old")
        assert dispatcher.last_seq == 1
        assert rt.synchronized
        dispatcher.close()

    def test_live_gap_replays_from_the_consumer_applied_cursor(self, receivers):
        rt = receivers(layered_replay=False, **FAST)
        stage = Usd.Stage.CreateInMemory()
        dispatcher = EventDispatcher(receiver=rt, adapter=UsdStageAdapter(stage))
        server = scripted_start(rt)
        first = server.connect(rt)
        first.send(_event(1, "/P1"), _event(2, "/P2"))
        dispatcher.drain_and_apply()
        first.send(_event(3, "/P3"), _event(2, "/P2"))
        assert rt.queued_message_count == 1, "duplicates are dropped"
        first.send_closing(_event(5, "/P5"))
        assert rt.queued_message_count == 0

        resumed = server.connect(rt)
        assert resumed.hello["sync_from"] == 3, "queued but unapplied frames are replayed"
        resumed.send({"type": "resync"}, _event(1, "/P1"))
        dispatcher.drain_and_apply()
        resumed.send_closing(_event(3, "/P3"))
        assert server.accept().hello["sync_from"] == 2
        dispatcher.close()

    @pytest.mark.parametrize("instance", ["scripted", "replacement"])
    @pytest.mark.parametrize("queue_full", [False, True])
    def test_changed_hello_identity_waits_for_the_accepted_reset(
        self,
        receivers,
        instance,
        queue_full,
    ):
        rt = receivers(max_queue=1, **FAST)
        server = scripted_start(rt)
        first = server.connect(rt)
        first.send(_event(1, "/Old"))
        rt.drain_queue()
        first.close()

        second = server.accept()
        assert second.hello["sync_from"] == 2
        assert _claim(second.hello) == ("scripted", 0)
        second.accept_hello(rt, server_instance=instance, replay_epoch=1)
        assert rt.last_seq == 1
        assert rt.server_instance == ""
        assert not rt.synchronized
        if queue_full:
            second.send({"type": "layer_stack_state", "layers": []})
            second.send_closing({"type": "resync"})
            assert rt.last_seq == 1
            rt.drain_queue()
            retry = server.accept().hello
            assert retry["sync_from"] == 2
            assert _claim(retry) == ("scripted", 0)
        else:
            second.send({"type": "resync"})
            assert rt.last_seq == 0
            second.close()
            retry = server.accept().hello
            assert retry["sync_from"] == 1
            assert _claim(retry) == (instance, 1)

    def test_replay_request_from_a_token_callback_keeps_identity_unpublished(self, receivers):
        def replay(_token):
            rt.request_replay_from(2)

        rt = receivers(on_token_issued=replay, **FAST)
        server = scripted_start(rt)
        first = server.accept()
        assert first.connection.deliver(
            framed({"type": "hello_ok", "layered_replay": True, "token": "x"})
        )
        assert first.connection.wait_closed()
        assert not rt.connected
        assert rt.server_instance == ""
        assert server.accept().hello["sync_from"] == 2


class TestTcpTransport:
    def test_tcp_connection_delivers_frames_and_stop_interrupts_a_blocked_read(self, receivers):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(5)
            rt = receivers(host="127.0.0.1", port=listener.getsockname()[1], reconnect=False)
            rt.start()
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(5)
                assert message_to_dict(recv_framed(conn))["sync_from"] == 1
                send_framed(conn, encode_message({"type": "hello_ok", "layered_replay": True}))
                assert rt.wait_connected(timeout=5)
                send_framed(conn, encode_message(_event(1)))
                wait_until(lambda: rt.queued_message_count == 1)

                started = time.monotonic()
                rt.stop()
                rt.join(timeout=5)
                assert not rt.is_alive()
                assert time.monotonic() - started < 2, "stop waited for the read timeout"
                assert conn.recv(1) == b""

    def test_stop_interrupts_a_pending_connect(self, receivers):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        rt = receivers(host="127.0.0.1", port=port)
        rt.start()
        started = time.monotonic()
        rt.stop()
        rt.join(timeout=5)
        assert not rt.is_alive()
        assert time.monotonic() - started < 1
