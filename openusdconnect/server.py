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
import threading
import time
from dataclasses import dataclass, field

from pxr import Sdf, Usd

from .event_store import EventStore, SqliteEventStore
from .protocol import (
    EVENT_KIND_ORDER,
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_SET_GPRIM_ATTRS,
    K_SET_MATERIAL_BINDING,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_SHADER_CONNECTION,
    K_SET_SHADER_INPUT,
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
    K_SET_MATERIAL_BINDING,
    K_SET_SHADER_CONNECTION,
    K_DEACTIVATE_PRIM,
})


@dataclass
class ClientInfo:
    """Metadata for a connected client (emitter or receiver)."""

    role: str
    address: tuple
    client_id: str | None = None
    origin: str | None = None
    connected_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    event_count: int = 0


class UsdSyncServer:
    """Holds all shared server state: stage, sequence counter, client list, event store."""

    def __init__(
        self,
        base_usd_path: str | None = None,
        log_path: str = "usd_events.db",
        event_store: EventStore | None = None,
    ):
        if base_usd_path:
            self.stage = Usd.Stage.Open(base_usd_path)
            if self.stage is None:
                raise RuntimeError(f"Failed to open base USD: {base_usd_path}")
        else:
            self.stage = Usd.Stage.CreateInMemory()
            self.stage.DefinePrim("/Root", "Xform")

        # Non-destructive editing: all server-applied events go to an override
        # sublayer, keeping the base layer(s) untouched.
        self.edit_layer = self._create_edit_layer()

        self.stage_lock = threading.Lock()
        self.clients_lock = threading.Lock()
        self.receivers: set = set()
        self.clients: dict[str, ClientInfo] = {}
        self._event_listeners: list = []
        self._start_time = time.time()
        self._seq_lock = threading.Lock()

        # Pluggable event store — defaults to SQLite
        self.store: EventStore = event_store or SqliteEventStore(log_path)
        self._next_seq = self.store.get_max_seq() + 1

        # Rebuild stage from the event log so the composed stage matches
        # what receivers would get on replay.
        self._replay_log_into_stage()

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

    def _replay_log_into_stage(self):
        """Apply all events from the event store to restore stage on startup."""
        from .event_apply import apply_events

        rows = self.store.get_all_asc()
        if not rows:
            return
        events = []
        for _seq, record_json in rows:
            rec = json.loads(record_json)
            events.append(rec.get("event", rec))
        apply_events(self.stage, events)
        LOG.info("Restored stage from event log: %d events", len(events))

    def compact_log(self):
        """Compact the event log, keeping only the latest state per prim.

        For latest-wins events (TRS, visibility, etc.), only the final value
        is kept.  Partial TRS fields are merged.  delete_prim tombstones all
        prior events for that prim.  deactivate_prim is latest-wins (TRS
        preserved for payload reload).
        """
        rows = self.store.get_all_asc()
        if not rows:
            return

        tombstoned: set[str] = set()
        # (ev, metadata) tuples where metadata holds origin/client/client_id.
        latest: dict[tuple[str, str], tuple[dict, dict]] = {}

        for _seq, event_json in rows:
            rec = json.loads(event_json)
            ev = rec.get("event", rec)
            prim = ev.get("prim", "")
            k = ev.get("k", "")
            meta = {}
            for field in ("origin", "client", "client_id"):
                val = rec.get(field)
                if val:
                    meta[field] = val

            if k in (K_DELETE_PRIM, K_RENAME_PRIM):
                tombstoned.add(prim)
                latest = {key: val for key, val in latest.items() if key[0] != prim}
                latest[(prim, k)] = (ev, meta)
                continue

            if prim in tombstoned:
                continue

            # load/unload are mutually exclusive — only the last one wins.
            if k == K_LOAD_PAYLOAD:
                latest.pop((prim, K_UNLOAD_PAYLOAD), None)
                latest[(prim, k)] = (ev, meta)
                continue
            if k == K_UNLOAD_PAYLOAD:
                latest.pop((prim, K_LOAD_PAYLOAD), None)
                latest[(prim, k)] = (ev, meta)
                continue

            if k == K_SET_XFORM_TRS:
                existing = latest.get((prim, k))
                if existing:
                    prev = existing[0]
                    for field in ("t", "r", "s"):
                        if field in ev.get("fields", []):
                            prev[field] = ev[field]
                            if field not in prev["fields"]:
                                prev["fields"].append(field)
                    latest[(prim, k)] = (prev, meta)
                else:
                    latest[(prim, k)] = (ev, meta)
            elif k == K_SET_GPRIM_ATTRS:
                existing = latest.get((prim, k))
                if existing:
                    prev = existing[0]
                    prev.setdefault("attrs", {}).update(ev.get("attrs", {}))
                    new_meta = ev.get("primvar_meta", {})
                    if new_meta:
                        prev.setdefault("primvar_meta", {}).update(new_meta)
                    new_interp = ev.get("attr_interp", {})
                    if new_interp:
                        prev.setdefault("attr_interp", {}).update(new_interp)
                    latest[(prim, k)] = (prev, meta)
                else:
                    latest[(prim, k)] = (ev, meta)
            elif k == K_SET_SHADER_INPUT:
                existing = latest.get((prim, k))
                if existing:
                    prev = existing[0]
                    prev.setdefault("inputs", {}).update(ev.get("inputs", {}))
                    prev.setdefault("input_types", {}).update(
                        ev.get("input_types", {}),
                    )
                    if ev.get("shader_id"):
                        prev["shader_id"] = ev["shader_id"]
                    latest[(prim, k)] = (prev, meta)
                else:
                    latest[(prim, k)] = (ev, meta)
            else:
                latest[(prim, k)] = (ev, meta)

        sorted_entries = sorted(
            latest.values(),
            key=lambda entry: (
                entry[0]["prim"].count("/"),
                entry[0]["prim"],
                EVENT_KIND_ORDER[entry[0]["k"]],
            ),
        )

        with self._seq_lock:
            self._next_seq = 1
        records = []
        for ev, meta in sorted_entries:
            seq = self.assign_seq()
            rec = {"type": MSG_EVENT, "seq": seq, "event": ev}
            rec.update(meta)
            records.append((seq, json.dumps(rec)))
        self.store.clear_and_rewrite(records)

        LOG.info("Compacted event log: %d -> %d events", len(rows), len(sorted_entries))

        # Tell connected receivers to reset and replay from the compacted log.
        self.broadcast({"type": MSG_RESYNC, "reason": "compact"})
        with self.clients_lock:
            for handler in self.receivers:
                self.replay_from(handler, 1)

    def purge(self):
        """Clear all events, reset the edit layer, and resync receivers.

        Wipes the event store, clears all opinions from the server's edit
        layer (restoring the base scene), resets the sequence counter, and
        sends a resync to all connected receivers so they start fresh.
        """
        self.store.clear_and_rewrite([])
        with self._seq_lock:
            self._next_seq = 1
        with self.stage_lock:
            self.edit_layer.Clear()
        LOG.info("Purged event log and reset edit layer")
        self.broadcast({"type": MSG_RESYNC, "reason": "purge"})

    def assign_seq(self) -> int:
        with self._seq_lock:
            s = self._next_seq
            self._next_seq += 1
            return s

    def append_log(self, rec: dict):
        """Append event record to the event store."""
        try:
            self.store.append(rec["seq"], json.dumps(rec))
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
        replay_kinds = {
            K_ENSURE_PRIM, K_ENSURE_XFORM_OPS, K_SET_XFORM_TRS, K_SET_VISIBILITY,
            K_SET_MATERIAL_BINDING, K_SET_SHADER_INPUT, K_SET_SHADER_CONNECTION,
        }

        record_jsons = self.store.search_like(f'%"prim": "{prefix}%')

        # Collect the latest event of each relevant kind per child prim.
        # Store (ev, origin) tuples so replayed broadcasts can suppress
        # echo back to the original sender.
        latest: dict[tuple[str, str], tuple[dict, str | None]] = {}
        for event_json in record_jsons:
            rec = json.loads(event_json)
            ev = rec.get("event", rec)
            ep = ev.get("prim", "")
            ek = ev.get("k", "")
            if ep.startswith(prefix) and ek in replay_kinds:
                latest[(ep, ek)] = (ev, rec.get("origin"))

        if not latest:
            return

        # Order: ensure_prim → ensure_xform_ops → set_xform_trs → set_visibility
        sorted_events = sorted(
            latest.values(),
            key=lambda e: (e[0]["prim"], EVENT_KIND_ORDER[e[0]["k"]]),
        )

        for ev, origin in sorted_events:
            rec = {"type": MSG_EVENT, "seq": self.assign_seq(), "event": ev}
            if origin:
                rec["origin"] = origin
            self.append_log(rec)
            self.broadcast(rec, exclude_origin=origin)

        LOG.info(
            "Replayed %d child events after load_payload %s",
            len(sorted_events),
            prim_path,
        )

    def add_event_listener(self, callback) -> None:
        """Subscribe to broadcast events. Callback receives the event record dict."""
        self._event_listeners.append(callback)

    def remove_event_listener(self, callback) -> None:
        """Unsubscribe from broadcast events."""
        if callback in self._event_listeners:
            self._event_listeners.remove(callback)

    def register_client(
        self, address: tuple, role: str,
        client_id: str | None = None, origin: str | None = None,
    ):
        """Register a connected client for tracking."""
        key = f"{address[0]}:{address[1]}"
        with self.clients_lock:
            self.clients[key] = ClientInfo(
                role=role, address=address, client_id=client_id, origin=origin,
            )

    def unregister_client(self, address: tuple):
        """Remove a client from tracking."""
        key = f"{address[0]}:{address[1]}"
        with self.clients_lock:
            self.clients.pop(key, None)

    def broadcast(self, rec: dict, exclude_origin: str | None = None):
        """Broadcast a record to all connected receivers.

        If *exclude_origin* is given, receivers whose ``_origin`` matches
        are skipped — this suppresses echo back to the DCC instance that
        sent the event.
        """
        line = (json.dumps(rec) + "\n").encode("utf-8")
        dead = []
        with self.clients_lock:
            for h in self.receivers:
                if exclude_origin and getattr(h, "_origin", None) == exclude_origin:
                    continue
                try:
                    h.request.sendall(line)
                except OSError:
                    LOG.debug("Broadcast failed for %s, marking as dead", h.client_address)
                    dead.append(h)
            for h in dead:
                self.receivers.discard(h)
        # Notify event listeners (e.g. dashboard, monitoring).
        # Copy the list since listeners may remove themselves on error.
        for listener in list(self._event_listeners):
            try:
                listener(rec)
            except Exception:
                LOG.debug("Event listener failed, removing")
                self._event_listeners.remove(listener)

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

    def get_prim_count(self) -> int:
        """Return the number of prims on the composed stage (thread-safe)."""
        with self.stage_lock:
            return sum(1 for _ in self.stage.Traverse())

    def get_tracked_prim_count(self) -> int:
        """Return the number of prims tracked in the event log.

        Counts distinct prim paths from ensure_prim events minus
        delete_prim tombstones.
        """
        rows = self.store.get_all_asc()
        prims: set[str] = set()
        for _seq, record_json in rows:
            rec = json.loads(record_json)
            ev = rec.get("event", {})
            k = ev.get("k")
            path = ev.get("prim", "")
            if k == K_ENSURE_PRIM:
                prims.add(path)
            elif k == K_DELETE_PRIM:
                prims.discard(path)
        return len(prims)

    def get_event_count(self) -> int:
        """Return the number of events in the log (thread-safe)."""
        return self.store.get_count()

    def query_events(
        self,
        offset: int = 0,
        limit: int = 50,
        kind: str = "",
        prim_contains: str = "",
    ) -> tuple[list[dict], int]:
        """Return a page of events and total matching count (thread-safe)."""
        record_jsons, count = self.store.query(
            offset=offset, limit=limit,
            kind=kind, prim_contains=prim_contains,
        )
        return [json.loads(r) for r in record_jsons], count

    def get_client_list(self) -> list[dict]:
        """Return a snapshot of connected clients (thread-safe)."""
        now = time.time()
        with self.clients_lock:
            return [
                {
                    "key": k,
                    "role": i.role,
                    "address": f"{i.address[0]}:{i.address[1]}",
                    "client_id": i.client_id,
                    "origin": i.origin,
                    "connected_at": i.connected_at,
                    "last_activity_ago": round(now - i.last_activity, 1),
                    "event_count": i.event_count,
                }
                for k, i in self.clients.items()
            ]

    def get_uptime(self) -> float:
        """Return server uptime in seconds."""
        return time.time() - self._start_time

    def get_server_info(self) -> dict:
        """Return server configuration."""
        root = self.stage.GetRootLayer()
        return {
            "base_usd_path": root.realPath or None,
            "root_layer": root.identifier,
            "edit_layer": self.edit_layer.identifier,
        }

    def get_prim_tree(self) -> list[dict]:
        """Reconstruct the prim tree from the event log.

        Reads ensure_prim and delete_prim events from the store — no
        stage access or stage_lock needed.
        """
        rows = self.store.get_all_asc()
        prims: dict[str, str] = {}  # path → typeName
        for _seq, record_json in rows:
            rec = json.loads(record_json)
            ev = rec.get("event", {})
            k = ev.get("k")
            path = ev.get("prim", "")
            if k == K_ENSURE_PRIM:
                prims[path] = ev.get("typeName", "Xform")
            elif k == K_DELETE_PRIM:
                prims.pop(path, None)

        # Build tree structure from flat paths
        result = []
        for path in sorted(prims):
            parent = path.rsplit("/", 1)[0] or "/"
            depth = path.count("/")
            has_children = any(
                p.startswith(path + "/") and p.count("/") == depth + 1
                for p in prims
            )
            result.append({
                "path": path,
                "typeName": prims[path],
                "parent": parent,
                "depth": depth,
                "has_children": has_children,
            })
        return result

    def export_edit_layer(self, file_path: str | None = None) -> str:
        """Export the server's edit layer as a USDA string (thread-safe).

        If *file_path* is given, also writes the layer to disk.  The exported
        layer contains only the opinions authored by the server — the base
        layer and its sublayers are not included.
        """
        with self.stage_lock:
            usda = self.edit_layer.ExportToString()
            if file_path:
                self.edit_layer.Export(file_path)
        if file_path:
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

    def export_flattened_string(self) -> str:
        """Return the fully composed stage as a USDA string (thread-safe)."""
        with self.stage_lock:
            return self.stage.Flatten().ExportToString()

    def replay_from(self, handler, seq_start: int):
        """Replay events from the event store starting at seq_start.

        All events are replayed regardless of origin — the receiver needs
        its own prior edits (which share its origin) to restore state.
        Origin filtering only applies to live broadcast to prevent echo.
        """
        try:
            record_jsons = self.store.get_from_seq(seq_start)
            for record_json in record_jsons:
                handler.request.sendall((record_json + "\n").encode("utf-8"))
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
        client_id = hello.get("client_id")
        self._origin = hello.get("origin")
        LOG.info("Client connected: role=%s origin=%s from %s", role, self._origin, self.client_address)
        sync_server.register_client(self.client_address, role, client_id, origin=self._origin)

        if role == "receiver":
            sync_from = int(hello.get("sync_from", 1))

            # If sync_from is beyond the current log (e.g., after compaction
            # reset seq numbers), send resync so the receiver resets its
            # sequence counter, then replay the full log.
            max_seq = sync_server.store.get_max_seq()
            if sync_from > max_seq > 0:
                from .transport import send_line

                send_line(self.request, {"type": MSG_RESYNC, "reason": "seq_overflow"})
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
            sync_server.unregister_client(self.client_address)
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
            addr_key = f"{self.client_address[0]}:{self.client_address[1]}"
            with sync_server.clients_lock:
                info = sync_server.clients.get(addr_key)
            client_id = info.client_id if info else None
            for ev in events:
                rec = {
                    "type": MSG_EVENT,
                    "seq": sync_server.assign_seq(),
                    "event": ev,
                    "client": addr_key,
                    "client_id": client_id,
                }
                if self._origin:
                    rec["origin"] = self._origin
                sync_server.append_log(rec)
                sync_server.broadcast(rec, exclude_origin=self._origin)
            # Update client activity tracking — no lock needed since
            # each connection has its own _read_loop thread.
            if info:
                info.last_activity = time.time()
                info.event_count += len(events)

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
    dashboard_port: int | None = None,
):
    """Start the server (blocking)."""
    sync_server = UsdSyncServer(base_usd_path=base_usd_path, log_path=log_path)

    if compact:
        sync_server.compact_log()

    if dashboard_port:
        from integrations.dashboard import run_dashboard

        run_dashboard(sync_server, dashboard_port)
        LOG.info("Dashboard running on http://localhost:%d", dashboard_port)

    server = ThreadedTCPServer((host, port), ConnectionHandler, sync_server)

    # Ensure the DB is closed even on hard kills (Stop-Process, SIGTERM).
    def _cleanup():
        if export_diff:
            sync_server.export_edit_layer(export_diff)
        try:
            sync_server.store.close()
            LOG.info("Event store closed")
        except Exception:
            LOG.exception("Failed to close event store")

    atexit.register(_cleanup)
    if threading.current_thread() is threading.main_thread():
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
    ap.add_argument(
        "--dashboard", type=int, default=None, metavar="PORT",
        help="Start admin dashboard on this port (e.g. --dashboard 8080)",
    )
    args = ap.parse_args()
    run_server(
        host=args.host,
        port=args.port,
        base_usd_path=args.base,
        log_path=args.log,
        compact=args.compact,
        export_diff=args.export_diff,
        dashboard_port=args.dashboard,
    )


if __name__ == "__main__":
    main()
