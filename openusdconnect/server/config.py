"""Configuration for embedded and command-line sync servers."""

from __future__ import annotations

from dataclasses import dataclass

from pxr import Ar

from ..defaults import (
    DEFAULT_EVENT_LOG,
    DEFAULT_HOST,
    DEFAULT_SYNC_PORT,
    DEFAULT_VFS_LAYER_DIR,
    DEFAULT_VFS_MANIFEST_NAME,
    DEFAULT_VFS_NAME,
    DEFAULT_VFS_SHARE,
)
from ..plugin_environment import DEFAULT_SDR_SHADER_IDS
from ..protocol_constants import LayerMode


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
    txn_batch_size: int = 256
    txn_batch_delay_ms: float = 0.5
    wire_metrics: bool = False
    compact_interval: float = 0
    reclaim_interval: float = 0
    vfs: VfsConfig | None = None
    plugin_dll_dirs: list[str] | None = None
    preflight_shader_ids: tuple[str, ...] = DEFAULT_SDR_SHADER_IDS
    preflight_plugins: bool = True
