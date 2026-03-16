"""Authoritative TCP sequencer server.

Maintains an in-memory Usd.Stage, accepts transactions from emitters,
applies them atomically, assigns monotonic sequence numbers, broadcasts
to all connected receivers, and logs events to a SQLite database for replay.

CLI usage:
    python -m openusdconnect.server --port 7200 --base test_scene.usda --log events.db
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import signal
import socketserver
import sqlite3
import threading

from pxr import Usd

from .protocol import (
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_LOAD_PAYLOAD,
    K_SET_VISIBILITY,
    K_SET_XFORM_TRS,
)

LOG = logging.getLogger(__name__)


class UsdSyncServer:
    """Holds all shared server state: stage, sequence counter, client list, SQLite event log."""

    def __init__(self, base_usd_path: str | None = None, log_path: str = "usd_events.db"):
        if base_usd_path:
            self.stage = Usd.Stage.Open(base_usd_path)
            if self.stage is None:
                raise RuntimeError(f"Failed to open base USD: {base_usd_path}")
        else:
            self.stage = Usd.Stage.CreateInMemory()
            # Define a minimal root prim — name is arbitrary for an empty stage
            self.stage.DefinePrim("/Root", "Xform")

        self.log_path = log_path
        self.stage_lock = threading.Lock()
        self.clients_lock = threading.Lock()
        self.receivers: set = set()
        self._seq_lock = threading.Lock()

        # SQLite event log with WAL mode for concurrent reads
        self.db_conn = self._init_db(log_path)
        self.db_lock = threading.Lock()

        # Resume sequence counter from existing DB
        self._next_seq = self._load_max_seq() + 1

    def _init_db(self, db_path: str) -> sqlite3.Connection:
        """Initialize SQLite database with events table."""
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY,
                event TEXT NOT NULL
            )
        """)
        conn.commit()
        return conn

    def _load_max_seq(self) -> int:
        """Read the highest seq from the DB, or 0 if empty."""
        try:
            row = self.db_conn.execute("SELECT MAX(seq) FROM events").fetchone()
            return row[0] or 0
        except sqlite3.Error as e:
            LOG.warning("Failed to load max seq: %s", e)
            return 0

    def assign_seq(self) -> int:
        with self._seq_lock:
            s = self._next_seq
            self._next_seq += 1
            return s

    def append_log(self, rec: dict):
        """Append event to SQLite database."""
        try:
            with self.db_lock:
                self.db_conn.execute(
                    "INSERT INTO events(seq, event) VALUES (?, ?)", (rec["seq"], json.dumps(rec))
                )
                self.db_conn.commit()
        except Exception:
            LOG.exception("Failed to write event log")

    def replay_children_after_load(self, prim_path: str):
        """After load_payload, re-broadcast the latest events for children.

        Queries the event log for the most recent structural and TRS events
        for each child of prim_path, assigns new sequence numbers, and
        broadcasts them so receivers re-apply the authoritative state.

        Also reactivates children on the server's stage that may have been
        deactivated by _detect_deletions during a previous unload cycle.
        """
        # Reactivate children on the server's stage (clear stale SetActive(False))
        with self.stage_lock:
            prim = self.stage.GetPrimAtPath(prim_path)
            if prim and prim.IsValid():
                for child in Usd.PrimRange(prim, Usd.PrimAllPrimsPredicate):
                    if not child.IsActive():
                        child.SetActive(True)

        prefix = prim_path + "/"
        replay_kinds = {K_ENSURE_PRIM, K_ENSURE_XFORM_OPS, K_SET_XFORM_TRS, K_SET_VISIBILITY}

        # Pre-filter with LIKE to avoid deserializing the entire event log.
        like_pattern = f'%"prim": "{prefix}%'
        with self.db_lock:
            rows = self.db_conn.execute(
                "SELECT event FROM events WHERE event LIKE ? ORDER BY seq",
                (like_pattern,),
            ).fetchall()

        # Collect the latest event of each relevant kind per child prim
        latest: dict[tuple[str, str], dict] = {}
        for (event_json,) in rows:
            rec = json.loads(event_json)
            ev = rec.get("event", rec)
            ep = ev.get("prim", "")
            ek = ev.get("k", "")
            if ep.startswith(prefix) and ek in replay_kinds:
                latest[(ep, ek)] = ev

        if not latest:
            return

        # Order: ensure_prim → ensure_xform_ops → set_xform_trs → set_visibility
        kind_order = {K_ENSURE_PRIM: 0, K_ENSURE_XFORM_OPS: 1, K_SET_XFORM_TRS: 2, K_SET_VISIBILITY: 3}
        sorted_events = sorted(
            latest.values(),
            key=lambda e: (e["prim"], kind_order.get(e["k"], 99)),
        )

        for ev in sorted_events:
            rec = {"type": "event", "seq": self.assign_seq(), "event": ev}
            self.append_log(rec)
            self.broadcast(rec)

        LOG.info(
            "Replayed %d child events after load_payload %s",
            len(sorted_events),
            prim_path,
        )

    def broadcast(self, rec: dict):
        line = (json.dumps(rec) + "\n").encode("utf-8")
        dead = []
        with self.clients_lock:
            for h in self.receivers:
                try:
                    h.request.sendall(line)
                except OSError:
                    LOG.debug("Broadcast failed for %s, marking as dead", h.client_address)
                    dead.append(h)
            for h in dead:
                self.receivers.discard(h)

    def apply_txn(self, events: list[dict]):
        from .event_apply import apply_events

        with self.stage_lock:
            apply_events(self.stage, events)

    def replay_from(self, handler, seq_start: int):
        """Replay events from SQLite database starting at seq_start."""
        try:
            cursor = self.db_conn.cursor()
            cursor.execute(
                "SELECT seq, event FROM events WHERE seq >= ? ORDER BY seq", (seq_start,)
            )
            for row in cursor:
                handler.request.sendall((row[1] + "\n").encode("utf-8"))
        except Exception:
            LOG.exception("Failed to replay events")


