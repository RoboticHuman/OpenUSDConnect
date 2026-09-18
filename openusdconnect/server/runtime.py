"""Explicit, signal-free ownership of a sync server and its optional services."""

from __future__ import annotations

import logging
import os
import threading

from ..cli_common import validate_file_name, validate_path_segment
from ..defaults import advertise_host_for_bind as _default_advertise_host
from ..defaults import host_for_url as _host_for_url
from ..plugin_environment import prepare_usd_plugin_environment
from ..protocol_constants import LayerMode
from .config import ServerConfig
from .connection import ConnectionHandler, ThreadedTCPServer
from .rate_limit import validate_rate_limit_config
from .state import UsdSyncServer

LOG = logging.getLogger(__name__)


def _normalize_vfs_share(value: str) -> str:
    try:
        return validate_path_segment(value)
    except ValueError as exc:
        raise ValueError(f"--vfs-share {exc}") from exc


def _validate_vfs_name(value: str) -> str:
    try:
        return validate_file_name(value)
    except ValueError as exc:
        raise ValueError(f"--vfs-name {exc}") from exc


class ServerRuntime:
    """Single-use embedded server handle; installs no signals or atexit hooks.

    Call start() explicitly, or enter this handle as a context manager.
    start() is idempotent while running; stop() is idempotent, including after
    failed startup. A stopped handle cannot be restarted. server_address contains
    the actual bound TCP address (including an OS-selected port for port=0).
    Lifecycle calls are serialized; do not call stop() from a server callback.
    """

    def __init__(self, config: ServerConfig | None = None):
        self.config = config or ServerConfig()
        self.sync_server = None
        self.server = None
        self.vfs_handle = None
        self._vfs_prewarm = None
        self.dashboard_handle = None
        self._thread = None
        self._status = "new"
        self._lock = threading.RLock()
        self._finished = threading.Event()
        self._serve_error = None

    @property
    def server_address(self):
        return None if self.server is None else self.server.server_address

    @property
    def running(self) -> bool:
        return self._status == "running" and not self._finished.is_set()

    def start(self) -> ServerRuntime:
        with self._lock:
            if self.running:
                return self
            if self._status != "new":
                raise RuntimeError("A stopped server runtime cannot be restarted")
            self._status = "starting"
            try:
                self._start()
                self._status = "running"
            except BaseException:
                self.stop()
                raise
        return self

    def _start(self):
        config = self.config
        layer_mode = LayerMode(config.layer_mode)
        validate_rate_limit_config(config.txn_rate, config.txn_burst)
        if layer_mode is LayerMode.SHARED_STAGE and config.vfs is not None:
            raise ValueError("the managed VFS composition is unavailable in shared-stage mode")
        if layer_mode is LayerMode.SHARED_STAGE and config.export_diff:
            raise ValueError("--export-diff is unavailable in shared-stage mode")
        if config.preflight_plugins:
            plugin_result = prepare_usd_plugin_environment(
                dll_dirs=config.plugin_dll_dirs or (),
                shader_ids=config.preflight_shader_ids,
            )
            if plugin_result.added_dll_dirs:
                LOG.info(
                    "Added USD plugin DLL directories: %s",
                    os.pathsep.join(plugin_result.added_dll_dirs),
                )
            if plugin_result.missing_dll_dirs:
                LOG.warning(
                    "USD plugin DLL directories do not exist: %s",
                    os.pathsep.join(plugin_result.missing_dll_dirs),
                )
            if plugin_result.unresolved_shader_ids:
                LOG.warning(
                    "Sdr preflight could not resolve shader identifiers: %s",
                    ", ".join(plugin_result.unresolved_shader_ids),
                )
            LOG.info("Sdr preflight completed in %.1f ms", plugin_result.elapsed_ms)

        sync_server = self.sync_server = UsdSyncServer(
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

        server = self.server = ThreadedTCPServer(
            (config.host, config.port),
            ConnectionHandler,
            sync_server,
            max_workers=config.max_connections,
        )

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
                sync_port=server.server_address[1],
                share=share,
                vfs_base_url=vfs_base_url,
                write_mode=write_mode,
                live_name=live_name,
                layer_dir=layer_dir,
                manifest_name=manifest_name,
                scene_id=sync_server.scene_id,
                validate_writes=vfs.validate_writes,
            )
            self.vfs_handle = run_vfs_server(provider_file, bind_host, vfs.port, share=share)
            if vfs.prewarm:
                self._vfs_prewarm = provider_file.prewarm(include_flattened=True)
                LOG.info("VFS snapshot prewarm started")
            LOG.info("VFS WebDAV running on %s/", vfs_base_url)
            LOG.info("VFS flattened snapshot: %s/%s", vfs_base_url, file_name)
            LOG.info("VFS live composition root: %s/%s", vfs_base_url, provider_file.live_name)
            LOG.info("VFS manifest: %s/%s", vfs_base_url, manifest_name)

        if config.dashboard_port is not None:
            from integrations.dashboard import run_dashboard

            self.dashboard_handle = run_dashboard(sync_server, config.dashboard_port)
            LOG.info("Dashboard running on http://localhost:%d", config.dashboard_port)

        self._thread = threading.Thread(target=self._serve, name="OpenUSDConnect_TCP", daemon=True)
        self._thread.start()
        LOG.info(
            "Server listening on %s:%s (PID %d) durability=%s",
            *server.server_address,
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

    def _serve(self):
        try:
            self.server.serve_forever(poll_interval=0.1)
        except BaseException as exc:
            self._serve_error = exc
        finally:
            self._finished.set()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for serving to finish, propagating any background serving error."""
        finished = self._finished.wait(timeout)
        if finished and self._serve_error is not None:
            raise RuntimeError("TCP server failed") from self._serve_error
        return finished

    @staticmethod
    def _cleanup(label, callback):
        try:
            callback()
        except Exception:
            LOG.exception("Failed to %s", label)

    def stop(self) -> None:
        """Release acquired resources, attempting every cleanup on failure."""
        with self._lock:
            if self._status == "stopped":
                return
            self._status = "stopped"
            if self.server is not None:
                if self._thread is not None and self._thread.is_alive():
                    self._cleanup("stop TCP serving", self.server.shutdown)
                    self._thread.join()
                self._cleanup("close TCP clients and listener", self.server.server_close)
            if self.vfs_handle is not None:
                self._cleanup("stop VFS", self.vfs_handle.stop)
            if self._vfs_prewarm is not None:
                self._cleanup("join VFS prewarm", self._vfs_prewarm.join)
            if self.dashboard_handle is not None:
                self._cleanup("stop dashboard", self.dashboard_handle.stop)
            if self.sync_server is not None:
                self._cleanup("shut down state workers", self.sync_server.shutdown)
                if self.config.export_diff:
                    self._cleanup(
                        "export edit layer",
                        lambda: self.sync_server.export_edit_layer(self.config.export_diff),
                    )
                store = getattr(self.sync_server, "store", None)
                if store is not None:
                    self._cleanup("close event store", store.close)
            self._finished.set()

    def __enter__(self) -> ServerRuntime:
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop()


def start_server(config: ServerConfig | None = None) -> ServerRuntime:
    """Start a background server and return its explicit ownership handle."""
    return ServerRuntime(config).start()
