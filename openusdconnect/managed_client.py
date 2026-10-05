"""Single bidirectional client for server-owned collaboration layers.

``ManagedClient`` composes the emitter, sender, receiver, and dispatcher so a
USD-native application authors and observes one stage.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pxr import Sdf, Usd

from ._client_base import EmitterClientBase
from ._client_lifecycle import DEFAULT_WAIT_TIMEOUT_S, deadline_after, remaining_time
from ._client_utils import client_origin, require_app_name, validate_layered_source
from .adapters import UsdStageAdapter
from .client_id import make_stable_client_id
from .client_observer import ClientObserver
from .client_types import SyncUpdate
from .defaults import DEFAULT_HOST, DEFAULT_SYNC_PORT
from .dispatcher import AssetDependencyRefreshResult, EventDispatcher
from .emitter import PrimChannel
from .receiver import ReceiverThread
from .recovery import RecoveryArtifact, RecoveryError
from .sender import EventSender


@dataclass(frozen=True, slots=True)
class ManagedRecoveryResult:
    """Preserved local state returned after selecting authoritative state."""

    recovery_artifact: RecoveryArtifact
    preserved_authoring_layer: Sdf.Layer


class ManagedClient(EmitterClientBase):
    """Bidirectional sync through one client-owned transient authoring layer.

    The stage edit target is moved to :attr:`authoring_layer` at construction
    and after every rebind. Keeping all optimistic work in that layer makes
    authoritative rollback and preservation deterministic.
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
        department: str | None = None,
        token: str | None = None,
        persist_token: bool = True,
        reconnect: bool = True,
        observer: ClientObserver | None = None,
        attr_filter: Callable[[str], bool] | None = None,
        replicated_api_schemas: set[str] | None = None,
        extra_channels: Sequence[PrimChannel] | None = None,
        transform_coalesce_seconds: float = 0.0,
    ):
        app_name = require_app_name(app_name)
        if not isinstance(stage, Usd.Stage):
            raise TypeError("ManagedClient requires a Usd.Stage")
        validate_layered_source(stage)
        super().__init__(
            host=host, port=port, token=token, persist_token=persist_token, observer=observer,
        )
        self._stage: Usd.Stage | None = stage
        self._app_name = app_name
        self._authoring_layer: Sdf.Layer | None = None
        self._last_recovery_result: ManagedRecoveryResult | None = None
        self._backlog_marker = 0
        self._init_emitter(
            stage,
            attr_filter=attr_filter,
            replicated_api_schemas=replicated_api_schemas,
            extra_channels=extra_channels,
            transform_coalesce_seconds=transform_coalesce_seconds,
        )
        identity = {
            "client_id": client_id or make_stable_client_id(app_name),
            "origin": origin or client_origin(app_name, "sync"),
        }
        credential = self._credential.endpoint_kwargs()
        self._sender = EventSender(
            host, port, department=department,
            **identity, **credential,
        )
        self._receiver = ReceiverThread(
            host=host, port=port, sync_from=1, reconnect=reconnect, layered_replay=True,
            **identity, **credential, **self._hooks.receiver_callbacks(),
        )
        self._dispatcher = EventDispatcher(
            receiver=self._receiver,
            adapter=UsdStageAdapter(stage),
            emitter=self._emitter,
            on_resync=self._hooks.on_resync,
        )
        self._dispatcher.on_applied_events = self._hooks.applied_events_for(self._dispatcher)
        # Modify the stage last so a failed construction leaves it untouched.
        with self._emitter.suppressed():
            self._authoring_layer = self._create_authoring_layer(stage, app_name)

    @property
    def stage(self) -> Usd.Stage | None:
        """Application-owned stage, or ``None`` while parked."""
        return self._stage

    @property
    def authoring_layer(self) -> Sdf.Layer | None:
        """Client-owned transient layer for all local managed-mode edits."""
        return self._authoring_layer

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
    def pending_asset_dependencies(self) -> tuple[str, ...]:
        return self._dispatcher.pending_asset_dependencies

    @property
    def last_recovery_result(self) -> ManagedRecoveryResult | None:
        """Most recent preserved recovery data, retained until dismissed."""
        return self._last_recovery_result

    def dismiss_recovery_result(self) -> None:
        """Release references held for the most recently resolved incident."""
        self._last_recovery_result = None

    def update(self, *, max_messages: int | None = None) -> SyncUpdate:
        """Freeze local edits, apply the commit stream, then publish them.

        ``max_messages`` bounds one call's receive work; local edits are held
        until the backlog queued before them has been applied.
        """
        if not self._begin_update() or self._stage is None:
            return self._progress()

        # A queued authoritative record may touch the same field as a newer
        # local opinion. Freeze the local delta before dispatcher invalidation
        # advances emitter baselines, then send that exact batch after the
        # authoritative prefix has applied. SharedStageClient follows the same
        # prepare/apply/restore ordering at the Sdf-layer level.
        self._validate_authoring_target()
        had_batch = bool(self._emitter.prepared_event_count)
        outgoing = self._prepare_outgoing_events()
        if not had_batch and self._emitter.prepared_event_count:
            self._backlog_marker = self._receiver.freeze_marker()
        received = self._apply_queued(max_messages)
        if self._closed:
            return self._progress(received)

        sent = 0
        if self._receiver.connected and not self._sender.connected:
            self._sender.request_connect()
        if (
            self._sender.connected
            and self._is_synchronized()
            and self._receiver.drained_through(self._backlog_marker)
        ):
            sent = self._send(outgoing)
        return self._progress(received, sent)

    def rebind_stage(self, stage: Usd.Stage | None, *, discard_unsent: bool = False) -> None:
        """Move sending and receiving to a new stage with a fresh authoring layer.

        ``None`` parks the client. Refuses while work is unacknowledged, or
        unsent unless ``discard_unsent=True`` drops it.
        """
        self._require_open()
        adapter = None
        if stage is not None:
            adapter = UsdStageAdapter(stage)
            validate_layered_source(stage)
        if self._sender.pending_event_count:
            raise RuntimeError("cannot rebind with submitted work pending; call submit_and_wait()")
        if self._has_unsent_changes() and not discard_unsent:
            raise RuntimeError(
                "cannot rebind with unsent changes; call submit_and_wait() "
                "or pass discard_unsent=True"
            )
        if discard_unsent:
            self._emitter.discard_prepared_events()
            self._transform_coalescing.mark_submitted()
        if stage is None:
            self._dispatcher.unbind_stage()
            self._emitter.cleanup()
            self._stage = None
            self._authoring_layer = None
            return
        self._dispatcher.adapter = adapter
        self._dispatcher.bind_layered_stage(stage)
        self._authoring_layer = self._create_authoring_layer(stage, self._app_name)
        self._emitter.rebind_stage(stage)
        self._stage = stage

    def refresh_asset_dependency(
        self,
        asset_path: str | None = None,
    ) -> AssetDependencyRefreshResult:
        """Retry dependencies under the stage's current resolver context."""
        self._require_open()
        return self._dispatch(self._dispatcher.refresh_asset_dependency, asset_path)

    def recover_use_server(
        self,
        *,
        session_id: str | None = None,
        timeout: float | None = DEFAULT_WAIT_TIMEOUT_S,
    ) -> ManagedRecoveryResult:
        """Discard local optimistic opinions and start a fresh producer session.

        Rejected work is preserved before the client-owned transient authoring
        layer is cleared. The producer reconnect is attempted within the same
        timeout budget; if it cannot complete, the normal update loop retries.
        """
        self._require_started()
        stage = self._stage
        authoring = self._authoring_layer
        if stage is None or authoring is None:
            raise RecoveryError("stage_unavailable", "ManagedClient has no bound stage to recover")
        if not self._sender.recovery_required:
            raise RecoveryError("no_incident", "there is no recovery incident to resolve")
        if stage.GetEditTarget().GetLayer() is not authoring:
            raise RecoveryError(
                "edit_target_changed", "the active edit target changed during recovery",
            )

        deadline = deadline_after(timeout)
        self._replay_to_fresh_checkpoint(timeout)

        preserved = Sdf.Layer.CreateAnonymous("openusdconnect-recovery-authoring")
        preserved.TransferContent(authoring)
        self._emitter.cleanup()
        try:
            with Sdf.ChangeBlock():
                authoring.Clear()
            transactions = self._sender.abandon_rejected_session(session_id=session_id)
            result = ManagedRecoveryResult(
                recovery_artifact=transactions,
                preserved_authoring_layer=preserved,
            )
            # Store before reattaching the emitter. If reattachment raises,
            # integrations can still inspect or export the preserved work.
            self._last_recovery_result = result
        except Exception:
            with Sdf.ChangeBlock():
                authoring.TransferContent(preserved)
            raise
        finally:
            self._emitter.rebind_stage(stage)
        self._transform_coalescing.mark_submitted()
        self._resume_sender_after_recovery(remaining_time(deadline))
        return result

    def _is_synchronized(self) -> bool:
        return (
            self._stage is not None
            and self._receiver.synchronized
            and not self._sender.recovery_required
        )

    def _is_parked(self) -> bool:
        return self._stage is None

    def _has_unsent_changes(self) -> bool:
        return self._stage is not None and super()._has_unsent_changes()

    def _role_status(self) -> dict:
        return {
            "edit_target_is_published": (
                self._stage is not None
                and self._stage.GetEditTarget().GetLayer() is self._authoring_layer
            ),
        }

    def _apply_queued(self, max_messages: int | None = None) -> int:
        return self._dispatch(self._dispatcher.drain_and_apply, max_messages=max_messages)

    def _can_capture_edit_target(self) -> bool:
        if self._stage is None:
            return False
        self._validate_authoring_target()
        return True

    def _validate_authoring_target(self) -> None:
        if self._stage.GetEditTarget().GetLayer() is not self._authoring_layer:
            raise RuntimeError(
                "ManagedClient publishes only from client.authoring_layer; "
                "restore that edit target before update()"
            )

    @staticmethod
    def _create_authoring_layer(stage: Usd.Stage, label: str) -> Sdf.Layer:
        """Create and select the one transient layer owned by this client."""
        session = stage.GetSessionLayer()
        authoring = Sdf.Layer.CreateAnonymous(f"openusdconnect-{label}-authoring")
        with Sdf.ChangeBlock():
            session.subLayerPaths.append(authoring.identifier)
        stage.SetEditTarget(Usd.EditTarget(authoring))
        return authoring

    def _release(self) -> None:
        self._dispatcher.close()
        super()._release()


__all__ = ["ManagedClient", "ManagedRecoveryResult"]
