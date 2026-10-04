"""A partial replay must retain enough identity to resume after queue overflow."""

from typing import NamedTuple

import pytest
from pxr import Usd

from openusdconnect.adapters import UsdStageAdapter
from openusdconnect.dispatcher import EventDispatcher
from openusdconnect.receiver import ReceiverThread
from tests.helpers import ensure_prim_event, in_process_server, relay_connection


@pytest.fixture
def replay_server():
    with in_process_server() as server:
        yield server


class Boundary(NamedTuple):
    completed: bool
    # The hello that opened the connection.
    hello: dict


def _is_replay_complete(message):
    return message["type"] == "replay_complete"


def _receive_until_boundary(receiver, dispatcher, port, *, after_replay=None):
    """Do not drain until overflow or ReplayComplete, independent of scheduling."""
    relay = relay_connection(receiver, port)
    try:
        completed = relay.pump(_is_replay_complete) is not None
        if after_replay is not None:
            assert completed
            dispatcher.drain_and_apply()
            assert receiver.synchronized
            after_replay()
            completed = relay.pump(_is_replay_complete) is not None
        if not completed:
            assert not receiver.connected, "receiver stopped before replay"
            assert receiver.queued_message_count == receiver.max_queue
        dispatcher.drain_and_apply()
        if completed:
            assert receiver.synchronized
        return Boundary(completed, relay.hello)
    finally:
        relay.close()


@pytest.mark.parametrize("after_purge", [False, True], ids=["initial", "after-purge"])
def test_partial_replay_advances_across_queue_overflow(replay_server, after_purge):
    state, port = replay_server
    receiver = ReceiverThread(host="127.0.0.1", port=port, max_queue=3)
    stage = Usd.Stage.CreateInMemory()
    dispatcher = EventDispatcher(receiver=receiver, adapter=UsdStageAdapter(stage))
    try:
        if after_purge:
            state._commit_events([ensure_prim_event("/Before")])
            assert _receive_until_boundary(receiver, dispatcher, port).completed
            assert stage.GetPrimAtPath("/Before")
            state.purge()

        paths = [f"/P{index}" for index in range(8)]
        state._commit_events([ensure_prim_event(path) for path in paths])
        head = state.store.get_max_seq()
        assert head == len(paths)

        progress = []
        completed = False
        for _ in range(len(paths)):
            completed = _receive_until_boundary(receiver, dispatcher, port).completed
            progress.append(dispatcher.last_seq)
            if completed:
                break

        assert len(progress) > 1, "fixture must force at least one replay overflow"
        assert all(after > before for before, after in zip(progress, progress[1:], strict=False)), (
            f"partial replay restarted instead of advancing: {progress}"
        )
        assert completed, f"replay never completed: {progress}"
        assert dispatcher.last_seq == head
        assert receiver.server_instance == state.server_instance
        assert receiver.replay_epoch == state.get_replay_token()[0]
        assert all(stage.GetPrimAtPath(path) for path in paths)
        assert not stage.GetPrimAtPath("/Before")
    finally:
        receiver.stop()
        dispatcher.close()


@pytest.mark.parametrize("layered_replay", [False, True], ids=["flat", "layered"])
@pytest.mark.parametrize("explicit_replay", [False, True], ids=["initial-overflow", "full-replay"])
def test_snapshot_cursor_overflow_before_first_reset_event_recovers(
    replay_server, layered_replay, explicit_replay,
):
    state, port = replay_server
    snapshot_paths = [f"/P{index}" for index in range(1, 4)]
    remaining_paths = [f"/P{index}" for index in range(4, 7)]
    state._commit_events([ensure_prim_event(path) for path in snapshot_paths])
    stage = Usd.Stage.CreateInMemory()
    for path in snapshot_paths:
        stage.DefinePrim(path, "Xform")
    receiver = ReceiverThread(
        host="127.0.0.1", port=port, sync_from=4,
        max_queue=2 if layered_replay else 1, layered_replay=layered_replay,
    )
    dispatcher = EventDispatcher(receiver=receiver, adapter=UsdStageAdapter(stage))
    dispatcher.last_seq = 3
    try:
        if explicit_replay:
            assert _receive_until_boundary(receiver, dispatcher, port).completed
            assert receiver.server_instance == ""
            receiver.request_replay_from(1)
        state._commit_events([ensure_prim_event(path) for path in remaining_paths])

        # The reset (plus layer stack when negotiated) fills the queue before
        # event 1. Its next connection must start at 1, not the snapshot cursor 4.
        progress = []
        completed = False
        for _ in range(10):
            completed = _receive_until_boundary(receiver, dispatcher, port).completed
            progress.append(dispatcher.last_seq)
            if completed:
                break
        assert 0 in progress, f"fixture did not overflow before event 1: {progress}"
        assert completed, f"snapshot replay never completed: {progress}"
        reset_index = progress.index(0)
        assert progress[reset_index:] == list(range(7))
        assert receiver.last_seq == dispatcher.last_seq == state.store.get_max_seq() == 6
        assert receiver.server_instance == state.server_instance
        assert receiver.replay_epoch == state.get_replay_token()[0]
        assert all(stage.GetPrimAtPath(path) for path in snapshot_paths + remaining_paths)
    finally:
        receiver.stop()
        dispatcher.close()


def test_live_compaction_overflow_resumes_in_new_epoch(replay_server):
    state, port = replay_server
    paths = [f"/P{index}" for index in range(8)]
    state._commit_events([ensure_prim_event(paths[0])])
    receiver = ReceiverThread(host="127.0.0.1", port=port, max_queue=3)
    stage = Usd.Stage.CreateInMemory()
    dispatcher = EventDispatcher(receiver=receiver, adapter=UsdStageAdapter(stage))

    def compact_live():
        # Keep the initial replay small, then force a large reset on the same socket.
        state._commit_events([ensure_prim_event(path) for path in paths[1:]])
        state.compact_log()

    try:
        assert not _receive_until_boundary(
            receiver, dispatcher, port, after_replay=compact_live,
        ).completed
        assert state.get_replay_token()[0] == 1
        assert receiver.replay_epoch == 0

        # The epoch-less live Resync needs one fresh handshake/reset. Subsequent
        # overflows must retain that handshake's epoch and advance normally.
        progress = []
        completed = False
        for _ in range(len(paths)):
            boundary = _receive_until_boundary(receiver, dispatcher, port)
            if not progress:
                # The live reset left no proven identity, so the hello claims none.
                assert boundary.hello["replay_server_instance"] == ""
                assert "replay_epoch" not in boundary.hello
            progress.append(dispatcher.last_seq)
            completed = boundary.completed
            if completed:
                break
        assert all(after > before for before, after in zip(progress, progress[1:], strict=False)), (
            f"live reset replay restarted instead of advancing: {progress}"
        )
        assert completed, f"live reset replay never completed: {progress}"
        assert dispatcher.last_seq == state.store.get_max_seq()
        assert receiver.replay_epoch == 1
        assert receiver.server_instance == state.server_instance
        assert all(stage.GetPrimAtPath(path) for path in paths)
    finally:
        receiver.stop()
        dispatcher.close()
