"""Transaction admission, batching, and completion for the server commit worker.

Stage mutation and durable publication stay in the supplied commit callbacks.
This coordinator owns each request's maintenance barrier until completion.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from pxr import Sdf

from ..checkpoints import TransactionCheckpoint
from ._txn_barrier import _TxnBarrier
from .types import TransactionCommit, TransactionOutcome

LOG = logging.getLogger(__name__)
_QUEUE_MAX = 10_000


@dataclass(slots=True)
class TransactionRequest:
    """Submitted command and wait handle; only the coordinator sets its outcome."""

    events: list[dict]
    session_id: str
    txn_id: int
    client_id: str
    origin: str | None
    client_addr: str | None
    layer: Sdf.Layer | None
    layer_key: str
    done: threading.Event = field(default_factory=threading.Event)
    commit: TransactionCommit | None = None
    error: BaseException | None = None

    def wait(self) -> TransactionCommit:
        """Wait for the durable outcome, propagating a failed commit to its caller."""
        self.done.wait()
        if self.error is not None:
            raise self.error
        if self.commit is None:
            raise RuntimeError("transaction coordinator returned no result")
        return self.commit


class TransactionCoordinator:
    def __init__(
        self,
        *,
        barrier: _TxnBarrier,
        commit: Callable[[TransactionRequest], TransactionCommit],
        commit_group: Callable[[list[TransactionRequest]], list[TransactionOutcome]] | None,
        checkpoint: Callable[[], TransactionCheckpoint],
        batch_size: int,
        batch_delay: float,
    ):
        self._barrier = barrier
        self._commit = commit
        self._commit_group = commit_group
        self._checkpoint = checkpoint
        self._batch_size = batch_size
        self._batch_delay = batch_delay
        self._submission_lock = threading.RLock()
        self._stopping = False
        self._queue: queue.Queue[TransactionRequest | None] = queue.Queue(maxsize=_QUEUE_MAX)
        self._thread = (
            threading.Thread(target=self._run, name="ouc-transaction-commit", daemon=True)
            if batch_size > 1 and commit_group is not None else None
        )

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            self._thread.start()

    def shutdown(self) -> None:
        # Serialize the stop marker with admission so no request can be queued
        # behind it and retain a maintenance barrier forever.
        with self._submission_lock:
            if self._stopping:
                return
            self._stopping = True
            if self._thread is None or self._thread.ident is None:
                return
            self._queue.put(None)
        self._thread.join()

    def submit(self, request: TransactionRequest) -> TransactionRequest:
        with self._submission_lock:
            if self._stopping:
                raise RuntimeError("transaction coordinator is shutting down")
            # Ownership transfers to the worker after enqueue. Maintenance
            # waits for queued requests as well as active commits/publication.
            self._barrier.acquire_shared()
            if self._thread is None:
                try:
                    outcome = self._commit_one(request)
                except BaseException as exc:
                    self._complete([request], [exc])
                    raise
                self._complete([request], [outcome])
            else:
                try:
                    self._queue.put(request)
                except BaseException:
                    self._barrier.release_shared()
                    raise
        return request

    def _run(self) -> None:
        stop = False
        while not stop:
            first = self._queue.get()
            if first is None:
                return
            requests = [first]
            deadline = time.monotonic() + self._batch_delay
            while len(requests) < self._batch_size:
                try:
                    request = self._queue.get_nowait()
                except queue.Empty:
                    if time.monotonic() >= deadline:
                        break
                    # Sub-millisecond Queue.get timeouts round up to the OS
                    # scheduler quantum on Windows; yield without that delay.
                    time.sleep(0)
                    continue
                if request is None:
                    stop = True
                    break
                requests.append(request)
            self._execute(requests)

    def _execute(self, requests: list[TransactionRequest]) -> None:
        try:
            try:
                outcomes = self._commit_group(requests)
                if len(outcomes) != len(requests):
                    raise RuntimeError("group commit must return one outcome per request")
            except Exception:
                # The group callback restores layers and sequence reservations
                # on failure. Retry separately so one bad request cannot reject
                # its neighbors.
                LOG.debug("Grouped transaction commit failed; retrying individually", exc_info=True)
                outcomes = [self._commit_one(request) for request in requests]
        except BaseException as exc:
            self._complete(requests, [exc] * len(requests))
            raise
        self._complete(requests, outcomes)

    def _commit_one(self, request: TransactionRequest) -> TransactionOutcome:
        try:
            return self._commit(request)
        except Exception as exc:
            return exc

    def _complete(
        self, requests: list[TransactionRequest], outcomes: list[TransactionOutcome],
    ) -> None:
        checkpoint = None
        try:
            if any(
                isinstance(outcome, TransactionCommit) and outcome.status == "committed"
                for outcome in outcomes
            ):
                # Capture once per batch, while maintenance is still excluded.
                # A checkpoint failure must not turn a durable commit into failure.
                try:
                    checkpoint = self._checkpoint()
                except Exception:
                    LOG.exception("Could not capture transaction visibility checkpoint")
        finally:
            # Duplicates prove producer progress, not their original replay epoch.
            for request, outcome in zip(requests, outcomes, strict=True):
                try:
                    if isinstance(outcome, BaseException):
                        request.error = outcome
                        request.commit = None
                    else:
                        request.error = None
                        request.commit = (
                            replace(outcome, checkpoint=checkpoint)
                            if outcome.status == "committed" and checkpoint is not None
                            else outcome
                        )
                finally:
                    self._barrier.release_shared()
                    request.done.set()
