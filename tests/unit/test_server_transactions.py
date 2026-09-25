"""Commit worker admission and maintenance-barrier ownership."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from openusdconnect.checkpoints import TransactionCheckpoint
from openusdconnect.server._txn_barrier import _TxnBarrier
from openusdconnect.server.transactions import TransactionCoordinator, TransactionRequest
from openusdconnect.server.types import TransactionCommit


def request(txn_id):
    return TransactionRequest(
        events=[], session_id="session", txn_id=txn_id, client_id="client",
        origin=None, client_addr=None, layer=None, layer_key="",
    )


def commit_one(item):
    return TransactionCommit("committed", item.txn_id)


def coordinator(*, commit=commit_one, commit_group=None, batch_size=1):
    return TransactionCoordinator(
        barrier=_TxnBarrier(), commit=commit, commit_group=commit_group,
        checkpoint=lambda: TransactionCheckpoint(epoch=0, head_seq=1),
        batch_size=batch_size, batch_delay=0,
    )


def assert_maintenance_can_enter(barrier):
    entered = threading.Event()

    def maintenance():
        with barrier.exclusive():
            entered.set()

    worker = threading.Thread(target=maintenance, daemon=True)
    worker.start()
    assert entered.wait(5), "completed requests still block maintenance"
    worker.join(5)


def test_shutdown_drains_queued_requests_and_releases_their_barriers():
    entered = threading.Event()
    release = threading.Event()

    def commit_group(requests):
        entered.set()
        assert release.wait(5)
        assert all(item.commit is None and not item.done.is_set() for item in requests)
        return [TransactionCommit("committed", item.txn_id) for item in requests]

    worker = coordinator(commit_group=commit_group, batch_size=8)
    worker.start()
    try:
        first = worker.submit(request(1))
        assert entered.wait(5)
        second = worker.submit(request(2))
        with ThreadPoolExecutor(max_workers=1) as executor:
            stopped = executor.submit(worker.shutdown)
            try:
                assert not first.done.is_set()
                assert not second.done.is_set()
            finally:
                release.set()
            stopped.result(timeout=5)
        assert first.done.is_set() and second.done.is_set()
        assert first.wait().txn_id == 1
        assert second.wait().txn_id == 2
        assert_maintenance_can_enter(worker._barrier)
        assert not worker.running
        with pytest.raises(RuntimeError, match="shutting down"):
            worker.submit(request(3))
    finally:
        release.set()
        worker.shutdown()


def test_submission_cannot_race_past_shutdown_marker(monkeypatch):
    worker = coordinator(
        commit_group=lambda requests: [commit_one(item) for item in requests], batch_size=8,
    )
    stop_entered = threading.Event()
    allow_stop = threading.Event()
    submit_entered = threading.Event()
    put = worker._queue.put

    def hold_stop_marker(item):
        if item is None:
            stop_entered.set()
            assert allow_stop.wait(5)
        put(item)

    def submit():
        submit_entered.set()
        return worker.submit(request(1))

    monkeypatch.setattr(worker._queue, "put", hold_stop_marker)
    worker.start()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            stopped = executor.submit(worker.shutdown)
            try:
                assert stop_entered.wait(5)
                submitted = executor.submit(submit)
                assert submit_entered.wait(5)
            finally:
                allow_stop.set()
            stopped.result(timeout=5)
            with pytest.raises(RuntimeError, match="shutting down"):
                submitted.result(timeout=5)
        assert_maintenance_can_enter(worker._barrier)
        assert not worker.running
    finally:
        allow_stop.set()
        worker.shutdown()


def test_synchronous_commit_can_submit_from_an_in_process_callback():
    nested = []

    def commit(item):
        if item.txn_id == 1:
            nested.append(worker.submit(request(2)).wait())
        return TransactionCommit("committed", item.txn_id)

    worker = coordinator(commit=commit)
    try:
        assert worker.submit(request(1)).wait().txn_id == 1
        assert nested[0].txn_id == 2
        assert_maintenance_can_enter(worker._barrier)
    finally:
        worker.shutdown()


def test_group_outcomes_are_completed_by_the_coordinator():
    failure = ValueError("request rejected")

    def commit_group(requests):
        assert all(item.commit is None and item.error is None for item in requests)
        return [
            TransactionCommit("committed", item.txn_id) if item.txn_id == 1 else failure
            for item in requests
        ]

    worker = coordinator(commit_group=commit_group, batch_size=8)
    worker.start()
    try:
        committed = worker.submit(request(1))
        rejected = worker.submit(request(2))
        assert committed.done.wait(5)
        assert rejected.done.wait(5)
        assert committed.wait().checkpoint == TransactionCheckpoint(epoch=0, head_seq=1)
        with pytest.raises(ValueError, match="request rejected"):
            rejected.wait()
        assert_maintenance_can_enter(worker._barrier)
    finally:
        worker.shutdown()


@pytest.mark.parametrize("grouped", [False, True])
def test_interrupted_callback_completes_handles_and_releases_maintenance(grouped):
    def interrupt(_request):
        raise KeyboardInterrupt("interrupted commit")

    worker = coordinator(commit=interrupt, commit_group=interrupt, batch_size=8 if grouped else 1)
    item = request(1)
    try:
        with pytest.raises(KeyboardInterrupt, match="interrupted commit"):
            if grouped:
                worker._barrier.acquire_shared()
                worker._execute([item])
            else:
                worker.submit(item)
        assert item.done.is_set()
        with pytest.raises(KeyboardInterrupt, match="interrupted commit"):
            item.wait()
        assert_maintenance_can_enter(worker._barrier)
    finally:
        worker.shutdown()
