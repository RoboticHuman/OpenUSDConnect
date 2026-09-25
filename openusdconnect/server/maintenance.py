"""Exclusive history replacement and background compaction scheduling.

HistoryMaintenance owns the durable rewrite boundary. The caller retains its
exclusive window through receiver publication; networking stays in the server.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager, nullcontext

from ..codec import encode_message
from ..protocol_constants import MSG_LAYER_GRAPH_STATE, NON_COLLABORATION_KINDS
from ._txn_barrier import _TxnBarrier
from .compaction import LogCompaction
from .journal import EncodedEvents, EventJournal, encode_event_record
from .scene import SceneState
from .snapshots import PreparedSnapshot

LOG = logging.getLogger(__name__)


class PeriodicCompactor:
    """Schedule optional compaction without owning the scene or publication."""

    def __init__(self, compact: Callable[[], None], journal: EventJournal, interval: float):
        self._compact = compact
        self._journal = journal
        self._compact_interval = max(0.0, float(interval or 0))
        self._compact_stop = False
        self._compact_wake = threading.Event()
        self._compact_thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._compact_thread is not None and self._compact_thread.is_alive()

    def request_stop(self) -> None:
        self._compact_stop = True
        self._compact_wake.set()

    def join(self) -> None:
        if self._compact_thread is not None and self._compact_thread.ident is not None:
            self._compact_thread.join()

    def start(self) -> None:
        if self._compact_interval > 0 and self._compact_thread is None:
            self._start_compaction_thread()

    def _start_compaction_thread(self):
        self._compact_thread = threading.Thread(
            target=self._compaction_loop,
            daemon=True,
        )
        self._compact_thread.start()

    def set_compact_interval(self, seconds: float) -> None:
        """Set the periodic compaction interval in seconds (0 disables).

        Takes effect immediately: the compaction thread re-reads the
        interval on wake, so shortening, lengthening, and disabling all
        apply without waiting out the previous period.
        """
        self._compact_interval = max(0.0, float(seconds or 0))
        if self._compact_interval > 0 and self._compact_thread is None:
            self._start_compaction_thread()
        self._compact_wake.set()

    def get_compact_interval(self) -> float:
        return self._compact_interval

    def _compaction_loop(self):
        """Compact every interval, skipping when no event arrived since the
        last compaction so idle servers don't resync receivers for nothing."""
        while not self._compact_stop:
            interval = self._compact_interval
            if interval <= 0:
                self._compact_wake.wait()
                self._compact_wake.clear()
                continue
            if self._compact_wake.wait(timeout=interval):
                self._compact_wake.clear()
                continue
            if self._compact_stop:
                return
            if not self._journal.needs_compaction():
                continue
            try:
                self._compact()
            except Exception:
                LOG.exception("Periodic compaction failed")


