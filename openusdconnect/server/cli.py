"""Command-line entry point for the OpenUSDConnect sync server.

Usage:
    python -m openusdconnect.server --port 7200 --base test_scene.usda --event-log events.db
"""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import signal
import threading
from dataclasses import dataclass

from pxr import Ar

from ..cli_common import (
    add_sync_endpoint_args,
    add_vfs_resource_args,
    comma_separated,
    file_name,
    nonnegative_float,
    nonnegative_int,
    nonnegative_seconds,
    path_segment,
    port_number,
    positive_int,
    validate_file_name,
    validate_path_segment,
)
from ..defaults import (
    DEFAULT_EVENT_LOG,
    DEFAULT_HOST,
    DEFAULT_SYNC_PORT,
    DEFAULT_VFS_LAYER_DIR,
    DEFAULT_VFS_MANIFEST_NAME,
    DEFAULT_VFS_NAME,
    DEFAULT_VFS_SHARE,
    VFS_WRITE_MODES,
)
from ..defaults import (
    advertise_host_for_bind as _default_advertise_host,
)
from ..defaults import (
    host_for_url as _host_for_url,
)
from ..protocol_constants import LayerMode
from .connection import ConnectionHandler, ThreadedTCPServer
from .state import UsdSyncServer

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class VfsConfig:
    """Configuration for the optional WebDAV live-open endpoint."""

    port: int
    host: str | None = None
    share: str = DEFAULT_VFS_SHARE
    name: str = DEFAULT_VFS_NAME
    live_name: str | None = None
    layer_dir: str = DEFAULT_VFS_LAYER_DIR
    manifest_name: str = DEFAULT_VFS_MANIFEST_NAME
    write_mode: str = "forbid"
    validate_writes: bool = True
    prewarm: bool = True
    advertise_host: str | None = None


