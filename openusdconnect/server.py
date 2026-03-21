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

from pxr import Sdf, Usd

from .protocol import (
    EVENT_KIND_ORDER,
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_SET_GPRIM_ATTRS,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_VARIANT_SELECTIONS,
    K_SET_VISIBILITY,
    K_SET_XFORM_MATRICES,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
    MSG_COMPACT,
    MSG_EVENT,
    MSG_HELLO,
    MSG_QUIT,
    MSG_RESYNC,
    MSG_TXN,
)

LOG = logging.getLogger(__name__)

# Event kinds where only the latest event per prim matters.
LATEST_WINS_KINDS = frozenset({
    K_SET_XFORM_TRS,
    K_SET_XFORM_MATRICES,
    K_SET_VISIBILITY,
    K_SET_REFERENCE,
    K_SET_PAYLOAD,
    K_SET_VARIANT_SELECTIONS,
    K_DEACTIVATE_PRIM,
})


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

        # Non-destructive editing: all server-applied events go to an override
        # sublayer, keeping the base layer(s) untouched.  The override is
        # inserted as the strongest sublayer so its opinions compose on top.
        self.edit_layer = self._create_edit_layer()

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

    def _create_edit_layer(self, label: str = "server-edits") -> Sdf.Layer:
        """Create an override sublayer on the session layer and set it as the edit target.

        The session layer is stronger than the entire root layer stack, so
        opinions authored here always compose on top of the base file and
        its sublayers.  The override is inserted as a sublayer of the session
        layer (rather than using the session layer directly) so that
        multi-user mode can add per-client sublayers alongside it.

        Accepts an optional *label* for the layer identifier — this is the
        extension point for per-client layers.
        """
        layer = Sdf.Layer.CreateAnonymous(label)
        session = self.stage.GetSessionLayer()
        session.subLayerPaths.insert(0, layer.identifier)
        self.stage.SetEditTarget(Usd.EditTarget(layer))
        return layer

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

    def compact_log(self):
        """Compact the event log, keeping only the latest state per prim.

        For latest-wins events (TRS, visibility, etc.), only the final value
        is kept.  Partial TRS fields are merged.  delete_prim tombstones all
        prior events for that prim.  deactivate_prim is latest-wins (TRS
        preserved for payload reload).
        """
        with self.db_lock:
            rows = self.db_conn.execute(
                "SELECT seq, event FROM events ORDER BY seq"
            ).fetchall()

        if not rows:
            return

        tombstoned: set[str] = set()
        latest: dict[tuple[str, str], dict] = {}

        for _seq, event_json in rows:
            rec = json.loads(event_json)
            ev = rec.get("event", rec)
            prim = ev.get("prim", "")
            k = ev.get("k", "")

            if k == K_DELETE_PRIM:
                tombstoned.add(prim)
                latest = {key: val for key, val in latest.items() if key[0] != prim}
                latest[(prim, k)] = ev
                continue

            if k == K_RENAME_PRIM:
                tombstoned.add(prim)
                latest = {key: val for key, val in latest.items() if key[0] != prim}
                latest[(prim, k)] = ev
                continue

            if prim in tombstoned:
                continue

            # load/unload are mutually exclusive — only the last one wins.
            if k == K_LOAD_PAYLOAD:
                latest.pop((prim, K_UNLOAD_PAYLOAD), None)
                latest[(prim, k)] = ev
                continue
            if k == K_UNLOAD_PAYLOAD:
                latest.pop((prim, K_LOAD_PAYLOAD), None)
                latest[(prim, k)] = ev
                continue

            if k == K_SET_XFORM_TRS:
                prev = latest.get((prim, k))
                if prev:
                    for field in ("t", "r", "s"):
                        if field in ev.get("fields", []):
                            prev[field] = ev[field]
                            if field not in prev["fields"]:
                                prev["fields"].append(field)
                else:
                    latest[(prim, k)] = ev
            elif k == K_SET_GPRIM_ATTRS:
                prev = latest.get((prim, k))
                if prev:
                    prev.setdefault("attrs", {}).update(ev.get("attrs", {}))
                    new_meta = ev.get("primvar_meta", {})
                    if new_meta:
                        prev.setdefault("primvar_meta", {}).update(new_meta)
                    new_interp = ev.get("attr_interp", {})
                    if new_interp:
                        prev.setdefault("attr_interp", {}).update(new_interp)
                else:
                    latest[(prim, k)] = ev
            elif k in LATEST_WINS_KINDS:
                latest[(prim, k)] = ev
            else:
                latest[(prim, k)] = ev

        sorted_events = sorted(
            latest.values(),
            key=lambda e: (e["prim"].count("/"), e["prim"], EVENT_KIND_ORDER[e["k"]]),
        )

        with self.db_lock:
            self.db_conn.execute("DELETE FROM events")
            with self._seq_lock:
                self._next_seq = 1
            for ev in sorted_events:
                seq = self.assign_seq()
                rec = {"type": MSG_EVENT, "seq": seq, "event": ev}
                self.db_conn.execute(
                    "INSERT INTO events(seq, event) VALUES (?, ?)",
                    (seq, json.dumps(rec)),
                )
            self.db_conn.commit()

        LOG.info("Compacted event log: %d -> %d events", len(rows), len(sorted_events))

        # Tell connected receivers to reset and replay from the compacted log.
        self.broadcast({"type": MSG_RESYNC})
        with self.clients_lock:
            for handler in self.receivers:
                self.replay_from(handler, 1)

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
        sorted_events = sorted(
            latest.values(),
            key=lambda e: (e["prim"], EVENT_KIND_ORDER[e["k"]]),
        )

        for ev in sorted_events:
            rec = {"type": MSG_EVENT, "seq": self.assign_seq(), "event": ev}
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

    def apply_txn(self, events: list[dict], layer: Sdf.Layer | None = None):
        """Apply a transaction to the stage.

        Events are authored into *layer* (defaults to ``self.edit_layer``).
        The optional parameter is the multi-user extension point — pass a
        per-client layer to route edits to a specific sublayer.
        """
        from .event_apply import apply_events

        target = layer or self.edit_layer
        with self.stage_lock:
            self.stage.SetEditTarget(Usd.EditTarget(target))
            apply_events(self.stage, events)

    def export_edit_layer(self, file_path: str | None = None) -> str:
        """Export the server's edit layer as a USDA string.

        If *file_path* is given, also writes the layer to disk.  The exported
        layer contains only the opinions authored by the server — the base
        layer and its sublayers are not included.
        """
        usda = self.edit_layer.ExportToString()
        if file_path:
            self.edit_layer.Export(file_path)
            LOG.info("Exported edit layer to %s", file_path)
        return usda

    def export_flattened(self, file_path: str) -> None:
        """Export the fully composed stage as a single flattened USD file.

        All layers, composition arcs, and opinions are resolved into final
        values.  The result is a standalone file with no external dependencies
        — useful for archiving, delivery, or rendering.
        """
        with self.stage_lock:
            self.stage.Export(file_path)
        LOG.info("Exported flattened stage to %s", file_path)

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

        if hello.get("type") != MSG_HELLO:
            return

        role = hello.get("role")
        LOG.info("Client connected: role=%s from %s", role, self.client_address)

        if role == "receiver":
            sync_from = int(hello.get("sync_from", 1))

            # If sync_from is beyond the current log (e.g., after compaction
            # reset seq numbers), send resync so the receiver resets its
            # sequence counter, then replay the full log.
            max_seq = sync_server._load_max_seq()
            if sync_from > max_seq > 0:
                from .transport import send_line

                send_line(self.request, {"type": MSG_RESYNC})
                sync_from = 1

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

            if msg.get("type") == MSG_QUIT:
                break

            if msg.get("type") == MSG_COMPACT:
                LOG.info("Compact requested by %s", self.client_address)
                sync_server.compact_log()
                continue

            if msg.get("type") != MSG_TXN:
                continue

            events = msg.get("events", [])
            if not isinstance(events, list) or not events:
                continue

            # Apply atomically
            sync_server.apply_txn(events)

            # Sequence and broadcast each event
            for ev in events:
                rec = {"type": MSG_EVENT, "seq": sync_server.assign_seq(), "event": ev}
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
    compact: bool = False,
    export_diff: str | None = None,
):
    """Start the server (blocking)."""
    sync_server = UsdSyncServer(base_usd_path=base_usd_path, log_path=log_path)

    if compact:
        sync_server.compact_log()

    server = ThreadedTCPServer((host, port), ConnectionHandler, sync_server)

    # Ensure the DB is closed even on hard kills (Stop-Process, SIGTERM).
    def _cleanup():
        if export_diff:
            sync_server.export_edit_layer(export_diff)
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
    if export_diff:
        LOG.info("Will export diff to %s on shutdown", export_diff)
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
    ap.add_argument("--compact", action="store_true", help="Compact event log on startup")
    ap.add_argument(
        "--export-diff", default=None, metavar="PATH",
        help="Export the override layer as USDA on shutdown",
    )
    args = ap.parse_args()
    run_server(
        host=args.host,
        port=args.port,
        base_usd_path=args.base,
        log_path=args.log,
        compact=args.compact,
        export_diff=args.export_diff,
    )


if __name__ == "__main__":
    main()
