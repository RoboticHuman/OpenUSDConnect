"""A partial replay must retain enough identity to resume after queue overflow."""

from typing import NamedTuple

import pytest
from pxr import Usd

from openusdconnect.adapters import UsdStageAdapter
from openusdconnect.dispatcher import EventDispatcher
from openusdconnect.receiver import ReceiverThread
from tests.helpers import ensure_prim_event, recorded_hellos, server_state, serving, wait_until

# Covers a reconnect after the default base delay, including a refused attempt.
BOUNDARY_TIMEOUT = 15


class Boundary(NamedTuple):
    completed: bool
    # The hello that opened the connection.
    hello: dict


class _Replica:
    """A stage whose consumer applies the receiver queue only at replay boundaries.

    Until the consumer drains, a connection either overflows the queue or
    holds the whole replay, so each boundary is independent of scheduling.
    After an overflow the receiver reconnects only once the queue is drained.
    """

    def __init__(self, state, port, hellos, *, stage=None, **options):
        self.stage = stage or Usd.Stage.CreateInMemory()
        self.receiver = ReceiverThread(host="127.0.0.1", port=port, **options)
        self.dispatcher = EventDispatcher(
            receiver=self.receiver, adapter=UsdStageAdapter(self.stage)
        )
        self._state = state
        self._hellos = hellos
        self._next_hello = len(hellos)

    def close(self):
        self.receiver.stop()
        self.receiver.join(5)
        self.dispatcher.close()

    def next_boundary(self, *, after_replay=None):
        """Wait for overflow or the replay's last record, then apply the queue."""
        if self.receiver.ident is None:
            self.receiver.start()
        hello = self._next_hello
        completed = self._reach_boundary()
        if after_replay is not None:
            assert completed
            self._apply(completed)
            after_replay()
            completed = self._reach_boundary()
        self._apply(completed)
        return Boundary(completed, self._hellos[hello])

    def _overflowed(self):
        receiver = self.receiver
        return not receiver.connected and receiver.queued_message_count == receiver.max_queue

    def _reach_boundary(self):
        receiver = self.receiver
        head = self._state.store.get_max_seq()
        wait_until(
            lambda: self._overflowed() or (receiver.connected and receiver.last_seq == head),
            timeout=BOUNDARY_TIMEOUT,
        )
        return not self._overflowed()

    def _apply(self, completed):
        # The receiver reconnects only after this drain, so its next hello follows.
        self._next_hello = len(self._hellos)
        if completed:
            wait_until(
                lambda: self.dispatcher.drain_and_apply() is not None and self.receiver.synchronized
            )
        else:
            self.dispatcher.drain_and_apply()


@pytest.mark.parametrize("after_purge", [False, True], ids=["initial", "after-purge"])
def test_partial_replay_advances_across_queue_overflow(monkeypatch, after_purge):
    hellos = recorded_hellos(monkeypatch)
    with server_state() as state:
        port = 0
        replica = None
        try:
            if after_purge:
                state._commit_events([ensure_prim_event("/Before")])
                with serving(state) as port:
                    replica = _Replica(state, port, hellos, max_queue=3)
                    assert replica.next_boundary().completed
                    assert replica.stage.GetPrimAtPath("/Before")
                # The receiver finds the purge when it reconnects.
                state.purge()

            paths = [f"/P{index}" for index in range(8)]
            state._commit_events([ensure_prim_event(path) for path in paths])
            head = state.store.get_max_seq()
            assert head == len(paths)

            with serving(state, port) as port:
                replica = replica or _Replica(state, port, hellos, max_queue=3)
                progress = []
                completed = False
                for _ in range(len(paths)):
                    completed = replica.next_boundary().completed
                    progress.append(replica.dispatcher.last_seq)
                    if completed:
                        break

                assert len(progress) > 1, "fixture must force at least one replay overflow"
                assert all(
                    after > before for before, after in zip(progress, progress[1:], strict=False)
                ), f"partial replay restarted instead of advancing: {progress}"
                assert completed, f"replay never completed: {progress}"
                assert replica.dispatcher.last_seq == head
                assert replica.receiver.server_instance == state.server_instance
                assert replica.receiver.replay_epoch == state.get_replay_token()[0]
                assert all(replica.stage.GetPrimAtPath(path) for path in paths)
                assert not replica.stage.GetPrimAtPath("/Before")
        finally:
            if replica is not None:
                replica.close()