@dataclass(slots=True)
class ServerConfig:
    """Runtime configuration for the sync server and optional services."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_SYNC_PORT
    base_usd_path: str | None = None
    layer_mode: LayerMode | str = LayerMode.MANAGED
    resolver_context: Ar.ResolverContext | None = None
    log_path: str = DEFAULT_EVENT_LOG
    compact: bool = False
    export_diff: str | None = None
    dashboard_port: int | None = None
    op_cache_size: int | None = None
    department_priority: list[str] | None = None
    require_token: bool = False
    durability: str = "strict"
    max_connections: int | None = None
    txn_rate: float = 0
    txn_burst: int = 0
    txn_batch_size: int = 128
    txn_batch_delay_ms: float = 0.5
    wire_metrics: bool = False
    compact_interval: float = 0
    reclaim_interval: float = 0
    vfs: VfsConfig | None = None


def _normalize_vfs_share(share: str) -> str:
    try:
        return validate_path_segment(share)
    except ValueError as exc:
        raise ValueError(f"--vfs-share {exc}") from exc


def _validate_vfs_name(name: str) -> str:
    try:
        return validate_file_name(name)
    except ValueError as exc:
        raise ValueError(f"--vfs-name {exc}") from exc


def _create_resolver_context(values: list[str] | None) -> Ar.ResolverContext | None:
    if not values:
        return None
    configurations: list[tuple[str, str]] = []
    for value in values:
        if not value:
            raise ValueError("--resolver-context values must be non-empty")
        scheme, separator, configuration = value.partition(":")
        is_windows_path = len(scheme) == 1 and configuration.startswith(("/", "\\"))
        if separator and not is_windows_path:
            configurations.append((scheme, configuration))
        else:
            configurations.append(("", value))
    return Ar.GetResolver().CreateContextFromStrings(configurations)


def run_server(config: ServerConfig | None = None):
    """Start the server (blocking)."""
    config = config or ServerConfig()
    layer_mode = LayerMode(config.layer_mode)
    if layer_mode is LayerMode.SHARED_STAGE and config.vfs is not None:
        raise ValueError("the managed VFS composition is unavailable in shared-stage mode")
    if layer_mode is LayerMode.SHARED_STAGE and config.export_diff:
        raise ValueError("--export-diff is unavailable in shared-stage mode")
    sync_server = UsdSyncServer(
        base_usd_path=config.base_usd_path,
        layer_mode=layer_mode,
        resolver_context=config.resolver_context,
        log_path=config.log_path,
        op_cache_size=config.op_cache_size,
        department_priority=config.department_priority,
        require_token=config.require_token,
        durability=config.durability,
        txn_rate=config.txn_rate,
        txn_burst=config.txn_burst,
        txn_batch_size=config.txn_batch_size,
        txn_batch_delay=config.txn_batch_delay_ms / 1000.0,
        wire_metrics=config.wire_metrics,
        compact_interval=config.compact_interval,
        reclaim_interval=config.reclaim_interval,
    )

    if config.compact:
        sync_server.compact_log()

    if config.dashboard_port:
        from integrations.dashboard import run_dashboard

        run_dashboard(sync_server, config.dashboard_port)
        LOG.info("Dashboard running on http://localhost:%d", config.dashboard_port)

    server = ThreadedTCPServer(
        (config.host, config.port),
        ConnectionHandler,
        sync_server,
        max_workers=config.max_connections,
    )
    vfs_handle = None

    if config.vfs is not None:
        from .vfs import VirtualStageFileSet, WriteMode, run_vfs_server

        vfs = config.vfs
        share = _normalize_vfs_share(vfs.share)
        file_name = _validate_vfs_name(vfs.name)
        live_name = _validate_vfs_name(vfs.live_name) if vfs.live_name else None
        layer_dir = _normalize_vfs_share(vfs.layer_dir)
        manifest_name = _validate_vfs_name(vfs.manifest_name)
        write_mode = WriteMode(vfs.write_mode)
        bind_host = vfs.host or config.host
        public_host = vfs.advertise_host or _default_advertise_host(bind_host)
        vfs_base_url = f"http://{_host_for_url(public_host)}:{vfs.port}/{share}"
        provider_file = VirtualStageFileSet(
            sync_server,
            flat_name=file_name,
            advertise_host=public_host,
            sync_port=config.port,
            share=share,
            vfs_base_url=vfs_base_url,
            write_mode=write_mode,
            live_name=live_name,
            layer_dir=layer_dir,
            manifest_name=manifest_name,
            scene_id=sync_server.scene_id,
            validate_writes=vfs.validate_writes,
        )
        vfs_handle = run_vfs_server(provider_file, bind_host, vfs.port, share=share)
        if vfs.prewarm:
            provider_file.prewarm(include_flattened=True)
            LOG.info("VFS snapshot prewarm started")
        LOG.info("VFS WebDAV running on %s/", vfs_base_url)
        LOG.info("VFS flattened snapshot: %s/%s", vfs_base_url, file_name)
        LOG.info("VFS live composition root: %s/%s", vfs_base_url, provider_file.live_name)
        LOG.info("VFS manifest: %s/%s", vfs_base_url, manifest_name)

    _cleaned_up = False

    def _cleanup():
        nonlocal _cleaned_up
        if _cleaned_up:
            return
        _cleaned_up = True
        if config.export_diff:
            sync_server.export_edit_layer(config.export_diff)
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

    atexit.register(_cleanup)
    if threading.current_thread() is threading.main_thread():
        shutdown_requested = threading.Event()

        def _request_shutdown(*_):
            if shutdown_requested.is_set():
                return
            shutdown_requested.set()
            # socketserver.BaseServer.shutdown() must run outside the thread
            # executing serve_forever(), otherwise both wait on each other.
            threading.Thread(
                target=server.shutdown,
                name="server-shutdown",
                daemon=True,
            ).start()

        signal.signal(signal.SIGTERM, _request_shutdown)

    LOG.info(
        "Server listening on %s:%s (PID %d) durability=%s",
        config.host,
        config.port,
        os.getpid(),
        sync_server.durability,
    )
    LOG.info("Event log: %s", config.log_path)
    if config.base_usd_path:
        LOG.info("Base USD: %s", config.base_usd_path)
    if config.resolver_context is not None:
        LOG.info("Using an explicit asset resolver context")
    if config.export_diff:
        LOG.info("Will export diff to %s on shutdown", config.export_diff)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Server shutting down")
    finally:
        server.shutdown()
        server.server_close()
        _cleanup()


def main(argv: list[str] | None = None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    ap = argparse.ArgumentParser(
        prog="openusdconnect-server",
        description="OpenUSDConnect sync server",
        allow_abbrev=False,
    )
    endpoint = ap.add_argument_group("sync endpoint")
    add_sync_endpoint_args(endpoint)

    scene = ap.add_argument_group("scene and persistence")
    scene.add_argument("--base", default=None, help="Base USD file to load")
    scene.add_argument(
        "--layer-mode",
        choices=[mode.value for mode in LayerMode],
        default=LayerMode.MANAGED.value,
        help=(
            "managed: receiver-owned collaboration layers; shared_stage: "
            "synchronize the base file's root-layer graph"
        ),
    )
    scene.add_argument(
        "--resolver-context",
        action="append",
        default=None,
        metavar="[SCHEME:]CONFIG",
        help=(
            "Create the server stage's ArResolverContext from this configuration. "
            "May be specified more than once for multiple resolvers."
        ),
    )
    scene.add_argument(
        "--event-log",
        dest="event_log",
        default=DEFAULT_EVENT_LOG,
        help="SQLite event log file path",
    )
    scene.add_argument("--compact", action="store_true", help="Compact event log on startup")
    scene.add_argument(
        "--export-diff",
        default=None,
        metavar="PATH",
        help="Export the override layer as USDA on shutdown",
    )
    services = ap.add_argument_group("services")
    services.add_argument(
        "--dashboard-port",
        dest="dashboard_port",
        type=port_number,
        default=None,
        metavar="PORT",
        help="Start the admin dashboard on this port",
    )

    limits = ap.add_argument_group("limits and maintenance")
    limits.add_argument(
        "--op-cache-size",
        type=positive_int,
        default=None,
        metavar="N",
        help=f"Max xform op cache entries (default: {UsdSyncServer.DEFAULT_OP_CACHE_SIZE})",
    )
    limits.add_argument(
        "--departments",
        type=comma_separated,
        default=None,
        metavar="LIST",
        help="Comma-separated department priority (strongest first). "
        "Clients in one department share its ordered collaboration layer. "
        "Example: --departments lighting,fx,animation,layout",
    )
    services.add_argument(
        "--require-token",
        action="store_true",
        help="Enable TOFU token authentication. Clients are issued a token "
        "on first connect and must present it on reconnect.",
    )
    limits.add_argument(
        "--durability",
        choices=["strict", "realtime"],
        default="strict",
        help="strict: persist every write before broadcast. realtime: allow "
        "eligible server-internal writes to persist asynchronously; producer "
        "transaction acknowledgements remain durable.",
    )
    limits.add_argument(
        "--max-connections",
        type=positive_int,
        default=None,
        metavar="N",
        help=f"Max concurrent client connections (default: {ThreadedTCPServer.MAX_WORKERS})",
    )
    limits.add_argument(
        "--txn-rate",
        type=nonnegative_float,
        default=0,
        metavar="N",
        help="Max transactions per second per client (0 = unlimited, default: 0)",
    )
    limits.add_argument(
        "--txn-burst",
        type=nonnegative_int,
        default=0,
        metavar="N",
        help="Max burst size for transaction rate limiter (default: 0 = disabled)",
    )
    limits.add_argument(
        "--txn-batch-size",
        type=positive_int,
        default=128,
        metavar="N",
        help="Maximum durable managed transactions per SQLite group commit "
        "(1 disables batching, default: 128)",
    )
    limits.add_argument(
        "--txn-batch-delay-ms",
        type=nonnegative_float,
        default=0.5,
        metavar="MS",
        help="Maximum time to collect a transaction group in milliseconds "
        "(default: 0.5)",
    )
    limits.add_argument(
        "--wire-metrics",
        action="store_true",
        help="Track encoded record bytes per event kind (off by default; "
        "exposed via the dashboard /api/wire-metrics endpoint)",
    )
    limits.add_argument(
        "--compact-interval",
        type=nonnegative_seconds,
        default=0,
        metavar="SECONDS",
        help="Compact the event log every SECONDS (0 = disabled, default). "
        "Skips when no events arrived since the last compaction. "
        "Adjustable at runtime via the dashboard.",
    )
    limits.add_argument(
        "--reclaim-interval",
        type=nonnegative_seconds,
        default=0,
        metavar="SECONDS",
        help="Reclaim event log disk space (VACUUM on the SQLite backend) "
        "at most every SECONDS, at compaction/purge commits "
        "(0 = disabled, default). Pair with --compact-interval.",
    )
    vfs_group = ap.add_argument_group("virtual file service")
    add_vfs_resource_args(vfs_group, host_default=None, port_default=None)
    vfs_group.add_argument(
        "--vfs-live-name",
        type=file_name,
        default=None,
        metavar="FILE",
        help="Composition-aware virtual USD root (default: <vfs-name-stem>.live.usda)",
    )
    vfs_group.add_argument(
        "--vfs-layer-dir",
        type=path_segment,
        default=DEFAULT_VFS_LAYER_DIR,
        metavar="NAME",
        help="Virtual directory containing exported live layers (default: _layers)",
    )
    vfs_group.add_argument(
        "--vfs-manifest-name",
        type=file_name,
        default=DEFAULT_VFS_MANIFEST_NAME,
        metavar="FILE",
        help="Virtual manifest filename (default: openusdconnect.json)",
    )
    vfs_group.add_argument(
        "--vfs-write-mode",
        choices=VFS_WRITE_MODES,
        default="forbid",
        help=(
            "How WebDAV PUT writes are handled: forbid with 403, accept/drop, "
            "or translate full-file USD saves into live events"
        ),
    )
    vfs_group.add_argument(
        "--vfs-bypass-write-validation",
        action="store_true",
        help=(
            "Do not validate translate-mode PUT bodies as readable USD before accepting them. "
            "Drop mode never validates because writes are discarded."
        ),
    )
    vfs_group.add_argument(
        "--no-vfs-prewarm",
        action="store_true",
        help="Do not pre-generate VFS snapshots in the background on startup",
    )
    vfs_group.add_argument(
        "--advertise-host",
        default=None,
        metavar="HOST",
        help="Host embedded in live metadata (default: bind host, or 127.0.0.1 for all interfaces)",
    )
    args = ap.parse_args(argv)
    vfs = (
        VfsConfig(
            port=args.vfs_port,
            host=args.vfs_host,
            share=args.vfs_share,
            name=args.vfs_name,
            live_name=args.vfs_live_name,
            layer_dir=args.vfs_layer_dir,
            manifest_name=args.vfs_manifest_name,
            write_mode=args.vfs_write_mode,
            validate_writes=not args.vfs_bypass_write_validation,
            prewarm=not args.no_vfs_prewarm,
            advertise_host=args.advertise_host,
        )
        if args.vfs_port is not None
        else None
    )
    run_server(
        ServerConfig(
            host=args.host,
            port=args.port,
            base_usd_path=args.base,
            layer_mode=args.layer_mode,
            resolver_context=_create_resolver_context(args.resolver_context),
            log_path=args.event_log,
            compact=args.compact,
            export_diff=args.export_diff,
            dashboard_port=args.dashboard_port,
            op_cache_size=args.op_cache_size,
            department_priority=args.departments,
            require_token=args.require_token,
            durability=args.durability,
            max_connections=args.max_connections,
            txn_rate=args.txn_rate,
            txn_burst=args.txn_burst,
            txn_batch_size=args.txn_batch_size,
            txn_batch_delay_ms=args.txn_batch_delay_ms,
            wire_metrics=args.wire_metrics,
            compact_interval=args.compact_interval,
            reclaim_interval=args.reclaim_interval,
            vfs=vfs,
        )
    )


if __name__ == "__main__":
    main()
