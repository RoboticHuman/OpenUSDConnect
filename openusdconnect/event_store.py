"""Event log storage abstraction.

Defines the EventStore interface and a SQLite implementation.  Other
backends (PostgreSQL, DuckDB, etc.) can be plugged in by implementing
the same interface.

Events are stored as binary FlatBuffers blobs.  Dict conversion is done
on read via ``codec.message_to_dict()`` only when needed (compaction,
dashboard).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from abc import ABC, abstractmethod

LOG = logging.getLogger(__name__)


class EventStore(ABC):
    """Abstract interface for persisting and querying the event log."""

    @abstractmethod
    def append(self, seq: int, record_bin: bytes,
               client_id: str | None = None,
               kind: str | None = None,
               prim: str | None = None) -> None:
        """Persist a single event record (FlatBuffers binary)."""
        raise NotImplementedError

    @abstractmethod
    def append_batch(self, records: list[tuple[int, bytes, str | None,
                       str | None, str | None]]) -> None:
        """Persist multiple event records in a single transaction.

        Each record is (seq, record_bin, client_id, kind, prim).
        """
        raise NotImplementedError

    @abstractmethod
    def get_max_seq(self) -> int:
        """Return the highest sequence number, or 0 if empty."""
        raise NotImplementedError

    @abstractmethod
    def get_count(self) -> int:
        """Return the total number of stored events."""
        raise NotImplementedError

    @abstractmethod
    def get_all_asc(self) -> list[tuple[int, bytes]]:
        """Return all (seq, record_bin) tuples ordered by seq ascending."""
        raise NotImplementedError

    @abstractmethod
    def get_from_seq_bin(self, seq_start: int) -> list[bytes]:
        """Return binary blobs for all events with seq >= seq_start.

        This is the primary replay path — binary is sent directly to
        receivers without re-serialization.
        """
        raise NotImplementedError

    @abstractmethod
    def get_from_seq_asc(self, seq_start: int) -> list[tuple[int, bytes]]:
        """Return (seq, record_bin) tuples for events with seq >= seq_start."""
        raise NotImplementedError

    @abstractmethod
    def query(
        self,
        offset: int = 0,
        limit: int = 50,
        kind: str = "",
        prim_contains: str = "",
    ) -> tuple[list[bytes], int]:
        """Return a page of record blobs (newest first) and total count.

        Filtering by event kind and prim path substring is optional.
        Callers decode with ``codec.message_to_dict()`` as needed.
        """
        raise NotImplementedError

    @abstractmethod
    def get_by_prim_prefix(self, prefix: str, kinds: set[str]) -> list[bytes]:
        """Return record blobs whose prim starts with *prefix* and whose
        kind is in *kinds*, ordered by seq ascending."""
        raise NotImplementedError

    @abstractmethod
    def clear_and_rewrite(self, records: list[tuple[int, bytes, str | None,
                       str | None, str | None]]) -> None:
        """Delete all events and insert *records* as
        (seq, record_bin, client_id, kind, prim) tuples."""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """Release resources (close connections, etc.)."""
        raise NotImplementedError


class SqliteEventStore(EventStore):
    """SQLite-backed event store with WAL mode for concurrent reads."""

    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY,
                event_bin BLOB NOT NULL,
                client_id TEXT,
                kind TEXT,
                prim TEXT
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_client_id ON events(client_id)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_prim ON events(prim)"
        )
        self._conn.commit()
        self._lock = threading.Lock()

    def append(self, seq: int, record_bin: bytes,
               client_id: str | None = None,
               kind: str | None = None,
               prim: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(seq, event_bin, client_id, kind, prim)"
                " VALUES (?, ?, ?, ?, ?)",
                (seq, record_bin, client_id, kind, prim),
            )
            self._conn.commit()

    def append_batch(self, records: list[tuple[int, bytes, str | None,
                       str | None, str | None]]) -> None:
        if not records:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO events(seq, event_bin, client_id, kind, prim)"
                " VALUES (?, ?, ?, ?, ?)",
                records,
            )
            self._conn.commit()

    def get_max_seq(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(seq) FROM events"
            ).fetchone()
            return row[0] or 0

    def get_count(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM events"
            ).fetchone()[0]

    def get_all_asc(self) -> list[tuple[int, bytes]]:
        with self._lock:
            return self._conn.execute(
                "SELECT seq, event_bin FROM events ORDER BY seq"
            ).fetchall()

    def get_from_seq_bin(self, seq_start: int) -> list[bytes]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_bin FROM events WHERE seq >= ? ORDER BY seq",
                (seq_start,),
            ).fetchall()
        return [row[0] for row in rows]

    def get_from_seq_asc(self, seq_start: int) -> list[tuple[int, bytes]]:
        with self._lock:
            return self._conn.execute(
                "SELECT seq, event_bin FROM events WHERE seq >= ? ORDER BY seq",
                (seq_start,),
            ).fetchall()

    def query(
        self,
        offset: int = 0,
        limit: int = 50,
        kind: str = "",
        prim_contains: str = "",
    ) -> tuple[list[bytes], int]:
        with self._lock:
            conditions = []
            params: list = []
            if kind:
                conditions.append("kind = ?")
                params.append(kind)
            if prim_contains:
                conditions.append("prim LIKE '%' || ? || '%'")
                params.append(prim_contains)

            where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

            total = self._conn.execute(
                f"SELECT COUNT(*) FROM events{where}", params,
            ).fetchone()[0]

            rows = self._conn.execute(
                f"SELECT event_bin FROM events{where}"
                " ORDER BY seq DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()

        return [r[0] for r in rows], total

    def get_by_prim_prefix(self, prefix: str, kinds: set[str]) -> list[bytes]:
        if not kinds:
            return []
        placeholders = ",".join("?" for _ in kinds)
        # Range scan on the indexed prim column. Prim paths are ASCII, so
        # appending U+FFFF gives a tight exclusive upper bound without the
        # collation caveats of LIKE.
        upper = prefix + "\uffff"
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_bin FROM events"
                f" WHERE prim >= ? AND prim < ? AND kind IN ({placeholders})"
                " ORDER BY seq",
                [prefix, upper, *kinds],
            ).fetchall()
        return [r[0] for r in rows]

    def clear_and_rewrite(self, records: list[tuple[int, bytes, str | None,
                       str | None, str | None]]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM events")
            self._conn.executemany(
                "INSERT INTO events(seq, event_bin, client_id, kind, prim)"
                " VALUES (?, ?, ?, ?, ?)",
                records,
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()
