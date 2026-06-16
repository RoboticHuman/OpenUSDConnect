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


def _default_advertise_host(bind_host: str) -> str:
    if bind_host in ("", "0.0.0.0", "::"):
        return "127.0.0.1"
    return bind_host


def _host_for_url(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _normalize_vfs_share(share: str) -> str:
    normalized = share.strip("/")
    if (
        not normalized
        or normalized in (".", "..")
        or "/" in normalized
        or "\\" in normalized
    ):
        raise ValueError("--vfs-share must be a single non-empty path segment")
    return normalized


def _validate_vfs_name(name: str) -> str:
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise ValueError("--vfs-name must be a single file name, not a path")
    return name


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
    vfs_port: int | None = None,
    vfs_host: str | None = None,
    vfs_share: str = "usd",
    vfs_name: str = "scene.usd",
    vfs_live_name: str | None = None,
    vfs_layer_dir: str = "_layers",
    vfs_manifest_name: str = "openusdconnect.json",
    vfs_write_mode: str = "forbid",
    vfs_validate_writes: bool = True,
    vfs_prewarm: bool = True,
    advertise_host: str | None = None,
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
    )

    if compact:
        sync_server.compact_log()

    if dashboard_port:
        from integrations.dashboard import run_dashboard

        run_dashboard(sync_server, dashboard_port)
        LOG.info("Dashboard running on http://localhost:%d", dashboard_port)

    vfs_handle = None
    server = None
    served = False
    _cleaned_up = False

    def _cleanup():
        nonlocal _cleaned_up
        if _cleaned_up:
            return
        _cleaned_up = True
        if export_diff:
            sync_server.export_edit_layer(export_diff)
        try:
            if vfs_handle is not None:
                vfs_handle.stop()
        except Exception:
            LOG.exception("Failed to stop VFS WebDAV server")
        try:
            sync_server.shutdown()
        except Exception:
            LOG.exception("Failed to shut down background threads")
        try:
            sync_server.store.close()
            LOG.info("Event store closed")
        except Exception:
            LOG.exception("Failed to close event store")

    try:
        server = ThreadedTCPServer(
            (host, port), ConnectionHandler, sync_server, max_workers=max_connections
        )

        if vfs_port is not None:
            from .vfs import VirtualStageFileSet, WriteMode, run_vfs_server

            share = _normalize_vfs_share(vfs_share)
            file_name = _validate_vfs_name(vfs_name)
            live_name = _validate_vfs_name(vfs_live_name) if vfs_live_name else None
            layer_dir = _normalize_vfs_share(vfs_layer_dir)
            manifest_name = _validate_vfs_name(vfs_manifest_name)
            write_mode = WriteMode(vfs_write_mode)
            bind_host = vfs_host or host
            public_host = advertise_host or _default_advertise_host(bind_host)
            vfs_base_url = f"http://{_host_for_url(public_host)}:{vfs_port}/{share}"
            provider_file = VirtualStageFileSet(
                sync_server,
                flat_name=file_name,
                advertise_host=public_host,
                sync_port=port,
                share=share,
                vfs_base_url=vfs_base_url,
                write_mode=write_mode,
                live_name=live_name,
                layer_dir=layer_dir,
                manifest_name=manifest_name,
                scene_id=sync_server.scene_id,
                validate_writes=vfs_validate_writes,
            )
            vfs_handle = run_vfs_server(provider_file, bind_host, vfs_port, share=share)
            if vfs_prewarm:
                provider_file.prewarm(include_flattened=True)
                LOG.info("VFS snapshot prewarm started")
            LOG.info("VFS WebDAV running on %s/", vfs_base_url)
            LOG.info("VFS flattened snapshot: %s/%s", vfs_base_url, file_name)
            LOG.info("VFS live composition root: %s/%s", vfs_base_url, provider_file.live_name)
            LOG.info("VFS manifest: %s/%s", vfs_base_url, manifest_name)
            LOG.info(
                "Windows UNC path: \\\\%s@%d\\%s\\%s",
                public_host,
                vfs_port,
                share,
                file_name,
            )
            LOG.info(
                "Windows UNC live root: \\\\%s@%d\\%s\\%s",
                public_host,
                vfs_port,
                share,
                provider_file.live_name,
            )

        atexit.register(_cleanup)
        if threading.current_thread() is threading.main_thread():
            signal.signal(
                signal.SIGTERM,
                lambda *_: threading.Thread(target=server.shutdown, daemon=True).start(),
            )

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
        served = True
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Server shutting down")
    finally:
        if server is not None:
            if served:
                server.shutdown()
            server.server_close()
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
        "--vfs-port",
        type=int,
        default=None,
        metavar="PORT",
        help="Start a WebDAV virtual USD file endpoint on this port",
    )
    ap.add_argument(
        "--vfs-host",
        default=None,
        metavar="HOST",
        help="Host/interface for the WebDAV endpoint (default: same as --host)",
    )
    ap.add_argument(
        "--vfs-share",
        default="usd",
        metavar="NAME",
        help="WebDAV share/collection name (default: usd)",
    )
    ap.add_argument(
        "--vfs-name",
        default="scene.usd",
        metavar="FILE",
        help="Virtual USD filename to expose (default: scene.usd)",
    )
    ap.add_argument(
        "--vfs-live-name",
        default=None,
        metavar="FILE",
        help="Composition-aware virtual USD root (default: <vfs-name-stem>.live.usda)",
    )
    ap.add_argument(
        "--vfs-layer-dir",
        default="_layers",
        metavar="NAME",
        help="Virtual directory containing exported live layers (default: _layers)",
    )
    ap.add_argument(
        "--vfs-manifest-name",
        default="openusdconnect.json",
        metavar="FILE",
        help="Virtual manifest filename (default: openusdconnect.json)",
    )
    ap.add_argument(
        "--vfs-write-mode",
        choices=["forbid", "drop", "translate"],
        default="forbid",
        help=(
            "How WebDAV PUT writes are handled: forbid with 403, accept/drop, "
            "or translate full-file USD saves into live events"
        ),
    )
    ap.add_argument(
        "--vfs-bypass-write-validation",
        action="store_true",
        help=(
            "Do not validate translate-mode PUT bodies as readable USD before accepting them. "
            "Drop mode never validates because writes are discarded."
        ),
    )
    ap.add_argument(
        "--no-vfs-prewarm",
        action="store_true",
        help="Do not pre-generate VFS snapshots in the background on startup",
    )
    ap.add_argument(
        "--advertise-host",
        default=None,
        metavar="HOST",
        help="Host embedded in live metadata (default: bind host, or 127.0.0.1 for all interfaces)",
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
        vfs_port=args.vfs_port,
        vfs_host=args.vfs_host,
        vfs_share=args.vfs_share,
        vfs_name=args.vfs_name,
        vfs_live_name=args.vfs_live_name,
        vfs_layer_dir=args.vfs_layer_dir,
        vfs_manifest_name=args.vfs_manifest_name,
        vfs_write_mode=args.vfs_write_mode,
        vfs_validate_writes=not args.vfs_bypass_write_validation,
        vfs_prewarm=not args.no_vfs_prewarm,
        advertise_host=args.advertise_host,
    )


if __name__ == "__main__":
    main()
