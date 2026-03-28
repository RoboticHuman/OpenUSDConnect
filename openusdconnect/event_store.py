"""Event log storage abstraction.

Defines the EventStore interface and a SQLite implementation.  Other
backends (PostgreSQL, DuckDB, etc.) can be plugged in by implementing
the same interface.
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
    def append(self, seq: int, record_json: str) -> None:
        """Persist a single event record (already JSON-serialized)."""
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
    def get_all_asc(self) -> list[tuple[int, str]]:
        """Return all (seq, record_json) tuples ordered by seq ascending."""
        raise NotImplementedError

    @abstractmethod
    def get_from_seq(self, seq_start: int) -> list[str]:
        """Return record_json strings for all events with seq >= seq_start."""
        raise NotImplementedError

    @abstractmethod
    def query(
        self,
        offset: int = 0,
        limit: int = 50,
        kind: str = "",
        prim_contains: str = "",
    ) -> tuple[list[str], int]:
        """Return a page of record_json strings (newest first) and total count.

        Filtering by event kind and prim path substring is optional.
        """
        raise NotImplementedError

    @abstractmethod
    def search_like(self, pattern: str) -> list[str]:
        """Return record_json strings where the stored text matches *pattern*."""
        raise NotImplementedError

    @abstractmethod
    def clear_and_rewrite(self, records: list[tuple[int, str]]) -> None:
        """Delete all events and insert *records* as (seq, record_json) pairs."""
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
                event TEXT NOT NULL
            )
        """)
        self._conn.commit()
        self._lock = threading.Lock()

    def append(self, seq: int, record_json: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(seq, event) VALUES (?, ?)",
                (seq, record_json),
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

    def get_all_asc(self) -> list[tuple[int, str]]:
        with self._lock:
            return self._conn.execute(
                "SELECT seq, event FROM events ORDER BY seq"
            ).fetchall()

    def get_from_seq(self, seq_start: int) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, event FROM events WHERE seq >= ? ORDER BY seq",
                (seq_start,),
            ).fetchall()
        return [row[1] for row in rows]

    def query(
        self,
        offset: int = 0,
        limit: int = 50,
        kind: str = "",
        prim_contains: str = "",
    ) -> tuple[list[str], int]:
        with self._lock:
            if kind or prim_contains:
                where_parts = []
                params: list = []
                if kind:
                    where_parts.append("event LIKE ?")
                    params.append(f'%"k": "{kind}"%')
                if prim_contains:
                    where_parts.append("event LIKE ?")
                    params.append(f'%"prim": "%{prim_contains}%')
                where_clause = " AND ".join(where_parts)

                count = self._conn.execute(
                    f"SELECT COUNT(*) FROM events WHERE {where_clause}",
                    params,
                ).fetchone()[0]
                rows = self._conn.execute(
                    f"SELECT event FROM events WHERE {where_clause}"
                    " ORDER BY seq DESC LIMIT ? OFFSET ?",
                    [*params, limit, offset],
                ).fetchall()
            else:
                count = self._conn.execute(
                    "SELECT COUNT(*) FROM events"
                ).fetchone()[0]
                rows = self._conn.execute(
                    "SELECT event FROM events ORDER BY seq DESC"
                    " LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()

        return [r[0] for r in rows], count

    def search_like(self, pattern: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT event FROM events WHERE event LIKE ? ORDER BY seq",
                (pattern,),
            ).fetchall()
        return [r[0] for r in rows]

    def clear_and_rewrite(self, records: list[tuple[int, str]]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM events")
            self._conn.executemany(
                "INSERT INTO events(seq, event) VALUES (?, ?)",
                records,
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()
