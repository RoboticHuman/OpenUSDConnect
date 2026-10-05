"""Directional clients for USD-native Python applications.

The classes in this module compose the low-level sender, receiver, emitter,
and dispatcher without changing their event or stage semantics. Applications
remain responsible for calling ``update`` from the thread that owns their USD
stage or host scene.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pxr import Usd

from ._client_base import ClientBase, EmitterClientBase
from ._client_utils import client_origin, require_app_name, validate_layered_source
from .adapters import DCCAdapter, UsdStageAdapter
from .client_id import make_stable_client_id
from .client_observer import ClientObserver
from .client_types import SyncUpdate
from .defaults import DEFAULT_HOST, DEFAULT_SYNC_PORT
from .dispatcher import AssetDependencyRefreshResult, EventDispatcher
from .emitter import PrimChannel
from .receiver import ReceiverThread
from .sender import EventSender


class UsdReceiver(ClientBase):
    """Receive authoritative layered replay through a high-level lifecycle.

    ``start`` launches only the socket reader. ``update`` drains and applies
    queued events synchronously and must run on the stage- or scene-owning
    thread. By default, events apply directly to ``stage``. Pass an external
    ``DCCAdapter`` to keep ``stage`` as the composition mirror and project its
    composed values into an application-owned scene.
    """

    def __init__(
        self,
        stage: Usd.Stage,
        *,
        app_name: str,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_SYNC_PORT,
        client_id: str | None = None,
        origin: str | None = None,
        token: str | None = None,
        persist_token: bool = True,
        reconnect: bool = True,
        adapter: DCCAdapter | None = None,
        observer: ClientObserver | None = None,
    ):
        app_name = require_app_name(app_name)
        if not isinstance(stage, Usd.Stage):
            raise TypeError("UsdReceiver requires a Usd.Stage composition source")
        validate_layered_source(stage)
        super().__init__(
            host=host, port=port, token=token, persist_token=persist_token, observer=observer,
        )
        self._stage: Usd.Stage | None = stage
        self._owns_stage_adapter = adapter is None
        destination = adapter or UsdStageAdapter(stage)
        self._receiver = ReceiverThread(
            host=host,
            port=port,
            sync_from=1,
            reconnect=reconnect,
            client_id=client_id or make_stable_client_id(app_name),
            origin=origin or client_origin(app_name, "recv"),
            layered_replay=True,
            **self._credential.endpoint_kwargs(),
            **self._hooks.receiver_callbacks(),
        )
        self._dispatcher = EventDispatcher(
            receiver=self._receiver,
            adapter=destination,
            mirror_stage=None if destination.targets_stage() is stage else stage,
            on_resync=self._hooks.on_resync,
        )
        self._dispatcher.on_applied_events = self._hooks.applied_events_for(self._dispatcher)

    @property
    def stage(self) -> Usd.Stage | None:
        """Composition stage receiving authoritative changes, or ``None`` while parked.

        With an external adapter this is the receiver's USD mirror, not the
        adapter-owned destination scene.
        """
        return self._stage

    @property
    def receiver(self) -> ReceiverThread:
        """The underlying :class:`ReceiverThread`; a diagnostic handle."""
        return self._receiver

    @property
    def dispatcher(self) -> EventDispatcher:
        """The underlying :class:`EventDispatcher`; a diagnostic handle."""
        return self._dispatcher

    @property
    def last_seq(self) -> int:
        return self._dispatcher.last_seq

    @property
    def server_instance(self) -> str:
        """Identity of the server whose replay has been fully applied."""
        return self._receiver.server_instance

    @property
    def replay_epoch(self) -> int:
        """Epoch of the replay that has been fully applied."""
        return self._receiver.replay_epoch

    @property
    def pending_asset_dependencies(self) -> tuple[str, ...]:
        return self._dispatcher.pending_asset_dependencies

    def update(self, *, max_messages: int | None = None) -> SyncUpdate:
        """Apply queued messages on the calling thread.

        ``max_messages`` bounds one call's receive work for interactive
        applications. Replay becomes ready only after every message preceding
        the server's synchronization watermark has been applied.
        """
        if not self._begin_update() or self._stage is None:
            return self._progress()
        return self._progress(
            self._dispatch(self._dispatcher.drain_and_apply, max_messages=max_messages)
        )

    def rebind_stage(self, stage: Usd.Stage | None) -> None:
        """Move receive-side composition and managed layers to a new stage.

        ``None`` parks the receiver: it stays connected and the queue keeps
        filling until a stage is bound. A caller-provided adapter stays attached.
        """
        self._require_open()
        if stage is None:
            self._stage = None
            self._dispatcher.unbind_stage()
            return
        if not isinstance(stage, Usd.Stage):
            raise TypeError("UsdReceiver requires a Usd.Stage composition source")
        validate_layered_source(stage)
        self._stage = stage
        if isinstance(self._dispatcher.adapter, UsdStageAdapter):
            self._dispatcher.adapter.stage = stage
        elif self._owns_stage_adapter:
            self._dispatcher.adapter = UsdStageAdapter(stage)
        self._dispatcher.mirror_stage = (
            None if self._dispatcher.adapter.targets_stage() is stage else stage
        )
        self._dispatcher.bind_layered_stage(stage)

    def refresh_asset_dependency(
        self,
        asset_path: str | None = None,
    ) -> AssetDependencyRefreshResult:
        """Retry dependencies under the stage's current resolver context."""
        self._require_open()
        return self._dispatch(self._dispatcher.refresh_asset_dependency, asset_path)

    def acknowledge_native_scene_rebuilt(self) -> None:
        """Resume projection after rebuilding an external adapter destination."""
        self._require_open()
        self._dispatcher.acknowledge_native_scene_rebuilt()

    def _is_synchronized(self) -> bool:
        return self._stage is not None and self._receiver.synchronized

    def _is_parked(self) -> bool:
        return self._stage is None

    def _rebuild_reason(self) -> str:
        if self._dispatcher.native_scene_rebuild_required:
            return "the adapter-owned scene must be rebuilt after resolver recomposition"
        return ""

    def _release(self) -> None:
        self._dispatcher.close()


