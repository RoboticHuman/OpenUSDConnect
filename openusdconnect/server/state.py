"""UsdSyncServer: authoritative state for the sync protocol.

Holds the in-memory ``Usd.Stage``, the collaboration layer stack, the SQLite
event log, the broadcast and persistence threads, the TOFU token store, and
proposal bookkeeping. Network handling lives in ``connection.py``; the CLI
lives in ``cli.py``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field

from pxr import Ar, Sdf, Usd, UsdGeom

from ..codec import encode_message, message_to_dict
from ..emitter import (
    NoticeEmitter,
    read_payloads,
    read_references,
    read_stage_metadata,
)
from ..event_store import EventStore, ProducerProgress, SqliteEventStore
from ..framing import frame_batch
from ..protocol_constants import (
    COLLABORATION_LAYER_KINDS,
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_REPLACE_SDF_LAYER_CONTENT,
    K_SET_CONNECTABLE_CONNECTION,
    K_SET_CONNECTABLE_INPUT,
    K_SET_GPRIM_ATTRS,
    K_SET_INSTANCEABLE,
    K_SET_MATERIAL_BINDING,
    K_SET_POINT_INSTANCER,
    K_SET_SDF_SPEC_FIELDS,
    K_SET_STAGE_METADATA,
    K_SET_SUBLAYERS,
    K_SET_VARIANT_SELECTIONS,
    K_SET_VISIBILITY,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
    MSG_EVENT,
    MSG_LAYER_GRAPH_STATE,
    MSG_LAYER_STACK_STATE,
    MSG_PING,
    MSG_PLAYBACK_STATE,
    MSG_REPLAY_COMPLETE,
    MSG_RESYNC,
    NON_COLLABORATION_KINDS,
    SDF_SPEC_KIND_ATTRIBUTE,
    SDF_SPEC_KIND_PROPERTY,
    SDF_SPEC_KIND_RELATIONSHIP,
    SHARED_STAGE_EVENT_KINDS,
    SHARED_STAGE_ONLY_KINDS,
    STAGE_METADATA_KEYS,
    LayerMode,
    event_apply_tier,
)
from ..sdf_spec_delta import merge_spec_events
from ..shared_layer_graph import PreparedSublayers, SharedLayerGraph, StaleLayerGraphError
from ..usd_state import read_material_binding, read_variant_selections
from ._txn_barrier import _TxnBarrier
from .layer_stack import CollaborationLayerStack
from .types import (
    AmbiguousVfsWriteError,
    ClientInfo,
    InvalidVfsWriteError,
    Proposal,
    ReplayModeConflictError,
    StaleVfsWriteError,
    TransactionCommit,
    TransactionRejectedError,
    UnsupportedVfsWriteError,
    VfsWriteAnalysis,
)

LOG = logging.getLogger(__name__)

# Bounded queue limits — provides natural backpressure when receivers or
# persistence can't keep up, preventing unbounded memory growth.
_BROADCAST_QUEUE_MAX = 10_000
_PERSIST_QUEUE_MAX = 10_000
_PING_INTERVAL = 30.0  # seconds between heartbeat pings during idle

_AUDIENCE_ALL = "all"
_AUDIENCE_FLAT = "flat"
_AUDIENCE_LAYERED = "layered"
_AUDIENCES = frozenset({_AUDIENCE_ALL, _AUDIENCE_FLAT, _AUDIENCE_LAYERED})
_DEFAULT_LAYER_KEY = "default"
_DEPARTMENT_LAYER_KEY_PREFIX = "department:"


@dataclass(slots=True)
class _TransactionRequest:
    events: list[dict]
    session_id: str
    txn_id: int
    client_id: str
    origin: str | None
    client_addr: str | None
    layer: Sdf.Layer | None
    layer_key: str
    done: threading.Event = field(default_factory=threading.Event)
    commit: TransactionCommit | None = None
    error: BaseException | None = None


@dataclass(slots=True)
class _PreparedTransaction:
    request: _TransactionRequest
    target_layer: Sdf.Layer
    collaboration_paths: set[str]
    has_session_events: bool
    records: list[tuple[dict, bytes]]
    persist_tuples: list[tuple[int, bytes, str | None, str | None, str | None]]
    progress: ProducerProgress


def _layer_key_for_department(department: str | None) -> str:
    if not department:
        return _DEFAULT_LAYER_KEY
    return f"{_DEPARTMENT_LAYER_KEY_PREFIX}{department}"


def _department_for_layer_key(layer_key: str) -> str | None:
    """Return department policy metadata for one OpenUSDConnect layer key."""
    if layer_key == _DEFAULT_LAYER_KEY:
        return None
    if layer_key.startswith(_DEPARTMENT_LAYER_KEY_PREFIX):
        department = layer_key[len(_DEPARTMENT_LAYER_KEY_PREFIX) :]
        if department:
            return department
    return None


def _label_for_layer_key(layer_key: str) -> str:
    department = _department_for_layer_key(layer_key)
    if department:
        return department
    if layer_key == _DEFAULT_LAYER_KEY:
        return "Default"
    return layer_key


# Log compaction is scoped to the layer that receives an event. Department
# policy maps clients onto portable collaboration layer keys. Shared session
# metadata and stage runtime state are global and therefore have no layer scope.
_CompactionKey = tuple[str, str, float | None, str]
_CompactedEntry = tuple[dict, dict, int]


def _prim_xform_trs(prim) -> dict | None:
    """Composed translate/orient/scale of a prim."""
    xf = UsdGeom.Xformable(prim)
    if not xf:
        return None
    trs: dict = {}
    for op in xf.GetOrderedXformOps():
        name = op.GetAttr().GetName()
        val = op.Get()
        if val is None:
            continue
        if name == "xformOp:translate":
            trs["t"] = [round(float(x), 5) for x in val]
        elif name == "xformOp:orient":
            trs["r"] = [round(float(val.GetReal()), 5)] + [
                round(float(x), 5) for x in val.GetImaginary()
            ]
        elif name == "xformOp:scale":
            trs["s"] = [round(float(x), 5) for x in val]
    return trs or None


def _abbrev_scalar(value) -> str:
    if value is None:
        return "—"
    s = str(value)
    return s if len(s) <= 120 else s[:117] + "…"


_PI_ARRAY_ATTRS = (
    "protoIndices",
    "positions",
    "orientations",
    "orientationsf",
    "scales",
    "velocities",
    "accelerations",
    "angularVelocities",
    "ids",
    "invisibleIds",
)


def _point_instancer_summary(prim) -> dict:
    """Bounded read of UsdGeomPointInstancer state for the inspector.

    Returns prototypes targets, instance count (from protoIndices length),
    which arrays are animated, and the size of the inactiveIds prim
    metadata when authored.
    """
    pi = UsdGeom.PointInstancer(prim)
    proto_rel = pi.GetPrototypesRel()
    targets = [t.pathString for t in proto_rel.GetTargets()] if proto_rel else []
    proto_indices = pi.GetProtoIndicesAttr()
    if proto_indices and proto_indices.HasAuthoredValue():
        sample_value = proto_indices.Get()
        instance_count = len(sample_value) if sample_value is not None else 0
    else:
        instance_count = 0
    animated = []
    for name in _PI_ARRAY_ATTRS:
        attr = prim.GetAttribute(name)
        if attr and attr.IsAuthored() and attr.GetNumTimeSamples() > 0:
            animated.append(name)
    inactive_count = None
    if prim.HasAuthoredMetadata("inactiveIds"):
        list_op = prim.GetMetadata("inactiveIds")
        if list_op is not None:
            inactive_count = len(list_op.ApplyOperations([]))
    return {
        "prototypes": targets,
        "instanceCount": instance_count,
        "animatedArrays": animated,
        "inactiveIdCount": inactive_count,
    }


def _authored_attr_rows(prim) -> list[dict]:
    """Authored attributes as ``{name, type, value, numTimeSamples}`` rows.

    Array-typed values are reported by type only — never materialized — so
    inspecting a heavy prim (e.g. a million-point mesh) copies no buffers
    while stage_lock is held.
    """
    rows = []
    for attr in sorted(prim.GetAuthoredAttributes(), key=lambda a: a.GetName()):
        tn = attr.GetTypeName()
        rows.append(
            {
                "name": attr.GetName(),
                "type": str(tn),
                "value": "[array]" if tn.isArray else _abbrev_scalar(attr.Get()),
                "numTimeSamples": attr.GetNumTimeSamples(),
            }
        )
    return rows


def _stage_prim_types(stage: Usd.Stage) -> dict[str, str]:
    return {
        str(prim.GetPath()): prim.GetTypeName()
        for prim in stage.Traverse()
        if str(prim.GetPath()) != "/"
    }


def _stage_live_metadata(stage: Usd.Stage) -> dict | None:
    layer_data = stage.GetRootLayer().customLayerData or {}
    if "openusdconnect" not in layer_data:
        return None
    metadata = layer_data["openusdconnect"]
    if not isinstance(metadata, dict):
        raise InvalidVfsWriteError("uploaded openusdconnect metadata must be a dictionary")
    return metadata


def _metadata_int(metadata: dict, key: str) -> int:
    value = metadata.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidVfsWriteError(
            f"uploaded openusdconnect metadata field {key!r} must be a non-negative integer"
        )
    return value


class _WireMetrics:
    """Thread-safe logical-record and actual transport byte counters."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._bytes: dict[str, int] = {}
        self._transport_counts: dict[str, int] = {}
        self._transport_bytes: dict[str, int] = {}

    def record(self, kind: str, nbytes: int) -> None:
        with self._lock:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            self._bytes[kind] = self._bytes.get(kind, 0) + nbytes

    def record_transport(self, channel: str, nbytes: int, *, count: int = 1) -> None:
        """Record framed bytes actually read or successfully written."""
        with self._lock:
            self._transport_counts[channel] = (
                self._transport_counts.get(channel, 0) + count
            )
            self._transport_bytes[channel] = (
                self._transport_bytes.get(channel, 0) + nbytes
            )

    def snapshot(self) -> dict:
        with self._lock:
            kinds = {
                k: {"count": self._counts[k], "bytes": self._bytes[k]} for k in sorted(self._counts)
            }
            transport = {
                channel: {
                    "count": self._transport_counts[channel],
                    "bytes": self._transport_bytes[channel],
                }
                for channel in sorted(self._transport_counts)
            }
        return {
            "kinds": kinds,
            "total_count": sum(v["count"] for v in kinds.values()),
            "total_bytes": sum(v["bytes"] for v in kinds.values()),
            "transport": transport,
            "transport_total_count": sum(v["count"] for v in transport.values()),
            "transport_total_bytes": sum(v["bytes"] for v in transport.values()),
        }


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
        txn_batch_size: int = 128,
        txn_batch_delay: float = 0.0005,
        wire_metrics: bool = False,
        compact_interval: float = 0,
        reclaim_interval: float = 0,
        stage: Usd.Stage | None = None,
        resolver_context: Ar.ResolverContext | None = None,
        layer_mode: LayerMode | str = LayerMode.MANAGED,
    ):
        if stage is not None and base_usd_path:
            raise ValueError("stage and base_usd_path are mutually exclusive")
        if stage is not None and resolver_context is not None:
            raise ValueError("a supplied stage already owns its resolver context")

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

        self.stage_lock = threading.RLock()

        # Non-destructive editing: fallback layer for clients without an ID.
        self.edit_layer = self._create_edit_layer()

        # Department assignment is collaboration policy. The layer stack itself
        # is keyed generically so replay does not depend on department concepts.
        self.client_layers: dict[str, Sdf.Layer] = {}
        self._client_layer_keys: dict[str, str] = {}
        self._client_departments: dict[str, str] = {}
        self.department_priority: list[str] = list(department_priority or [])
        if any(not department for department in self.department_priority):
            raise ValueError("department names must be non-empty")
        if len(set(self.department_priority)) != len(self.department_priority):
            raise ValueError("department priority contains duplicates")

        self.layer_stack = CollaborationLayerStack(
            self.stage,
            self.edit_layer,
            default_key=_DEFAULT_LAYER_KEY,
        )

        # Flat replay cannot represent layer order or muting. Reservations and
        # stack-policy changes share this lock so neither can invalidate the
        # other's contract during a handshake.
        self._replay_mode_lock = threading.Lock()
        self._flat_receiver_count = 0

        self.clients_lock = threading.Lock()
        self.receivers: set = set()
        self.clients: dict[str, ClientInfo] = {}
        self._event_listeners: list = []
        self._start_time = time.time()
        self._seq_lock = threading.Lock()
        self.txn_barrier = _TxnBarrier()
        # Identity lookup, USD mutation, sequence assignment, and producer
        # progress form one failure boundary across connection threads. The
        # same lock also covers live enqueue: a later durable sequence must
        # never become visible before an earlier one.
        self._transaction_commit_lock = threading.RLock()
        # Shared graph revisions participate in the same global persisted/live
        # sequence order as managed transactions.
        self._shared_stage_commit_lock = self._transaction_commit_lock
        self.txn_batch_size = max(1, int(txn_batch_size))
        self.txn_batch_delay = max(0.0, float(txn_batch_delay))
        self._transaction_queue: queue.Queue[_TransactionRequest | None] | None = None
        self._transaction_thread: threading.Thread | None = None
        self._transaction_stopping = False

        # Pluggable event store — defaults to SQLite
        self.store: EventStore = event_store or SqliteEventStore(log_path)
        # Lazy durable highwater cache. Commit serialization makes this an
        # exact mirror of producer_sessions after the first lookup; reconnect
        # handshakes and hot transaction batches avoid one SQLite query per
        # producer per group while restart still recovers from the store.
        self._producer_progress_cache: dict[tuple[str, str], int] = {}
        self._next_seq = self.store.get_max_seq() + 1
        self._event_count = self.store.get_count()
        # Periodic compaction skips when no seq was assigned since the last
        # compaction. Starts at 1 so a pre-existing uncompacted log gets one
        # compaction on the first tick.
        self._seq_at_last_compact = 1
        # Virtual-file snapshots are keyed by an epoch plus the latest assigned
        # sequence. Epoch disambiguates compaction/purge resetting sequence IDs.
        self._snapshot_epoch = 0
        self.scene_id = self._make_scene_id(base_usd_path)
        self.last_vfs_write_analysis: dict | None = None

        # TOFU token authentication
        self.require_token = require_token
        self.token_store = None
        if require_token:
            from ..token_store import TokenStore

            _token_path = token_db_path or log_path.replace(".db", "_tokens.db")
            self.token_store = TokenStore(_token_path)

        # Cross-department edit proposals
        self.proposals_lock = threading.Lock()
        self.proposals: dict[str, Proposal] = {}

        # Cached prim count — invalidated on structural events to avoid
        # full Traverse on every dashboard poll.
        self._prim_count: int = 0
        self._prim_count_dirty: bool = True

        # Playback synchronization — a single leader drives the shared
        # playhead. apply_playback_control rejects writes from non-leaders.
        self.playback_lock = threading.Lock()
        self.playback: dict = {
            "time": 0.0,
            "playing": False,
            "rate": 1.0,
            "leader_client_id": None,
        }

        # Async broadcast — emitter threads push to the queue, a dedicated
        # thread handles the actual network sends so emitters are never
        # blocked by slow receivers.
        # Each queued item retains the receiver membership captured when the
        # broadcast was authored. A receiver joining later obtains older
        # records only through its bounded replay window.
        # Item: (payload_bytes, receiver_handlers, exclude_origin, audience)
        self._broadcast_queue: queue.Queue = queue.Queue(maxsize=_BROADCAST_QUEUE_MAX)
        self._broadcast_thread = threading.Thread(
            target=self._broadcast_loop,
            daemon=True,
        )
        self._broadcast_thread.start()

        # Durability mode:
        #   "strict"   — persist to DB before broadcast (no lost events on crash)
        #   "realtime" — broadcast first, persist async (lower latency)
        self.durability = durability
        # Per-client rate limiting (0 = disabled)
        self.txn_rate = txn_rate
        self.txn_burst = txn_burst

        # Opt-in wire traffic diagnostics (--wire-metrics)
        self.wire_metrics: _WireMetrics | None = _WireMetrics() if wire_metrics else None

        # Periodic compaction (--compact-interval; 0 = disabled). Runtime
        # adjustable via set_compact_interval. compact_log itself is safe
        # against concurrent txns: its exclusive phase holds txn_barrier, so
        # incoming txns queue and proceed against the compacted log.
        self._compact_interval = max(0.0, float(compact_interval or 0))
        self._compact_stop = False
        self._compact_wake = threading.Event()
        self._compact_thread: threading.Thread | None = None
        if self._compact_interval > 0:
            self._start_compaction_thread()

        # Storage reclaim (--reclaim-interval; 0 = disabled). Evaluated at
        # compaction and purge commits, where the log was just rewritten and
        # the exclusive barrier is already held; no thread of its own.
        self._reclaim_interval = max(0.0, float(reclaim_interval or 0))
        self._last_reclaim = time.monotonic()
        self._persist_queue: queue.Queue | None = None
        if durability == "realtime":
            self._persist_queue = queue.Queue(maxsize=_PERSIST_QUEUE_MAX)
            self._persist_thread = threading.Thread(
                target=self._persist_loop,
                daemon=True,
            )
            self._persist_thread.start()

        # prim_path → (translate_op, orient_op, scale_op). A cached XformOp is
        # only valid while the stage edit target is unchanged: any SetEditTarget
        # (e.g. switching to another department's layer) invalidates it, so a
        # reused op authors against the wrong layer and the write is silently
        # lost. _op_cache_for clears the cache whenever the edit target changes;
        # consecutive edits to the same layer keep it (the single-client fast
        # path).
        from cachetools import LRUCache

        self.op_cache: LRUCache = LRUCache(
            maxsize=op_cache_size or self.DEFAULT_OP_CACHE_SIZE,
        )
        self._op_cache_layer: str | None = None

        # Incremental prim tracking — avoids full log scans on dashboard polls.
        self._prim_paths: dict[str, str] = {}  # prim_path → typeName
        # Per-prim flags maintained incrementally from events so dashboard
        # tree refreshes never have to query pxr per prim.
        self._instanceable_paths: set[str] = set()
        self._point_instancer_paths: set[str] = set()

        self.shared_layer_graph: SharedLayerGraph | None = None
        if self.layer_mode is LayerMode.SHARED_STAGE:
            self.shared_layer_graph = SharedLayerGraph(
                self.stage,
                authoritative=self.store.get_count() == 0,
            )
            if self.store.get_count() == 0:
                seq = self.assign_seq()
                self.append_log(self.shared_layer_graph.state_message(seq=seq))

        # Rebuild stage from the event log so the composed stage matches
        # what receivers would get on replay.
        self._replay_log_into_stage()
        if self.shared_layer_graph is not None:
            self.shared_layer_graph.authoritative = True
            self.refresh_shared_layer_dependencies()

        if (
            self.txn_batch_size > 1
            and self.layer_mode is LayerMode.MANAGED
        ):
            self._transaction_queue = queue.Queue(maxsize=_PERSIST_QUEUE_MAX)
            self._transaction_thread = threading.Thread(
                target=self._transaction_batch_loop,
                name="ouc-transaction-commit",
                daemon=True,
            )
            self._transaction_thread.start()

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
        """Signal background threads to drain queued work and exit.

        Compaction stops first (no rewrite mid-shutdown), then the persist
        queue drains (durability), then broadcast.
        """
        self._transaction_stopping = True
        if self._transaction_queue is not None:
            self._transaction_queue.put(None)
            self._transaction_thread.join(timeout=10.0)
        self._compact_stop = True
        self._compact_wake.set()
        if self._compact_thread is not None:
            self._compact_thread.join(timeout=10.0)
        if self._persist_queue is not None:
            self._persist_queue.put(None)
            self._persist_thread.join(timeout=10.0)
        self._broadcast_queue.put(None)
        self._broadcast_thread.join(timeout=10.0)

    # ------------------------------------------------------------------
    # Periodic compaction
    # ------------------------------------------------------------------

    def _start_compaction_thread(self):
        self._compact_thread = threading.Thread(
            target=self._compaction_loop,
            daemon=True,
        )
        self._compact_thread.start()

    def set_reclaim_interval(self, seconds: float) -> None:
        """Set the storage-reclaim interval in seconds (0 disables).

        Reclaim runs at compaction and purge commits, so an enabled
        interval needs compaction (periodic or manual) to take effect.
        """
        self._reclaim_interval = max(0.0, float(seconds or 0))

    def get_reclaim_interval(self) -> float:
        return self._reclaim_interval

    def _maybe_reclaim_storage(self) -> None:
        """Reclaim store disk space when the interval has elapsed.

        Called right after a log rewrite while the exclusive barrier is
        held; reclaiming then is cheap because only live data is copied.
        """
        if self._reclaim_interval <= 0:
            return
        if time.monotonic() - self._last_reclaim < self._reclaim_interval:
            return
        try:
            reclaimed = self.store.reclaim_storage()
        except Exception:
            LOG.exception("Failed to reclaim event-store storage")
            return
        self._last_reclaim = time.monotonic()
        if reclaimed:
            LOG.info("Reclaimed %.1f MB of event log storage", reclaimed / 1048576)

    def set_compact_interval(self, seconds: float) -> None:
        """Set the periodic compaction interval in seconds (0 disables).

        Takes effect immediately: the compaction thread re-reads the
        interval on wake, so shortening, lengthening, and disabling all
        apply without waiting out the previous period.
        """
        self._compact_interval = max(0.0, float(seconds or 0))
        if self._compact_interval > 0 and self._compact_thread is None:
            self._start_compaction_thread()
        self._compact_wake.set()

    def get_compact_interval(self) -> float:
        return self._compact_interval

    def _compaction_loop(self):
        """Compact every interval, skipping when no event arrived since the
        last compaction so idle servers don't resync receivers for nothing."""
        while not self._compact_stop:
            interval = self._compact_interval
            if interval <= 0:
                self._compact_wake.wait()
                self._compact_wake.clear()
                continue
            if self._compact_wake.wait(timeout=interval):
                self._compact_wake.clear()
                continue
            if self._compact_stop:
                return
            with self._seq_lock:
                pending = self._next_seq > self._seq_at_last_compact
            if not pending:
                continue
            try:
                self.compact_log()
            except Exception:
                LOG.exception("Periodic compaction failed")

    # ------------------------------------------------------------------
    # Playback synchronization
    # ------------------------------------------------------------------

    def get_stage_metadata_payload(self) -> dict:
        """Return the stage's authored metadata snapshot for hello_ok."""
        return read_stage_metadata(self.stage)

    def get_playback_state(self) -> dict:
        """Return a wire-shaped snapshot of the current playback state."""
        with self.playback_lock:
            return {
                "time": self.playback["time"],
                "playing": self.playback["playing"],
                "rate": self.playback["rate"],
                "leader_client_id": self.playback["leader_client_id"] or "",
            }

    def claim_playback(
        self,
        client_id: str,
        initial_time: float | None = None,
    ) -> tuple[bool, str]:
        """Grant the playback-leader role if vacant.

        Returns ``(granted, current_leader)``; on rejection the second value
        is the existing leader's client id so the caller can include it in
        a PlaybackRejected message. ``initial_time`` (optional) sets the
        shared playback timecode atomically with the grant so followers
        sync to the new leader's current playhead instead of the stale
        server-side value.
        """
        if not client_id:
            return False, self.playback["leader_client_id"] or ""
        with self.playback_lock:
            current = self.playback["leader_client_id"]
            if current and current != client_id:
                return False, current
            self.playback["leader_client_id"] = client_id
            if initial_time is not None:
                self.playback["time"] = float(initial_time)
            return True, client_id

    def apply_playback_control(
        self,
        client_id: str,
        action: str,
        time_value: float = 0.0,
        rate: float = 1.0,
    ) -> tuple[bool, dict | str, str]:
        """Apply a control command from the playback leader.

        Returns ``(True, new_state_dict, leader)`` on success or
        ``(False, reason, current_leader)`` when the requesting client is
        not the current leader. Always returns the current leader as the
        third element so callers don't have to re-read ``self.playback``
        outside the lock.
        """
        with self.playback_lock:
            leader = self.playback["leader_client_id"] or ""
            if leader != client_id:
                return False, f"not the playback leader (current: {leader})", leader
            if action == "play":
                self.playback["playing"] = True
            elif action == "pause":
                self.playback["playing"] = False
            elif action == "stop":
                self.playback["playing"] = False
                self.playback["time"] = 0.0
            elif action == "set_time":
                self.playback["time"] = float(time_value)
            elif action == "set_rate":
                self.playback["rate"] = float(rate)
            else:
                return False, f"unknown playback action {action!r}", leader
            return (
                True,
                {
                    "time": self.playback["time"],
                    "playing": self.playback["playing"],
                    "rate": self.playback["rate"],
                    "leader_client_id": self.playback["leader_client_id"] or "",
                },
                leader,
            )

    def release_playback(self, client_id: str) -> bool:
        """Release the leader role when a leader disconnects.

        Returns True if the role was actually released (so the caller knows
        to broadcast a PlaybackState with an empty leader_client_id).
        """
        if not client_id:
            return False
        with self.playback_lock:
            if self.playback["leader_client_id"] == client_id:
                self.playback["leader_client_id"] = None
                return True
            return False

    def _create_edit_layer(self, label: str = "server-edits") -> Sdf.Layer:
        """Create an override sublayer on the session layer and set it as the edit target.

        The session layer is stronger than the entire root layer stack, so
        opinions authored here always compose on top of the base file and
        its sublayers.  The override is inserted as a sublayer of the session
        layer (rather than using the session layer directly) so that
        multi-user mode can add per-client sublayers alongside it.

        Accepts an optional *label* for the layer identifier — this is the
        extension point for per-client layers.
        """
        layer = Sdf.Layer.CreateAnonymous(label)
        session = self.stage.GetSessionLayer()
        session.subLayerPaths.insert(0, layer.identifier)
        self.stage.SetEditTarget(Usd.EditTarget(layer))
        return layer

    def _op_cache_for(self, layer: Sdf.Layer):
        """Return the op cache for editing *layer*, clearing it when the edit
        target changed since it was last populated. A cached XformOp is only
        valid while the edit target is unchanged, so any switch to another layer
        invalidates every entry; without the clear a reused op authors against
        the wrong layer and the write is lost. Callers must SetEditTarget to
        *layer* before authoring."""
        if self._op_cache_layer != layer.identifier:
            self.op_cache.clear()
            self._op_cache_layer = layer.identifier
        return self.op_cache

    def _track_prim_event(self, ev: dict):
        """Update incremental prim trackers from a single event.

        Covers ensure/delete/rename plus instancing flags. The dashboard
        relies on this so its tree refresh never has to query pxr.
        """
        k = ev.get("k")
        prim = ev.get("prim", "")
        if k == K_ENSURE_PRIM:
            type_name = ev["typeName"]
            self._prim_paths[prim] = type_name
            if type_name == "PointInstancer":
                self._point_instancer_paths.add(prim)
        elif k == K_DELETE_PRIM:
            self._prim_paths.pop(prim, None)
            self._instanceable_paths.discard(prim)
            self._point_instancer_paths.discard(prim)
        elif k == K_RENAME_PRIM:
            type_name = self._prim_paths.pop(prim, "Xform")
            was_instanceable = prim in self._instanceable_paths
            was_pi = prim in self._point_instancer_paths
            self._instanceable_paths.discard(prim)
            self._point_instancer_paths.discard(prim)
            new_name = ev.get("new_name", "")
            if new_name:
                parent = prim.rsplit("/", 1)[0] or "/"
                new_path = f"{parent}/{new_name}" if parent != "/" else f"/{new_name}"
                self._prim_paths[new_path] = type_name
                if was_instanceable:
                    self._instanceable_paths.add(new_path)
                if was_pi:
                    self._point_instancer_paths.add(new_path)
        elif k == K_SET_INSTANCEABLE:
            if ev.get("instanceable", True):
                self._instanceable_paths.add(prim)
            else:
                self._instanceable_paths.discard(prim)

    def _replay_log_into_stage(self):
        """Apply all events from the event store to restore stage on startup.

        Each authored opinion routes by its portable collaboration layer key.
        Department metadata is recovered only as policy/UI context. Global log
        order is preserved across layers; adjacent events for one layer are
        batched. Shared session and stage-state events use the session layer.
        """
        from ..event_apply import apply_events

        rows = self.store.get_all_asc()
        if not rows:
            return
        if self.layer_mode is LayerMode.SHARED_STAGE:
            self._replay_shared_log(rows)
            return

        routed: list[tuple[Sdf.Layer, dict]] = []
        for _seq, record_bin in rows:
            rec = message_to_dict(record_bin, numpy_arrays=True)
            ev = rec.get("event", rec)
            client_id = rec.get("client_id")
            if ev.get("k") in NON_COLLABORATION_KINDS:
                layer = self.stage.GetSessionLayer()
            else:
                layer_key = rec.get("layer_key") or ""
                if not layer_key:
                    raise ValueError("persisted collaboration opinion is missing layer_key")
                layer, _added = self.layer_stack.ensure_layer(
                    layer_key,
                    label=_label_for_layer_key(layer_key),
                )
                if client_id:
                    self._client_layer_keys[client_id] = layer_key
                    self.client_layers[client_id] = layer
                    department = _department_for_layer_key(layer_key)
                    if department:
                        self._client_departments[client_id] = department
            routed.append((layer, ev))

        self._apply_department_order()

        current_layer = None
        run: list[dict] = []

        def _apply_run():
            if not run:
                return
            self.stage.SetEditTarget(Usd.EditTarget(current_layer))
            apply_events(
                self.stage,
                run,
                op_cache=self._op_cache_for(current_layer),
            )

        for layer, ev in routed:
            if current_layer is not None and layer is not current_layer:
                _apply_run()
                run = []
            current_layer = layer
            run.append(ev)
        _apply_run()
        self.stage.SetEditTarget(Usd.EditTarget(self.edit_layer))

        # Populate incremental prim tracking from replayed events.
        for _layer, ev in routed:
            self._track_prim_event(ev)

        LOG.info("Restored stage from event log: %d events", len(routed))

    def _replay_shared_log(self, rows: list[tuple[int, bytes]]) -> None:
        """Restore exact file-backed layer opinions in persisted order."""
        from ..event_apply import apply_events

        graph = self.shared_layer_graph
        if graph is None:
            raise RuntimeError("shared-stage replay requires a layer graph")

        baseline_seen = False
        current_key = ""
        run: list[dict] = []

        def _apply_run() -> None:
            if not run:
                return
            layer = graph.layer_for(current_key)
            if layer is None:
                raise ValueError(f"persisted event targets unresolved layer key {current_key!r}")
            with Usd.EditContext(self.stage, Usd.EditTarget(layer)):
                apply_events(self.stage, run)

        with self.stage_lock:
            for _seq, record_bin in rows:
                record = message_to_dict(record_bin, numpy_arrays=True)
                if record.get("type") == MSG_LAYER_GRAPH_STATE:
                    _apply_run()
                    run = []
                    current_key = ""
                    graph.apply_state(record)
                    baseline_seen = True
                    continue
                if record.get("type") != MSG_EVENT:
                    raise ValueError("shared-stage log contains an unsupported record")
                if not baseline_seen:
                    raise ValueError("shared-stage log must begin with a layer graph baseline")
                event = record.get("event", {})
                layer_key = record.get("layer_key") or ""
                if event.get("k") == K_SET_SUBLAYERS:
                    _apply_run()
                    run = []
                    current_key = ""
                    graph.apply_sublayers(layer_key, event)
                    continue
                if event.get("k") not in (
                    K_SET_SDF_SPEC_FIELDS,
                    K_REPLACE_SDF_LAYER_CONTENT,
                ):
                    raise ValueError(
                        f"shared-stage log contains unsupported event {event.get('k')!r}"
                    )
                if run and layer_key != current_key:
                    _apply_run()
                    run = []
                current_key = layer_key
                run.append(event)
            _apply_run()

        if not baseline_seen:
            raise ValueError("shared-stage log has no layer graph baseline")
        self.stage.SetEditTarget(Usd.EditTarget(self.edit_layer))
        self._prim_count_dirty = True
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
            # First connect — issue token (TOFU)
            new_token = self.token_store.issue(client_id, department)
            return True, new_token

        if token and self.token_store.verify(client_id, token):
            return True, None

        LOG.warning("Auth rejected for %s — invalid or missing token", client_id)
        return False, None

    def revoke_token(self, client_id: str) -> bool:
        """Revoke a client's token via dashboard/API."""
        if not self.token_store:
            return False
        return self.token_store.revoke(client_id)

    def bump_snapshot_epoch(self, reason: str = "") -> None:
        """Invalidate cached virtual-file snapshots after non-log stage changes."""
        with self._seq_lock:
            self._snapshot_epoch += 1
            epoch = self._snapshot_epoch
        if reason:
            LOG.debug("Snapshot epoch bumped to %d (%s)", epoch, reason)
        else:
            LOG.debug("Snapshot epoch bumped to %d", epoch)

    def get_token_list(self) -> list[dict]:
        """Return all token records for the dashboard."""
        if not self.token_store:
            return []
        return self.token_store.get_all()

    # -- Proposals (cross-department edit requests) ----------------------

    def create_proposal(
        self,
        from_client: str,
        target_department: str,
        description: str = "",
    ) -> str:
        """Create a proposal targeting another department.

        Creates a muted layer for the proposal. The proposer sends txns
        targeting this proposal_id. The target department reviews in the
        dashboard.

        Returns the proposal_id.
        """
        if self.layer_mode is LayerMode.SHARED_STAGE:
            raise RuntimeError("department proposals are unavailable in shared-stage mode")
        import uuid

        proposal_id = f"prop-{uuid.uuid4().hex[:8]}"
        from_dept = self._client_departments.get(from_client)

        with self.stage_lock:
            layer = Sdf.Layer.CreateAnonymous(f"proposal-{proposal_id}")
            session = self.stage.GetSessionLayer()
            # Insert at the end (weakest) so preview doesn't accidentally
            # override real layers.
            session.subLayerPaths.append(layer.identifier)
            self.stage.MuteLayer(layer.identifier)

        proposal = Proposal(
            proposal_id=proposal_id,
            from_client=from_client,
            from_department=from_dept,
            target_department=target_department,
            description=description,
            layer=layer,
        )
        with self.proposals_lock:
            self.proposals[proposal_id] = proposal
        LOG.info(
            "Proposal %s created: %s → %s (%s)",
            proposal_id,
            from_client,
            target_department,
            description,
        )
        return proposal_id

    def list_proposals(
        self,
        department: str | None = None,
        include_usda: bool = True,
    ) -> list[dict]:
        """List proposals, optionally filtered by target department.

        If department is None, returns all proposals (admin view).
        Set include_usda=False to skip expensive ExportToString().
        """
        with self.proposals_lock:
            snapshot = list(self.proposals.values())
        result = []
        for p in snapshot:
            if department and p.target_department != department:
                continue
            entry = {
                "proposal_id": p.proposal_id,
                "from_client": p.from_client,
                "from_department": p.from_department,
                "target_department": p.target_department,
                "description": p.description,
                "status": p.status,
                "created_at": p.created_at,
            }
            if include_usda:
                entry["layer_usda"] = p.layer.ExportToString()
            result.append(entry)
        return result

    def approve_proposal(self, proposal_id: str) -> bool:
        """Approve a proposal — replay events into target department layer.

        Replays the accumulated events through apply_txn targeting the
        department layer. This handles stage mutation, broadcast, and log
        persistence through the same path as normal transactions.
        Then removes the proposal layer.
        """
        self.txn_barrier.acquire_shared()
        try:
            return self._approve_proposal_inner(proposal_id)
        finally:
            self.txn_barrier.release_shared()

    def _approve_proposal_inner(self, proposal_id: str) -> bool:
        with self.proposals_lock:
            p = self.proposals.get(proposal_id)
        if not p or p.status != "pending":
            return False

        target_layer = self.resolve_layer(p.target_department)
        if not target_layer:
            LOG.warning(
                "Cannot approve proposal %s — target department '%s' has no layer",
                proposal_id,
                p.target_department,
            )
            return False

        if p.events:
            self.apply_txn(p.events, layer=target_layer)

            records: list[tuple[dict, bytes]] = []
            persist_tuples = []
            for ev in p.events:
                # department is the TARGET: the merge authored these opinions
                # into the target department's layer, so replay must too.
                rec = {
                    "type": MSG_EVENT,
                    "seq": self.assign_seq(),
                    "event": ev,
                    "client_id": p.from_client,
                    "origin": f"proposal-{p.proposal_id}",
                }
                if ev.get("k") not in NON_COLLABORATION_KINDS:
                    rec["layer_key"] = _layer_key_for_department(
                        p.target_department,
                    )
                rec_bin = encode_message(rec)
                records.append((rec, rec_bin))
                persist_tuples.append(
                    (
                        rec["seq"],
                        rec_bin,
                        p.from_client,
                        ev.get("k"),
                        ev.get("prim"),
                    )
                )
            self.append_log_batch(persist_tuples)
            self.broadcast_transaction_views(records)

        # Remove proposal layer from session
        with self.stage_lock:
            self.stage.UnmuteLayer(p.layer.identifier)
            session = self.stage.GetSessionLayer()
            idx = list(session.subLayerPaths).index(p.layer.identifier)
            del session.subLayerPaths[idx]

        p.status = "approved"
        self.bump_snapshot_epoch(f"approve_proposal:{proposal_id}")
        LOG.info("Proposal %s approved — merged into %s", proposal_id, p.target_department)
        return True

    def reject_proposal(self, proposal_id: str) -> bool:
        """Reject a proposal — discard the layer."""
        with self.proposals_lock:
            p = self.proposals.get(proposal_id)
        if not p or p.status != "pending":
            return False

        with self.stage_lock:
            self.stage.MuteLayer(p.layer.identifier)
            session = self.stage.GetSessionLayer()
            idx = list(session.subLayerPaths).index(p.layer.identifier)
            del session.subLayerPaths[idx]

        p.status = "rejected"
        self.bump_snapshot_epoch(f"reject_proposal:{proposal_id}")
        LOG.info("Proposal %s rejected", proposal_id)
        return True

    def get_proposal_count(self) -> int:
        """Thread-safe proposal count for dashboard polling."""
        with self.proposals_lock:
            return len(self.proposals)

    def get_proposal_layer(self, proposal_id: str) -> Sdf.Layer | None:
        """Get the layer for a proposal (for txn routing)."""
        with self.proposals_lock:
            p = self.proposals.get(proposal_id)
        if p and p.status == "pending":
            return p.layer
        return None

    def apply_proposal_txn(self, proposal_id: str, events: list[dict]) -> bool:
        """Apply a txn to a pending proposal's muted layer (no broadcast).

        Muted layers can't be an edit target, so unmute for the write and
        re-mute after — the opinions stay out of composition. Events also
        accumulate on the proposal for the merge + log replay on approval.
        """
        if self.layer_mode is LayerMode.SHARED_STAGE:
            return False
        from ..event_apply import apply_events

        with self.proposals_lock:
            p = self.proposals.get(proposal_id)
        if not p or p.status != "pending":
            return False
        unsupported = sorted(
            {
                event.get("k", "")
                for event in events
                if event.get("k") not in COLLABORATION_LAYER_KINDS
            }
        )
        if unsupported:
            LOG.warning(
                "Proposal %s contains non-collaboration events: %s",
                proposal_id,
                ", ".join(repr(kind) for kind in unsupported),
            )
            return False
        with self.stage_lock:
            self.stage.UnmuteLayer(p.layer.identifier)
            self.stage.SetEditTarget(Usd.EditTarget(p.layer))
            apply_events(self.stage, events, op_cache=self._op_cache_for(p.layer))
            self.stage.MuteLayer(p.layer.identifier)
        p.events.extend(events)
        LOG.debug("Applied %d events to proposal %s", len(events), proposal_id)
        return True

    # -- Per-client layer management ------------------------------------

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
        layer_key = _layer_key_for_department(department)
        if department:
            layer = self._get_or_create_department_layer(department)
            self._client_departments[client_id] = department
        else:
            layer = self.edit_layer
        self._client_layer_keys[client_id] = layer_key
        self.client_layers[client_id] = layer
        return layer

    def _flat_replay_rejection_reason_unlocked(self) -> str:
        """Return why a new flat receiver cannot mirror this server."""
        if self.department_priority:
            return "department collaboration requires layered replay"
        if len(self.layer_stack.layer_keys) != 1:
            return "multiple collaboration layers require layered replay"
        layer = self.layer_stack.ordered_layers[0]
        with self.stage_lock:
            if self.stage.IsLayerMuted(layer.identifier):
                return "muted collaboration layers require layered replay"
        return ""

    def reserve_receiver_replay_mode(self, layered: bool) -> tuple[bool, str]:
        """Atomically admit a receiver under the current layer-stack contract."""
        if layered:
            return True, ""
        with self._replay_mode_lock:
            reason = self._flat_replay_rejection_reason_unlocked()
            if reason:
                return False, reason
            self._flat_receiver_count += 1
        return True, ""

    def release_receiver_replay_mode(self, layered: bool) -> None:
        """Release a replay-mode reservation when a receiver disconnects."""
        if layered:
            return
        with self._replay_mode_lock:
            if self._flat_receiver_count <= 0:
                raise RuntimeError("flat receiver reservation underflow")
            self._flat_receiver_count -= 1

    def _reject_layer_stack_change_for_flat_receivers(self) -> None:
        if self._flat_receiver_count:
            raise ReplayModeConflictError(
                "layer-stack changes require all connected receivers to use layered replay"
            )

    def _get_or_create_department_layer(self, department: str) -> Sdf.Layer:
        """Resolve department policy to one shared collaboration layer."""
        layer_key = _layer_key_for_department(department)
        with self._replay_mode_lock:
            if not self.layer_stack.has_layer(layer_key):
                self._reject_layer_stack_change_for_flat_receivers()
            with self.stage_lock:
                layer, added = self.layer_stack.ensure_layer(
                    layer_key,
                    label=department,
                )
                if added:
                    self._apply_department_order()
        if not added:
            return layer

        self.bump_snapshot_epoch(f"create_department_layer:{department}")
        self.broadcast_layer_stack_state()
        LOG.info("Created shared layer for department %s", department)
        return layer

    def _apply_department_order(self) -> bool:
        """Project department priority onto the currently materialized keys."""
        priority_keys = []
        for department in self.department_priority:
            layer_key = _layer_key_for_department(department)
            if self.layer_stack.has_layer(layer_key):
                priority_keys.append(layer_key)
        priority_set = set(priority_keys)
        unlisted_keys = [
            layer_key
            for layer_key in self.layer_stack.layer_keys
            if layer_key != _DEFAULT_LAYER_KEY and layer_key not in priority_set
        ]
        return self.layer_stack.set_order([*priority_keys, *unlisted_keys, _DEFAULT_LAYER_KEY])

    def _ordered_department_names(self) -> list[str]:
        """Return department policy entries in composed strength order."""
        departments = []
        for layer_key in self.layer_stack.layer_keys:
            department = _department_for_layer_key(layer_key)
            if department:
                departments.append(department)
        return departments

    def resolve_layer_key(self, key: str) -> str | None:
        """Resolve a client ID, department name, or layer key."""
        client_key = self._client_layer_keys.get(key)
        if client_key:
            return client_key
        department_key = _layer_key_for_department(key)
        if self.layer_stack.has_layer(department_key):
            return department_key
        if self.layer_stack.has_layer(key):
            return key
        return None

    def resolve_layer(self, key: str) -> Sdf.Layer | None:
        """Resolve a layer by client ID, department name, or layer key."""
        layer_key = self.resolve_layer_key(key)
        return self.layer_stack.layer_for(layer_key) if layer_key else None

    def department_for_layer(self, layer: Sdf.Layer) -> str | None:
        """Return department policy metadata for a managed layer."""
        layer_key = self.layer_stack.key_for_layer(layer)
        return _department_for_layer_key(layer_key) if layer_key is not None else None

    def mute_layer(self, key: str) -> bool:
        """Mute a layer by client_id or department — opinions hidden but preserved."""
        layer_key = self.resolve_layer_key(key)
        if not layer_key:
            return False
        with self._replay_mode_lock:
            with self.stage_lock:
                layer = self.layer_stack.layer_for(layer_key)
                if self.stage.IsLayerMuted(layer.identifier):
                    changed = False
                else:
                    self._reject_layer_stack_change_for_flat_receivers()
                    changed = self.layer_stack.set_muted(layer_key, True)
        if not changed:
            return True
        self.bump_snapshot_epoch(f"mute_layer:{key}")
        self.broadcast_layer_stack_state()
        return True

    def unmute_layer(self, key: str) -> bool:
        """Unmute a layer by client_id or department."""
        layer_key = self.resolve_layer_key(key)
        if not layer_key:
            return False
        with self._replay_mode_lock:
            with self.stage_lock:
                layer = self.layer_stack.layer_for(layer_key)
                if not self.stage.IsLayerMuted(layer.identifier):
                    changed = False
                else:
                    self._reject_layer_stack_change_for_flat_receivers()
                    changed = self.layer_stack.set_muted(layer_key, False)
        if not changed:
            return True
        self.bump_snapshot_epoch(f"unmute_layer:{key}")
        self.broadcast_layer_stack_state()
        return True

    def merge_layer(self, client_id: str) -> bool:
        """Merge a client's layer opinions into the root layer, then remove it.

        Copies each leaf prim spec individually via Sdf.CopySpec so
        existing root opinions on sibling prims are preserved.
        Returns False for clients on the shared edit_layer (no-op).
        """
        layer = self.client_layers.get(client_id)
        if not layer or layer is self.edit_layer:
            return False

        # Collect leaf prim paths outside the lock — this is a read-only
        # walk of the source layer, no stage mutation involved.
        leaves = []

        def _walk(spec):
            if not spec.nameChildren:
                leaves.append(spec.path)
            else:
                for child in spec.nameChildren:
                    _walk(child)

        for prim_spec in layer.rootPrims:
            _walk(prim_spec)

        # Only hold stage_lock for the actual Sdf copy operations.
        with self.stage_lock:
            root = self.stage.GetRootLayer()
            for path in leaves:
                parent = path.GetParentPath()
                if parent != Sdf.Path.absoluteRootPath and not root.GetPrimAtPath(parent):
                    Sdf.CreatePrimInLayer(root, parent)

                dst_spec = root.GetPrimAtPath(path)
                if dst_spec:
                    src_spec = layer.GetPrimAtPath(path)
                    for prop in src_spec.properties:
                        Sdf.CopySpec(layer, prop.path, root, prop.path)
                else:
                    Sdf.CopySpec(layer, path, root, path)
        self._cleanup_client_refs(client_id)
        self.bump_snapshot_epoch(f"merge_layer:{client_id}")
        LOG.info("Merged and removed layer for client %s", client_id)
        return True

    def delete_layer(self, client_id: str) -> bool:
        """Delete a client's layer and discard all opinions.

        Returns False for clients on the shared edit_layer (no-op).
        """
        layer = self.client_layers.get(client_id)
        if not layer or layer is self.edit_layer:
            return False
        self._cleanup_client_refs(client_id)
        self.bump_snapshot_epoch(f"delete_layer:{client_id}")
        LOG.info("Deleted layer for client %s", client_id)
        return True

    def _cleanup_client_refs(self, client_id: str):
        """Remove a client from all tracking dicts.

        If this was the last client in a department, also removes the
        orphaned department layer reference.
        """
        layer_key = self._client_layer_keys.pop(client_id, None)
        self.client_layers.pop(client_id, None)
        self._client_departments.pop(client_id, None)
        if not layer_key or layer_key == _DEFAULT_LAYER_KEY:
            return
        if layer_key in self._client_layer_keys.values():
            return

        with self.stage_lock:
            if self.layer_stack.has_layer(layer_key):
                self.layer_stack.remove_layer(layer_key)
        self.broadcast_layer_stack_state()

    def set_department_priority(self, ordered_departments: list[str]) -> None:
        """Set department priority ordering (strongest first)."""
        ordered_departments = list(ordered_departments)
        if any(not department for department in ordered_departments):
            raise ValueError("department names must be non-empty")
        if len(set(ordered_departments)) != len(ordered_departments):
            raise ValueError("department priority contains duplicates")
        with self._replay_mode_lock:
            if ordered_departments != self.department_priority:
                self._reject_layer_stack_change_for_flat_receivers()
            with self.stage_lock:
                policy_changed = ordered_departments != self.department_priority
                self.department_priority = ordered_departments
                order_changed = self._apply_department_order()
        if not order_changed and not policy_changed:
            return
        self.bump_snapshot_epoch("set_department_priority")
        self.broadcast_layer_stack_state()

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
        with self.stage_lock:
            muted = set(self.stage.GetMutedLayers())
            layer_keys = self.layer_stack.layer_keys
            layers = {layer_key: self.layer_stack.layer_for(layer_key) for layer_key in layer_keys}
            labels = {layer_key: self.layer_stack.label_for(layer_key) for layer_key in layer_keys}
            client_keys = dict(self._client_layer_keys)
            authored = {layer_key: not layers[layer_key].empty for layer_key in layer_keys}

        clients_by_key: dict[str, list[str]] = {}
        for client_id, layer_key in client_keys.items():
            clients_by_key.setdefault(layer_key, []).append(client_id)

        return [
            {
                "layer_key": layer_key,
                "label": labels[layer_key],
                "department": _department_for_layer_key(layer_key),
                "clients": clients_by_key.get(layer_key, []),
                "identifier": layers[layer_key].identifier,
                "muted": layers[layer_key].identifier in muted,
                "shared": layer_key == _DEFAULT_LAYER_KEY,
            }
            for layer_key in layer_keys
            if clients_by_key.get(layer_key) or authored[layer_key]
        ]

    def compact_log(self):
        """Compact the event log, keeping only the latest state per prim.

        For latest-wins events (TRS, visibility, etc.), only the final value
        is kept.  Partial TRS fields are merged.  delete_prim tombstones all
        prior events for that prim's subtree.  deactivate_prim is latest-wins
        (TRS preserved for payload reload).  Surviving events keep their
        original relative order so replay stays causally valid.

        Two-phase design minimizes emitter blocking:
          Phase 1 (no lock): snapshot the log and build the compacted dict.
          Phase 2 (exclusive): merge any delta, rewrite store, resync.
        """
        # Phase 1 — snapshot + compute (no txn_barrier, emitters keep running)
        rows = self.store.get_all_asc()
        if not rows:
            return
        max_seq = rows[-1][0]
        latest = self._build_compacted(rows)
        original_count = len(rows)

        # Phase 2 — merge delta + commit (exclusive, emitters blocked)
        self.txn_barrier.acquire_exclusive()
        try:
            # Every transaction that began before the exclusive barrier is now
            # complete. Drain its async side effects before reading the delta
            # or resetting sequence numbers, otherwise an old high-sequence
            # broadcast could arrive after the compacted replay.
            if self._persist_queue is not None:
                self._persist_queue.join()
            self._broadcast_queue.join()

            # Catch any events that arrived during phase 1
            delta = self.store.get_from_seq_asc(max_seq + 1)
            if delta:
                for seq, record_bin in delta:
                    self._merge_event(latest, seq, record_bin)
                original_count += len(delta)

            self._commit_compaction(latest, original_count)
        finally:
            self.txn_barrier.release_exclusive()

    @staticmethod
    def _merge_event(
        latest: dict[_CompactionKey, _CompactedEntry],
        seq: int,
        record_bin: bytes,
    ):
        """Merge a single event record into the compacted state.

        Keys are ``(prim, kind, time, layer_scope)``. Opinions compact only
        with other opinions authored into the same logical layer, and distinct
        time samples remain independent.
        ``set_material_binding`` keys additionally carry the binding
        purpose so allPurpose/preview/full bindings survive independently.

        Each entry carries a replay stamp — the seq of the last record
        merged into it, except creates, which keep their first-seen seq.
        Rewriting in stamp order preserves the original log's causal
        order: a prim's create replays before every event that references
        it (connections, bindings), and a delete replays before the
        events that recreate the prim.

        Decodes with numpy arrays: the per-element list path is ~100x
        slower and turns compaction of geometry-heavy logs (meshes,
        instancer arrays) into minutes of decode.
        """
        rec = message_to_dict(record_bin, numpy_arrays=True)
        if rec.get("type") == MSG_LAYER_GRAPH_STATE:
            return
        ev = rec.get("event", rec)
        prim = ev.get("prim", "")
        k = ev.get("k", "")
        if k == K_SET_SUBLAYERS:
            return
        layer_scope = "" if k in NON_COLLABORATION_KINDS else rec.get("layer_key") or ""
        key = (prim, k, ev.get("time"), layer_scope)
        if k == K_SET_MATERIAL_BINDING:
            key = (
                prim,
                f"{k}:{ev.get('material_purpose') or ''}",
                ev.get("time"),
                layer_scope,
            )
        elif k == K_SET_SDF_SPEC_FIELDS:
            spec_kind = ev.get("spec_kind") or ""
            if spec_kind in (
                SDF_SPEC_KIND_ATTRIBUTE,
                SDF_SPEC_KIND_PROPERTY,
                SDF_SPEC_KIND_RELATIONSHIP,
            ):
                spec_kind = SDF_SPEC_KIND_PROPERTY
            key = (
                prim,
                f"{k}:{spec_kind}:{ev.get('spec_path') or ''}",
                None,
                layer_scope,
            )
        meta = {}
        for meta_key in ("origin", "client", "client_id", "layer_key"):
            val = rec.get(meta_key)
            if val:
                meta[meta_key] = val

        if k in (K_DELETE_PRIM, K_RENAME_PRIM):
            # Tombstone the whole subtree: descendants of a deleted or
            # renamed prim must not replay and recreate it as a typeless
            # zombie. Events after the tombstone are kept — they represent
            # a recreation and replay after the delete via their stamps.
            child_prefix = prim + "/"
            to_remove = [
                existing
                for existing in latest
                if existing[3] == layer_scope
                and (existing[0] == prim or existing[0].startswith(child_prefix))
            ]
            for existing in to_remove:
                del latest[existing]
            latest[key] = (ev, meta, seq)
            return

        if k == K_REPLACE_SDF_LAYER_CONTENT:
            to_remove = [
                existing
                for existing in latest
                if existing[3] == layer_scope
                and (
                    existing[1] == K_REPLACE_SDF_LAYER_CONTENT
                    or existing[1].startswith(f"{K_SET_SDF_SPEC_FIELDS}:")
                )
            ]
            for existing in to_remove:
                del latest[existing]
            latest[key] = (ev, meta, seq)
            return

        # load/unload are mutually exclusive — only the last one wins.
        if k == K_LOAD_PAYLOAD:
            latest.pop((prim, K_UNLOAD_PAYLOAD, None, layer_scope), None)
            latest[key] = (ev, meta, seq)
            return
        if k == K_UNLOAD_PAYLOAD:
            latest.pop((prim, K_LOAD_PAYLOAD, None, layer_scope), None)
            latest[key] = (ev, meta, seq)
            return

        if k == K_SET_XFORM_TRS:
            existing = latest.get(key)
            if existing:
                prev = existing[0]
                for comp in ("t", "r", "s"):
                    if comp in ev.get("fields", []):
                        prev[comp] = ev[comp]
                        if comp not in prev["fields"]:
                            prev["fields"].append(comp)
                latest[key] = (prev, meta, seq)
            else:
                latest[key] = (ev, meta, seq)
        elif k == K_SET_GPRIM_ATTRS:
            existing = latest.get(key)
            if existing:
                prev = existing[0]
                prev.setdefault("attrs", {}).update(ev.get("attrs", {}))
                new_meta = ev.get("primvar_meta", {})
                if new_meta:
                    prev.setdefault("primvar_meta", {}).update(new_meta)
                new_interp = ev.get("attr_interp", {})
                if new_interp:
                    prev.setdefault("attr_interp", {}).update(new_interp)
                latest[key] = (prev, meta, seq)
            else:
                latest[key] = (ev, meta, seq)
        elif k == K_SET_CONNECTABLE_INPUT:
            existing = latest.get(key)
            if existing:
                prev = existing[0]
                prev.setdefault("inputs", {}).update(ev.get("inputs", {}))
                prev.setdefault("input_types", {}).update(
                    ev.get("input_types", {}),
                )
                if ev.get("info_id"):
                    prev["info_id"] = ev["info_id"]
                latest[key] = (prev, meta, seq)
            else:
                latest[key] = (ev, meta, seq)
        elif k == K_SET_CONNECTABLE_CONNECTION:
            existing = latest.get(key)
            if existing:
                prev = existing[0]
                connections = prev.setdefault("connections", {})
                disconnections = dict.fromkeys(prev.get("disconnections", ()))
                for local_attr, connection in ev.get("connections", {}).items():
                    connections[local_attr] = connection
                    disconnections.pop(local_attr, None)
                # Application processes connections before disconnections, so
                # retain an earlier edge as declaration/type context while the
                # disconnection remains the final authored state.
                for local_attr in ev.get("disconnections", ()):
                    disconnections[local_attr] = None
                if disconnections:
                    prev["disconnections"] = list(disconnections)
                else:
                    prev.pop("disconnections", None)
                latest[key] = (prev, meta, seq)
            else:
                merged = dict(ev)
                connections = dict(merged.get("connections", {}))
                disconnections = dict.fromkeys(merged.get("disconnections", ()))
                merged["connections"] = connections
                if disconnections:
                    merged["disconnections"] = list(disconnections)
                else:
                    merged.pop("disconnections", None)
                latest[key] = (merged, meta, seq)
        elif k == K_SET_VARIANT_SELECTIONS:
            existing = latest.get(key)
            if existing:
                prev = existing[0]
                prev.setdefault("selections", {}).update(
                    ev.get("selections", {}),
                )
                latest[key] = (prev, meta, seq)
            else:
                latest[key] = (ev, meta, seq)
        elif k == K_SET_POINT_INSTANCER:
            existing = latest.get(key)
            if existing:
                prev = existing[0]
                for f in ev.get("fields", []):
                    prev[f] = ev[f]
                    if f not in prev["fields"]:
                        prev["fields"].append(f)
                latest[key] = (prev, meta, seq)
            else:
                latest[key] = (ev, meta, seq)
        elif k == K_SET_STAGE_METADATA:
            existing = latest.get(key)
            if existing:
                prev = existing[0]
                prev.update({field: ev[field] for field in STAGE_METADATA_KEYS if field in ev})
                latest[key] = (prev, meta, seq)
            else:
                latest[key] = (ev, meta, seq)
        elif k == K_SET_SDF_SPEC_FIELDS:
            existing = latest.get(key)
            if existing:
                previous = existing[0]
                if previous.get("spec_kind") == ev.get("spec_kind"):
                    ev = merge_spec_events(previous, ev)
                latest[key] = (ev, meta, seq)
            else:
                latest[key] = (ev, meta, seq)
        elif k == K_ENSURE_PRIM:
            # Union api_schemas across subsequent ensure_prim events for the
            # same prim (latest typeName wins; api_schemas accumulates) so
            # ShapingAPI added later doesn't clobber a previously-merged
            # ShadowAPI. Multi-apply names (e.g. "CollectionAPI:render") are
            # unique strings so set-union is correct for them too.
            # Keeps the first-seen stamp: later re-ensures must not push the
            # create past events that reference the prim.
            existing = latest.get(key)
            if existing:
                prev = existing[0]
                if "typeName" in ev:
                    prev["typeName"] = ev["typeName"]
                merged = set(prev.get("api_schemas") or [])
                merged.update(ev.get("api_schemas") or [])
                if merged:
                    prev["api_schemas"] = list(merged)
                latest[key] = (prev, meta, existing[2])
            else:
                latest[key] = (ev, meta, seq)
        elif k == K_ENSURE_XFORM_OPS:
            if key not in latest:
                latest[key] = (ev, meta, seq)
        else:
            latest[key] = (ev, meta, seq)

    @staticmethod
    def _build_compacted(
        rows: list[tuple[int, bytes]],
    ) -> dict[_CompactionKey, _CompactedEntry]:
        """Build compacted event dict from raw log rows.

        Returns ``{(prim, kind, time, layer_scope): (event, metadata, stamp)}``
        where ``stamp`` is the replay-order sequence (see ``_merge_event``).
        """
        latest: dict[_CompactionKey, _CompactedEntry] = {}
        for seq, record_bin in rows:
            UsdSyncServer._merge_event(latest, seq, record_bin)
        return latest

    def _commit_compaction(
        self,
        latest: dict[_CompactionKey, _CompactedEntry],
        original_count: int,
    ):
        """Commit compacted state: rewrite store, reset seqs, resync receivers.

        Must be called under exclusive txn_barrier.

        Surviving events are rewritten in stamp order, preserving causal
        ordering within and across authored layers: creates precede the
        connections and bindings that reference them, and deletes precede
        recreates.
        """
        sorted_entries = sorted(latest.values(), key=lambda entry: entry[2])
        records = []
        first_event_seq = 1
        if self.layer_mode is LayerMode.SHARED_STAGE:
            graph = self.shared_layer_graph
            if graph is None:
                raise RuntimeError("shared-stage compaction requires a layer graph")
            reachable = set(graph.reachable_layer_keys())
            sorted_entries = [
                entry for entry in sorted_entries if entry[1].get("layer_key") in reachable
            ]
            graph_record = graph.state_message(seq=1)
            records.append((1, encode_message(graph_record), None, MSG_LAYER_GRAPH_STATE, None))
            first_event_seq = 2

        for seq, (ev, meta, _stamp) in enumerate(sorted_entries, start=first_event_seq):
            rec = {"type": MSG_EVENT, "seq": seq, "event": ev}
            rec.update(meta)
            records.append(
                (seq, encode_message(rec), meta.get("client_id"), ev.get("k"), ev.get("prim"))
            )
        self.store.clear_and_rewrite(records)
        with self._seq_lock:
            self._event_count = len(records)
            self._next_seq = len(records) + 1
            self._seq_at_last_compact = self._next_seq
            self._snapshot_epoch += 1
        self._maybe_reclaim_storage()

        self.op_cache.clear()
        self._op_cache_layer = None

        # Rebuild incremental prim tracking from compacted state.
        self._prim_paths.clear()
        self._instanceable_paths.clear()
        self._point_instancer_paths.clear()
        for ev, _meta, _stamp in sorted_entries:
            self._track_prim_event(ev)

        LOG.info("Compacted event log: %d -> %d records", original_count, len(records))

        # Reset and replay each receiver while holding its send lock. Sending
        # the control message directly avoids an async-broadcast race where
        # replay records could otherwise overtake the resync.
        with self.clients_lock:
            targets = list(self.receivers)
        disconnected = []
        for handler in targets:
            try:
                with handler.send_lock:
                    controls = [encode_message({"type": MSG_RESYNC})]
                    if getattr(handler, "_layered_replay", False):
                        controls.append(encode_message(self.get_layer_stack_state()))
                    handler.request.sendall(frame_batch(controls))
                    self.replay_from(handler, 1)
                    replay_epoch, replay_head = self.get_snapshot_token()
                    handler.request.sendall(
                        frame_batch(
                            [
                                encode_message(
                                    {
                                        "type": MSG_REPLAY_COMPLETE,
                                        "head_seq": replay_head,
                                        "epoch": replay_epoch,
                                    }
                                )
                            ]
                        )
                    )
            except (OSError, TimeoutError):
                LOG.info(
                    "Receiver disconnected during compaction replay: %s",
                    handler.client_address,
                )
                disconnected.append(handler)
        self._discard_unreachable_receivers(disconnected)

    def purge(self):
        """Clear all events, reset the edit layer, and resync receivers."""
        if self.layer_mode is LayerMode.SHARED_STAGE:
            raise RuntimeError(
                "purge is unavailable in shared-stage mode because file-backed "
                "authored opinions are not server-owned"
            )
        self.txn_barrier.acquire_exclusive()
        try:
            if self._persist_queue is not None:
                self._persist_queue.join()
            self._broadcast_queue.join()
            self._purge_inner()
        finally:
            self.txn_barrier.release_exclusive()

    def _purge_inner(self):
        # Producer high-water marks survive a scene purge. Otherwise an old
        # ambiguous retry could resurrect pre-purge edits, and still-connected
        # producers would be rejected for starting above transaction 1.
        self.store.clear_and_rewrite([])
        self._maybe_reclaim_storage()
        with self._seq_lock:
            self._event_count = 0
            self._next_seq = 1
            self._seq_at_last_compact = 1
            self._snapshot_epoch += 1
        with self.stage_lock:
            self.layer_stack.clear()
        self.op_cache.clear()
        self._op_cache_layer = None
        self._prim_paths.clear()
        self._instanceable_paths.clear()
        self._point_instancer_paths.clear()
        LOG.info("Purged event log and reset authored collaboration layers")
        replay_epoch, replay_head = self.get_snapshot_token()
        with self.clients_lock:
            targets = list(self.receivers)
        disconnected = []
        for handler in targets:
            try:
                with handler.send_lock:
                    controls = [encode_message({"type": MSG_RESYNC, "reason": "purge"})]
                    if getattr(handler, "_layered_replay", False):
                        controls.append(encode_message(self.get_layer_stack_state()))
                    controls.append(
                        encode_message(
                            {
                                "type": MSG_REPLAY_COMPLETE,
                                "head_seq": replay_head,
                                "epoch": replay_epoch,
                            }
                        )
                    )
                    handler.request.sendall(frame_batch(controls))
            except (OSError, TimeoutError):
                LOG.info(
                    "Receiver disconnected during purge resync: %s",
                    handler.client_address,
                )
                disconnected.append(handler)
        self._discard_unreachable_receivers(disconnected)

    def assign_seq(self) -> int:
        with self._seq_lock:
            s = self._next_seq
            self._next_seq += 1
            return s

    def get_snapshot_token(self) -> tuple[int, int]:
        """Return ``(epoch, latest_seq)`` for virtual-file cache keys."""
        with self._seq_lock:
            return self._snapshot_epoch, max(0, self._next_seq - 1)

    def replace_from_stage_snapshot(
        self,
        uploaded_stage: Usd.Stage,
        *,
        client_id: str = "vfs-write",
        origin: str = "vfs-write",
        reject_stale: bool = True,
        reject_ambiguous: bool = True,
    ) -> int:
        """Replace the live edit state from a complete uploaded USD snapshot.

        This is the WebDAV write-fallback path. A plain DCC save gives us a
        complete USD layer, not semantic incremental events, so the server
        translates it into a full event snapshot, resets the live edit log,
        broadcasts a resync, then broadcasts the translated events.

        Returns the number of translated events persisted to the event log.
        """
        uploaded_meta = _stage_live_metadata(uploaded_stage)
        if uploaded_meta is None:
            uploaded_scene_id = None
            uploaded_epoch = None
            uploaded_seq = None
        else:
            uploaded_scene_id = uploaded_meta.get("scene_id")
            if not isinstance(uploaded_scene_id, str) or not uploaded_scene_id:
                raise InvalidVfsWriteError(
                    "uploaded openusdconnect metadata field 'scene_id' must be a non-empty string"
                )
            uploaded_epoch = _metadata_int(uploaded_meta, "epoch")
            uploaded_seq = _metadata_int(uploaded_meta, "snapshot_seq")

        self.txn_barrier.acquire_exclusive()
        try:
            current_epoch, current_seq = self.get_snapshot_token()
            with self.stage_lock:
                department_layers = self._ordered_department_names()
                additional_layers = [
                    layer_key
                    for layer_key in self.layer_stack.layer_keys
                    if layer_key != _DEFAULT_LAYER_KEY
                    and _department_for_layer_key(layer_key) is None
                ]
                before_types = _stage_prim_types(self.stage)
            with self.proposals_lock:
                pending_proposals = sorted(
                    proposal_id
                    for proposal_id, proposal in self.proposals.items()
                    if proposal.status == "pending"
                )

            uploaded_types = _stage_prim_types(uploaded_stage)
            before_paths = set(before_types)
            uploaded_paths = set(uploaded_types)
            created_paths = sorted(uploaded_paths - before_paths)
            removed_paths = sorted(
                before_paths - uploaded_paths,
                key=lambda p: p.count("/"),
                reverse=True,
            )
            type_changed_paths = sorted(
                p for p in before_paths & uploaded_paths if before_types[p] != uploaded_types[p]
            )

            notes = []

            def _analysis(status: str, status_notes: list[str], event_counts=None):
                return VfsWriteAnalysis(
                    status=status,
                    current_epoch=current_epoch,
                    current_seq=current_seq,
                    uploaded_epoch=uploaded_epoch,
                    uploaded_seq=uploaded_seq,
                    before_prim_count=len(before_paths),
                    uploaded_prim_count=len(uploaded_paths),
                    created_prims=created_paths,
                    removed_prims=removed_paths,
                    type_changed_prims=type_changed_paths,
                    event_counts=event_counts or {},
                    notes=status_notes,
                )

            if department_layers or additional_layers or pending_proposals:
                details = []
                if department_layers:
                    details.append(f"department layers: {', '.join(department_layers)}")
                if additional_layers:
                    details.append(f"collaboration layers: {', '.join(additional_layers)}")
                if pending_proposals:
                    details.append(f"pending proposals: {', '.join(pending_proposals)}")
                analysis = _analysis(
                    "unsupported_rejected",
                    [
                        "translate write fallback is disabled while non-default "
                        f"collaboration or proposal layers are active ({'; '.join(details)})"
                    ],
                )
                self.last_vfs_write_analysis = analysis.to_dict()
                raise UnsupportedVfsWriteError(
                    "VFS translate writes are disabled while non-default "
                    "collaboration or proposal layers are active"
                )

            if uploaded_meta is None:
                analysis = _analysis(
                    "metadata_rejected",
                    ["uploaded snapshot is missing openusdconnect metadata"],
                )
                self.last_vfs_write_analysis = analysis.to_dict()
                raise InvalidVfsWriteError(
                    "uploaded VFS snapshot is missing openusdconnect metadata"
                )

            if uploaded_scene_id != self.scene_id:
                analysis = _analysis(
                    "metadata_rejected",
                    ["uploaded snapshot belongs to a different live scene"],
                )
                self.last_vfs_write_analysis = analysis.to_dict()
                raise InvalidVfsWriteError(
                    "uploaded VFS snapshot scene_id does not match this server: "
                    f"file={uploaded_scene_id!r}, server={self.scene_id!r}"
                )

            uploaded_token = (uploaded_epoch, uploaded_seq)
            current_token = (current_epoch, current_seq)
            if uploaded_token != current_token:
                if uploaded_token < current_token and not reject_stale:
                    notes.append(
                        "stale snapshot token accepted because stale-write rejection was disabled"
                    )
                else:
                    relation = "older" if uploaded_token < current_token else "newer"
                    analysis = _analysis(
                        "stale_rejected" if relation == "older" else "future_rejected",
                        [f"uploaded snapshot is {relation} than the current live server state"],
                    )
                    self.last_vfs_write_analysis = analysis.to_dict()
                    raise StaleVfsWriteError(
                        "uploaded VFS snapshot token does not match the current server: "
                        f"file epoch/seq={uploaded_epoch}/{uploaded_seq}, "
                        f"server epoch/seq={current_epoch}/{current_seq}"
                    )

            if uploaded_stage.GetEditTarget().GetLayer().subLayerPaths:
                analysis = _analysis(
                    "unsupported_rejected",
                    [
                        "uploaded snapshot contains sublayer topology, which cannot "
                        "be mapped into the managed collaboration layer stack"
                    ],
                )
                self.last_vfs_write_analysis = analysis.to_dict()
                raise UnsupportedVfsWriteError(
                    "uploaded VFS snapshot contains unsupported sublayer topology"
                )

            removed_fraction = len(removed_paths) / max(1, len(before_paths))
            removes_rootish_prim = any(
                path.count("/") <= 1 and path != "/Root" for path in removed_paths
            )
            ambiguous_destructive = bool(removed_paths) and (
                not uploaded_paths
                or removes_rootish_prim
                or (len(before_paths) >= 10 and removed_fraction >= 0.8)
            )
            if reject_ambiguous and ambiguous_destructive:
                analysis = _analysis(
                    "ambiguous_rejected",
                    [
                        "uploaded snapshot removes a root-level prim or most of the scene; "
                        "refusing automatic fallback translation"
                    ],
                )
                self.last_vfs_write_analysis = analysis.to_dict()
                raise AmbiguousVfsWriteError(
                    "uploaded VFS snapshot looks destructively incomplete; "
                    f"removed {len(removed_paths)} of {len(before_paths)} prims"
                )

            emitter = NoticeEmitter(uploaded_stage)
            try:
                events = emitter.snapshot_events()
            finally:
                emitter.cleanup()

            # Hide prims that existed in the previous composed stage but are absent
            # from the uploaded snapshot. This lets full-file saves express deletes
            # even when the original prim lives in the immutable base layer.
            for prim_path in removed_paths:
                events.append({"k": K_DEACTIVATE_PRIM, "prim": prim_path, "active": False})

            event_counts: dict[str, int] = {}
            for ev in events:
                kind = ev.get("k", "")
                if kind:
                    event_counts[kind] = event_counts.get(kind, 0) + 1

            analysis = _analysis("translated", notes, event_counts)

            # Build the complete replacement off-stage first. This validates
            # every generated event and gives us an authored layer that can be
            # installed without incrementally mutating authoritative state.
            from ..event_apply import apply_events
            from ..sdf_spec_delta import validate_spec_delta

            for event in events:
                if event.get("k") == K_SET_SDF_SPEC_FIELDS:
                    validate_spec_delta(event)

            replacement_layer = Sdf.Layer.CreateAnonymous("vfs-replacement-edits")
            replacement_session = Sdf.Layer.CreateAnonymous("vfs-replacement-session")
            replacement_session.subLayerPaths = [replacement_layer.identifier]
            with self.stage_lock:
                replacement_stage = Usd.Stage.Open(
                    self.stage.GetRootLayer(),
                    replacement_session,
                    self.stage.GetPathResolverContext(),
                )
            if replacement_stage is None:
                raise RuntimeError("failed to create the VFS replacement stage")
            replacement_stage.SetEditTarget(Usd.EditTarget(replacement_layer))
            replacement_events = [
                event for event in events if event.get("k") not in NON_COLLABORATION_KINDS
            ]
            shared_events = [event for event in events if event.get("k") in NON_COLLABORATION_KINDS]
            if replacement_events:
                apply_events(replacement_stage, replacement_events, prevalidated=True)
            if shared_events:
                replacement_stage.SetEditTarget(Usd.EditTarget(replacement_stage.GetSessionLayer()))
                apply_events(replacement_stage, shared_events, prevalidated=True)

            records: list[tuple[dict, bytes]] = []
            persist_tuples = []
            for seq, event in enumerate(events, start=1):
                record: dict = {
                    "type": MSG_EVENT,
                    "seq": seq,
                    "event": event,
                    "client": None,
                    "client_id": client_id,
                    "origin": origin,
                }
                if event.get("k") not in NON_COLLABORATION_KINDS:
                    record["layer_key"] = _DEFAULT_LAYER_KEY
                record_bin = encode_message(record)
                records.append((record, record_bin))
                persist_tuples.append(
                    (
                        seq,
                        record_bin,
                        client_id,
                        event.get("k"),
                        event.get("prim"),
                    )
                )

            # Realtime durability may still have accepted transactions in its
            # queue from before this exclusive barrier was acquired.
            if self._persist_queue is not None:
                self._persist_queue.join()
            self._broadcast_queue.join()

            # This is the durability boundary. EventStore implementations must
            # leave the previous log intact if the atomic replacement fails.
            # Authoritative in-memory state is deliberately unchanged until it
            # returns successfully.
            self.store.clear_and_rewrite(persist_tuples)

            with self.stage_lock:
                self.edit_layer.TransferContent(replacement_layer)
                self.stage.SetEditTarget(Usd.EditTarget(self.edit_layer))
                if shared_events:
                    self.stage.SetEditTarget(Usd.EditTarget(self.stage.GetSessionLayer()))
                    try:
                        apply_events(self.stage, shared_events, prevalidated=True)
                    finally:
                        self.stage.SetEditTarget(Usd.EditTarget(self.edit_layer))

            self.op_cache.clear()
            self._op_cache_layer = None
            self._prim_paths.clear()
            self._instanceable_paths.clear()
            self._point_instancer_paths.clear()
            for event in events:
                self._track_prim_event(event)
            self._prim_count_dirty = True

            with self._seq_lock:
                self._event_count = len(records)
                self._next_seq = len(records) + 1
                self._seq_at_last_compact = self._next_seq
                self._snapshot_epoch += 1

            self.last_vfs_write_analysis = analysis.to_dict()
            self._maybe_reclaim_storage()

            self.broadcast({"type": MSG_RESYNC, "reason": "vfs-write"})
            if records:
                record_dicts = [record for record, _record_bin in records]
                record_bins = [record_bin for _record, record_bin in records]
                if self.wire_metrics is not None:
                    for record, record_bin in records:
                        self.wire_metrics.record(record["event"].get("k", ""), len(record_bin))
                self.broadcast_bytes(
                    frame_batch(record_bins),
                    record_dicts,
                )
            LOG.info(
                "Translated VFS snapshot write into %d live events "
                "(created=%d removed=%d type_changed=%d)",
                len(events),
                len(created_paths),
                len(removed_paths),
                len(type_changed_paths),
            )
            return len(events)
        finally:
            self.txn_barrier.release_exclusive()

    def append_log(self, rec: dict) -> bytes:
        """Append an event record and return its encoded representation.

        Raises on persistence failure — callers should not broadcast
        events that were not successfully persisted.
        """
        ev = rec.get("event", {})
        rec_bin = encode_message(rec)
        self.store.append(rec["seq"], rec_bin, kind=ev.get("k"), prim=ev.get("prim"))
        with self._seq_lock:
            self._event_count += 1
        return rec_bin

    def append_log_batch(
        self,
        tuples: list[tuple[int, bytes, str | None, str | None, str | None]],
        *,
        producer_progress: tuple[ProducerProgress, ...] = (),
    ):
        """Persist pre-serialized event records.

        Each tuple is (seq, record_bin, client_id, kind, prim).
        In strict mode, writes synchronously (caller blocks until DB commit).
        In realtime mode, enqueues for async write (caller returns immediately).
        """
        if self._persist_queue is not None and not producer_progress:
            self._persist_queue.put(tuples)
        else:
            # A transaction result is acknowledged only after the atomic
            # event+producer-progress commit returns, including in realtime mode.
            self.store.append_batch(tuples, producer_progress=producer_progress)
            with self._seq_lock:
                self._event_count += len(tuples)

    def replay_children_after_load(self, prim_path: str):
        """After load_payload, re-broadcast the latest events for children.

        Queries the event log for the most recent structural and TRS events
        for each child of prim_path, assigns new sequence numbers, and
        broadcasts them so receivers re-apply the authoritative state.

        Also reactivates children on the server's stage that may have been
        deactivated by _detect_deletions during a previous unload cycle.
        """
        # Reactivate children on the server's stage (clear stale SetActive(False))
        with self.stage_lock:
            prim = self.stage.GetPrimAtPath(prim_path)
            if prim and prim.IsValid():
                for child in Usd.PrimRange(prim, Usd.PrimAllPrimsPredicate):
                    if not child.IsActive():
                        child.SetActive(True)

        prefix = prim_path + "/"
        replay_kinds = {
            K_ENSURE_PRIM,
            K_ENSURE_XFORM_OPS,
            K_SET_XFORM_TRS,
            K_SET_VISIBILITY,
            K_SET_MATERIAL_BINDING,
            K_SET_CONNECTABLE_INPUT,
            K_SET_CONNECTABLE_CONNECTION,
        }

        record_blobs = self.store.get_by_prim_prefix(prefix, replay_kinds)

        # Collect the latest event of each relevant kind per child prim.
        # Keep the logical layer target so replayed broadcasts preserve
        # authored-layer routing.
        latest: dict[
            tuple[str, str],
            tuple[dict, str | None, str | None],
        ] = {}
        for blob in record_blobs:
            rec = message_to_dict(blob)
            ev = rec.get("event", rec)
            ep = ev.get("prim", "")
            ek = ev.get("k", "")
            if ep.startswith(prefix) and ek in replay_kinds:
                latest[(ep, ek)] = (
                    ev,
                    rec.get("origin"),
                    rec.get("layer_key"),
                )

        if not latest:
            return

        # Tier-major: every create replays before the bindings and
        # connections that reference sibling prims; within a tier, path
        # order puts parents before children.
        sorted_events = sorted(
            latest.values(),
            key=lambda e: (event_apply_tier(e[0]["k"]), e[0]["prim"]),
        )

        for ev, origin, layer_key in sorted_events:
            rec = {"type": MSG_EVENT, "seq": self.assign_seq(), "event": ev}
            if origin:
                rec["origin"] = origin
            if layer_key:
                rec["layer_key"] = layer_key
            rec_bin = self.append_log(rec)
            if self.wire_metrics is not None:
                self.wire_metrics.record(ev.get("k", ""), len(rec_bin))
            self.broadcast_transaction_views([(rec, rec_bin)])

        LOG.info(
            "Replayed %d child events after load_payload %s",
            len(sorted_events),
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
    def receiver_replay_window(self, handler):
        """Register a receiver at a stable replay-to-live boundary.

        The exclusive barrier is held only long enough to make prior realtime
        writes durable, capture the replay watermark, and join the live
        receiver set. The handler's send lock remains held while the caller
        sends bounded replay, so newer captured broadcasts follow it.
        """
        self.txn_barrier.acquire_exclusive()
        send_lock_acquired = False
        receiver_registered = False
        try:
            if self._persist_queue is not None:
                self._persist_queue.join()
            handler.send_lock.acquire()
            send_lock_acquired = True
            with self.clients_lock:
                self.receivers.add(handler)
                receiver_registered = True
            replay_end = self.store.get_max_seq()
            replay_epoch, _latest_seq = self.get_snapshot_token()
        except Exception:
            if receiver_registered:
                with self.clients_lock:
                    self.receivers.discard(handler)
            if send_lock_acquired:
                handler.send_lock.release()
            raise
        finally:
            self.txn_barrier.release_exclusive()

        try:
            yield replay_end, replay_epoch
        finally:
            handler.send_lock.release()

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
        records: list[tuple[dict, bytes]],
    ) -> None:
        """Deliver one authored transaction to the complete commit stream."""
        self.broadcast_transaction_group_views([records])

    def broadcast_transaction_group_views(
        self,
        transactions: list[list[tuple[dict, bytes]]],
    ) -> None:
        """Deliver a committed group as one complete ordered stream.

        Every receiver consumes every durable USD record, including records
        authored by the same origin. Origin is diagnostic metadata rather than
        a delivery filter. This gives live delivery the same contract as replay
        and lets every replica apply the server's total order.
        """
        transactions = [records for records in transactions if records]
        if not transactions:
            return

        all_records = [
            record
            for records in transactions
            for record, _encoded in records
        ]
        all_payload = frame_batch(
            [
                encoded
                for records in transactions
                for _record, encoded in records
            ]
        )
        if self.layer_mode is LayerMode.SHARED_STAGE:
            self.broadcast_bytes(all_payload, all_records)
            return

        # Keep the managed-mode receiver cohorts separate. They consume the
        # same complete commit stream, while retaining independently captured
        # target sets for their different replay/application contracts.
        has_flat_receivers, has_layered_receivers = self._receiver_audience_presence()
        if has_layered_receivers:
            self.broadcast_bytes(
                all_payload,
                all_records,
                audience=_AUDIENCE_LAYERED,
                notify_listeners=False,
            )
        if has_flat_receivers:
            self.broadcast_bytes(
                all_payload,
                all_records,
                audience=_AUDIENCE_FLAT,
                notify_listeners=False,
            )
        self._notify_event_listeners(all_records)

    @staticmethod
    def _validate_audience(audience: str) -> None:
        if audience not in _AUDIENCES:
            raise ValueError(f"unknown receiver audience {audience!r}")

    def _receiver_audience_presence(self) -> tuple[bool, bool]:
        """Return whether flat and layered receivers are connected."""
        with self.clients_lock:
            has_flat = False
            has_layered = False
            for handler in self.receivers:
                if getattr(handler, "_layered_replay", False):
                    has_layered = True
                else:
                    has_flat = True
                if has_flat and has_layered:
                    break
        return has_flat, has_layered

    def _has_layered_receivers(self) -> bool:
        return self._receiver_audience_presence()[1]

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

        The actual network sends happen on the dedicated broadcast thread,
        so the calling emitter thread is never blocked by slow receivers.
        """
        if not records:
            return
        self._validate_audience(audience)
        framed_payloads = [encode_message(rec) for rec in records]
        if self.wire_metrics is not None:
            for rec, buf in zip(records, framed_payloads, strict=True):
                kind = rec.get("event", {}).get("k") or rec.get("type", "")
                self.wire_metrics.record(kind, len(buf))
        payload = frame_batch(framed_payloads)
        self._enqueue_broadcast(payload, exclude_origin, audience)
        # Notify event listeners synchronously — these are in-process
        # callbacks (e.g. dashboard) that are fast and must see events
        # in order.
        self._notify_event_listeners(records)

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
        self._validate_audience(audience)
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

        Bypasses the event listener path — playback messages are control-plane
        signals, not USD scene events, so they shouldn't appear in the
        dashboard event log.
        """
        self._validate_audience(audience)
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
            self._broadcast_queue.put((payload, targets, exclude_origin, audience))

    def _broadcast_loop(self):
        """Dedicated thread: drain the broadcast queue and send to receivers.

        During idle periods, sends periodic pings to detect dead receivers.
        Exits cleanly when a None sentinel is enqueued via shutdown().
        """
        _ping_payload = frame_batch([encode_message({"type": MSG_PING})])

        while True:
            try:
                item = self._broadcast_queue.get(timeout=_PING_INTERVAL)
            except queue.Empty:
                # Idle — send pings to detect dead receivers
                self._send_to_all(_ping_payload)
                continue

            if item is None:
                # Drain remaining items before exiting
                while True:
                    try:
                        remaining = self._broadcast_queue.get_nowait()
                    except queue.Empty:
                        break
                    if remaining is None:
                        continue
                    payload, targets, exclude_origin, audience = remaining
                    try:
                        self._send_to_all(
                            payload,
                            exclude_origin=exclude_origin,
                            audience=audience,
                            targets=targets,
                        )
                    except Exception:
                        LOG.exception("Error draining broadcast queue")
                return

            payload, targets, exclude_origin, audience = item
            try:
                self._send_to_all(
                    payload,
                    exclude_origin=exclude_origin,
                    audience=audience,
                    targets=targets,
                )
            except Exception:
                LOG.exception("Unexpected error in broadcast loop")
            finally:
                self._broadcast_queue.task_done()

    def _send_to_all(
        self,
        payload: bytes,
        exclude_origin: str | None = None,
        audience: str = _AUDIENCE_ALL,
        targets: tuple | None = None,
    ):
        """Send payload to matching receivers, removing dead ones.

        A failed send is the earliest reliable signal that a client is
        gone — releases the playback-leader role here too so other
        clients don't wait a full keepalive cycle to reclaim it. The
        dead handler's recv loop exits separately once keepalive expires.
        """
        if targets is None:
            targets = self._receiver_targets(
                exclude_origin=exclude_origin,
                audience=audience,
            )
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
        with self.clients_lock:
            targets = []
            for handler in self.receivers:
                layered = bool(getattr(handler, "_layered_replay", False))
                if audience == _AUDIENCE_LAYERED and not layered:
                    continue
                if audience == _AUDIENCE_FLAT and layered:
                    continue
                if exclude_origin and getattr(handler, "_origin", None) == exclude_origin:
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
            release_replay = getattr(
                handler,
                "release_receiver_replay_reservation",
                None,
            )
            if release_replay is not None:
                release_replay()
            client_id = getattr(handler, "_client_id", "") or ""
            if client_id and self.release_playback(client_id):
                released_any = True
        if released_any:
            self.broadcast_message(
                {"type": MSG_PLAYBACK_STATE, **self.get_playback_state()},
            )

    def _persist_loop(self):
        """Dedicated thread for realtime durability: drain persistence queue
        and write to SQLite without blocking emitter threads.
        Exits cleanly when a None sentinel is enqueued via shutdown()."""
        while True:
            tuples = self._persist_queue.get()
            if tuples is None:
                # Drain remaining items before exiting
                while True:
                    try:
                        remaining = self._persist_queue.get_nowait()
                    except queue.Empty:
                        break
                    if remaining is None:
                        continue
                    try:
                        self.store.append_batch(remaining)
                        with self._seq_lock:
                            self._event_count += len(remaining)
                    except Exception:
                        LOG.exception("Error draining persist queue")
                return
            try:
                self.store.append_batch(tuples)
                with self._seq_lock:
                    self._event_count += len(tuples)
            except Exception:
                LOG.exception("Unexpected error in persist loop")
            finally:
                self._persist_queue.task_done()

    def apply_txn(self, events: list[dict], layer: Sdf.Layer | None = None) -> None:
        """Apply a transaction to the stage.

        Layer opinions are authored into *layer* (defaults to
        ``self.edit_layer``). Shared session metadata and stage-state events
        are applied under the primary session edit target.
        """
        from ..event_apply import apply_events
        from ..sdf_spec_delta import validate_spec_delta

        for event in events:
            if event.get("k") == K_SET_SDF_SPEC_FIELDS:
                validate_spec_delta(event)

        target = layer or self.edit_layer

        with self.stage_lock:
            edit_target = Usd.EditTarget(target)
            session_target = Usd.EditTarget(self.stage.GetSessionLayer())
            has_layer_opinions = any(ev.get("k") not in NON_COLLABORATION_KINDS for ev in events)
            target_was_muted = self.stage.IsLayerMuted(target.identifier)
            was_muted = has_layer_opinions and target_was_muted
            if was_muted:
                self.stage.UnmuteLayer(target.identifier)
            try:
                for ev in events:
                    if ev.get("k") != K_SET_SDF_SPEC_FIELDS or not ev.get("removed", False):
                        continue
                    event_path = Sdf.Path(ev.get("spec_path", ""))
                    if not event_path.IsAbsolutePath():
                        continue
                    spec = target.GetObjectAtPath(event_path)
                    if spec:
                        ev["fields"] = sorted(
                            set(ev.get("fields", ())) | {str(key) for key in spec.ListInfoKeys()}
                        )

                start = 0
                while start < len(events):
                    non_collaboration = events[start].get("k") in NON_COLLABORATION_KINDS
                    end = start + 1
                    while (
                        end < len(events)
                        and (events[end].get("k") in NON_COLLABORATION_KINDS) == non_collaboration
                    ):
                        end += 1
                    run = events[start:end]
                    run_target = session_target if non_collaboration else edit_target
                    run_layer = self.stage.GetSessionLayer() if non_collaboration else target
                    self.stage.SetEditTarget(run_target)
                    apply_events(
                        self.stage,
                        run,
                        op_cache=self._op_cache_for(run_layer),
                        prevalidated=True,
                    )
                    start = end
            finally:
                if target_was_muted:
                    self.stage.SetEditTarget(session_target)
                else:
                    self.stage.SetEditTarget(edit_target)
                if was_muted:
                    self.stage.MuteLayer(target.identifier)

            for ev in events:
                k = ev.get("k")
                if k in (K_ENSURE_PRIM, K_DELETE_PRIM, K_RENAME_PRIM):
                    self._prim_count_dirty = True
                    self._track_prim_event(ev)
                elif k == K_SET_INSTANCEABLE:
                    self._track_prim_event(ev)

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
    ) -> _TransactionRequest:
        """Submit without waiting; the coordinator owns its maintenance barrier."""
        if not client_id or not session_id or len(session_id) > 128 or txn_id < 1:
            raise TransactionRejectedError(
                "invalid_identity",
                "client_id and session_id are required and txn_id must be positive",
            )

        if self._transaction_stopping:
            raise RuntimeError("transaction coordinator is shutting down")

        request = _TransactionRequest(
            events=events,
            session_id=session_id,
            txn_id=txn_id,
            client_id=client_id,
            origin=origin,
            client_addr=client_addr,
            layer=layer,
            layer_key=layer_key,
        )
        # Maintenance cannot pass this request while it is queued, applying,
        # persisting, or publishing. Ownership transfers to the coordinator,
        # which releases the shared barrier immediately before setting done.
        self.txn_barrier.acquire_shared()
        if self._transaction_queue is None:
            try:
                self._commit_one_transaction_request(request)
            finally:
                self.txn_barrier.release_shared()
                request.done.set()
            return request
        try:
            self._transaction_queue.put(request)
        except BaseException:
            self.txn_barrier.release_shared()
            raise
        return request

    @staticmethod
    def wait_for_transaction(request: _TransactionRequest) -> TransactionCommit:
        """Wait for a previously submitted transaction's terminal outcome."""
        request.done.wait()
        if request.error is not None:
            raise request.error
        if request.commit is None:
            raise RuntimeError("transaction coordinator returned no result")
        return request.commit

    def _process_idempotent_txn_now(
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
        """Execute one transaction synchronously on the commit worker."""

        with self._transaction_commit_lock:
            committed_through = self._producer_progress_locked(client_id, session_id)
            if txn_id <= committed_through:
                return TransactionCommit("duplicate", committed_through)

            expected = committed_through + 1
            if txn_id != expected:
                raise TransactionRejectedError(
                    "unexpected_id",
                    f"expected transaction {expected}, received {txn_id}",
                    expected_txn_id=expected,
                )

            first_reserved_seq = self._next_seq
            try:
                records = self.process_txn(
                    events,
                    client_id=client_id,
                    origin=origin,
                    client_addr=client_addr,
                    layer=layer,
                    layer_key=layer_key,
                    transaction_identity=(session_id, txn_id),
                )
            except Exception:
                with self._seq_lock:
                    self._next_seq = first_reserved_seq
                raise
            self._producer_progress_cache[(client_id, session_id)] = txn_id
            commit = TransactionCommit(
                "committed",
                txn_id,
                tuple(records),
            )
            return self._publish_transaction_commit(
                commit,
                events=events,
                session_id=session_id,
                txn_id=txn_id,
                origin=origin,
            )

    def _publish_transaction_commit(
        self,
        commit: TransactionCommit,
        *,
        events: list[dict],
        session_id: str,
        txn_id: int,
        origin: str | None,
    ) -> TransactionCommit:
        """Enqueue one durable commit before releasing global commit order."""
        try:
            self.broadcast_transaction_views(list(commit.records))
            for event in events:
                if event.get("k") == K_LOAD_PAYLOAD:
                    self.replay_children_after_load(event["prim"])
        except Exception:
            LOG.exception(
                "Transaction %s/%d committed but its live broadcast failed",
                session_id,
                txn_id,
            )
        return TransactionCommit(
            commit.status,
            commit.txn_id,
            commit.records,
        )

    def _transaction_batch_loop(self) -> None:
        """Collect a bounded set of producer transactions for one DB commit."""
        transaction_queue = self._transaction_queue
        if transaction_queue is None:
            return
        stop = False
        while not stop:
            first = transaction_queue.get()
            if first is None:
                break
            requests = [first]
            deadline = time.monotonic() + self.txn_batch_delay
            while len(requests) < self.txn_batch_size:
                try:
                    request = transaction_queue.get_nowait()
                except queue.Empty:
                    if time.monotonic() >= deadline:
                        break
                    # Sub-millisecond Queue.get timeouts round up to the OS
                    # scheduler quantum on Windows. A cooperative zero sleep
                    # gives connection threads a chance to enqueue without a
                    # 10-16 ms interactive-latency penalty.
                    time.sleep(0)
                    continue
                if request is None:
                    stop = True
                    break
                requests.append(request)
            self._execute_transaction_requests(requests)

    def _execute_transaction_requests(
        self,
        requests: list[_TransactionRequest],
    ) -> None:
        try:
            if self.layer_mode is LayerMode.SHARED_STAGE:
                for request in requests:
                    self._commit_one_transaction_request(request)
            else:
                try:
                    self._commit_managed_transaction_group(requests)
                except Exception:
                    # The group failure boundary restored every USD layer and
                    # sequence reservation. Re-run individually so one invalid
                    # payload or storage failure does not reject its neighbors.
                    LOG.debug(
                        "Grouped transaction commit failed; retrying individually",
                        exc_info=True,
                    )
                    for request in requests:
                        self._commit_one_transaction_request(request)
        finally:
            for request in requests:
                self.txn_barrier.release_shared()
                request.done.set()

    def _commit_one_transaction_request(self, request: _TransactionRequest) -> None:
        request.commit = None
        request.error = None
        try:
            request.commit = self._process_idempotent_txn_now(
                request.events,
                session_id=request.session_id,
                txn_id=request.txn_id,
                client_id=request.client_id,
                origin=request.origin,
                client_addr=request.client_addr,
                layer=request.layer,
                layer_key=request.layer_key,
            )
        except Exception as exc:
            request.error = exc

    def _commit_managed_transaction_group(
        self,
        requests: list[_TransactionRequest],
    ) -> None:
        """Commit queued managed transactions through one failure boundary."""
        with self._transaction_commit_lock:
            accepted: list[_TransactionRequest] = []
            next_by_session: dict[tuple[str, str], int] = {}

            for request in requests:
                producer = (request.client_id, request.session_id)
                committed_through = next_by_session.get(producer)
                if committed_through is None:
                    committed_through = self._producer_progress_locked(*producer)
                if request.txn_id <= committed_through:
                    request.commit = TransactionCommit("duplicate", committed_through)
                    continue

                expected = committed_through + 1
                if request.txn_id != expected:
                    request.error = TransactionRejectedError(
                        "unexpected_id",
                        f"expected transaction {expected}, received {request.txn_id}",
                        expected_txn_id=expected,
                    )
                    continue
                next_by_session[producer] = request.txn_id
                accepted.append(request)

            if not accepted:
                return

            first_reserved_seq = self._next_seq
            try:
                prepared = [self._prepare_managed_transaction(request) for request in accepted]
                paths_by_layer: dict[str, tuple[Sdf.Layer, set[str]]] = {}
                snapshot_session = False
                for transaction in prepared:
                    if transaction.collaboration_paths:
                        entry = paths_by_layer.setdefault(
                            transaction.target_layer.identifier,
                            (transaction.target_layer, set()),
                        )
                        entry[1].update(transaction.collaboration_paths)
                    snapshot_session = snapshot_session or transaction.has_session_events

                from ..event_apply import atomic_apply_layer

                with self.stage_lock:
                    original_target = self.stage.GetEditTarget()
                    try:
                        with ExitStack() as rollback:
                            for target_layer, paths in paths_by_layer.values():
                                rollback.enter_context(atomic_apply_layer(target_layer, paths))
                            if snapshot_session:
                                rollback.enter_context(
                                    atomic_apply_layer(self.stage.GetSessionLayer())
                                )
                            for transaction in prepared:
                                self.apply_txn(
                                    transaction.request.events,
                                    layer=transaction.target_layer,
                                )
                            progress_by_producer = {
                                (transaction.progress.client_id, transaction.progress.session_id):
                                    transaction.progress
                                for transaction in prepared
                            }
                            self.store.append_batch(
                                [
                                    record
                                    for transaction in prepared
                                    for record in transaction.persist_tuples
                                ],
                                producer_progress=tuple(progress_by_producer.values()),
                            )
                            for producer, progress in progress_by_producer.items():
                                self._producer_progress_cache[producer] = (
                                    progress.committed_through
                                )
                            with self._seq_lock:
                                self._event_count += sum(
                                    len(transaction.persist_tuples)
                                    for transaction in prepared
                                )
                    finally:
                        self.stage.SetEditTarget(original_target)
            except Exception:
                with self._seq_lock:
                    self._next_seq = first_reserved_seq
                self.op_cache.clear()
                self._op_cache_layer = None
                raise

            for transaction in prepared:
                records = tuple(transaction.records)
                commit = TransactionCommit(
                    "committed",
                    transaction.request.txn_id,
                    records,
                )
                transaction.request.commit = commit
            # Persistence and live enqueue are one ordering boundary. Keep
            # this inside _transaction_commit_lock so another transaction can
            # neither reserve a later sequence nor publish ahead of the group.
            self._broadcast_grouped_transactions(requests)

    def _producer_progress_locked(self, client_id: str, session_id: str) -> int:
        """Return durable producer progress while the commit lock is held."""
        producer = (client_id, session_id)
        cached = self._producer_progress_cache.get(producer)
        if cached is None:
            cached = self.store.get_producer_progress(client_id, session_id)
            self._producer_progress_cache[producer] = cached
        return cached

    def producer_committed_through(self, client_id: str, session_id: str) -> int:
        """Return the cumulative durable acknowledgement for a handshake."""
        with self._transaction_commit_lock:
            return self._producer_progress_locked(client_id, session_id)

    def _prepare_managed_transaction(
        self,
        request: _TransactionRequest,
    ) -> _PreparedTransaction:
        if request.layer_key:
            raise ValueError("managed transactions cannot select an arbitrary layer key")
        shared_only = {
            event.get("k")
            for event in request.events
            if event.get("k") in SHARED_STAGE_ONLY_KINDS
        }
        if shared_only:
            raise ValueError(
                f"shared-stage events are unavailable in managed mode: {sorted(shared_only)!r}"
            )
        target_layer = request.layer or self.edit_layer
        layer_key = self.layer_stack.key_for_layer(target_layer)
        if layer_key is None and any(
            event.get("k") not in NON_COLLABORATION_KINDS for event in request.events
        ):
            raise ValueError("transaction target is not a managed collaboration layer")

        collaboration_paths = {
            event.get("prim")
            for event in request.events
            if event.get("k") not in NON_COLLABORATION_KINDS and event.get("prim")
        }
        has_session_events = any(
            event.get("k") in NON_COLLABORATION_KINDS for event in request.events
        )
        records, persist_tuples = self._encode_managed_txn_records(
            request.events,
            client_id=request.client_id,
            origin=request.origin,
            client_addr=request.client_addr,
            layer_key=layer_key or "",
        )
        progress = ProducerProgress(
            request.client_id,
            request.session_id,
            request.txn_id,
        )
        return _PreparedTransaction(
            request,
            target_layer,
            collaboration_paths,
            has_session_events,
            records,
            persist_tuples,
            progress,
        )

    def _broadcast_grouped_transactions(
        self,
        requests: list[_TransactionRequest],
    ) -> None:
        pending: list[_TransactionRequest] = []

        def flush_pending() -> None:
            if not pending:
                return
            try:
                self.broadcast_transaction_group_views(
                    [
                        list(request.commit.records)
                        for request in pending
                    ]
                )
            except Exception:
                LOG.exception(
                    "Committed transaction group could not be broadcast live"
                )
            for request in pending:
                commit = request.commit
                request.commit = TransactionCommit(
                    commit.status,
                    commit.txn_id,
                    commit.records,
                )
            pending.clear()

        for request in requests:
            commit = request.commit
            if commit is None or commit.status != "committed" or not commit.records:
                continue
            load_events = [
                event for event in request.events if event.get("k") == K_LOAD_PAYLOAD
            ]
            if not load_events:
                pending.append(request)
                continue

            # Payload child replay must stay immediately after the transaction
            # that loaded it, so it forms a boundary between broadcast groups.
            flush_pending()
            try:
                self.broadcast_transaction_views(list(commit.records))
                for event in load_events:
                    self.replay_children_after_load(event["prim"])
            except Exception:
                LOG.exception(
                    "Transaction %s/%d committed but its live broadcast failed",
                    request.session_id,
                    request.txn_id,
                )
            request.commit = TransactionCommit(
                commit.status,
                commit.txn_id,
                commit.records,
            )
        flush_pending()

    def process_txn(
        self,
        events: list[dict],
        *,
        client_id: str | None = None,
        origin: str | None = None,
        client_addr: str | None = None,
        layer: Sdf.Layer | None = None,
        layer_key: str = "",
        transaction_identity: tuple[str, int] | None = None,
    ) -> list[tuple[dict, bytes]]:
        """Apply, seq-assign, encode, and persist a transaction.

        Runs apply_txn, assigns monotonic sequence numbers, encodes each
        event as a FlatBuffers record, and batches the persist to the
        event store. Callers that need txn_barrier coordination (e.g.
        ConnectionHandler around broadcast) must acquire/release the
        shared lock themselves — this method does not, so it stays
        composable with broader critical sections.

        Returns encoded broadcast records in input order. Callers that only
        need authoritative state plus a populated log may ignore the result.

        Collaboration records derive their portable layer key from the actual
        edit target. Client policy selects that target before this method is
        called; cached client metadata is not authoritative for persistence.
        """
        if self.layer_mode is LayerMode.SHARED_STAGE:
            if layer is not None:
                raise ValueError("managed layer routing is unavailable in shared-stage mode")
            with self._shared_stage_commit_lock:
                return self._process_shared_txn(
                    events,
                    layer_key=layer_key,
                    client_id=client_id,
                    origin=origin,
                    client_addr=client_addr,
                    transaction_identity=transaction_identity,
                )
        if layer_key:
            raise ValueError("managed transactions cannot select an arbitrary layer key")
        shared_only = {
            event.get("k")
            for event in events
            if event.get("k") in SHARED_STAGE_ONLY_KINDS
        }
        if shared_only:
            raise ValueError(
                f"shared-stage events are unavailable in managed mode: {sorted(shared_only)!r}"
            )

        target_layer = layer or self.edit_layer
        layer_key = self.layer_stack.key_for_layer(target_layer)
        if layer_key is None and any(ev.get("k") not in NON_COLLABORATION_KINDS for ev in events):
            raise ValueError("transaction target is not a managed collaboration layer")

        if transaction_identity is None:
            records, persist_tuples = self._encode_managed_txn_records(
                events,
                client_id=client_id,
                origin=origin,
                client_addr=client_addr,
                layer_key=layer_key,
            )
            self.apply_txn(events, layer=target_layer)
            self.append_log_batch(persist_tuples)
            return records

        # Keep the USD mutation inside the durable failure boundary. Scoped
        # snapshots make rollback proportional to touched prims rather than to
        # the full collaboration layer; session metadata uses a full snapshot.
        from ..event_apply import atomic_apply_layer

        collaboration_paths = {
            event.get("prim")
            for event in events
            if event.get("k") not in NON_COLLABORATION_KINDS and event.get("prim")
        }
        has_session_events = any(
            event.get("k") in NON_COLLABORATION_KINDS for event in events
        )
        records, persist_tuples = self._encode_managed_txn_records(
            events,
            client_id=client_id,
            origin=origin,
            client_addr=client_addr,
            layer_key=layer_key,
        )
        if not client_id:
            raise ValueError("idempotent transaction persistence requires client_id")
        session_id, txn_id = transaction_identity
        progress = ProducerProgress(
            client_id,
            session_id,
            txn_id,
        )
        with self.stage_lock:
            original_target = self.stage.GetEditTarget()
            try:
                with ExitStack() as rollback:
                    if collaboration_paths:
                        rollback.enter_context(
                            atomic_apply_layer(target_layer, collaboration_paths)
                        )
                    if has_session_events:
                        rollback.enter_context(
                            atomic_apply_layer(self.stage.GetSessionLayer())
                        )
                    self.apply_txn(events, layer=target_layer)
                    self.append_log_batch(
                        persist_tuples,
                        producer_progress=(progress,),
                    )
            finally:
                self.stage.SetEditTarget(original_target)
        return records

    def _encode_managed_txn_records(
        self,
        events: list[dict],
        *,
        client_id: str | None,
        origin: str | None,
        client_addr: str | None,
        layer_key: str,
    ) -> tuple[
        list[tuple[dict, bytes]],
        list[tuple[int, bytes, str | None, str | None, str | None]],
    ]:
        """Assign sequences and encode one managed transaction."""
        records = []
        persist_tuples = []
        for event in events:
            record: dict = {
                "type": MSG_EVENT,
                "seq": self.assign_seq(),
                "event": event,
                "client": client_addr,
                "client_id": client_id,
            }
            if origin:
                record["origin"] = origin
            if event.get("k") not in NON_COLLABORATION_KINDS:
                record["layer_key"] = layer_key
            record_bin = encode_message(record)
            if self.wire_metrics is not None:
                self.wire_metrics.record(event.get("k", ""), len(record_bin))
            records.append((record, record_bin))
            persist_tuples.append(
                (
                    record["seq"],
                    record_bin,
                    client_id,
                    event.get("k"),
                    event.get("prim"),
                )
            )
        return records, persist_tuples

    def _process_shared_txn(
        self,
        events: list[dict],
        *,
        layer_key: str,
        client_id: str | None,
        origin: str | None,
        client_addr: str | None,
        transaction_identity: tuple[str, int] | None = None,
    ) -> list[tuple[dict, bytes]]:
        """Apply one exact authored-layer transaction."""
        from ..event_apply import apply_events, atomic_apply
        from ..protocol_validation import validate_event
        from ..sdf_spec_delta import (
            validate_layer_content_replacement,
            validate_spec_delta,
        )

        graph = self.shared_layer_graph
        if graph is None:
            raise RuntimeError("shared-stage transaction requires a layer graph")
        if not layer_key:
            raise ValueError("shared-stage transactions require layer_key")
        target = graph.layer_for(layer_key)
        if target is None or layer_key not in graph.reachable_layer_keys():
            raise TransactionRejectedError(
                "stale_layer_graph",
                f"unknown or unresolved shared layer key {layer_key!r}",
            )
        unsupported = {
            event.get("k")
            for event in events
            if event.get("k") not in SHARED_STAGE_EVENT_KINDS
        }
        if unsupported:
            raise ValueError(f"unsupported shared-stage events: {sorted(unsupported)!r}")
        if sum(event.get("k") == K_SET_SUBLAYERS for event in events) > 1:
            raise ValueError("one shared-stage transaction may replace a parent topology once")
        if sum(event.get("k") == K_REPLACE_SDF_LAYER_CONTENT for event in events) > 1:
            raise ValueError("one shared-stage transaction may replace layer content once")

        prepared: PreparedSublayers | None = None
        canonical_events = []
        try:
            for event in events:
                if event.get("k") == K_SET_SUBLAYERS:
                    prepared = graph.canonicalize_sublayers(layer_key, event)
                    canonical = prepared.event
                elif event.get("k") == K_REPLACE_SDF_LAYER_CONTENT:
                    canonical = dict(event)
                    validate_layer_content_replacement(canonical)
                else:
                    canonical = dict(event)
                    validate_spec_delta(canonical)
                if not validate_event(canonical):
                    raise ValueError(f"invalid shared-stage event {canonical.get('k')!r}")
                canonical_events.append(canonical)
        except StaleLayerGraphError as exc:
            raise TransactionRejectedError("stale_layer_graph", str(exc)) from exc

        routed_events = [(layer_key, event) for event in canonical_events]
        try:
            with (
                self.stage_lock,
                Usd.EditContext(self.stage, Usd.EditTarget(target)),
                graph.transaction(),
            ):
                for event in canonical_events:
                    if event.get("k") != K_SET_SDF_SPEC_FIELDS or not event.get(
                        "removed", False
                    ):
                        continue
                    path = Sdf.Path(event["spec_path"])
                    spec = target.GetObjectAtPath(path)
                    if spec:
                        event["fields"] = sorted(
                            set(event.get("fields", ()))
                            | {str(key) for key in spec.ListInfoKeys()}
                        )
                with atomic_apply(self.stage):
                    apply_events(self.stage, canonical_events, prevalidated=True)
                    if prepared is not None:
                        graph.accept_sublayers(prepared)
                        routed_events.extend(graph.discover_sublayer_states(prepared.mappings))
                    records = self._persist_shared_events(
                        routed_events,
                        client_id=client_id,
                        origin=origin,
                        client_addr=client_addr,
                        transaction_identity=transaction_identity,
                    )
        except StaleLayerGraphError as exc:
            raise TransactionRejectedError("stale_layer_graph", str(exc)) from exc
        self._prim_count_dirty = True
        return records

    def _persist_shared_events(
        self,
        routed_events: list[tuple[str, dict]],
        *,
        client_id: str | None,
        origin: str | None,
        client_addr: str | None,
        transaction_identity: tuple[str, int] | None = None,
    ) -> list[tuple[dict, bytes]]:
        records = []
        persist_tuples = []
        for event_layer_key, event in routed_events:
            record = {
                "type": MSG_EVENT,
                "seq": self.assign_seq(),
                "event": event,
                "client": client_addr,
                "client_id": client_id,
                "layer_key": event_layer_key,
            }
            if origin:
                record["origin"] = origin
            record_bin = encode_message(record)
            if self.wire_metrics is not None:
                self.wire_metrics.record(event.get("k", ""), len(record_bin))
            records.append((record, record_bin))
            persist_tuples.append(
                (
                    record["seq"],
                    record_bin,
                    client_id,
                    event.get("k"),
                    event.get("prim"),
                )
            )
        producer_progress: tuple[ProducerProgress, ...] = ()
        if transaction_identity is not None:
            if not client_id:
                raise ValueError("idempotent transaction persistence requires client_id")
            session_id, txn_id = transaction_identity
            producer_progress = (ProducerProgress(
                client_id,
                session_id,
                txn_id,
            ),)
        self.append_log_batch(
            persist_tuples,
            producer_progress=producer_progress,
        )
        return records

    def refresh_shared_layer_dependencies(self) -> tuple[str, ...]:
        """Resolve newly available sublayers and publish their routing state."""
        graph = self.shared_layer_graph
        if self.layer_mode is not LayerMode.SHARED_STAGE or graph is None:
            raise RuntimeError("shared layer dependency refresh requires shared-stage mode")

        self.txn_barrier.acquire_shared()
        try:
            with self._shared_stage_commit_lock:
                before = set(graph.reachable_layer_keys())
                with self.stage_lock, graph.transaction():
                    Ar.GetResolver().RefreshContext(self.stage.GetPathResolverContext())
                    routed_events = list(graph.refresh_resolved_sublayers())
                if routed_events:
                    records = self._persist_shared_events(
                        routed_events,
                        client_id=None,
                        origin=None,
                        client_addr=None,
                    )
                    self.broadcast_transaction_views(records)
                return tuple(
                    layer_key
                    for layer_key in graph.reachable_layer_keys()
                    if layer_key not in before
                )
        finally:
            self.txn_barrier.release_shared()

    def get_prim_count(self) -> int:
        """Return the number of prims on the composed stage (thread-safe, cached)."""
        if self._prim_count_dirty:
            with self.stage_lock:
                self._prim_count = sum(1 for _ in self.stage.Traverse())
            self._prim_count_dirty = False
        return self._prim_count

    def get_tracked_prim_count(self) -> int:
        """Return the number of prims tracked (incremental, no log scan)."""
        return len(self._prim_paths)

    def get_event_count(self) -> int:
        """Return the number of events in the log (thread-safe, cached)."""
        return self._event_count

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
        """Build the prim tree from the incremental _prim_paths dict.

        No event log scan needed — uses the in-memory prim tracking.
        Instancing flags come from set lookups, and ``has_children``
        falls out of a single-pass parent count, so the tree refresh
        stays linear and free of per-prim pxr calls.
        """
        prims = dict(self._prim_paths)  # snapshot
        instanceable = set(self._instanceable_paths)
        point_instancer = set(self._point_instancer_paths)

        # Parent path -> direct-child count. Builds in O(N) and turns the
        # has_children lookup into O(1) per row.
        child_counts: dict[str, int] = {}
        for path in prims:
            parent = path.rsplit("/", 1)[0] or "/"
            child_counts[parent] = child_counts.get(parent, 0) + 1

        result = []
        for path in sorted(prims):
            parent = path.rsplit("/", 1)[0] or "/"
            result.append(
                {
                    "path": path,
                    "typeName": prims[path],
                    "parent": parent,
                    "depth": path.count("/"),
                    "has_children": child_counts.get(path, 0) > 0,
                    "instanceable": path in instanceable,
                    "is_point_instancer": path in point_instancer,
                }
            )
        return result

    def get_instance_count(self) -> int:
        """Count of prims with authored ``instanceable=true``.

        Whether each actually composes into an instance depends on a
        composition arc; this is the upper bound and matches what the
        tree's badge shows.
        """
        return len(self._instanceable_paths)

    def get_prototype_count(self) -> int:
        """Number of implicit prototype prims the stage composed."""
        with self.stage_lock:
            return len(self.stage.GetPrototypes())

    def get_prim_detail(self, path: str) -> dict:
        """Composed snapshot of one prim for the dashboard inspector.

        Reads are bounded (array buffers are never materialized, no flatten)
        so stage_lock is held only briefly — safe on the realtime path.
        """
        with self.stage_lock:
            prim = self.stage.GetPrimAtPath(path)
            if not prim or not prim.IsValid():
                return {"path": path, "exists": False}
            img = UsdGeom.Imageable(prim)
            vis = img.GetVisibilityAttr().Get() if img else None
            prototype = prim.GetPrototype() if prim.IsInstance() else None
            detail = {
                "path": path,
                "exists": True,
                "typeName": str(prim.GetTypeName()),
                "active": prim.IsActive(),
                "visibility": str(vis) if vis is not None else None,
                "apiSchemas": [str(s) for s in prim.GetAppliedSchemas()],
                "xform": _prim_xform_trs(prim),
                "references": [a or p for a, p in read_references(self.stage, path)],
                "payloads": [a or p for a, p in read_payloads(self.stage, path)],
                "variantSelections": dict(read_variant_selections(self.stage, path)),
                "materialBinding": read_material_binding(self.stage, path) or None,
                "attributes": _authored_attr_rows(prim),
                "isInstanceable": prim.IsInstanceable(),
                "isInstance": prim.IsInstance(),
                "isInstanceProxy": prim.IsInstanceProxy(),
                "prototype": (prototype.GetPath().pathString if prototype else None),
            }
            if prim.IsA(UsdGeom.PointInstancer):
                detail["pointInstancer"] = _point_instancer_summary(prim)
            return detail

    def get_transforms_snapshot(self) -> list[dict]:
        """Composed translate/orient/scale for every Xformable prim with ops.

        Bounded scalar reads only, so stage_lock is held briefly. Returns raw
        TRS rows (path plus t/r/s); the caller formats for display.
        """
        rows = []
        with self.stage_lock:
            for prim in self.stage.Traverse():
                trs = _prim_xform_trs(prim)
                if trs:
                    rows.append({"path": str(prim.GetPath()), **trs})
        return rows

    def export_edit_layer(self, file_path: str | None = None) -> str:
        """Export the server's edit layer as a USDA string (thread-safe).

        If *file_path* is given, also writes the layer to disk.  The exported
        layer contains only the opinions authored by the server — the base
        layer and its sublayers are not included.
        """
        # Snapshot layer ref under lock, serialize outside — avoids holding
        # stage_lock during the (potentially slow) ExportToString call.
        with self.stage_lock:
            layer = self.edit_layer
        usda = layer.ExportToString()
        if file_path:
            layer.Export(file_path)
            LOG.info("Exported edit layer to %s", file_path)
        return usda

    def export_layer(self, key: str) -> str:
        """Export one client/department layer as USDA (thread-safe).

        Resolves the layer ref under stage_lock, serializes outside it, the
        same discipline as export_edit_layer.
        """
        with self.stage_lock:
            layer = self.resolve_layer(key)
        return layer.ExportToString() if layer else "# layer not found"

    def export_flattened(self, file_path: str) -> None:
        """Export the fully composed stage as a single flattened USD file.

        All layers, composition arcs, and opinions are resolved into final
        values.  The result is a standalone file with no external dependencies
        — useful for archiving, delivery, or rendering.
        """
        # Flatten under lock (fast — creates a composed snapshot), then
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
        _REPLAY_CHUNK = 65536
        blobs = self.store.get_from_seq_bin(seq_start, seq_end)
        buf_parts: list[bytes] = []
        buf_size = 0
        for blob in blobs:
            framed = frame_batch([blob])
            buf_parts.append(framed)
            buf_size += len(framed)
            if buf_size >= _REPLAY_CHUNK:
                handler.request.sendall(b"".join(buf_parts))
                buf_parts.clear()
                buf_size = 0
        if buf_parts:
            handler.request.sendall(b"".join(buf_parts))
