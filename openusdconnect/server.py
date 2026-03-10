"""Authoritative TCP sequencer server.

Maintains an in-memory Usd.Stage, accepts transactions from emitters,
applies them atomically, assigns monotonic sequence numbers, broadcasts
to all connected receivers, and logs events to a JSONL file for replay.

CLI usage:
    python -m openusdconnect.server --port 7200 --base test_scene.usda --log events.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import socketserver
import threading
from typing import List, Optional, Set

from pxr import Usd, Sdf

from .event_apply import apply_event

LOG = logging.getLogger(__name__)


class UsdSyncServer:
    """Holds all shared server state: stage, sequence counter, client list, log file."""

    def __init__(self, base_usd_path: Optional[str] = None, log_path: str = "usd_events.jsonl"):
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
        self.receivers: Set = set()
        self._seq_lock = threading.Lock()
        self._next_seq = 1

    def assign_seq(self) -> int:
        with self._seq_lock:
            s = self._next_seq
            self._next_seq += 1
            return s

    def append_log(self, rec: dict):
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            LOG.exception("Failed to write event log")

    def broadcast(self, rec: dict):
        line = (json.dumps(rec) + "\n").encode("utf-8")
        dead = []
        with self.clients_lock:
            for h in self.receivers:
                try:
                    h.request.sendall(line)
                except Exception:
                    dead.append(h)
            for h in dead:
                self.receivers.discard(h)

    def apply_txn(self, events: List[dict]):
        with self.stage_lock:
            with Sdf.ChangeBlock():
                for ev in events:
                    apply_event(self.stage, ev)

    def replay_from(self, handler, seq_start: int):
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    rec = json.loads(line)
                    if int(rec.get("seq", 0)) >= seq_start:
                        handler.request.sendall(
                            (json.dumps(rec) + "\n").encode("utf-8")
                        )
        except FileNotFoundError:
            pass


class ConnectionHandler(socketserver.StreamRequestHandler):
    """Handles a single client connection (emitter or receiver)."""

    server: "ThreadedTCPServer"

    def handle(self):
        sync_server = self.server.sync_server

        # Read hello
        line = self.rfile.readline()
        if not line:
            return
        try:
            hello = json.loads(line.decode("utf-8"))
        except Exception:
            return

        if hello.get("type") != "hello":
            return

        role = hello.get("role")
        LOG.info("Client connected: role=%s from %s", role, self.client_address)

        if role == "receiver":
            with sync_server.clients_lock:
                sync_server.receivers.add(self)
            sync_from = int(hello.get("sync_from", 1))
            sync_server.replay_from(self, sync_from)

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
            except Exception:
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


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True

    def __init__(self, server_address, handler_class, sync_server: UsdSyncServer):
        self.sync_server = sync_server
        super().__init__(server_address, handler_class)


def run_server(host: str = "127.0.0.1", port: int = 7200,
               base_usd_path: Optional[str] = None,
               log_path: str = "usd_events.jsonl"):
    """Start the server (blocking)."""
    sync_server = UsdSyncServer(base_usd_path=base_usd_path, log_path=log_path)
    server = ThreadedTCPServer((host, port), ConnectionHandler, sync_server)
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
    ap.add_argument("--log", default="usd_events.jsonl", help="Event log file path")
    args = ap.parse_args()
    run_server(host=args.host, port=args.port, base_usd_path=args.base, log_path=args.log)


if __name__ == "__main__":
    main()