class UsdPublisher(EmitterClientBase):
    """Publish current-edit-target opinions authored on a USD stage."""

    def __init__(
        self,
        stage: Usd.Stage,
        *,
        app_name: str,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_SYNC_PORT,
        client_id: str | None = None,
        origin: str | None = None,
        department: str | None = None,
        token: str | None = None,
        persist_token: bool = True,
        observer: ClientObserver | None = None,
        attr_filter: Callable[[str], bool] | None = None,
        replicated_api_schemas: set[str] | None = None,
        extra_channels: Sequence[PrimChannel] | None = None,
        transform_coalesce_seconds: float = 0.0,
    ):
        app_name = require_app_name(app_name)
        if not isinstance(stage, Usd.Stage):
            raise TypeError("UsdPublisher requires a Usd.Stage")
        super().__init__(
            host=host, port=port, token=token, persist_token=persist_token, observer=observer,
        )
        self._stage = stage
        self._init_emitter(
            stage,
            attr_filter=attr_filter,
            replicated_api_schemas=replicated_api_schemas,
            extra_channels=extra_channels,
            transform_coalesce_seconds=transform_coalesce_seconds,
        )
        self._sender = EventSender(
            host,
            port,
            client_id=client_id or make_stable_client_id(app_name),
            origin=origin or client_origin(app_name, "emit"),
            department=department,
            on_stage_metadata=self._hooks.on_stage_metadata,
            **self._credential.endpoint_kwargs(),
        )

    @property
    def stage(self) -> Usd.Stage:
        """Application-owned stage observed for authored changes."""
        return self._stage

    def disconnect(self) -> None:
        """Close the socket and pause reconnection until :meth:`connect`."""
        if not self._closed:
            self._paused = True
            self._sender.disconnect()

    def update(self, *, max_messages: int | None = None) -> SyncUpdate:
        """Submit one retryable batch, or schedule a reconnect while disconnected.

        ``max_messages`` is accepted for a uniform host loop; a publisher
        receives nothing.
        """
        if not self._begin_update():
            return self._progress()
        sent = 0
        if self._sender.connected:
            sent = self._send(self._prepare_outgoing_events())
        elif not self._paused:
            self._sender.request_connect()
        return self._progress(submitted=sent)

    def _is_synchronized(self) -> bool:
        return self._sender.connected

    def _connect_sender(self, timeout: float | None = None) -> bool:
        self._paused = False
        return super()._connect_sender(timeout)


__all__ = ["UsdPublisher", "UsdReceiver"]
