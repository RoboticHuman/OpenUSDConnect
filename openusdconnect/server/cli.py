"""Command-line entry point for the OpenUSDConnect sync server.

Usage:
    python -m openusdconnect.server --port 7200 --base test_scene.usda --event-log events.db
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading

import pxr
from pxr import Ar, Usd

from .._version import __version__
from ..cli_common import (
    add_sync_endpoint_args,
    add_version_argument,
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
from ..codec import SCHEMA_VERSION
from ..defaults import (
    DEFAULT_EVENT_LOG,
    DEFAULT_VFS_LAYER_DIR,
    DEFAULT_VFS_MANIFEST_NAME,
    VFS_WRITE_MODES,
)
from ..defaults import (
    advertise_host_for_bind as _default_advertise_host,  # noqa: F401
)
from ..defaults import (
    host_for_url as _host_for_url,  # noqa: F401
)
from ..plugin_environment import (
    PluginEnvironmentError,
)
from ..protocol_constants import PROTOCOL_VERSION, LayerMode
from .config import ServerConfig, VfsConfig
from .connection import ThreadedTCPServer
from .rate_limit import validate_rate_limit_config
from .runtime import ServerRuntime
from .state import UsdSyncServer

LOG = logging.getLogger(__name__)


def _log_usd_runtime() -> None:
    version = ".".join(str(part) for part in Usd.GetVersion())
    bindings = getattr(pxr, "__file__", None) or "unknown"
    LOG.info("OpenUSD runtime: %s; bindings: %s", version, bindings)


def _log_openusdconnect_version() -> None:
    LOG.info(
        "OpenUSDConnect %s; protocol %d; schema %d",
        __version__,
        PROTOCOL_VERSION,
        SCHEMA_VERSION,
    )


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


def run_server(config: ServerConfig | None = None) -> None:
    """Run until interrupted; process signal handling belongs only to this wrapper."""
    _log_openusdconnect_version()
    _log_usd_runtime()
    runtime = ServerRuntime(config)
    shutdown_requested = threading.Event()
    previous_sigterm = None
    handles_signals = threading.current_thread() is threading.main_thread()
    try:
        if handles_signals:
            previous_sigterm = signal.signal(signal.SIGTERM, lambda *_: shutdown_requested.set())
        with runtime:
            while not shutdown_requested.wait(0.1):
                if runtime.wait(timeout=0):
                    break
    except KeyboardInterrupt:
        LOG.info("Server shutting down")
    finally:
        runtime.stop()
        if handles_signals and previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    ap = argparse.ArgumentParser(
        prog="openusdconnect-server",
        description="OpenUSDConnect sync server",
        allow_abbrev=False,
    )
    add_version_argument(ap)
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
    plugins = ap.add_argument_group("USD plugin environment")
    plugins.add_argument(
        "--plugin-dll-dir",
        action="append",
        default=None,
        metavar="DIR",
        help=(
            "Prepend DIR for Windows USD plugin dependency loading before Sdr "
            "initialization; repeat for multiple directories. Also read from "
            "OPENUSDCONNECT_DLL_DIRS"
        ),
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
        help="Max transactions per second per client; requires --txn-burst "
        "(0 = unlimited, default)",
    )
    limits.add_argument(
        "--txn-burst",
        type=nonnegative_int,
        default=0,
        metavar="N",
        help="Max burst size; requires --txn-rate (0 = disabled, default)",
    )
    limits.add_argument(
        "--txn-batch-size",
        type=positive_int,
        default=256,
        metavar="N",
        help="Maximum durable managed transactions per SQLite group commit "
        "(1 disables batching, default: 256)",
    )
    limits.add_argument(
        "--txn-batch-delay-ms",
        type=nonnegative_float,
        default=0.5,
        metavar="MS",
        help="Maximum time to collect a transaction group in milliseconds (default: 0.5)",
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
    try:
        validate_rate_limit_config(args.txn_rate, args.txn_burst)
    except ValueError as exc:
        ap.error(str(exc))
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
    config = ServerConfig(
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
        plugin_dll_dirs=args.plugin_dll_dir,
    )
    try:
        run_server(config)
    except PluginEnvironmentError as exc:
        LOG.error("%s", exc)
        return 2
    return 0


if __name__ == "__main__":
    main()
