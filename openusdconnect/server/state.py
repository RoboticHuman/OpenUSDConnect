"""UsdSyncServer: authoritative state for the sync protocol.

Coordinates the scene, event journal, durable commits, maintenance, and receiver publication.
USD mutation and caches live in ``scene.py``; history lives in ``journal.py``;
atomic transaction application lives in ``commit.py``. Collaboration policy
and history replacement live in ``collaboration.py`` and ``maintenance.py``.
Network request handling lives in ``connection.py``; the CLI lives in ``cli.py``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import threading
import time
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pxr import Ar, Sdf, Usd

from ..codec import (
    decode_envelope,
    decode_received_event,
    encode_message,
    message_to_dict,
    resolve_payload,
)
from ..emitter import read_stage_metadata
from ..event_store import EventStore, LayerIdentity, ProducerProgress, SqliteEventStore
from ..framing import frame_batch
from ..protocol_constants import (
    K_ERASE_TIME_SAMPLES,
    K_REPLACE_SDF_LAYER_CONTENT,
    K_SET_SDF_SPEC_FIELDS,
    K_SET_SUBLAYERS,
    MSG_EVENT,
    MSG_LAYER_GRAPH_STATE,
    MSG_LAYER_STACK_STATE,
    MSG_PING,
    MSG_PLAYBACK_STATE,
    MSG_REPLAY_COMPLETE,
    MSG_RESYNC,
    NON_COLLABORATION_KINDS,
    LayerMode,
)
from ..protocol_validation import validate_events
from ..shared_layer_graph import SharedLayerGraph
from . import inspection
from ._txn_barrier import _TxnBarrier
from .collaboration import (
    CollaborationPolicy,
    department_for_layer_key,
    label_for_layer_key,
)
from .commit import TransactionCommitter
from .compaction import child_replay_records
from .journal import EncodedEvents, EventJournal
from .maintenance import HistoryMaintenance, PeriodicCompactor
from .metrics import WireMetrics
from .playback import PlaybackController
from .rate_limit import validate_rate_limit_config
from .scene import SceneState
from .snapshots import (
    SnapshotState,
    create_replacement_stage,
    prepare_stage_snapshot,
    read_snapshot_metadata,
    validate_stage_snapshot,
)
from .transactions import Transaction, TransactionCoordinator, TransactionRequest
from .types import (
    ClientInfo,
    TransactionCommit,
    TransactionOutcome,
    TransactionRejectedError,
    VfsWriteRejectedError,
)

if TYPE_CHECKING:
    from .connection import ConnectionHandler

LOG = logging.getLogger(__name__)

# A bounded queue applies backpressure when receiver delivery can't keep up.
_BROADCAST_QUEUE_MAX = 10_000
_PING_INTERVAL = 30.0  # seconds between heartbeat pings during idle

_AUDIENCE_ALL = "all"
_AUDIENCE_FLAT = "flat"
_AUDIENCE_LAYERED = "layered"
_AUDIENCES = frozenset({_AUDIENCE_ALL, _AUDIENCE_FLAT, _AUDIENCE_LAYERED})
_DEFAULT_LAYER_KEY = "default"


@dataclass(frozen=True, slots=True)
class _ReceiverReplay:
    head_seq: int
    epoch: int
    records: tuple[bytes, ...]
    layer_stack_state: bytes | None
    resync_reason: str | None


class UsdSyncServer:
    """Holds all shared server state: stage, sequence counter, client list, event store."""

    DEFAULT_OP_CACHE_SIZE = 4096

    def __init__(
        self,
        base_usd_path: str | None = None,
        log_path: str = "usd_events.db",
        event_store: EventStore | None = None,
        op_cache_size: int | None = None,
        department_priority: list[str] | None = None,
        require_token: bool = False,
        token_db_path: str | None = None,
        durability: str = "strict",
        txn_rate: float = 0,
        txn_burst: int = 0,
        txn_batch_size: int = 256,
        txn_batch_delay: float = 0.0005,
        wire_metrics: bool = False,
        compact_interval: float = 0,
        reclaim_interval: float = 0,
        stage: Usd.Stage | None = None,
        resolver_context: Ar.ResolverContext | None = None,
        layer_mode: LayerMode | str = LayerMode.MANAGED,
    ):
        self._shutdown_lock = threading.Lock()
        self._shutdown_complete = False
        self._transactions: TransactionCoordinator | None = None
        self._compactor: PeriodicCompactor | None = None
        self._journal: EventJournal | None = None
        self._broadcast_thread = None
        self._owns_store = event_store is None
        try:
            if stage is not None and base_usd_path:
                raise ValueError("stage and base_usd_path are mutually exclusive")
            if stage is not None and resolver_context is not None:
                raise ValueError("a supplied stage already owns its resolver context")
            validate_rate_limit_config(txn_rate, txn_burst)

            self.layer_mode = LayerMode(layer_mode)
            if self.layer_mode is LayerMode.SHARED_STAGE and department_priority:
                raise ValueError("department policy is not available in shared-stage mode")

            if stage is not None:
                self.stage = stage
            elif base_usd_path:
                self.stage = (
                    Usd.Stage.Open(base_usd_path, resolver_context)
                    if resolver_context is not None
                    else Usd.Stage.Open(base_usd_path)
                )
                if self.stage is None:
                    raise RuntimeError(f"Failed to open base USD: {base_usd_path}")
            else:
                self.stage = (
                    Usd.Stage.CreateInMemory("openusdconnect-server.usda", resolver_context)
                    if resolver_context is not None
                    else Usd.Stage.CreateInMemory()
                )
                self.stage.DefinePrim("/Root", "Xform")
            if self.layer_mode is LayerMode.SHARED_STAGE and Sdf.Layer.IsAnonymousLayerIdentifier(
                self.stage.GetRootLayer().identifier
            ):
                raise ValueError("shared-stage mode requires a portable root layer")

            self._scene = SceneState(
                self.stage, layer_mode=self.layer_mode,
                op_cache_size=op_cache_size or self.DEFAULT_OP_CACHE_SIZE,
            )
            self.stage_lock = self._scene.lock
            self.edit_layer = self._scene.edit_layer
            self.layer_stack = self._scene.layer_stack
            self.op_cache = self._scene.op_cache

            self._collaboration = CollaborationPolicy(
                self._scene, department_priority=department_priority,
                bump_snapshot_epoch=self.bump_snapshot_epoch,
                broadcast_layer_stack_state=self.broadcast_layer_stack_state,
            )

            self.clients_lock = threading.Lock()
            self.receivers: set[ConnectionHandler] = set()
            self.clients: dict[str, ClientInfo] = {}
            self._event_listeners: list = []
            self._start_time = time.time()
            self.txn_barrier = _TxnBarrier()
            self.txn_batch_size = max(1, int(txn_batch_size))
            self.txn_batch_delay = max(0.0, float(txn_batch_delay))

            # Pluggable event store defaults to SQLite
            self.store: EventStore = event_store or SqliteEventStore(log_path)
            self.wire_metrics = WireMetrics() if wire_metrics else None
            self._journal = EventJournal(
                self.store, durability=durability, metrics=self.wire_metrics,
            )
            self._committer = TransactionCommitter(self._scene, self._journal)
            self.server_instance = uuid.uuid4().hex
            self.scene_id = self._make_scene_id(base_usd_path)
            self.last_vfs_write_analysis: dict | None = None

            # TOFU token authentication
            self.require_token = require_token
            self.token_store = None
            if require_token:
                from ..token_store import TokenStore

                _token_path = token_db_path or log_path.replace(".db", "_tokens.db")
                self.token_store = TokenStore(_token_path)

            self._playback = PlaybackController()

            # A dedicated thread sends queued broadcasts. Slow receivers apply
            # backpressure to publishers when the bounded queue fills.
            # Each queued item retains the receiver membership captured when the
            # broadcast was authored. A receiver joining later obtains older
            # records only through its bounded replay window.
            # Item: (payload_bytes, receiver_handlers); routing is settled at enqueue.
            self._broadcast_queue: queue.Queue = queue.Queue(maxsize=_BROADCAST_QUEUE_MAX)
            self._broadcast_thread = threading.Thread(
                target=self._broadcast_loop,
                daemon=True,
            )

            # Durability mode for writes without producer progress. Idempotent
            # producer transactions always persist their event and cumulative
            # high-water mark atomically before acknowledgement and publication.
            #   "strict" persists every write before broadcast
            #   "realtime" allows eligible server-internal writes to persist async
            self.durability = durability
            # Per-client rate limiting (0 = disabled)
            self.txn_rate = txn_rate
            self.txn_burst = txn_burst

            self._maintenance = HistoryMaintenance(
                self._scene, self._journal, self.txn_barrier,
                drain_broadcasts=self._broadcast_queue.join,
                reclaim_interval=reclaim_interval,
            )
            self._compactor = PeriodicCompactor(
                lambda: self.compact_log(), self._journal, compact_interval,
            )
            if self.layer_mode is LayerMode.SHARED_STAGE:
                existing_graph_log = self.store.get_count() > 0
                self._scene.shared_layer_graph = SharedLayerGraph(
                    self.stage,
                    authoritative=not existing_graph_log,
                )
                if not existing_graph_log:
                    self._committer.append_graph_baseline()

            # Rebuild stage from the event log so the composed stage matches
            # what receivers would get on replay.
            self._replay_log_into_stage()
            if self.shared_layer_graph is not None:
                if existing_graph_log:
                    identities = self.store.get_layer_identities()
                    if not identities:
                        raise ValueError(
                            "shared-stage database has no durable layer identity registry; "
                            "recreate databases written before this protocol version"
                        )
                    self.shared_layer_graph.restore_identity_records(
                        (identity.identifier, identity.layer_key)
                        for identity in identities
                    )
                self.shared_layer_graph.authoritative = True
                self.refresh_shared_layer_dependencies()

            self._transactions = TransactionCoordinator(
                barrier=self.txn_barrier,
                commit=self._process_idempotent_txn_now,
                commit_group=(
                    self._commit_managed_transaction_group
                    if self.layer_mode is LayerMode.MANAGED else None
                ),
                checkpoint=self._journal.durable_checkpoint,
                batch_size=self.txn_batch_size,
                batch_delay=self.txn_batch_delay,
            )

            # No worker sees partially initialized state or replay in progress.
            self._broadcast_thread.start()
            self._journal.start()
            self._compactor.start()
            self._transactions.start()
        except BaseException:
            try:
                self.shutdown()
            finally:
                if self._owns_store and hasattr(self, "store"):
                    self.store.close()
            raise

    @property
    def shared_layer_graph(self) -> SharedLayerGraph | None:
        return self._scene.shared_layer_graph

    @staticmethod
    def _make_scene_id(base_usd_path: str | None) -> str:
        """Readable, stable-ish identifier for the currently hosted stage."""
        if base_usd_path:
            label = os.path.splitext(os.path.basename(base_usd_path))[0] or "scene"
            digest_src = os.path.abspath(base_usd_path)
        else:
            label = "scene"
            digest_src = "in-memory"
        digest = hashlib.sha1(digest_src.encode("utf-8")).hexdigest()[:12]
        return f"{label}-{digest}"

    def shutdown(self):
        """Idempotently drain workers, including after incomplete initialization.

        Successful construction leaves store ownership with the caller, as before.
        Failed construction closes only the store created by this state object.
        """
        with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._shutdown_complete = True
            if self._compactor is not None:
                self._compactor.request_stop()
            if self._transactions is not None:
                self._transactions.shutdown()
            if self._compactor is not None:
                self._compactor.join()
            if self._journal is not None:
                self._journal.shutdown()
            if self._broadcast_thread is not None and self._broadcast_thread.ident is not None:
                self._broadcast_queue.put(None)
                self._broadcast_thread.join()

    # ------------------------------------------------------------------
    # Periodic compaction
    # ------------------------------------------------------------------

    def set_reclaim_interval(self, seconds: float) -> None:
        """Set the storage-reclaim interval in seconds (0 disables).

        Reclaim runs at compaction and purge commits, so an enabled
        interval needs compaction (periodic or manual) to take effect.
        """
        self._maintenance.set_reclaim_interval(seconds)

    def get_reclaim_interval(self) -> float:
        return self._maintenance.get_reclaim_interval()

    def set_compact_interval(self, seconds: float) -> None:
        """Set the periodic compaction interval in seconds (0 disables).

        Takes effect immediately: the compaction thread re-reads the
        interval on wake, so shortening, lengthening, and disabling all
        apply without waiting out the previous period.
        """
        self._compactor.set_compact_interval(seconds)

    def get_compact_interval(self) -> float:
        return self._compactor.get_compact_interval()

    # ------------------------------------------------------------------
    # Playback synchronization
    # ------------------------------------------------------------------

    def get_stage_metadata_payload(self) -> dict:
        """Return the stage's authored metadata snapshot for hello_ok."""
        return read_stage_metadata(self.stage)

    def get_playback_state(self) -> dict:
        """Return a wire-shaped snapshot of the current playback state."""
        return self._playback.snapshot()

    def claim_playback(
        self,
        client_id: str,
        initial_time: float | None = None,
    ) -> tuple[bool, str]:
        """Claim leadership, optionally setting the initial playhead atomically.

        Returns (granted, current_leader), including on rejection.
        """
        return self._playback.claim(client_id, initial_time)

    def apply_playback_control(
        self,
        client_id: str,
        action: str,
        time_value: float = 0.0,
        rate: float = 1.0,
    ) -> tuple[bool, dict | str, str]:
        """Return (accepted, new_state_or_reason, current_leader) for a control."""
        return self._playback.apply_control(client_id, action, time_value, rate)

    def release_playback(self, client_id: str) -> bool:
        """Release a disconnected leader; return whether followers need an update."""
        return self._playback.release(client_id)

    def _replay_log_into_stage(self):
        """Apply all events from the event store to restore stage on startup.

        Each authored opinion routes by its portable collaboration layer key.
        Department metadata is recovered only as policy/UI context. Global log
        order is preserved across layers; adjacent events for one layer are
        batched. Shared session and stage-state events use the session layer.
        """
        rows = self.store.get_all_asc()
        if not rows:
            return
        if self.layer_mode is LayerMode.SHARED_STAGE:
            self._replay_shared_log(rows)
            return

        routed: list[tuple[Sdf.Layer, dict]] = []
        for _seq, record_bin in rows:
            message_type, broadcast = resolve_payload(decode_envelope(record_bin))
            if message_type != MSG_EVENT:
                raise ValueError("managed log contains an unsupported record")
            record = decode_received_event(broadcast, numpy_arrays=True)
            ev = record.event
            if ev["k"] in NON_COLLABORATION_KINDS:
                layer = self.stage.GetSessionLayer()
            else:
                layer_key = record.layer_key
                if not layer_key:
                    raise ValueError("persisted collaboration opinion is missing layer_key")
                layer, _added = self.layer_stack.ensure_layer(
                    layer_key,
                    label=label_for_layer_key(layer_key),
                )
                if record.client_id:
                    self._collaboration.restore_assignment(record.client_id, layer_key)
            routed.append((layer, ev))

        self._collaboration.apply_department_order()

        self._scene.replay_events(routed)

        LOG.info("Restored stage from event log: %d events", len(routed))

    def _replay_shared_log(self, rows: list[tuple[int, bytes]]) -> None:
        """Restore exact authored-layer opinions in persisted order."""
        graph = self.shared_layer_graph
        if graph is None:
            raise RuntimeError("shared-stage replay requires a layer graph")

        baseline_seen = False
        current_key = ""
        run: list[dict] = []

        def flush_run() -> None:
            if not run:
                return
            layer = graph.layer_for(current_key)
            if layer is None:
                raise ValueError(f"persisted event targets unresolved layer key {current_key!r}")
            self._scene.replay_events((layer, event) for event in run)
            run.clear()

        with self.stage_lock:
            for _seq, record_bin in rows:
                record = message_to_dict(record_bin, numpy_arrays=True)
                if record.get("type") == MSG_LAYER_GRAPH_STATE:
                    flush_run()
                    graph.apply_state(record)
                    baseline_seen = True
                    continue
                if record.get("type") != MSG_EVENT:
                    raise ValueError("shared-stage log contains an unsupported record")
                if not baseline_seen:
                    raise ValueError("shared-stage log must begin with a layer graph baseline")
                event = record["event"]
                layer_key = record.get("layer_key") or ""
                if event["k"] == K_SET_SUBLAYERS:
                    flush_run()
                    graph.apply_sublayers(layer_key, event)
                    continue
                if event["k"] not in (
                    K_SET_SDF_SPEC_FIELDS,
                    K_ERASE_TIME_SAMPLES,
                    K_REPLACE_SDF_LAYER_CONTENT,
                ):
                    raise ValueError(
                        f"shared-stage log contains unsupported event {event.get('k')!r}"
                    )
                if run and layer_key != current_key:
                    flush_run()
                current_key = layer_key
                run.append(event)
            flush_run()

        if not baseline_seen:
            raise ValueError("shared-stage log has no layer graph baseline")
        self._scene.invalidate_prim_count()
        LOG.info("Restored shared stage from event log: %d records", len(rows))

    # -- TOFU authentication -------------------------------------------

    def authenticate(
        self, client_id: str | None, token: str | None, department: str | None = None
    ) -> tuple[bool, str | None]:
        """Authenticate a client using TOFU.

        Returns (accepted, issued_token).
        - First connect (no token stored): issues a new token → (True, new_token)
        - Reconnect with valid token: accepted → (True, None)
        - Reconnect with wrong/missing token: rejected → (False, None)
        - Token not required: always accepted → (True, None)
        - Token required and no client_id: rejected → (False, None)
        """
        if not self.require_token or not self.token_store:
            return True, None

        if not client_id:
            LOG.warning("Auth rejected: missing client_id")
            return False, None

        if not self.token_store.has_token(client_id):
            # First connect issue token (TOFU)
            new_token = self.token_store.issue(client_id, department)
            return True, new_token

        if token and self.token_store.verify(client_id, token):
            return True, None

        LOG.warning("Auth rejected for %s invalid or missing token", client_id)
        return False, None

    def revoke_token(self, client_id: str) -> bool:
        """Revoke a client's token via dashboard/API."""
        if not self.token_store:
            return False
        return self.token_store.revoke(client_id)

    def bump_snapshot_epoch(self, reason: str = "") -> None:
        """Invalidate cached virtual-file snapshots after non-log stage changes."""
        epoch = self._journal.bump_snapshot_epoch()
        if reason:
            LOG.debug("Snapshot epoch bumped to %d (%s)", epoch, reason)
        else:
            LOG.debug("Snapshot epoch bumped to %d", epoch)

    def get_token_list(self) -> list[dict]:
        """Return all token records for the dashboard."""
        if not self.token_store:
            return []
        return self.token_store.get_all()

    # -- Client assignments to collaboration layers ---------------------

    @property
    def department_priority(self) -> list[str]:
        return self._collaboration.department_priority

    @property
    def client_layers(self) -> dict[str, Sdf.Layer]:
        """Return a snapshot of client assignments to shared collaboration layers."""
        return self._collaboration.client_layers

    def get_or_create_client_layer(
        self,
        client_id: str,
        department: str | None = None,
    ) -> Sdf.Layer:
        """Get or create a layer for this client.

        With department: clients share a department layer (last-write-wins
        within the department, department priority controls strength).
        Without department: uses the shared edit_layer (weakest, last-write-wins).
        """
        return self._collaboration.get_or_create_client_layer(client_id, department)

    def reserve_receiver_replay_mode(self, layered: bool) -> tuple[bool, str]:
        """Atomically admit a receiver under the current layer-stack contract."""
        return self._collaboration.reserve_receiver_replay_mode(layered)

    def release_receiver_replay_mode(self, layered: bool) -> None:
        """Release a replay-mode reservation when a receiver disconnects."""
        self._collaboration.release_receiver_replay_mode(layered)

    def resolve_layer_key(self, key: str) -> str | None:
        """Resolve a client ID, department name, or layer key."""
        return self._collaboration.resolve_layer_key(key)

    def resolve_layer(self, key: str) -> Sdf.Layer | None:
        """Resolve a layer by client ID, department name, or layer key."""
        return self._collaboration.resolve_layer(key)

    def department_for_layer(self, layer: Sdf.Layer) -> str | None:
        """Return department policy metadata for a managed layer."""
        return self._collaboration.department_for_layer(layer)

    def mute_layer(self, key: str) -> bool:
        """Mute a layer by client_id or department opinions hidden but preserved."""
        return self._collaboration.set_muted(key, True)

    def unmute_layer(self, key: str) -> bool:
        """Unmute a layer by client_id or department."""
        return self._collaboration.set_muted(key, False)

    def merge_layer(self, client_id: str) -> bool:
        """Merge the client's department opinions into the root layer.

        Releases this client; the department layer remains while other clients
        use it. Existing root opinions on sibling prims are preserved.
        Returns False for clients on the shared edit_layer (no-op).
        """
        return self._collaboration.release_client_layer(client_id, merge_into_root=True)

    def delete_layer(self, client_id: str) -> bool:
        """Release a client's department assignment.

        The department layer is discarded only when its last client leaves.

        Returns False for clients on the shared edit_layer (no-op).
        """
        return self._collaboration.release_client_layer(client_id, merge_into_root=False)

    def set_department_priority(self, ordered_departments: list[str]) -> None:
        """Set department priority ordering (strongest first)."""
        self._collaboration.set_department_priority(ordered_departments)

    def get_layer_stack_state(self) -> dict:
        """Return the portable collaboration stack for capable receivers."""
        with self.stage_lock:
            return {
                "type": MSG_LAYER_STACK_STATE,
                **self.layer_stack.state(),
            }

    def broadcast_layer_stack_state(self) -> None:
        """Publish current logical layer order and muting."""
        if not self._has_layered_receivers():
            return
        self.broadcast_message(
            self.get_layer_stack_state(),
            audience=_AUDIENCE_LAYERED,
        )

    def get_layer_stack_info(self) -> list[dict]:
        """Return ordered layer stack info for the dashboard.

        Department policy metadata is projected over the generic stack.
        Unused configured slots stay out of the dashboard until they have a
        client or authored content, matching the existing product behavior.
        """
        return self._collaboration.get_layer_stack_info()

    def compact_log(self):
        """Build compacted history off-lock, then catch up and replace the log."""
        # Phase 1 snapshot + compute (no txn_barrier, emitters keep running)
        rows = self.store.get_all_asc()
        if not rows:
            return
        max_seq = rows[-1][0]
        compaction = self._maintenance.build_compacted(rows)
        original_count = len(rows)

        # Phase 2 merge delta + commit (exclusive, emitters blocked)
        with self._maintenance.exclusive():
            # Catch any events that arrived during phase 1
            delta = self.store.get_from_seq_asc(max_seq + 1)
            for seq, record_bin in delta:
                compaction.add_record(seq, record_bin)
            original_count += len(delta)

            self._maintenance.commit_compaction(compaction, original_count)
            self._resync_receivers()

    def purge(self):
        """Clear all events, reset the edit layer, and resync receivers."""
        if self.layer_mode is LayerMode.SHARED_STAGE:
            raise RuntimeError(
                "purge is unavailable in shared-stage mode because application-owned "
                "authored opinions are not server-owned"
            )
        with self._maintenance.exclusive():
            self._maintenance.purge()
            self._resync_receivers()

    def _resync_receivers(self) -> None:
        """Send replaced history while the caller holds the exclusive maintenance window."""
        with self.clients_lock:
            targets = list(self.receivers)
        if not targets:
            return
        replay_epoch, replay_head = self.get_replay_token()
        reset_bin = encode_message({"type": MSG_RESYNC})
        complete_bin = encode_message({
            "type": MSG_REPLAY_COMPLETE, "head_seq": replay_head, "epoch": replay_epoch,
        })
        disconnected = []
        for handler in targets:
            try:
                with handler.send_lock:
                    controls = [reset_bin]
                    if handler._layered_replay:
                        controls.append(encode_message(self.get_layer_stack_state()))
                    # Keep reset, replay and completion in one send-lock window.
                    # Empty history needs only the control messages.
                    if replay_head:
                        handler.request.sendall(frame_batch(controls))
                        self.replay_from(handler, 1, seq_end=replay_head)
                        controls.clear()
                    controls.append(complete_bin)
                    handler.request.sendall(frame_batch(controls))
            except (OSError, TimeoutError):
                LOG.info(
                    "Receiver disconnected during history resync: %s",
                    handler.client_address,
                )
                disconnected.append(handler)
        self._discard_unreachable_receivers(disconnected)

    def assign_seq(self) -> int:
        return self._journal.assign_seq()

    def get_snapshot_token(self) -> tuple[int, int]:
        return self._journal.snapshot_token()

    def get_replay_token(self) -> tuple[int, int]:
        return self._journal.replay_token()

    def replace_from_stage_snapshot(
        self,
        uploaded_stage: Usd.Stage,
        *,
        client_id: str = "vfs-write",
        origin: str = "vfs-write",
        reject_stale: bool = True,
        reject_ambiguous: bool = True,
        unchanged_snapshot: bool = False,
    ) -> int:
        """Replace the live edit state from a complete uploaded USD snapshot.

        This is the WebDAV write-fallback path. A plain DCC save gives us a
        complete USD layer, not semantic incremental events, so the server
        translates it into a full event snapshot, resets the live edit log,
        broadcasts a resync, then broadcasts the translated events.

        Returns the number of translated events persisted to the event log.
        """
        uploaded_meta = read_snapshot_metadata(uploaded_stage)
        with self.txn_barrier.exclusive():
            epoch, seq = self.get_snapshot_token()
            with self.stage_lock:
                current = SnapshotState(
                    scene_id=self.scene_id,
                    epoch=epoch,
                    seq=seq,
                    prim_types=inspection.read_prim_types(self.stage),
                    department_layers=self._collaboration.ordered_department_names(),
                    additional_layers=[
                        key for key in self.layer_stack.layer_keys
                        if key != _DEFAULT_LAYER_KEY and department_for_layer_key(key) is None
                    ],
                )
            try:
                analysis = validate_stage_snapshot(
                    uploaded_stage, uploaded_meta, current,
                    reject_stale=reject_stale,
                    reject_ambiguous=reject_ambiguous,
                )
            except VfsWriteRejectedError as exc:
                self.last_vfs_write_analysis = exc.analysis.to_dict()
                raise
            if unchanged_snapshot:
                self.last_vfs_write_analysis = replace(
                    analysis,
                    status="unchanged",
                    notes=["uploaded bytes match the current virtual snapshot"],
                ).to_dict()
                LOG.info("VFS snapshot write is unchanged; no events generated")
                return 0

            with self.stage_lock:
                replacement_stage = create_replacement_stage(self.stage)
            prepared = prepare_stage_snapshot(
                uploaded_stage, analysis.removed_prims,
                replacement_stage=replacement_stage,
            )
            events = prepared.events
            analysis = replace(analysis, event_counts=dict(Counter(event["k"] for event in events)))

            encoded = self._maintenance.replace_snapshot(
                prepared, client_id=client_id, origin=origin,
            )
            records = encoded.records
            self.last_vfs_write_analysis = analysis.to_dict()

            # Queue the reset, replacement records, and completion marker
            # together so the same receivers observe them in order without
            # another broadcast interleaving. ReplayComplete marks the new
            # durable head and restores readiness, even with no records.
            replay_epoch, replay_head = self.get_replay_token()
            resync_bin = encode_message({"type": MSG_RESYNC, "reason": "vfs-write"})
            complete_bin = encode_message(
                {
                    "type": MSG_REPLAY_COMPLETE,
                    "head_seq": replay_head,
                    "epoch": replay_epoch,
                }
            )
            record_dicts = [record for record, _record_bin in records]
            record_bins = [record_bin for _record, record_bin in records]
            if self.wire_metrics is not None:
                self.wire_metrics.record(MSG_RESYNC, len(resync_bin))
                for record, record_bin in records:
                    self.wire_metrics.record(record["event"].get("k", ""), len(record_bin))
                self.wire_metrics.record(MSG_REPLAY_COMPLETE, len(complete_bin))
            self.broadcast_bytes(
                frame_batch([resync_bin, *record_bins, complete_bin]),
                record_dicts,
            )
            LOG.info(
                "Translated VFS snapshot write into %d live events "
                "(created=%d removed=%d type_changed=%d)",
                len(events),
                len(analysis.created_prims),
                len(analysis.removed_prims),
                len(analysis.type_changed_prims),
            )
            return len(events)

    def append_log(self, rec: dict) -> bytes:
        return self._journal.append_record(rec)

    def append_log_batch(
        self,
        tuples: list[tuple[int, bytes, str | None, str | None, str | None]],
        *,
        producer_progress: tuple[ProducerProgress, ...] = (),
        layer_identities: tuple[LayerIdentity, ...] = (),
    ):
        self._journal.append_batch(
            tuples, producer_progress=producer_progress, layer_identities=layer_identities,
        )

    def replay_children_after_load(self, prim_path: str):
        """After load_payload, re-broadcast the latest events for children.

        Queries the event log for the most recent structural and TRS events
        for each child of prim_path, assigns new sequence numbers, and
        broadcasts them so receivers re-apply the authoritative state.

        Also reactivates children on the server's stage that may have been
        deactivated by _detect_deletions during a previous unload cycle.
        Transaction callers retain their maintenance barrier through this replay.
        """
        # Reactivate children on the server's stage (clear stale SetActive(False))
        with self.stage_lock:
            prim = self.stage.GetPrimAtPath(prim_path)
            if prim and prim.IsValid():
                for child in Usd.PrimRange(prim, Usd.PrimAllPrimsPredicate):
                    if not child.IsActive():
                        child.SetActive(True)

        with self._journal.commit_scope():
            records = child_replay_records(self.store, prim_path)
            encoded = EncodedEvents()
            with self._journal.reserve_sequences():
                for record in records:
                    encoded.append(*self._journal.encode_event(
                        record.event, origin=record.origin, layer_key=record.layer_key,
                    ))
                self._journal.append_batch(encoded.store_rows, synchronous=True)
            self.broadcast_transaction_views(encoded.records)

        LOG.info(
            "Replayed %d child events after load_payload %s",
            len(records),
            prim_path,
        )

    def add_event_listener(self, callback) -> None:
        """Subscribe to broadcast events. Callback receives the event record dict."""
        self._event_listeners.append(callback)

    def remove_event_listener(self, callback) -> None:
        """Unsubscribe from broadcast events."""
        if callback in self._event_listeners:
            self._event_listeners.remove(callback)

    @contextmanager
    def receiver_replay_window(
        self,
        handler,
        sync_from: int = 1,
        *,
        replay_server_instance: str | None = None,
        replay_epoch: int | None = None,
    ):
        """Register a receiver at a stable replay-to-live boundary.

        The exclusive barrier is held only long enough to make prior realtime
        writes durable, capture immutable records and routing with their
        watermark, and join the live receiver set. Only the handler's send
        lock remains held during delivery, so newer broadcasts follow replay
        without network I/O holding the maintenance barrier.
        """
        with ExitStack() as replay_lock:
            with self.txn_barrier.exclusive():
                self._journal.drain()
                replay_lock.enter_context(handler.send_lock)
                with self.clients_lock:
                    self.receivers.add(handler)
                try:
                    replay_end = self.store.get_max_seq()
                    epoch, _latest_seq = self.get_replay_token()
                    # Absent identity fields preserve legacy explicit snapshot cursors.
                    prefix_mismatch = sync_from > 1 and replay_server_instance is not None and (
                        replay_server_instance != self.server_instance or replay_epoch != epoch
                    )
                    resync_reason = None
                    if prefix_mismatch or sync_from > replay_end + 1:
                        resync_reason = (
                            "replay_identity_changed" if prefix_mismatch else "seq_overflow"
                        )
                        sync_from = 1
                    with self.stage_lock:
                        layer_stack_state = (
                            encode_message(self.get_layer_stack_state())
                            if self.layer_mode is LayerMode.MANAGED
                            and handler._layered_replay else None
                        )
                        # Freeze only the selected suffix. Bytes are retained as-is;
                        # framing stays chunked and happens after the barrier releases.
                        records = tuple(self.store.get_from_seq_bin(sync_from, replay_end))
                    replay = _ReceiverReplay(
                        replay_end, epoch, records, layer_stack_state, resync_reason,
                    )
                except Exception:
                    with self.clients_lock:
                        self.receivers.discard(handler)
                    raise
            yield replay

    def register_client(
        self,
        address: tuple,
        role: str,
        client_id: str | None = None,
        origin: str | None = None,
        department: str | None = None,
    ):
        """Register a connected client for tracking."""
        key = f"{address[0]}:{address[1]}"
        with self.clients_lock:
            self.clients[key] = ClientInfo(
                role=role,
                address=address,
                client_id=client_id,
                origin=origin,
                department=department,
            )

    def unregister_client(self, address: tuple):
        """Remove a client from tracking."""
        key = f"{address[0]}:{address[1]}"
        with self.clients_lock:
            self.clients.pop(key, None)

    def broadcast_transaction_views(
        self,
        records: Sequence[tuple[dict, bytes]],
    ) -> None:
        """Deliver one authored transaction to the complete commit stream."""
        self.broadcast_transaction_group_views([records])

    def broadcast_transaction_group_views(
        self,
        transactions: Iterable[Sequence[tuple[dict, bytes]]],
    ) -> None:
        """Deliver a committed group as one complete ordered stream.

        Every receiver consumes every durable USD record, including records
        authored by the same origin. Origin is diagnostic metadata rather than
        a delivery filter. This gives live delivery the same contract as replay
        and lets every replica apply the server's total order.
        """
        all_records = []
        encoded_records = []
        for records in transactions:
            for record, encoded in records:
                all_records.append(record)
                encoded_records.append(encoded)
        if all_records:
            self.broadcast_bytes(frame_batch(encoded_records), all_records)

    def _has_layered_receivers(self) -> bool:
        with self.clients_lock:
            return any(handler._layered_replay for handler in self.receivers)

    def _notify_event_listeners(self, records: list[dict]) -> None:
        """Notify in-process event observers once, in record order."""
        for listener in list(self._event_listeners):
            for rec in records:
                try:
                    listener(rec)
                except Exception:
                    LOG.exception("Event listener failed, removing")
                    self._event_listeners.remove(listener)
                    break

    def broadcast(
        self,
        rec: dict,
        exclude_origin: str | None = None,
        *,
        audience: str = _AUDIENCE_ALL,
    ):
        """Broadcast a single record to all connected receivers."""
        self.broadcast_batch(
            [rec],
            exclude_origin=exclude_origin,
            audience=audience,
        )

    def broadcast_batch(
        self,
        records: list[dict],
        exclude_origin: str | None = None,
        *,
        audience: str = _AUDIENCE_ALL,
    ):
        """Enqueue records for async broadcast to all receivers.

        Network sends happen on the dedicated broadcast thread. The caller
        waits for queue capacity if receiver delivery falls behind.
        """
        if not records:
            return
        framed_payloads = [encode_message(rec) for rec in records]
        if self.wire_metrics is not None:
            for rec, buf in zip(records, framed_payloads, strict=True):
                kind = rec.get("event", {}).get("k") or rec.get("type", "")
                self.wire_metrics.record(kind, len(buf))
        self.broadcast_bytes(
            frame_batch(framed_payloads), records, exclude_origin, audience=audience,
        )

    def broadcast_bytes(
        self,
        payload: bytes,
        records: list[dict],
        exclude_origin: str | None = None,
        *,
        audience: str = _AUDIENCE_ALL,
        notify_listeners: bool = True,
    ):
        """Enqueue pre-framed payload for broadcast and notify listeners."""
        self._enqueue_broadcast(payload, exclude_origin, audience)
        if not notify_listeners:
            return
        self._notify_event_listeners(records)

    def broadcast_message(
        self,
        msg: dict,
        exclude_origin: str | None = None,
        *,
        audience: str = _AUDIENCE_ALL,
    ):
        """Enqueue a one-off non-event message (PlaybackState, etc.) for broadcast.

        Bypasses the event listener path playback messages are control-plane
        signals, not USD scene events, so they shouldn't appear in the
        dashboard event log.
        """
        payload = frame_batch([encode_message(msg)])
        self._enqueue_broadcast(payload, exclude_origin, audience)

    def _enqueue_broadcast(
        self,
        payload: bytes,
        exclude_origin: str | None,
        audience: str,
    ) -> None:
        targets = self._receiver_targets(
            exclude_origin=exclude_origin,
            audience=audience,
        )
        if targets:
            self._broadcast_queue.put((payload, targets))

    def _broadcast_loop(self):
        """Dedicated thread: drain the broadcast queue and send to receivers.

        During idle periods, sends periodic pings to detect dead receivers.
        Exits cleanly when a None sentinel is enqueued via shutdown().
        """
        _ping_payload = frame_batch([encode_message({"type": MSG_PING})])
        stopping = False
        while True:
            try:
                item = (
                    self._broadcast_queue.get_nowait() if stopping
                    else self._broadcast_queue.get(timeout=_PING_INTERVAL)
                )
            except queue.Empty:
                if stopping:
                    return
                # Idle send pings to detect dead receivers
                self._send_to_all(_ping_payload)
                continue

            try:
                if item is None:
                    stopping = True
                else:
                    payload, targets = item
                    self._send_to_all(payload, targets=targets)
            except Exception:
                LOG.exception("Unexpected error in broadcast loop")
            finally:
                self._broadcast_queue.task_done()

    def _send_to_all(
        self,
        payload: bytes,
        targets: tuple | None = None,
    ):
        """Send payload to matching receivers, removing dead ones.

        A failed send is the earliest reliable signal that a client is
        gone releases the playback-leader role here too so other
        clients don't wait a full keepalive cycle to reclaim it. The
        dead handler's recv loop exits separately once keepalive expires.
        """
        if targets is None:
            targets = self._receiver_targets()
        dead = []
        for h in targets:
            try:
                with h.send_lock:
                    h.request.sendall(payload)
                if self.wire_metrics is not None:
                    self.wire_metrics.record_transport(
                        "receiver_egress",
                        len(payload),
                    )
            except (OSError, TimeoutError):
                LOG.debug("Send failed for %s, marking as dead", h.client_address)
                dead.append(h)
        self._discard_unreachable_receivers(dead)

    def _receiver_targets(
        self,
        *,
        exclude_origin: str | None = None,
        audience: str = _AUDIENCE_ALL,
    ) -> tuple:
        """Capture receivers matching one broadcast's delivery contract."""
        if audience not in _AUDIENCES:
            raise ValueError(f"unknown receiver audience {audience!r}")
        with self.clients_lock:
            targets = []
            for handler in self.receivers:
                layered = handler._layered_replay
                if audience == _AUDIENCE_LAYERED and not layered:
                    continue
                if audience == _AUDIENCE_FLAT and layered:
                    continue
                if exclude_origin and handler._origin == exclude_origin:
                    continue
                targets.append(handler)
        return tuple(targets)

    def _discard_unreachable_receivers(self, handlers: list) -> None:
        """Remove failed receiver sockets and release their server-side roles."""
        if not handlers:
            return
        with self.clients_lock:
            for handler in handlers:
                self.receivers.discard(handler)
        released_any = False
        for handler in handlers:
            handler.release_receiver_replay_reservation()
            if handler._client_id and self.release_playback(handler._client_id):
                released_any = True
        if released_any:
            self.broadcast_message(
                {"type": MSG_PLAYBACK_STATE, **self.get_playback_state()},
            )

    def apply_txn(
        self,
        events: list[dict],
        layer: Sdf.Layer | None = None,
        *,
        update_tracking: bool = True,
    ) -> None:
        """Apply a transaction to the stage.

        Layer opinions are authored into *layer* (defaults to
        ``self.edit_layer``). Shared session metadata and stage-state events
        are applied under the primary session edit target.
        """
        validate_events(events, layer_mode=self.layer_mode)
        self._scene.apply_validated(
            events,
            layer=layer,
            update_tracking=update_tracking,
        )

    def process_idempotent_txn(
        self,
        events: list[dict],
        *,
        session_id: str,
        txn_id: int,
        client_id: str,
        origin: str | None = None,
        client_addr: str | None = None,
        layer: Sdf.Layer | None = None,
        layer_key: str = "",
    ) -> TransactionCommit:
        """Commit once for an ordered producer session and return its result."""
        request = self.submit_idempotent_txn(
            events,
            session_id=session_id,
            txn_id=txn_id,
            client_id=client_id,
            origin=origin,
            client_addr=client_addr,
            layer=layer,
            layer_key=layer_key,
        )
        return self.wait_for_transaction(request)

    def submit_idempotent_txn(
        self,
        events: list[dict],
        *,
        session_id: str,
        txn_id: int,
        client_id: str,
        origin: str | None = None,
        client_addr: str | None = None,
        layer: Sdf.Layer | None = None,
        layer_key: str = "",
    ) -> TransactionRequest:
        """Submit without waiting; the coordinator owns its maintenance barrier."""
        if not client_id or not session_id or len(session_id) > 128 or txn_id < 1:
            raise TransactionRejectedError(
                "invalid_identity",
                "client_id and session_id are required and txn_id must be positive",
            )
        validate_events(events, layer_mode=self.layer_mode)

        request = TransactionRequest(
            events=events,
            session_id=session_id,
            txn_id=txn_id,
            client_id=client_id,
            origin=origin,
            client_addr=client_addr,
            layer=layer,
            layer_key=layer_key,
        )
        return self._transactions.submit(request)

    @staticmethod
    def wait_for_transaction(request: TransactionRequest) -> TransactionCommit:
        """Wait for a previously submitted transaction's terminal outcome."""
        return request.wait()

    def _process_idempotent_txn_now(self, request: TransactionRequest) -> TransactionCommit:
        with self._journal.commit_scope():
            commit = self._committer.commit(request)
            if commit.status != "committed":
                return commit
            self._publish_transaction_commit(request, commit)
            return commit

    def _commit_managed_transaction_group(
        self, requests: list[TransactionRequest],
    ) -> list[TransactionOutcome]:
        """Commit an admitted batch; the coordinator ends it at any payload load."""
        with self._journal.commit_scope():
            outcomes = self._committer.commit_group(requests)
            self._broadcast_grouped_transactions(requests, outcomes)
            return outcomes

    def _publish_transaction_commit(
        self, request: TransactionRequest, commit: TransactionCommit,
    ) -> None:
        """Enqueue one durable commit before releasing global commit order."""
        try:
            self.broadcast_transaction_views(commit.records)
            for prim_path in request.payload_load_paths:
                self.replay_children_after_load(prim_path)
        except Exception:
            LOG.exception(
                "Transaction %s/%d committed but its live broadcast failed",
                request.session_id,
                request.txn_id,
            )

    def producer_committed_through(self, client_id: str, session_id: str) -> int:
        return self._journal.committed_through(client_id, session_id)

    def _broadcast_grouped_transactions(
        self,
        requests: list[TransactionRequest],
        outcomes: list[TransactionOutcome],
    ) -> None:
        records = []
        payload_replay = None
        for request, commit in zip(requests, outcomes, strict=True):
            if not isinstance(commit, TransactionCommit):
                continue
            if commit.status != "committed" or not commit.records:
                continue
            if request.payload_load_paths:
                # The coordinator ends the batch here, before later transactions
                # can reserve sequences that would precede the child replay.
                payload_replay = request, commit
            else:
                records.append(commit.records)
        try:
            self.broadcast_transaction_group_views(records)
        except Exception:
            LOG.exception("Committed transaction group could not be broadcast live")
        if payload_replay is not None:
            self._publish_transaction_commit(*payload_replay)

    def _commit_events(
        self,
        events: list[dict],
        *,
        client_id: str | None = None,
        origin: str | None = None,
        client_addr: str | None = None,
        layer: Sdf.Layer | None = None,
        layer_key: str = "",
    ) -> list[tuple[dict, bytes]]:
        """Commit events for in-process tests and maintenance utilities.

        Network producers must use :meth:`submit_idempotent_txn`. This private
        helper deliberately omits producer identity and acknowledgements, but
        still participates in the maintenance barrier and global commit order.
        Managed edits persist synchronously within the USD rollback scope.
        Shared-stage internal edits follow the configured durability policy.
        """
        if not events:
            return []
        validate_events(events, layer_mode=self.layer_mode)
        with self.txn_barrier.shared(), self._journal.commit_scope():
            return self._committer.commit_events(Transaction(
                events=events,
                client_id=client_id,
                origin=origin,
                client_addr=client_addr,
                layer=layer,
                layer_key=layer_key,
            ))

    def refresh_shared_layer_dependencies(self) -> tuple[str, ...]:
        """Resolve newly available sublayers and publish their routing state."""
        graph = self.shared_layer_graph
        if self.layer_mode is not LayerMode.SHARED_STAGE or graph is None:
            raise RuntimeError("shared layer dependency refresh requires shared-stage mode")

        with self.txn_barrier.shared():
            with self._journal.commit_scope():
                before = set(graph.reachable_layer_keys())
                with self.stage_lock, graph.transaction():
                    Ar.GetResolver().RefreshContext(self.stage.GetPathResolverContext())
                    routed_events = list(graph.refresh_resolved_sublayers())
                if routed_events:
                    records = self._committer.persist_shared_events(routed_events)
                    self.broadcast_transaction_views(records)
                return tuple(
                    layer_key
                    for layer_key in graph.reachable_layer_keys()
                    if layer_key not in before
                )

    def get_prim_count(self) -> int:
        return self._scene.get_prim_count()

    def get_tracked_prim_count(self) -> int:
        return self._scene.get_tracked_prim_count()

    def get_event_count(self) -> int:
        return self._journal.event_count

    def get_wire_metrics(self) -> dict:
        """Encoded record bytes per event kind since startup.

        Counts each record once at encode time (txn sequencing and
        server-initiated broadcasts); per-receiver fan-out and backlog
        replays are not multiplied in. Returns {"enabled": False} unless
        the server was started with wire metrics on.
        """
        if self.wire_metrics is None:
            return {"enabled": False}
        return {"enabled": True, **self.wire_metrics.snapshot()}

    def query_events(
        self,
        offset: int = 0,
        limit: int = 50,
        kind: str = "",
        prim_contains: str = "",
    ) -> tuple[list[dict], int]:
        """Return a page of events and total matching count (thread-safe)."""
        blobs, count = self.store.query(
            offset=offset,
            limit=limit,
            kind=kind,
            prim_contains=prim_contains,
        )
        return [message_to_dict(b) for b in blobs], count

    def get_client_list(self) -> list[dict]:
        """Return a snapshot of connected clients (thread-safe)."""
        now = time.time()
        with self.clients_lock:
            return [
                {
                    "key": k,
                    "role": i.role,
                    "address": f"{i.address[0]}:{i.address[1]}",
                    "client_id": i.client_id,
                    "origin": i.origin,
                    "connected_at": i.connected_at,
                    "last_activity_ago": round(now - i.last_activity, 1),
                    "event_count": i.event_count,
                }
                for k, i in self.clients.items()
            ]

    def get_uptime(self) -> float:
        """Return server uptime in seconds."""
        return time.time() - self._start_time

    def get_server_info(self) -> dict:
        """Return server configuration."""
        with self.stage_lock:
            root = self.stage.GetRootLayer()
            return {
                "base_usd_path": root.realPath or None,
                "root_layer": root.identifier,
                "edit_layer": self.edit_layer.identifier,
            }

    def get_prim_tree(self) -> list[dict]:
        return self._scene.get_prim_tree()

    def get_instance_count(self) -> int:
        return self._scene.get_instance_count()

    def get_prototype_count(self) -> int:
        return self._scene.get_prototype_count()

    def get_prim_detail(self, path: str) -> dict:
        return self._scene.get_prim_detail(path)

    def get_transforms_snapshot(self) -> list[dict]:
        return self._scene.get_transforms_snapshot()

    def export_edit_layer(self, file_path: str | None = None) -> str:
        """Export the server's edit layer as a USDA string (thread-safe).

        If *file_path* is given, also writes the layer to disk.  The exported
        layer contains only the opinions authored by the server; the base
        layer and its sublayers are not included.
        """
        layer = self._scene.snapshot_layer(self.edit_layer)
        usda = layer.ExportToString()
        if file_path:
            layer.Export(file_path)
            LOG.info("Exported edit layer to %s", file_path)
        return usda

    def export_layer(self, key: str) -> str:
        """Export one client/department layer as USDA (thread-safe).

        Copies the layer under the scene lock, then serializes the detached copy.
        """
        with self.stage_lock:
            layer = self.resolve_layer(key)
            if layer is not None:
                layer = self._scene.snapshot_layer(layer)
        return layer.ExportToString() if layer else "# layer not found"

    def export_flattened(self, file_path: str) -> None:
        """Export the fully composed stage as a single flattened USD file.

        All layers, composition arcs, and opinions are resolved into final
        values.  The result is a standalone file with no external dependencies
        useful for archiving, delivery, or rendering.
        """
        # Flatten under lock (fast creates a composed snapshot), then
        # export outside lock to avoid blocking mutations during disk I/O.
        with self.stage_lock:
            flat = self.stage.Flatten()
        flat.Export(file_path)
        LOG.info("Exported flattened stage to %s", file_path)

    def export_flattened_string(self) -> str:
        """Return the fully composed stage as a USDA string (thread-safe)."""
        with self.stage_lock:
            flat = self.stage.Flatten()
        return flat.ExportToString()

    def replay_from(
        self,
        handler,
        seq_start: int,
        *,
        seq_end: int | None = None,
    ):
        """Replay events from the event store in an inclusive sequence range.

        All events are replayed regardless of origin, matching live delivery.
        The receiver needs its own prior edits in the complete server order to
        restore and converge state.
        Layered receivers get every persisted authored record with its logical
        layer target. Flat receivers are admitted only for a single unmuted
        collaboration layer, so both modes replay stored records directly.
        """
        blobs = self.store.get_from_seq_bin(seq_start, seq_end)
        self.replay_records(handler, blobs)

    def replay_records(self, handler, records: Iterable[bytes]) -> None:
        """Send already captured records without rereading mutable history."""
        _REPLAY_CHUNK = 65536
        buf_parts: list[bytes] = []
        buf_size = 0
        for blob in records:
            framed = frame_batch([blob])
            buf_parts.append(framed)
            buf_size += len(framed)
            if buf_size >= _REPLAY_CHUNK:
                handler.request.sendall(b"".join(buf_parts))
                buf_parts.clear()
                buf_size = 0
        if buf_parts:
            handler.request.sendall(b"".join(buf_parts))
