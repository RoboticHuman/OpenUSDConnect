"""Bidirectional synchronization for equivalent file-backed USD stages."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pxr import Sdf, Usd

from ._client_utils import (
    client_origin,
    client_token_handlers,
    require_app_name,
    resolve_client_token,
)
from .client_id import make_stable_client_id
from .codec import ReceivedEvent, decode_messages
from .event_apply import apply_events, atomic_apply
from .protocol_constants import (
    K_REPLACE_SDF_LAYER_CONTENT,
    K_SET_SDF_SPEC_FIELDS,
    K_SET_SUBLAYERS,
    LayerMode,
)
from .receiver import ReceiverThread
from .sdf_layer_tracker import SdfLayerChangeTracker
from .sender import EventSender
from .shared_layer_graph import SharedLayerGraph
from .token_client import load_token

LOG = logging.getLogger(__name__)

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7200


@dataclass(frozen=True, slots=True)
class SharedStageUpdate:
    """Work completed by one :meth:`SharedStageClient.update` call."""

    received: int
    sent: int


class SharedStageClient:
    """Synchronize authored opinions in a stage's root-layer graph.

    Every process opens its own equivalent stage and resolver context. Opaque
    protocol keys route edits to the corresponding local ``Sdf.Layer``; local
    identifiers and resolved filesystem paths never cross the wire.
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
        on_stage_metadata: Callable[[dict], None] | None = None,
        on_playback_state: Callable[[dict], None] | None = None,
        on_playback_claimed: Callable[[dict], None] | None = None,
        on_playback_rejected: Callable[[dict], None] | None = None,
        on_token_issued: Callable[[str], None] | None = None,
        delegate_bridge_path: str | Path | None = None,
    ):
        if not isinstance(stage, Usd.Stage):
            raise TypeError("SharedStageClient requires a Usd.Stage")
        app_name = require_app_name(app_name)
        if Sdf.Layer.IsAnonymousLayerIdentifier(stage.GetRootLayer().identifier):
            raise ValueError("shared-stage synchronization requires a portable root layer")
        stable_client_id = client_id or make_stable_client_id(app_name)
        connection_origin = origin or client_origin(app_name, "shared")
        resolved_token = resolve_client_token(host, port, token, persist_token)
        token_callback = client_token_handlers(host, port, persist_token, on_token_issued)

        self._stage = stage
        self._host = host
        self._port = port
        self._persist_token = persist_token
        self._graph = SharedLayerGraph(stage)

        from .sdf_delegate_bridge import NativeSdfLayerChangeTracker, _find_bridge

        bridge_path = delegate_bridge_path or _find_bridge()
        if bridge_path is None:
            LOG.info(
                "Sdf delegate bridge not found; using Python fallback. "
                "Build and install with: uv run python -m openusdconnect.build_sdf_notice_bridge"
            )
            self._tracker = SdfLayerChangeTracker(stage, self._graph)
        else:
            self._tracker = NativeSdfLayerChangeTracker(
                stage,
                self._graph,
                bridge_path,
            )
        self._receiver = ReceiverThread(
            host=host,
            port=port,
            sync_from=1,
            reconnect=reconnect,
            client_id=stable_client_id,
            origin=connection_origin,
            token=resolved_token,
            on_token_issued=token_callback,
            on_stage_metadata=on_stage_metadata,
            on_playback_state=on_playback_state,
            on_playback_claimed=on_playback_claimed,
            on_playback_rejected=on_playback_rejected,
            layered_replay=False,
            layer_mode=LayerMode.SHARED_STAGE,
        )
        self._sender = EventSender(
            host,
            port,
            client_id=stable_client_id,
            origin=connection_origin,
            token=resolved_token,
            on_token_issued=token_callback,
            layer_mode=LayerMode.SHARED_STAGE,
        )
        self._last_seq = 0
        self._pending_records: list[ReceivedEvent] = []
        self._started = False
        self._closed = False

    @property
    def stage(self) -> Usd.Stage:
        return self._stage

    @property
    def sender(self):
        """The underlying :class:`EventSender`."""
        return self._sender

    @property
    def receiver(self):
        """The underlying :class:`ReceiverThread`."""
        return self._receiver

    @property
    def connected(self) -> bool:
        return not self._closed and self._receiver.connected and self._sender.connected

    @property
    def graph_ready(self) -> bool:
        return self._graph.ready

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
    def last_seq(self) -> int:
        return self._last_seq

    @property
    def prepared_event_count(self) -> int:
        return self._tracker.prepared_event_count

    @property
    def pending_event_count(self) -> int:
        return len(self._pending_records)

    @property
    def pending_layer_keys(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(record.layer_key or "" for record in self._pending_records))

    def is_layer_mapped(self, layer: Sdf.Layer) -> bool:
        """Return whether *layer* can be targeted by shared-stage transactions."""
        layer_key = self._graph.key_for(layer)
        return bool(layer_key and layer_key in self._graph.reachable_layer_keys())

    def start(self) -> SharedStageClient:
        """Start the background receiver; connect the sender after handshake."""
        if self._closed:
            raise RuntimeError("SharedStageClient is closed")
        if not self._started:
            if self._receiver.token is None and self._persist_token:
                self._receiver.token = load_token(self._host, self._port)
            self._receiver.start()
            self._started = True
        return self

    def wait_connected(self, timeout: float | None = None) -> bool:
        """Wait for the receiver handshake and shared-stage mode negotiation."""
        if not self._started:
            raise RuntimeError("SharedStageClient has not been started")
        if not self._receiver.wait_connected(timeout):
            if self._receiver.auth_rejected:
                raise PermissionError("shared-stage receiver authentication rejected")
            if self._receiver.hello_rejected:
                raise ConnectionError(
                    self._receiver.rejection_reason or "shared-stage receiver rejected"
                )
            return False
        if self._receiver.layer_mode_active is not LayerMode.SHARED_STAGE:
            raise RuntimeError("server did not negotiate shared-stage mode")
        return True

    def _connect_sender(self) -> None:
        if self._sender.connected:
            return
        self._sender.token = self._receiver.token
        if not self._sender.connect():
            if self._sender.auth_rejected:
                raise PermissionError("shared-stage sender authentication rejected")
            if self._sender.hello_rejected:
                raise ConnectionError(
                    self._sender.rejection_reason or "shared-stage sender rejected"
                )
            raise ConnectionError(f"could not connect sender to {self._host}:{self._port}")

    def start_sender(self) -> bool:
        """Connect the sender explicitly for fail-fast error handling."""
        self._connect_sender()
        return self._sender.connected

    def update(self) -> SharedStageUpdate:
        """Apply queued authoritative records, then publish local layer edits."""
        if self._closed:
            raise RuntimeError("SharedStageClient is closed")
        if not self._started:
            raise RuntimeError("SharedStageClient has not been started")

        self._tracker.prepare_local_changes()
        try:
            received = self._apply_incoming()
        finally:
            self._tracker.restore_prepared()
        sent = 0
        if self._graph.ready:
            try:
                self._connect_sender()
            except (PermissionError, ConnectionError):
                pass  # best-effort during update
        if self._sender.connected and self._graph.ready:
            while routed := self._tracker.next_routed_batch():
                batch, layer_key, events = routed
                if not self._sender.send_events(events, layer_key=layer_key):
                    break
                sent += len(events)
                self._tracker.mark_prepared_sent(batch)
        return SharedStageUpdate(received=received, sent=sent)

    def _apply_incoming(self) -> int:
        buffers = self._receiver.drain_queue()
        if not buffers:
            return 0
        result = decode_messages(
            buffers,
            last_seq=self._last_seq,
            numpy_arrays=True,
            clear_on_resync=True,
            preserve_envelopes=True,
        )
        if result.resync_requested:
            self._pending_records.clear()
        applied_seq = 0 if result.resync_requested else self._last_seq
        applied = 0
        try:
            with self._tracker.suppressed():
                for state in result.layer_graph_states:
                    self._graph.apply_state(state)
                    for layer_state in state["layers"]:
                        layer = self._graph.layer_for(layer_state["layer_key"])
                        if layer is not None:
                            self._tracker.accept_authoritative_sublayers(
                                layer,
                                layer_state["sublayers"],
                            )
                    self._tracker.sync_graph(force=True)
                    applied_seq = max(applied_seq, int(state.get("seq", 0)))
                for record in result.received_records:
                    if self._apply_record(record):
                        applied += 1
                    applied_seq = max(applied_seq, record.seq)
                applied += self._apply_pending()
        except Exception:
            self._last_seq = applied_seq
            self._receiver.request_replay_from(applied_seq + 1)
            raise

        self._last_seq = max(applied_seq, result.last_seq)
        if result.errors:
            self._receiver.request_replay_from(self._last_seq + 1)
            LOG.warning("Shared-stage decode failed: %s", result.errors[0])
        return applied

    def _apply_record(self, record: ReceivedEvent) -> bool:
        layer_key = record.layer_key
        if not layer_key:
            raise ValueError("shared-stage record is missing layer_key")
        event = record.event
        kind = event.get("k")
        if kind == K_SET_SUBLAYERS:
            with self._graph.transaction():
                layer = self._graph.layer_for(layer_key)
                if layer is None:
                    self._graph.apply_sublayers(layer_key, event)
                else:
                    with Usd.EditContext(self._stage, Usd.EditTarget(layer)):
                        with atomic_apply(self._stage):
                            self._graph.apply_sublayers(layer_key, event)
                    self._tracker.accept_authoritative_sublayers(
                        layer,
                        event["sublayers"],
                    )
            self._tracker.sync_graph(force=True)
            return True
        if kind not in (K_REPLACE_SDF_LAYER_CONTENT, K_SET_SDF_SPEC_FIELDS):
            raise ValueError(f"unsupported shared-stage event {kind!r}")

        layer = self._graph.layer_for(layer_key)
        if layer is None:
            self._pending_records.append(record)
            return False
        with Usd.EditContext(self._stage, Usd.EditTarget(layer)):
            with atomic_apply(self._stage):
                apply_events(self._stage, [event])
        self._tracker.accept_authoritative_event(layer, event)
        return True

    def _apply_pending(self) -> int:
        if not self._pending_records:
            return 0
        retained = []
        applied = 0
        groups: list[tuple[Sdf.Layer, list[ReceivedEvent]]] = []
        for record in self._pending_records:
            layer = self._graph.layer_for(record.layer_key)
            if layer is None:
                retained.append(record)
                continue
            if groups and groups[-1][0] is layer:
                groups[-1][1].append(record)
            else:
                groups.append((layer, [record]))
        for layer, records in groups:
            with Usd.EditContext(self._stage, Usd.EditTarget(layer)):
                with atomic_apply(self._stage):
                    apply_events(self._stage, [record.event for record in records])
            for record in records:
                self._tracker.accept_authoritative_event(layer, record.event)
            applied += len(records)
        self._pending_records = retained
        return applied

    def refresh_asset_dependency(self, asset_path: str | None = None) -> tuple[str, ...]:
        """Retry unresolved graph edges under this stage's resolver context.

        ``asset_path`` is accepted for API consistency with the managed-mode
        clients; shared-stage graph edges are not filtered by asset path.
        """
        with self._tracker.suppressed():
            mapped = self._graph.refresh_dependencies()
            self._tracker.sync_graph(force=True)
            for layer_key in self._graph.reachable_layer_keys():
                layer = self._graph.layer_for(layer_key)
                entries = self._graph.sublayers_for(layer_key)
                if layer is not None and entries is not None:
                    self._tracker.accept_authoritative_sublayers(layer, list(entries))
            self._apply_pending()
        self._tracker.restore_prepared()
        return mapped

    def close(self) -> None:
        if self._closed:
            return
        self._sender.disconnect()
        self._receiver.stop()
        if self._receiver.is_alive():
            self._receiver.join(timeout=2.0)
            if self._receiver.is_alive():
                LOG.warning("SharedStageClient receiver did not stop within 2 seconds")
        self._tracker.close()
        self._closed = True

    def __enter__(self) -> SharedStageClient:
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False


__all__ = ["SharedStageClient", "SharedStageUpdate"]
