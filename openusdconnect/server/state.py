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

from pxr import Ar, Sdf, Usd, UsdGeom, UsdUtils

from ..codec import encode_message, message_to_dict
from ..emitter import (
    NoticeEmitter,
    read_payloads,
    read_references,
    read_stage_metadata,
)
from ..event_store import EventStore, SqliteEventStore
from ..framing import frame_batch
from ..protocol_constants import (
    COLLABORATION_LAYER_KINDS,
    EVENT_KIND_INFO,
    FLAT_RECEIVER_PROJECTION_KINDS,
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_SET_CONNECTABLE_CONNECTION,
    K_SET_CONNECTABLE_INPUT,
    K_SET_GPRIM_ATTRS,
    K_SET_INSTANCEABLE,
    K_SET_MATERIAL_BINDING,
    K_SET_POINT_INSTANCER,
    K_SET_SDF_SPEC_FIELDS,
    K_SET_STAGE_METADATA,
    K_SET_VARIANT_SELECTIONS,
    K_SET_VISIBILITY,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
    MSG_EVENT,
    MSG_LAYER_STACK_STATE,
    MSG_PING,
    MSG_PLAYBACK_STATE,
    MSG_RESYNC,
    SHARED_STAGE_KINDS,
    STAGE_METADATA_KEYS,
    event_apply_tier,
)
from ..sdf_spec_delta import (
    SDF_SPEC_KIND_ATTRIBUTE,
    SDF_SPEC_KIND_RELATIONSHIP,
    composed_layer_spec_event,
    composed_property_spec_event,
    composed_property_spec_requires_flattening,
    merge_spec_events,
)
from ..usd_state import read_material_binding, read_variant_selections
from ..xform_decompose import as_matrix, decompose_trs_from_matrix
from ._txn_barrier import _TxnBarrier
from .layer_stack import CollaborationLayerStack
from .types import (
    AmbiguousVfsWriteError,
    ClientInfo,
    InvalidVfsWriteError,
    Proposal,
    StaleVfsWriteError,
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


def _needs_flattened_spec_projection(event: dict) -> bool:
    if event.get("k") != K_SET_SDF_SPEC_FIELDS:
        return False
    return (
        event.get("spec_kind")
        not in (
            SDF_SPEC_KIND_ATTRIBUTE,
            SDF_SPEC_KIND_RELATIONSHIP,
        )
        or Sdf.Path(event.get("spec_path", "")).ContainsPrimVariantSelection()
    )


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
    """Thread-safe per-event-kind counters of encoded record bytes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._bytes: dict[str, int] = {}

    def record(self, kind: str, nbytes: int) -> None:
        with self._lock:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            self._bytes[kind] = self._bytes.get(kind, 0) + nbytes

    def snapshot(self) -> dict:
        with self._lock:
            kinds = {
                k: {"count": self._counts[k], "bytes": self._bytes[k]} for k in sorted(self._counts)
            }
        return {
            "kinds": kinds,
            "total_count": sum(v["count"] for v in kinds.values()),
            "total_bytes": sum(v["bytes"] for v in kinds.values()),
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
        wire_metrics: bool = False,
        compact_interval: float = 0,
        reclaim_interval: float = 0,
        stage: Usd.Stage | None = None,
        resolver_context: Ar.ResolverContext | None = None,
    ):
        if stage is not None and base_usd_path:
            raise ValueError("stage and base_usd_path are mutually exclusive")
        if stage is not None and resolver_context is not None:
            raise ValueError("a supplied stage already owns its resolver context")

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

        self.clients_lock = threading.Lock()
        self.receivers: set = set()
        self.clients: dict[str, ClientInfo] = {}
        self._event_listeners: list = []
        self._start_time = time.time()
        self._seq_lock = threading.Lock()
        self.txn_barrier = _TxnBarrier()

        # Pluggable event store — defaults to SQLite
        self.store: EventStore = event_store or SqliteEventStore(log_path)
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
        # Item: (payload_bytes, exclude_origin, target_origin, audience)
        #   exclude_origin set -> broadcast to all except that origin
        #   target_origin set  -> send only to that origin (corrections)
        #   audience           -> all, flat, or layered receivers
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

        # Rebuild stage from the event log so the composed stage matches
        # what receivers would get on replay.
        self._replay_log_into_stage()

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

        routed: list[tuple[Sdf.Layer, dict]] = []
        for _seq, record_bin in rows:
            rec = message_to_dict(record_bin, numpy_arrays=True)
            ev = rec.get("event", rec)
            client_id = rec.get("client_id")
            if ev.get("k") in SHARED_STAGE_KINDS:
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
        department layer. This handles stage mutation, broadcast, gating,
        corrections, and log persistence — same path as normal txns.
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
            # Apply into the target department layer.
            changed_set = set(self.apply_txn(p.events, layer=target_layer))

            # Persist every authored opinion, but expose only the composed
            # result to flat live receivers, matching normal transaction
            # gating.
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
                if ev.get("k") not in SHARED_STAGE_KINDS:
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
            self.broadcast_transaction_views(
                records,
                changed_set,
                p.events,
            )

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
            self._client_departments[client_id] = department
            layer = self._get_or_create_department_layer(department)
        else:
            layer = self.edit_layer
        self._client_layer_keys[client_id] = layer_key
        self.client_layers[client_id] = layer
        return layer

    def _get_or_create_department_layer(self, department: str) -> Sdf.Layer:
        """Resolve department policy to one shared collaboration layer."""
        layer_key = _layer_key_for_department(department)
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
        with self.stage_lock:
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
        with self.stage_lock:
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
        ev = rec.get("event", rec)
        prim = ev.get("prim", "")
        k = ev.get("k", "")
        layer_scope = "" if k in SHARED_STAGE_KINDS else rec.get("layer_key") or ""
        key = (prim, k, ev.get("time"), layer_scope)
        if k == K_SET_MATERIAL_BINDING:
            key = (
                prim,
                f"{k}:{ev.get('material_purpose') or ''}",
                ev.get("time"),
                layer_scope,
            )
        elif k == K_SET_SDF_SPEC_FIELDS:
            key = (
                prim,
                (f"{k}:{ev.get('spec_kind') or ''}:{ev.get('spec_path') or ''}"),
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
                latest[key] = (merge_spec_events(existing[0], ev), meta, seq)
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
        recreates. Layered receivers reconstruct logical layer strength; flat
        receivers continue to consume the composed projection.
        """
        sorted_entries = sorted(latest.values(), key=lambda entry: entry[2])

        records = []
        for seq, (ev, meta, _stamp) in enumerate(sorted_entries, start=1):
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

        LOG.info("Compacted event log: %d -> %d events", original_count, len(sorted_entries))

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
            except (OSError, TimeoutError):
                LOG.info(
                    "Receiver disconnected during compaction replay: %s",
                    handler.client_address,
                )
                disconnected.append(handler)
        if disconnected:
            with self.clients_lock:
                for handler in disconnected:
                    self.receivers.discard(handler)

    def purge(self):
        """Clear all events, reset the edit layer, and resync receivers."""
        self.txn_barrier.acquire_exclusive()
        try:
            if self._persist_queue is not None:
                self._persist_queue.join()
            self._broadcast_queue.join()
            self._purge_inner()
        finally:
            self.txn_barrier.release_exclusive()

    def _purge_inner(self):
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
        self.broadcast({"type": MSG_RESYNC, "reason": "purge"})

    def _flatten_layer_stack_for_projection(self) -> Sdf.Layer:
        with self.stage_lock:
            return UsdUtils.FlattenLayerStack(
                self.stage,
                tag="openusdconnect-flat-projection.usda",
            )

    def _flattened_stage_layer_for_property_event(
        self,
        event: dict,
    ) -> Sdf.Layer | None:
        if event.get("k") != K_SET_SDF_SPEC_FIELDS or _needs_flattened_spec_projection(event):
            return None
        with self.stage_lock:
            if not composed_property_spec_requires_flattening(self.stage, event):
                return None
            return self.stage.Flatten()

    def build_correction(
        self,
        ev: dict,
        *,
        composed_layer: Sdf.Layer | None = None,
        flattened_stage_layer: Sdf.Layer | None = None,
    ) -> dict | None:
        """Build a correction event with the composed value for an overridden event.

        Returns a new event dict with the server's authoritative composed
        values, or None if no correction is needed.
        """
        k = ev.get("k")
        pp = ev.get("prim", "")
        if not pp:
            return None

        with self.stage_lock:
            if k == K_SET_SDF_SPEC_FIELDS:
                if _needs_flattened_spec_projection(ev):
                    layer = (
                        composed_layer
                        if composed_layer is not None
                        else self._flatten_layer_stack_for_projection()
                    )
                    return composed_layer_spec_event(
                        layer,
                        ev,
                    )
                if flattened_stage_layer is not None:
                    return composed_layer_spec_event(flattened_stage_layer, ev)
                return composed_property_spec_event(self.stage, ev)

            prim = self.stage.GetPrimAtPath(pp)
            if not prim or not prim.IsValid():
                return None

            if k == K_SET_XFORM_TRS:
                xf = UsdGeom.Xformable(prim)
                local = xf.GetLocalTransformation(Usd.TimeCode.Default())
                t, r, s = decompose_trs_from_matrix(as_matrix(local))
                return {
                    "k": K_SET_XFORM_TRS,
                    "prim": pp,
                    "fields": ["t", "r", "s"],
                    "t": t,
                    "r": r,
                    "s": s,
                }

            if k == K_SET_VISIBILITY:
                img = UsdGeom.Imageable(prim)
                vis = img.GetVisibilityAttr().Get()
                return {
                    "k": K_SET_VISIBILITY,
                    "prim": pp,
                    "visible": vis != "invisible",
                }

            if k == K_DEACTIVATE_PRIM:
                return {
                    "k": K_DEACTIVATE_PRIM,
                    "prim": pp,
                    "active": prim.IsActive(),
                }

        # For event types we can't build corrections for, return None.
        # The sender stays divergent until the next full resync.
        return None

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
                    "uploaded openusdconnect metadata field 'scene_id' "
                    "must be a non-empty string"
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
                    details.append(
                        f"collaboration layers: {', '.join(additional_layers)}"
                    )
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
                event for event in events if event.get("k") not in SHARED_STAGE_KINDS
            ]
            shared_events = [
                event for event in events if event.get("k") in SHARED_STAGE_KINDS
            ]
            if replacement_events:
                apply_events(replacement_stage, replacement_events, prevalidated=True)
            if shared_events:
                replacement_stage.SetEditTarget(
                    Usd.EditTarget(replacement_stage.GetSessionLayer())
                )
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
                if event.get("k") not in SHARED_STAGE_KINDS:
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

    def append_log(self, rec: dict):
        """Append event record to the event store.

        Raises on persistence failure — callers should not broadcast
        events that were not successfully persisted.
        """
        ev = rec.get("event", {})
        rec_bin = encode_message(rec)
        self.store.append(rec["seq"], rec_bin, kind=ev.get("k"), prim=ev.get("prim"))
        with self._seq_lock:
            self._event_count += 1

    def append_log_batch(
        self,
        tuples: list[tuple[int, bytes, str | None, str | None, str | None]],
    ):
        """Persist pre-serialized event records.

        Each tuple is (seq, record_bin, client_id, kind, prim).
        In strict mode, writes synchronously (caller blocks until DB commit).
        In realtime mode, enqueues for async write (caller returns immediately).
        """
        if self._persist_queue is not None:
            self._persist_queue.put(tuples)
        else:
            self.store.append_batch(tuples)
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
            self.append_log(rec)
            self.broadcast(rec, exclude_origin=origin)

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
        changed_indices: set[int],
        events: list[dict],
        *,
        exclude_origin: str | None = None,
    ) -> None:
        """Deliver one authored transaction to layered and flat receivers.

        Layered receivers always get every persisted authored record, including
        records from their own origin, because omitting one would leave their
        reconstructed layer stack incomplete. Flat receivers retain the
        existing composed-view behavior and origin echo suppression: winning
        records pass through, while overridden generic Sdf edits become
        authoritative composed corrections.
        """
        if not records:
            return
        if len(records) != len(events):
            raise ValueError("transaction record and event counts differ")

        has_flat_receivers, has_layered_receivers = self._receiver_audience_presence()
        if has_layered_receivers:
            authored_records = [record for record, _encoded in records]
            authored_bytes = [encoded for _record, encoded in records]
            self.broadcast_bytes(
                frame_batch(authored_bytes),
                authored_records,
                audience=_AUDIENCE_LAYERED,
                notify_listeners=False,
            )

        notify_flat_view = bool(self._event_listeners)
        if not has_flat_receivers and not notify_flat_view:
            return

        flat_records: list[dict] = []
        flat_bytes: list[bytes] = []
        projection_layer: Sdf.Layer | None = None
        flattened_stage_layer: Sdf.Layer | None = None

        def _flush_flat() -> None:
            if not flat_records:
                return
            if has_flat_receivers:
                self.broadcast_bytes(
                    frame_batch(flat_bytes),
                    flat_records,
                    exclude_origin=exclude_origin,
                    audience=_AUDIENCE_FLAT,
                )
            else:
                self._notify_event_listeners(flat_records)
            flat_records.clear()
            flat_bytes.clear()

        for index, ((record, encoded), event) in enumerate(
            zip(records, events, strict=True),
        ):
            if index in changed_indices:
                flat_records.append(record)
                flat_bytes.append(encoded)
                continue

            _flush_flat()
            if projection_layer is None and _needs_flattened_spec_projection(event):
                projection_layer = self._flatten_layer_stack_for_projection()
            if flattened_stage_layer is None:
                flattened_stage_layer = self._flattened_stage_layer_for_property_event(event)
            correction = self.build_correction(
                event,
                composed_layer=projection_layer,
                flattened_stage_layer=flattened_stage_layer,
            )
            if correction is None:
                continue
            correction_record = {
                "type": MSG_EVENT,
                # This is a receiver view of the persisted opinion, not a new
                # authored event, so it deliberately reuses the same sequence.
                "seq": record["seq"],
                "event": correction,
            }
            if exclude_origin and has_flat_receivers:
                self.send_to_origin(
                    correction_record,
                    exclude_origin,
                    audience=_AUDIENCE_FLAT,
                )
            if event.get("k") in FLAT_RECEIVER_PROJECTION_KINDS:
                if has_flat_receivers:
                    self.broadcast(
                        correction_record,
                        exclude_origin=exclude_origin,
                        audience=_AUDIENCE_FLAT,
                    )
                else:
                    self._notify_event_listeners([correction_record])
        _flush_flat()

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
        self._broadcast_queue.put((payload, exclude_origin, None, audience))
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
        self._broadcast_queue.put((payload, exclude_origin, None, audience))
        if not notify_listeners:
            return
        self._notify_event_listeners(records)

    def send_to_origin(
        self,
        rec: dict,
        origin: str,
        *,
        audience: str = _AUDIENCE_ALL,
    ):
        """Enqueue a record for async send to receivers matching an origin."""
        self._validate_audience(audience)
        payload = frame_batch([encode_message(rec)])
        self._broadcast_queue.put((payload, None, origin, audience))

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
        self._broadcast_queue.put((payload, exclude_origin, None, audience))

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
                    payload, exclude_origin, target_origin, audience = remaining
                    try:
                        self._send_to_all(
                            payload,
                            exclude_origin=exclude_origin,
                            target_origin=target_origin,
                            audience=audience,
                        )
                    except Exception:
                        LOG.exception("Error draining broadcast queue")
                return

            payload, exclude_origin, target_origin, audience = item
            try:
                self._send_to_all(
                    payload,
                    exclude_origin=exclude_origin,
                    target_origin=target_origin,
                    audience=audience,
                )
            except Exception:
                LOG.exception("Unexpected error in broadcast loop")
            finally:
                self._broadcast_queue.task_done()

    def _send_to_all(
        self,
        payload: bytes,
        exclude_origin: str | None = None,
        target_origin: str | None = None,
        audience: str = _AUDIENCE_ALL,
    ):
        """Send payload to matching receivers, removing dead ones.

        A failed send is the earliest reliable signal that a client is
        gone — releases the playback-leader role here too so other
        clients don't wait a full keepalive cycle to reclaim it. The
        dead handler's recv loop exits separately once keepalive expires.
        """
        with self.clients_lock:
            targets = list(self.receivers)
        dead = []
        for h in targets:
            h_origin = getattr(h, "_origin", None)
            layered = bool(getattr(h, "_layered_replay", False))
            if audience == _AUDIENCE_LAYERED and not layered:
                continue
            if audience == _AUDIENCE_FLAT and layered:
                continue
            if target_origin and h_origin != target_origin:
                continue
            if exclude_origin and h_origin == exclude_origin:
                continue
            try:
                with h.send_lock:
                    h.request.sendall(payload)
            except (OSError, TimeoutError):
                LOG.debug("Send failed for %s, marking as dead", h.client_address)
                dead.append(h)
        if not dead:
            return
        with self.clients_lock:
            for h in dead:
                self.receivers.discard(h)
        released_any = False
        for h in dead:
            client_id = getattr(h, "_client_id", "") or ""
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

    # Derived from the EVENT_KIND_INFO declaration table; semantics are
    # documented on EventKindInfo.strength_attrs. Kinds with no entry are
    # always broadcast (conservative default).
    _EVENT_ATTR_MAP: dict[str, list[str]] = {
        k: list(info.strength_attrs)
        for k, info in EVENT_KIND_INFO.items()
        if info.strength_attrs is not None
    }

    def _is_layer_winning(self, prim, target, ev) -> bool:
        """Check if the target layer's opinions from this event are visible.

        For attribute-writing events, checks ``GetPropertyStack`` per
        attribute.  For metadata and structural events, walks session
        sublayers with early exit (avoids building the full prim stack).
        For unknown event types, returns True (conservative — always broadcast).
        """
        k = ev.get("k", "")
        attr_names = self._EVENT_ATTR_MAP.get(k)

        if attr_names is None:
            return True  # unknown event type — always broadcast

        if k == K_SET_MATERIAL_BINDING:
            purpose = ev.get("material_purpose", "")
            if purpose:
                attr_names = [f"rel:material:binding:{purpose}"]

        prim_path = str(prim.GetPath())

        if not attr_names:
            # Structural event (ensure_prim, delete, rename) — walk
            # session sublayers strong-to-weak, first with a spec wins.
            # Uses HasSpec() (cheap lookup) instead of GetPrimStack()
            # (builds full spec vector).
            return self._is_first_layer_with_spec(prim_path, target)

        for attr_name in attr_names:
            if attr_name == "meta:active":
                # Walk session sublayers; first with HasInfo("active") wins.
                return self._is_first_layer_with_info(
                    prim_path,
                    target,
                    "active",
                )
            elif attr_name.startswith("rel:"):
                rel = prim.GetRelationship(attr_name[4:])
                if not rel or not rel.IsValid():
                    continue
                stack = rel.GetPropertyStack()
                if stack and stack[0].layer == target:
                    return True
            else:
                attr = prim.GetAttribute(attr_name)
                if not attr or not attr.IsValid():
                    continue
                stack = attr.GetPropertyStack()
                if stack and stack[0].layer == target:
                    return True

        return False

    def _is_first_layer_with_spec(self, prim_path: str, target) -> bool:
        """Check if target is the strongest layer with a spec for prim_path.

        Iterates cached session layer objects (strong → weak). Returns
        True at the first layer that has a spec — early exit, no
        Sdf.Layer.Find() calls.
        """
        for layer in self.layer_stack.ordered_layers:
            if layer.GetPrimAtPath(prim_path):
                return layer == target
        root = self.stage.GetRootLayer()
        if root.GetPrimAtPath(prim_path):
            return root == target
        return True

    def _is_first_layer_with_info(
        self,
        prim_path: str,
        target,
        field: str,
    ) -> bool:
        """Check if target is the strongest layer with a given metadata field."""
        for layer in self.layer_stack.ordered_layers:
            spec = layer.GetPrimAtPath(prim_path)
            if spec and spec.HasInfo(field):
                return layer == target
        root = self.stage.GetRootLayer()
        spec = root.GetPrimAtPath(prim_path)
        if spec and spec.HasInfo(field):
            return root == target
        return True

    def apply_txn(self, events: list[dict], layer: Sdf.Layer | None = None) -> list[int]:
        """Apply a transaction to the stage.

        Layer opinions are authored into *layer* (defaults to
        ``self.edit_layer``). Shared session metadata and stage-state events
        are applied under the primary session edit target.
        Returns indices of events that changed the composed view — events
        whose composed values are unchanged (overridden by a stronger layer)
        are excluded.  All events are persisted to the log regardless.

        Uses per-attribute strength checking via ``GetPropertyStack`` and
        ``PrimSpec.HasInfo`` — works for all event types without snapshotting.
        When the edit layer is the only unmuted session sublayer (no
        department layers), every event wins and the checks are skipped.

        Winning checks are cached per (prim_path, event_kind) for the
        duration of the txn.  The layer stack is frozen while we hold
        stage_lock, so the result cannot change between the first and
        subsequent checks for the same prim+kind.
        """
        from ..event_apply import apply_events
        from ..sdf_spec_delta import validate_spec_delta

        for event in events:
            if event.get("k") == K_SET_SDF_SPEC_FIELDS:
                validate_spec_delta(event)

        target = layer or self.edit_layer

        changed_indices = []
        with self.stage_lock:
            edit_target = Usd.EditTarget(target)
            session_target = Usd.EditTarget(self.stage.GetSessionLayer())
            has_layer_opinions = any(ev.get("k") not in SHARED_STAGE_KINDS for ev in events)
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
                    shared = events[start].get("k") in SHARED_STAGE_KINDS
                    end = start + 1
                    while (
                        end < len(events) and (events[end].get("k") in SHARED_STAGE_KINDS) == shared
                    ):
                        end += 1
                    run = events[start:end]
                    run_target = session_target if shared else edit_target
                    run_layer = self.stage.GetSessionLayer() if shared else target
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

            # Single-layer mode: the edit layer is the only unmuted session
            # sublayer, and local session opinions are strongest in LIVRPS
            # order, so every event's opinion is composed-visible. Skips
            # the per-event GetPropertyStack/HasSpec strength checks.
            if (
                target is self.edit_layer
                and len(self.layer_stack.ordered_layers) == 1
                and not was_muted
            ):
                for i, ev in enumerate(events):
                    ev_kind = ev.get("k")
                    if ev_kind in (K_ENSURE_PRIM, K_DELETE_PRIM, K_RENAME_PRIM):
                        self._prim_count_dirty = True
                        self._track_prim_event(ev)
                    elif ev_kind == K_SET_INSTANCEABLE:
                        self._track_prim_event(ev)
                    changed_indices.append(i)
                return changed_indices

            # Cache winning results per (prim_path, event_kind) — the
            # layer stack is frozen for the duration of this txn so the
            # result is stable across events touching the same prim.
            winning_cache: dict[tuple[str, str], bool] = {}

            for i, ev in enumerate(events):
                k = ev.get("k")
                if k in (K_ENSURE_PRIM, K_DELETE_PRIM, K_RENAME_PRIM):
                    self._prim_count_dirty = True
                    self._track_prim_event(ev)
                elif k == K_SET_INSTANCEABLE:
                    self._track_prim_event(ev)
                pp = ev.get("prim", "")
                if not pp:
                    changed_indices.append(i)
                    continue
                if k in FLAT_RECEIVER_PROJECTION_KINDS:
                    # Flat receivers need an authoritative composed correction
                    # so weaker opinions, dictionary composition, and clears
                    # cannot leak through.
                    continue

                cache_key = (pp, k)
                cached = winning_cache.get(cache_key)
                if cached is not None:
                    if cached:
                        changed_indices.append(i)
                    continue

                prim = self.stage.GetPrimAtPath(pp)
                if not prim or not prim.IsValid():
                    winning_cache[cache_key] = True
                    changed_indices.append(i)
                    continue
                wins = self._is_layer_winning(prim, target, ev)
                winning_cache[cache_key] = wins
                if wins:
                    changed_indices.append(i)

        return changed_indices

    def process_txn(
        self,
        events: list[dict],
        *,
        client_id: str | None = None,
        origin: str | None = None,
        client_addr: str | None = None,
        layer: Sdf.Layer | None = None,
    ) -> tuple[list[tuple[dict, bytes]], set[int]]:
        """Apply, seq-assign, encode, and persist a transaction.

        Runs apply_txn, assigns monotonic sequence numbers, encodes each
        event as a FlatBuffers record, and batches the persist to the
        event store. Callers that need txn_barrier coordination (e.g.
        ConnectionHandler around broadcast) must acquire/release the
        shared lock themselves — this method does not, so it stays
        composable with broader critical sections.

        Returns:
            records: list of (rec_dict, rec_bin) in input order — used by
                callers that need to broadcast the encoded records.
            changed_indices: set of event indices whose composed value
                actually landed on the stage (the rest were overridden by
                a stronger layer). Broadcast callers typically send only
                the changed records and emit corrections for the rest.

        Callers that need broadcast/correction (ConnectionHandler) consume
        the return value. Callers that just want authoritative state plus
        a populated log (tests, replay tooling) can ignore it.

        Collaboration records derive their portable layer key from the actual
        edit target. Client policy selects that target before this method is
        called; cached client metadata is not authoritative for persistence.
        """
        target_layer = layer or self.edit_layer
        layer_key = self.layer_stack.key_for_layer(target_layer)
        if layer_key is None and any(ev.get("k") not in SHARED_STAGE_KINDS for ev in events):
            raise ValueError("transaction target is not a managed collaboration layer")

        changed = self.apply_txn(events, layer=target_layer)
        changed_set = set(changed)

        records: list[tuple[dict, bytes]] = []
        persist_tuples: list[tuple[int, bytes, str | None, str | None, str | None]] = []
        for ev in events:
            rec: dict = {
                "type": MSG_EVENT,
                "seq": self.assign_seq(),
                "event": ev,
                "client": client_addr,
                "client_id": client_id,
            }
            if origin:
                rec["origin"] = origin
            if ev.get("k") not in SHARED_STAGE_KINDS:
                rec["layer_key"] = layer_key
            rec_bin = encode_message(rec)
            if self.wire_metrics is not None:
                self.wire_metrics.record(ev.get("k", ""), len(rec_bin))
            records.append((rec, rec_bin))
            persist_tuples.append((rec["seq"], rec_bin, client_id, ev.get("k"), ev.get("prim")))
        self.append_log_batch(persist_tuples)
        return records, changed_set

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

    def replay_from(self, handler, seq_start: int):
        """Replay events from the event store starting at seq_start.

        All events are replayed regardless of origin — the receiver needs
        its own prior edits (which share its origin) to restore state.
        Origin filtering only applies to live broadcast to prevent echo.
        Layered receivers get every persisted authored record with its logical
        layer target. Flat receivers retain the composed projection used by
        existing DCC adapters.

        Single-layer replay sends binary blobs directly from the store.
        """
        _REPLAY_CHUNK = 65536
        blobs = self.store.get_from_seq_bin(seq_start)
        buf_parts: list[bytes] = []
        buf_size = 0
        projection_layer: Sdf.Layer | None = None
        flattened_stage_layer: Sdf.Layer | None = None
        for blob in blobs:
            outbound = blob
            if len(self.layer_stack.layer_keys) > 1 and not getattr(
                handler,
                "_layered_replay",
                False,
            ):
                rec = message_to_dict(blob)
                event = rec.get("event", {})
                if event.get("k") in FLAT_RECEIVER_PROJECTION_KINDS:
                    if projection_layer is None and _needs_flattened_spec_projection(event):
                        projection_layer = self._flatten_layer_stack_for_projection()
                    if flattened_stage_layer is None:
                        flattened_stage_layer = self._flattened_stage_layer_for_property_event(
                            event
                        )
                    correction = self.build_correction(
                        event,
                        composed_layer=projection_layer,
                        flattened_stage_layer=flattened_stage_layer,
                    )
                    if correction is not None:
                        outbound = encode_message(
                            {
                                "type": MSG_EVENT,
                                "seq": rec["seq"],
                                "event": correction,
                            }
                        )
            framed = frame_batch([outbound])
            buf_parts.append(framed)
            buf_size += len(framed)
            if buf_size >= _REPLAY_CHUNK:
                handler.request.sendall(b"".join(buf_parts))
                buf_parts.clear()
                buf_size = 0
        if buf_parts:
            handler.request.sendall(b"".join(buf_parts))