class ConnectionHandler(socketserver.StreamRequestHandler):
    """Handles a single client connection (emitter or receiver)."""

    server: ThreadedTCPServer

    def handle(self):
        sync_server = self.server.sync_server

        # Read hello
        line = self.rfile.readline()
        if not line:
            return
        try:
            hello = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            LOG.warning("Failed to parse hello message: %s", e)
            return

        if hello.get("type") != "hello":
            return

        role = hello.get("role")
        LOG.info("Client connected: role=%s from %s", role, self.client_address)

        if role == "receiver":
            sync_from = int(hello.get("sync_from", 1))
            # Hold clients_lock during replay AND add to prevent race condition.
            # This blocks broadcasts during replay, ensuring no events slip through
            # the gap between replay finishing and being added to broadcast set.
            with sync_server.clients_lock:
                sync_server.replay_from(self, sync_from)
                sync_server.receivers.add(self)

        try:
            self._read_loop(sync_server)
        finally:
            with sync_server.clients_lock:
                sync_server.receivers.discard(self)
            LOG.info("Client disconnected: %s", self.client_address)

    def _read_loop(self, sync_server: UsdSyncServer):
        while True:
            line = self.rfile.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                LOG.warning("Failed to parse message: %s", e)
                continue

            if msg.get("type") == "quit":
                break

            if msg.get("type") != "txn":
                continue

            events = msg.get("events", [])
            if not isinstance(events, list) or not events:
                continue

            # Apply atomically
            sync_server.apply_txn(events)

            # Sequence and broadcast each event
            for ev in events:
                rec = {"type": "event", "seq": sync_server.assign_seq(), "event": ev}
                sync_server.append_log(rec)
                sync_server.broadcast(rec)

            # After load_payload, re-broadcast latest child state so
            # receivers re-apply authoritative TRS after re-import.
            for ev in events:
                if ev.get("k") == K_LOAD_PAYLOAD:
                    sync_server.replay_children_after_load(ev["prim"])


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True

    def __init__(self, server_address, handler_class, sync_server: UsdSyncServer):
        self.sync_server = sync_server
        super().__init__(server_address, handler_class)


def run_server(
    host: str = "127.0.0.1",
    port: int = 7200,
    base_usd_path: str | None = None,
    log_path: str = "usd_events.db",
):
    """Start the server (blocking)."""
    sync_server = UsdSyncServer(base_usd_path=base_usd_path, log_path=log_path)
    server = ThreadedTCPServer((host, port), ConnectionHandler, sync_server)

    # Ensure the DB is closed even on hard kills (Stop-Process, SIGTERM).
    def _cleanup():
        try:
            sync_server.db_conn.close()
            LOG.info("Event log closed: %s", log_path)
        except Exception:
            LOG.exception("Failed to close event log")

    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, lambda *_: server.shutdown())

    LOG.info("Server listening on %s:%s", host, port)
    LOG.info("Event log: %s", log_path)
    if base_usd_path:
        LOG.info("Base USD: %s", base_usd_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Server shutting down")
    finally:
        server.shutdown()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    ap = argparse.ArgumentParser(description="OpenUSDConnect sync server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7200)
    ap.add_argument("--base", default=None, help="Base USD file to load")
    ap.add_argument("--log", default="usd_events.db", help="SQLite event log file path")
    args = ap.parse_args()
    run_server(host=args.host, port=args.port, base_usd_path=args.base, log_path=args.log)


if __name__ == "__main__":
    main()
