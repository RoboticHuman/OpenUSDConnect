"""Layer-preserving client lifecycle for USD-native Python applications.

The classes in this module compose the low-level sender, receiver, emitter,
and dispatcher without changing their event or stage semantics. Applications
remain responsible for calling ``update`` from the thread that owns their USD
stage or host scene.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence

from pxr import Usd

from .adapters import UsdStageAdapter
from .client_id import make_stable_client_id
from .dispatcher import AssetDependencyRefreshResult, EventDispatcher
from .emitter import NoticeEmitter, PrimChannel
from .receiver import ReceiverThread
from .sender import EventSender
from .token_client import load_token, save_token

LOG = logging.getLogger(__name__)

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7200


def _require_app_name(app_name: str) -> str:
    value = str(app_name).strip()
    if not value:
        raise ValueError("app_name must not be empty")
    return value


def _resolve_token(host: str, port: int, token: str | None, persist: bool) -> str | None:
    if token is not None or not persist:
        return token
    return load_token(host, port)


def _token_callback(host: str, port: int, persist: bool) -> Callable[[str], None] | None:
    if not persist:
        return None
    return lambda token: save_token(host, port, token)


def _origin(app_name: str, role: str) -> str:
    return f"{app_name}-{uuid.uuid4().hex[:8]}-{role}"


def _validate_layered_source(stage: Usd.Stage) -> None:
    root = stage.GetRootLayer()
    metadata = (root.customLayerData or {}).get("openusdconnect") if root else None
    if metadata and metadata.get("live"):
        raise ValueError(
            "UsdReceiver requires the original base stage so it can rebuild "
            "collaboration layers from sequence 1; generated live files are "
            "continuation baselines"
        )


class UsdReceiver:
    """Receive authoritative layered replay into one application-owned stage.

    ``start`` launches only the socket reader. ``update`` drains and applies
    queued events synchronously and must run on the stage-owning thread.
    """

    def __init__(
        self,
        stage: Usd.Stage,
        *,
        app_name: str,
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
        client_id: str | None = None,
        origin: str | None = None,
        token: str | None = None,
        persist_token: bool = True,
        reconnect: bool = True,
        on_imported: Callable[[list[str]], None] | None = None,
        on_resync: Callable[[], None] | None = None,
        on_applied: Callable[[list[str]], None] | None = None,
        on_applied_events: Callable[[list[dict]], None] | None = None,
    ):
        app_name = _require_app_name(app_name)
        adapter = UsdStageAdapter(stage)
        _validate_layered_source(stage)
        resolved_token = _resolve_token(host, port, token, persist_token)
        self._stage = stage
        self._host = host
        self._port = port
        self._persist_token = persist_token
        self._receiver = ReceiverThread(
            host=host,
            port=port,
            sync_from=1,
            reconnect=reconnect,
            client_id=client_id or make_stable_client_id(app_name),
            origin=origin or _origin(app_name, "recv"),
            token=resolved_token,
            on_token_issued=_token_callback(host, port, persist_token),
            layered_replay=True,
        )
        self._dispatcher = EventDispatcher(
            receiver=self._receiver,
            adapter=adapter,
            on_imported=on_imported,
            on_resync=on_resync,
            on_applied=on_applied,
            on_applied_events=on_applied_events,
        )
        self._started = False
        self._closed = False

    @property
    def stage(self) -> Usd.Stage:
        """Application-owned stage receiving authoritative changes."""
        return self._stage

    @property
    def connected(self) -> bool:
        return not self._closed and self._receiver.connected

    @property
    def layered_replay_active(self) -> bool:
        return not self._closed and self._receiver.layered_replay_active

    @property
    def last_seq(self) -> int:
        return self._dispatcher.last_seq

    @property
    def auth_rejected(self) -> bool:
        return self._receiver.auth_rejected

    @property
    def connection_rejected(self) -> bool:
        return self._receiver.hello_rejected

    @property
    def stage_metadata(self) -> dict:
        return dict(self._receiver.stage_metadata)

    @property
    def pending_asset_dependencies(self) -> tuple[str, ...]:
        return self._dispatcher.pending_asset_dependencies

    def start(self) -> UsdReceiver:
        """Start the background socket reader and return this receiver."""
        if self._closed:
            raise RuntimeError("UsdReceiver is closed")
        if not self._started:
            if self._receiver.token is None and self._persist_token:
                self._receiver.token = load_token(self._host, self._port)
            self._receiver.start()
            self._started = True
        return self

    def wait_connected(self, timeout: float | None = None) -> bool:
        """Wait for the handshake; queued replay still requires ``update``."""
        if not self._started:
            raise RuntimeError("UsdReceiver has not been started")
        connected = self._receiver.wait_connected(timeout)
        if connected:
            self._require_layered_replay()
        elif self._receiver.auth_rejected:
            raise PermissionError("receiver authentication rejected")
        elif self._receiver.hello_rejected:
            raise ConnectionError(self._receiver.rejection_reason or "receiver connection rejected")
        return connected

    def _require_layered_replay(self) -> None:
        if self._receiver.connected and not self._receiver.layered_replay_active:
            self.close()
            raise RuntimeError("server did not negotiate required layered replay")

    def update(self) -> int:
        """Apply one queued receive batch on the calling thread."""
        if self._closed:
            raise RuntimeError("UsdReceiver is closed")
        if not self._started:
            raise RuntimeError("UsdReceiver has not been started")
        self._require_layered_replay()
        return self._dispatcher.drain_and_apply()

    def rebind_stage(self, stage: Usd.Stage) -> None:
        """Move receive-side application and managed layers to a new stage."""
        if self._closed:
            raise RuntimeError("UsdReceiver is closed")
        adapter = UsdStageAdapter(stage)
        _validate_layered_source(stage)
        self._stage = stage
        self._dispatcher.adapter = adapter
        self._dispatcher.bind_layered_stage(stage)

    def refresh_asset_dependency(
        self,
        asset_path: str | None = None,
    ) -> AssetDependencyRefreshResult:
        """Retry dependencies under the stage's current resolver context."""
        if self._closed:
            raise RuntimeError("UsdReceiver is closed")
        return self._dispatcher.refresh_asset_dependency(asset_path)

    def close(self) -> None:
        """Stop networking and release receiver-owned collaboration layers."""
        if self._closed:
            return
        self._receiver.stop()
        if self._receiver.is_alive():
            self._receiver.join(timeout=2.0)
            if self._receiver.is_alive():
                LOG.warning("UsdReceiver thread did not stop within 2 seconds")
        self._dispatcher.close()
        self._closed = True

    def __enter__(self) -> UsdReceiver:
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False


