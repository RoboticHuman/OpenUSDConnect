"""Concurrency invariants for the server maintenance barrier."""

import threading

from openusdconnect.server._txn_barrier import _TxnBarrier


def test_exclusive_owners_never_overlap():
    barrier = _TxnBarrier()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_attempted = threading.Event()
    second_entered = threading.Event()
    release_second = threading.Event()
    second = None

    def first_owner():
        barrier.acquire_exclusive()
        try:
            first_entered.set()
            release_first.wait(timeout=5)
        finally:
            barrier.release_exclusive()

    def second_owner():
        second_attempted.set()
        barrier.acquire_exclusive()
        try:
            second_entered.set()
            release_second.wait(timeout=5)
        finally:
            barrier.release_exclusive()

    first = threading.Thread(target=first_owner)
    first.start()
    try:
        assert first_entered.wait(timeout=5)
        second = threading.Thread(target=second_owner)
        second.start()
        assert second_attempted.wait(timeout=5)
        assert not second_entered.wait(timeout=0.1)

        release_first.set()
        assert second_entered.wait(timeout=5)
    finally:
        release_first.set()
        release_second.set()
        first.join(timeout=5)
        if second is not None:
            second.join(timeout=5)
    assert not first.is_alive()
    assert second is None or not second.is_alive()


def test_reader_waits_for_active_exclusive_owner_after_handoff():
    barrier = _TxnBarrier()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    release_second = threading.Event()
    reader_entered = threading.Event()
    second = None

    def first_owner():
        barrier.acquire_exclusive()
        try:
            first_entered.set()
            release_first.wait(timeout=5)
        finally:
            barrier.release_exclusive()

    def second_owner():
        barrier.acquire_exclusive()
        try:
            second_entered.set()
            release_second.wait(timeout=5)
        finally:
            barrier.release_exclusive()

    def reader():
        barrier.acquire_shared()
        try:
            reader_entered.set()
        finally:
            barrier.release_shared()

    first = threading.Thread(target=first_owner)
    first.start()
    try:
        assert first_entered.wait(timeout=5)
        second = threading.Thread(target=second_owner)
        second.start()
        release_first.set()
        assert second_entered.wait(timeout=5)

        reading = threading.Thread(target=reader)
        reading.start()
        assert not reader_entered.wait(timeout=0.1)
        release_second.set()
        assert reader_entered.wait(timeout=5)
        reading.join(timeout=5)
        assert not reading.is_alive()
    finally:
        release_first.set()
        release_second.set()
        first.join(timeout=5)
        if second is not None:
            second.join(timeout=5)
    assert not first.is_alive()
    assert second is None or not second.is_alive()