@pytest.mark.parametrize("layered_replay", [False, True], ids=["flat", "layered"])
@pytest.mark.parametrize("explicit_replay", [False, True], ids=["initial-overflow", "full-replay"])
def test_snapshot_cursor_overflow_before_first_reset_event_recovers(
    monkeypatch,
    layered_replay,
    explicit_replay,
):
    hellos = recorded_hellos(monkeypatch)
    snapshot_paths = [f"/P{index}" for index in range(1, 4)]
    remaining_paths = [f"/P{index}" for index in range(4, 7)]
    stage = Usd.Stage.CreateInMemory()
    for path in snapshot_paths:
        stage.DefinePrim(path, "Xform")
    with server_state() as state, serving(state) as port:
        state._commit_events([ensure_prim_event(path) for path in snapshot_paths])
        replica = _Replica(
            state,
            port,
            hellos,
            stage=stage,
            sync_from=4,
            max_queue=2 if layered_replay else 1,
            layered_replay=layered_replay,
        )
        replica.dispatcher.last_seq = 3
        try:
            if explicit_replay:
                assert replica.next_boundary().completed
                assert replica.receiver.server_instance == ""
                replica.receiver.request_replay_from(1)
            state._commit_events([ensure_prim_event(path) for path in remaining_paths])

            # The reset (plus layer stack when negotiated) fills the queue before
            # event 1. Its next connection must start at 1, not the snapshot cursor 4.
            progress = []
            completed = False
            for _ in range(10):
                completed = replica.next_boundary().completed
                progress.append(replica.dispatcher.last_seq)
                if completed:
                    break
            assert 0 in progress, f"fixture did not overflow before event 1: {progress}"
            assert completed, f"snapshot replay never completed: {progress}"
            reset_index = progress.index(0)
            assert progress[reset_index:] == list(range(7))
            receiver = replica.receiver
            assert receiver.last_seq == replica.dispatcher.last_seq == 6
            assert state.store.get_max_seq() == 6
            assert receiver.server_instance == state.server_instance
            assert receiver.replay_epoch == state.get_replay_token()[0]
            assert all(stage.GetPrimAtPath(path) for path in snapshot_paths + remaining_paths)
        finally:
            replica.close()


def test_live_compaction_overflow_resumes_in_new_epoch(monkeypatch):
    hellos = recorded_hellos(monkeypatch)
    paths = [f"/P{index}" for index in range(8)]
    with server_state() as state, serving(state) as port:
        state._commit_events([ensure_prim_event(paths[0])])
        replica = _Replica(state, port, hellos, max_queue=3)

        def compact_live():
            # Keep the initial replay small, then force a large reset on the same socket.
            state._commit_events([ensure_prim_event(path) for path in paths[1:]])
            state.compact_log()

        try:
            assert not replica.next_boundary(after_replay=compact_live).completed
            assert state.get_replay_token()[0] == 1
            assert replica.receiver.replay_epoch == 0

            # The epoch-less live Resync needs one fresh handshake/reset. Subsequent
            # overflows must retain that handshake's epoch and advance normally.
            progress = []
            completed = False
            for _ in range(len(paths)):
                boundary = replica.next_boundary()
                if not progress:
                    # The live reset left no proven identity, so the hello claims none.
                    assert boundary.hello["replay_server_instance"] == ""
                    assert "replay_epoch" not in boundary.hello
                progress.append(replica.dispatcher.last_seq)
                completed = boundary.completed
                if completed:
                    break
            assert all(
                after > before for before, after in zip(progress, progress[1:], strict=False)
            ), f"live reset replay restarted instead of advancing: {progress}"
            assert completed, f"live reset replay never completed: {progress}"
            assert replica.dispatcher.last_seq == state.store.get_max_seq()
            assert replica.receiver.replay_epoch == 1
            assert replica.receiver.server_instance == state.server_instance
            assert all(replica.stage.GetPrimAtPath(path) for path in paths)
        finally:
            replica.close()