class HistoryMaintenance:
    def __init__(
        self, scene: SceneState, journal: EventJournal, barrier: _TxnBarrier, *,
        drain_broadcasts: Callable[[], None], reclaim_interval: float,
    ):
        self._scene = scene
        self._journal = journal
        self._barrier = barrier
        self._drain_broadcasts = drain_broadcasts
        self.store = journal.store
        self._reclaim_interval = max(0.0, float(reclaim_interval or 0))
        self._last_reclaim = time.monotonic()

    @contextmanager
    def exclusive(self):
        """Drain earlier transactions before rewriting or publishing a new history."""
        with self._barrier.exclusive():
            self.drain()
            yield

    def drain(self) -> None:
        """Finish earlier writes and publication while holding the exclusive barrier."""
        self._journal.drain()
        self._drain_broadcasts()

    def _finish_rewrite(self, count: int) -> None:
        """Publish the new epoch after durable history and scene caches agree."""
        self._journal.reset_history(count)
        self._maybe_reclaim_storage()

    def set_reclaim_interval(self, seconds: float) -> None:
        """Set the storage-reclaim interval in seconds (0 disables).

        Reclaim runs at compaction and purge commits, so an enabled
        interval needs compaction (periodic or manual) to take effect.
        """
        self._reclaim_interval = max(0.0, float(seconds or 0))

    def get_reclaim_interval(self) -> float:
        return self._reclaim_interval

    def _maybe_reclaim_storage(self) -> None:
        """Reclaim store disk space when the interval has elapsed.

        Called right after a log rewrite while the exclusive barrier is
        held; reclaiming then is cheap because only live data is copied.
        """
        if self._reclaim_interval <= 0:
            return
        if time.monotonic() - self._last_reclaim < self._reclaim_interval:
            return
        try:
            reclaimed = self.store.reclaim_storage()
        except Exception:
            LOG.exception("Failed to reclaim event-store storage")
            return
        self._last_reclaim = time.monotonic()
        if reclaimed:
            LOG.info("Reclaimed %.1f MB of event log storage", reclaimed / 1048576)

    @staticmethod
    def build_compacted(rows: list[tuple[int, bytes]]) -> LogCompaction:
        compaction = LogCompaction()
        for seq, record_bin in rows:
            compaction.add_record(seq, record_bin)
        return compaction

    def commit_compaction(
        self,
        compaction: LogCompaction,
        original_count: int,
    ):
        """Replace compacted history; the caller holds the exclusive window."""
        compacted = compaction.replay_records()
        graph = self._scene.shared_layer_graph
        if graph is not None:
            reachable = set(graph.reachable_layer_keys())
            compacted = [record for record in compacted if record.layer_key in reachable]

        graph_transaction = graph.transaction() if graph is not None else nullcontext()
        with graph_transaction:
            records = []
            first_event_seq = 1
            if graph is not None:
                # Start a new replay baseline without changing layer identities.
                graph.start_new_generation()
                graph_record = graph.state_message(seq=1)
                records.append(
                    (1, encode_message(graph_record), None, MSG_LAYER_GRAPH_STATE, None)
                )
                first_event_seq = 2

            encoded = EncodedEvents()
            for seq, record in enumerate(compacted, start=first_event_seq):
                encoded.append(*encode_event_record(
                    seq, record.event, origin=record.origin, client=record.client,
                    client_id=record.client_id, layer_key=record.layer_key,
                ))
            records.extend(encoded.store_rows)
            self.store.clear_and_rewrite(records)
        self._scene.rebuild_caches(record.event for record in compacted)
        self._finish_rewrite(len(records))

        LOG.info("Compacted event log: %d -> %d records", original_count, len(records))

    def purge(self):
        # Producer high-water marks survive a scene purge. Otherwise an old
        # ambiguous retry could resurrect pre-purge edits, and still-connected
        # producers would be rejected for starting above transaction 1.
        self.store.clear_and_rewrite([])
        self._scene.clear_collaboration_layers()
        self._finish_rewrite(0)
        LOG.info("Purged event log and reset authored collaboration layers")

    def replace_snapshot(
        self, prepared: PreparedSnapshot, *, client_id: str, origin: str,
    ) -> EncodedEvents:
        """Persist a prepared snapshot before changing the authoritative scene."""
        encoded = EncodedEvents()
        for seq, event in enumerate(prepared.events, start=1):
            layer_key = None if event["k"] in NON_COLLABORATION_KINDS else "default"
            encoded.append(*encode_event_record(
                seq, event, client_id=client_id, origin=origin, layer_key=layer_key,
            ))

        # Validation and preparation can reject or identify an unchanged save
        # without waiting for outgoing traffic. Drain only an actual rewrite.
        self.drain()

        # This is the durability boundary. EventStore implementations must
        # leave the previous log intact if the atomic replacement fails.
        # Authoritative in-memory state is deliberately unchanged until it
        # returns successfully.
        self.store.clear_and_rewrite(encoded.store_rows)

        self._scene.install_snapshot(prepared)
        self._finish_rewrite(len(encoded.records))
        return encoded
