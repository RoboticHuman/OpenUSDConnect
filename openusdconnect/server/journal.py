"""Event sequencing, durable history, and optional asynchronous persistence.

The commit scope serializes sequence reservations through live publication.
The smaller state lock protects counters read by observers and maintenance.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field

from ..checkpoints import TransactionCheckpoint
from ..codec import BroadcastEventEncoder, encode_message
from ..event_store import EventStore, LayerIdentity, ProducerProgress
from ..protocol_constants import MSG_EVENT
from .metrics import WireMetrics

LOG = logging.getLogger(__name__)
type StoreRow = tuple[int, bytes, str | None, str | None, str | None]


def encode_event_record(
    sequence: int, event: dict, *, client: str | None = None,
    client_id: str | None = None, origin: str | None = None,
    layer_key: str | None = None, encoder: Callable[[dict], bytes] = encode_message,
) -> tuple[dict, bytes]:
    """Encode an explicit sequence without allocating it or changing journal state."""
    record = {"type": MSG_EVENT, "seq": sequence, "event": event,
              "client": client, "client_id": client_id}
    if origin:
        record["origin"] = origin
    if layer_key:
        record["layer_key"] = layer_key
    return record, encoder(record)


@dataclass(slots=True)
class EncodedEvents:
    records: list[tuple[dict, bytes]] = field(default_factory=list)
    store_rows: list[StoreRow] = field(default_factory=list)

    def append(self, record: dict, record_bin: bytes) -> None:
        event = record["event"]
        self.records.append((record, record_bin))
        self.store_rows.append(
            (record["seq"], record_bin, record.get("client_id"), event["k"], event.get("prim"))
        )


class EventJournal:
    def __init__(self, store: EventStore, *, durability: str, metrics: WireMetrics | None):
        self.store = store
        self.lock = threading.Lock()
        self._commit_lock = threading.RLock()
        self.next_seq = store.get_max_seq() + 1
        self.event_count = store.get_count()
        self.seq_at_last_compact = 1
        self.snapshot_epoch = 0
        self.replay_epoch = 0
        self._producer_progress: dict[tuple[str, str], int] = {}
        self._encoder = BroadcastEventEncoder()
        self._metrics = metrics
        self._queue: queue.Queue[list[StoreRow] | None] = queue.Queue(maxsize=10_000)
        self._thread = (
            threading.Thread(target=self._persist_loop, name="ouc-event-persist", daemon=True)
            if durability == "realtime"
            else None
        )

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            self._thread.start()

    def drain(self) -> None:
        self._queue.join()

    def shutdown(self) -> None:
        """Drain after transaction admission and maintenance workers have stopped."""
        if self.running:
            self._queue.put(None)
            self._thread.join()

    @contextmanager
    def commit_scope(self):
        """Serialize a commit and its publication as one ordering boundary.

        Transaction callers acquire the maintenance barrier first, then this
        scope, then the scene lock when accessing USD. Keep this scope open
        until live records are enqueued so later sequences cannot publish first.
        The journal's counter lock is held only for short state accesses and
        never while acquiring the barrier, this scope, or the scene lock.
        """
        with self._commit_lock:
            yield

    def assign_seq(self) -> int:
        with self.lock:
            sequence = self.next_seq
            self.next_seq += 1
            return sequence

    @contextmanager
    def reserve_sequences(self):
        """Restore reservations on failure inside the caller's commit scope."""
        with self.lock:
            first_sequence = self.next_seq
        try:
            yield
        except Exception:
            with self.lock:
                self.next_seq = first_sequence
            raise

    def snapshot_token(self) -> tuple[int, int]:
        with self.lock:
            return self.snapshot_epoch, max(0, self.next_seq - 1)

    def bump_snapshot_epoch(self) -> int:
        with self.lock:
            self.snapshot_epoch += 1
            return self.snapshot_epoch

    def needs_compaction(self) -> bool:
        with self.lock:
            return self.next_seq > self.seq_at_last_compact

    def replay_token(self) -> tuple[int, int]:
        with self.lock:
            return self.replay_epoch, max(0, self.next_seq - 1)

    def durable_checkpoint(self) -> TransactionCheckpoint:
        """Capture persisted history while the caller excludes maintenance."""
        # Reservations can roll back: visibility must describe persisted history.
        return TransactionCheckpoint(epoch=self.replay_epoch, head_seq=self.store.get_max_seq())

    def reset_history(self, record_count: int) -> None:
        """Publish a durable replacement while the exclusive barrier is held."""
        with self.lock:
            self.event_count = record_count
            self.next_seq = record_count + 1
            self.seq_at_last_compact = self.next_seq
            self.snapshot_epoch += 1
            self.replay_epoch += 1

    def committed_through(self, client_id: str, session_id: str) -> int:
        """Read progress safely, including outside a transaction commit scope."""
        with self._commit_lock:
            producer = (client_id, session_id)
            cached = self._producer_progress.get(producer)
            if cached is None:
                cached = self.store.get_producer_progress(client_id, session_id)
                self._producer_progress[producer] = cached
            return cached

    def remember_progress(self, updates: dict[tuple[str, str], int]) -> None:
        """Cache persisted progress; committing callers retain their outer scope."""
        with self._commit_lock:
            self._producer_progress.update(updates)

    def encode_events(
        self,
        routed_events: Iterable[tuple[str, dict]],
        *,
        client_id: str | None,
        origin: str | None,
        client_addr: str | None,
    ) -> EncodedEvents:
        encoded = EncodedEvents()
        encoder = self._encoder.encode
        for layer_key, event in routed_events:
            encoded.append(*self.encode_event(
                event, client_id=client_id, origin=origin,
                client_addr=client_addr, layer_key=layer_key, encoder=encoder,
            ))
        return encoded

    def encode_event(
        self, event: dict, *, client_id: str | None = None,
        origin: str | None = None, client_addr: str | None = None,
        layer_key: str | None = None,
        encoder: Callable[[dict], bytes] = encode_message,
    ) -> tuple[dict, bytes]:
        """Sequence and encode one event within the caller's commit scope."""
        record, record_bin = encode_event_record(
            self.assign_seq(), event, client=client_addr, client_id=client_id,
            origin=origin, layer_key=layer_key, encoder=encoder,
        )
        if self._metrics is not None:
            self._metrics.record(event["k"], len(record_bin))
        return record, record_bin

    def append_record(self, record: dict) -> bytes:
        event = record.get("event", {})
        record_bin = encode_message(record)
        self.store.append(
            record["seq"],
            record_bin,
            kind=event.get("k"),
            prim=event.get("prim"),
        )
        with self.lock:
            self.event_count += 1
        return record_bin

    def append_batch(
        self,
        rows: list[StoreRow],
        *,
        producer_progress: tuple[ProducerProgress, ...] = (),
        layer_identities: tuple[LayerIdentity, ...] = (),
        synchronous: bool = False,
    ) -> None:
        """Persist synchronously when requested or when durable metadata requires it.

        Other writes may be queued in realtime mode. A return from a queued
        write confirms admission only; use drain() before replacing history.
        """
        # Producer acknowledgements and graph identities always require a
        # durable commit. Only independent records may use realtime persistence.
        can_queue = self._thread is not None and not (
            synchronous or producer_progress or layer_identities
        )
        if can_queue:
            self._queue.put(rows)
            return
        if layer_identities:
            self.store.append_batch(
                rows,
                producer_progress=producer_progress,
                layer_identities=layer_identities,
            )
        else:
            self.store.append_batch(rows, producer_progress=producer_progress)
        with self.lock:
            self.event_count += len(rows)

    def _persist_loop(self) -> None:
        while True:
            rows = self._queue.get()
            try:
                if rows is None:
                    return
                self.store.append_batch(rows)
                with self.lock:
                    self.event_count += len(rows)
            except Exception:
                LOG.exception("Unexpected error in persist loop")
            finally:
                self._queue.task_done()
