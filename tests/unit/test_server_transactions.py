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


def test_shutdown_drains_queued_requests_and_releases_their_barriers():
    entered = threading.Event()
    release = threading.Event()

    def commit_group(requests):
        entered.set()
        assert release.wait(5)
        for item in requests:
            item.commit = TransactionCommit("committed", item.txn_id)

    worker = coordinator(commit_group=commit_group, batch_size=8)
    worker.start()
    try:
        first = worker.submit(request(1))
        assert entered.wait(5)
        second = worker.submit(request(2))
        assert worker._barrier._readers == 2
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
        assert worker._barrier._readers == 0
        assert not worker.running
        with pytest.raises(RuntimeError, match="shutting down"):
            worker.submit(request(3))
    finally:
        release.set()
        worker.shutdown()


def test_submission_cannot_race_past_shutdown_marker(monkeypatch):
    worker = coordinator(commit_group=lambda requests: None, batch_size=8)
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
        assert worker._barrier._readers == 0
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
        assert worker._barrier._readers == 0
    finally:
        worker.shutdown()