class UsdPublisher:
    """Publish current-edit-target opinions authored on a USD stage."""

    def __init__(
        self,
        stage: Usd.Stage,
        *,
        app_name: str,
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
        client_id: str | None = None,
        origin: str | None = None,
        department: str | None = None,
        token: str | None = None,
        persist_token: bool = True,
        attr_filter: Callable[[str], bool] | None = None,
        replicated_api_schemas: set[str] | None = None,
        extra_channels: Sequence[PrimChannel] | None = None,
    ):
        app_name = _require_app_name(app_name)
        if not isinstance(stage, Usd.Stage):
            raise TypeError("UsdPublisher requires a Usd.Stage")
        self._stage = stage
        self._host = host
        self._port = port
        self._persist_token = persist_token
        self._emitter = NoticeEmitter(
            stage,
            attr_filter=attr_filter,
            replicated_api_schemas=replicated_api_schemas,
            extra_channels=extra_channels,
        )
        self._sender = EventSender(
            host,
            port,
            client_id=client_id or make_stable_client_id(app_name),
            origin=origin or _origin(app_name, "emit"),
            department=department,
            token=_resolve_token(host, port, token, persist_token),
            on_token_issued=_token_callback(host, port, persist_token),
        )
        self._closed = False

    @property
    def stage(self) -> Usd.Stage:
        """Application-owned stage observed for authored changes."""
        return self._stage

    @property
    def connected(self) -> bool:
        return not self._closed and self._sender.connected

    @property
    def auth_rejected(self) -> bool:
        return self._sender.auth_rejected

    @property
    def prepared_event_count(self) -> int:
        """Number of events retained after an unsuccessful transport write."""
        return self._emitter.prepared_event_count

    def connect(self) -> bool:
        """Connect synchronously; safe to call again after disconnection."""
        if self._closed:
            raise RuntimeError("UsdPublisher is closed")
        if self._sender.token is None and self._persist_token:
            self._sender.token = load_token(self._host, self._port)
        return self._sender.connect()

    def disconnect(self) -> None:
        """Close the socket while retaining dirty and prepared emitter state."""
        if not self._closed:
            self._sender.disconnect()

    def _send(self, events: list[dict]) -> int:
        if not events:
            return 0
        if self._sender.send_events(events):
            self._emitter.mark_prepared_events_sent(events)
            return len(events)
        return 0

    def update(self) -> int:
        """Build and send one retryable batch of authored stage changes."""
        if self._closed:
            raise RuntimeError("UsdPublisher is closed")
        if not self.connected:
            return 0
        return self._send(self._emitter.prepare_events_for_send())

    def publish_current_edit_target(self) -> int:
        """Publish all opinions currently authored in the active edit target.

        An earlier retained batch must be retried with :meth:`update` first.
        This keeps one call from ambiguously mixing two transport transactions.
        """
        if self._closed:
            raise RuntimeError("UsdPublisher is closed")
        if not self.connected:
            return 0
        if self._emitter.prepared_event_count:
            raise RuntimeError(
                "an earlier publisher batch is still prepared; call update() "
                "before publishing the current edit target"
            )
        return self._send(self._emitter.prepare_snapshot_events_for_send())

    def close(self) -> None:
        """Disconnect and release the stage notice listener."""
        if self._closed:
            return
        self._sender.disconnect()
        self._emitter.cleanup()
        self._closed = True

    def __enter__(self) -> UsdPublisher:
        if not self.connect():
            self.close()
            raise ConnectionError(f"could not connect UsdPublisher to {self._host}:{self._port}")
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False


__all__ = ["UsdPublisher", "UsdReceiver"]
