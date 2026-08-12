"""Read-write barrier for serializing compaction against in-flight transactions."""

from __future__ import annotations

import threading


class _TxnBarrier:
    """Read-write barrier: multiple shared (txn) holders, exclusive for compaction.

    Lock ordering: _TxnBarrier is acquired BEFORE stage_lock / _seq_lock.
    """

    def __init__(self):
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._exclusive = False

    def acquire_shared(self):
        with self._cond:
            while self._exclusive:
                self._cond.wait()
            self._readers += 1

    def release_shared(self):
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    def acquire_exclusive(self):
        with self._cond:
            while self._exclusive:
                self._cond.wait()
            self._exclusive = True
            while self._readers > 0:
                self._cond.wait()

    def release_exclusive(self):
        with self._cond:
            self._exclusive = False
            self._cond.notify_all()
