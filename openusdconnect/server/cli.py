"""Command-line entry point for the OpenUSDConnect sync server.

Usage:
    python -m openusdconnect.server --port 7200 --base test_scene.usda --log events.db
"""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import signal
import threading

from .connection import ConnectionHandler, ThreadedTCPServer
from .state import UsdSyncServer

LOG = logging.getLogger(__name__)


def run_server(
    host: str = "127.0.0.1",
    port: int = 7200,
    base_usd_path: str | None = None,
    log_path: str = "usd_events.db",
    compact: bool = False,
    export_diff: str | None = None,
    dashboard_port: int | None = None,
    op_cache_size: int | None = None,
    department_priority: list[str] | None = None,
    require_token: bool = False,
    durability: str = "strict",
    max_connections: int | None = None,
    txn_rate: float = 0,
    txn_burst: int = 0,
    wire_metrics: bool = False,
):
    """Start the server (blocking)."""
    sync_server = UsdSyncServer(
        base_usd_path=base_usd_path,
        log_path=log_path,
        op_cache_size=op_cache_size,
        department_priority=department_priority,
        require_token=require_token,
        durability=durability,
        txn_rate=txn_rate,
        txn_burst=txn_burst,
        wire_metrics=wire_metrics,
    )

    if compact:
        sync_server.compact_log()

    if dashboard_port:
        from integrations.dashboard import run_dashboard

        run_dashboard(sync_server, dashboard_port)
        LOG.info("Dashboard running on http://localhost:%d", dashboard_port)

    server = ThreadedTCPServer(
        (host, port), ConnectionHandler, sync_server, max_workers=max_connections
    )

    _cleaned_up = False

    def _cleanup():
        nonlocal _cleaned_up
        if _cleaned_up:
            return
        _cleaned_up = True
        if export_diff:
            sync_server.export_edit_layer(export_diff)
        try:
            sync_server.shutdown()
        except Exception:
            LOG.exception("Failed to shut down background threads")
        try:
            sync_server.store.close()
            LOG.info("Event store closed")
        except Exception:
            LOG.exception("Failed to close event store")

    atexit.register(_cleanup)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: server.shutdown())

    LOG.info(
        "Server listening on %s:%s (PID %d) durability=%s",
        host,
        port,
        os.getpid(),
        sync_server.durability,
    )
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
        _cleanup()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    ap = argparse.ArgumentParser(description="OpenUSDConnect sync server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7200)
    ap.add_argument("--base", default=None, help="Base USD file to load")
    ap.add_argument("--log", default="usd_events.db", help="SQLite event log file path")
    ap.add_argument("--compact", action="store_true", help="Compact event log on startup")
    ap.add_argument(
        "--export-diff",
        default=None,
        metavar="PATH",
        help="Export the override layer as USDA on shutdown",
    )
    ap.add_argument(
        "--dashboard",
        type=int,
        default=None,
        metavar="PORT",
        help="Start admin dashboard on this port (e.g. --dashboard 8080)",
    )
    ap.add_argument(
        "--op-cache-size",
        type=int,
        default=None,
        metavar="N",
        help=f"Max xform op cache entries (default: {UsdSyncServer.DEFAULT_OP_CACHE_SIZE})",
    )
    ap.add_argument(
        "--departments",
        default=None,
        metavar="LIST",
        help="Comma-separated department priority (strongest first). "
        "Enables per-client layer ordering by department. "
        "Example: --departments lighting,fx,animation,layout",
    )
    ap.add_argument(
        "--require-token",
        action="store_true",
        help="Enable TOFU token authentication. Clients are issued a token "
        "on first connect and must present it on reconnect.",
    )
    ap.add_argument(
        "--durability",
        choices=["strict", "realtime"],
        default="strict",
        help="strict: persist to DB before broadcast (no lost events). "
        "realtime: broadcast first, persist async (lower latency).",
    )
    ap.add_argument(
        "--max-connections",
        type=int,
        default=None,
        metavar="N",
        help=f"Max concurrent client connections (default: {ThreadedTCPServer.MAX_WORKERS})",
    )
    ap.add_argument(
        "--txn-rate",
        type=float,
        default=0,
        metavar="N",
        help="Max transactions per second per client (0 = unlimited, default: 0)",
    )
    ap.add_argument(
        "--txn-burst",
        type=int,
        default=0,
        metavar="N",
        help="Max burst size for transaction rate limiter (default: 0 = disabled)",
    )
    ap.add_argument(
        "--wire-metrics",
        action="store_true",
        help="Track encoded record bytes per event kind (off by default; "
        "exposed via the dashboard /api/wire-metrics endpoint)",
    )
    args = ap.parse_args()
    dept_list = args.departments.split(",") if args.departments else None
    run_server(
        host=args.host,
        port=args.port,
        base_usd_path=args.base,
        log_path=args.log,
        compact=args.compact,
        export_diff=args.export_diff,
        dashboard_port=args.dashboard,
        op_cache_size=args.op_cache_size,
        department_priority=dept_list,
        require_token=args.require_token,
        durability=args.durability,
        max_connections=args.max_connections,
        txn_rate=args.txn_rate,
        txn_burst=args.txn_burst,
        wire_metrics=args.wire_metrics,
    )


if __name__ == "__main__":
    main()
