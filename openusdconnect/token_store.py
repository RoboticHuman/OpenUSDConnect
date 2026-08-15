"""TOFU (Trust On First Use) token store.

Manages authentication tokens for client identity verification.
Tokens are issued on first connect and verified on reconnect.
Storage is SQLite same as the event store.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import threading
import time

LOG = logging.getLogger(__name__)

TOKEN_LENGTH = 32  # 256-bit tokens


class TokenStore:
    """SQLite-backed token store for TOFU authentication."""

    def __init__(self, db_path: str = "tokens.db"):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS tokens (
                client_id TEXT PRIMARY KEY,
                token TEXT NOT NULL,
                department TEXT,
                created_at REAL NOT NULL,
                last_seen REAL NOT NULL
            )
        """)
        self._conn.commit()

    def issue(self, client_id: str, department: str | None = None) -> str:
        """Issue a new token for a client_id. Overwrites any existing token."""
        token = secrets.token_hex(TOKEN_LENGTH)
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tokens "
                "(client_id, token, department, created_at, last_seen) "
                "VALUES (?, ?, ?, ?, ?)",
                (client_id, token, department, now, now),
            )
            self._conn.commit()
        LOG.info("Issued token for %s", client_id)
        return token

    def verify(self, client_id: str, token: str) -> bool:
        """Verify a token matches the stored one. Updates last_seen on success."""
        with self._lock:
            row = self._conn.execute(
                "SELECT token FROM tokens WHERE client_id = ?",
                (client_id,),
            ).fetchone()
            if row is None or not secrets.compare_digest(row[0], token):
                return False
            self._conn.execute(
                "UPDATE tokens SET last_seen = ? WHERE client_id = ?",
                (time.time(), client_id),
            )
            self._conn.commit()
        return True

    def has_token(self, client_id: str) -> bool:
        """Check if a client_id already has a token issued."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tokens WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        return row is not None

    def revoke(self, client_id: str) -> bool:
        """Revoke a client's token. Returns True if a token was deleted."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM tokens WHERE client_id = ?",
                (client_id,),
            )
            self._conn.commit()
        revoked = cursor.rowcount > 0
        if revoked:
            LOG.info("Revoked token for %s", client_id)
        return revoked

    def get_all(self) -> list[dict]:
        """Return all token records (without the actual token values)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT client_id, department, created_at, last_seen FROM tokens"
            ).fetchall()
        return [
            {
                "client_id": r[0],
                "department": r[1],
                "created_at": r[2],
                "last_seen": r[3],
            }
            for r in rows
        ]

    def close(self):
        self._conn.close()
