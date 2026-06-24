"""UsdSyncServer — authoritative state for the sync protocol.

Holds the in-memory ``Usd.Stage``, the per-client/per-department layer
stack, the SQLite event log, the broadcast and persistence threads, the
TOFU token store, and proposal bookkeeping.  Network handling lives in
``connection.py``; the CLI lives in ``cli.py``.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from pxr import Sdf, Usd, UsdGeom

from ..codec import encode_message, message_to_dict
from ..emitter import (
    read_material_binding,
    read_payloads,
    read_references,
    read_stage_metadata,
    read_variant_selections,
)
from ..event_store import EventStore, SqliteEventStore
from ..framing import frame_batch
from ..protocol_constants import (
    EVENT_KIND_INFO,
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
    K_SET_VISIBILITY,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
    MSG_EVENT,
    MSG_PING,
    MSG_PLAYBACK_STATE,
    MSG_RESYNC,
    event_apply_tier,
)
from ..xform_decompose import as_matrix, decompose_trs_from_matrix
from ._txn_barrier import _TxnBarrier
from .types import ClientInfo, Proposal

LOG = logging.getLogger(__name__)

# Bounded queue limits — provides natural backpressure when receivers or
# persistence can't keep up, preventing unbounded memory growth.
_BROADCAST_QUEUE_MAX = 10_000
_PERSIST_QUEUE_MAX = 10_000
_PING_INTERVAL = 30.0  # seconds between heartbeat pings during idle


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
    "protoIndices", "positions", "orientations", "orientationsf", "scales",
    "velocities", "accelerations", "angularVelocities", "ids", "invisibleIds",
)


def _point_instancer_summary(prim) -> dict:
    """Bounded read of UsdGeomPointInstancer state for the inspector.

    Returns prototypes targets, instance count (from protoIndices length),
    which arrays are animated, and the size of the inactiveIds prim
    metadata when authored.
    """
    pi = UsdGeom.PointInstancer(prim)
    proto_rel = pi.GetPrototypesRel()
    targets = (
        [t.pathString for t in proto_rel.GetTargets()] if proto_rel else []
    )
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
    ):
        if base_usd_path:
            self.stage = Usd.Stage.Open(base_usd_path)
            if self.stage is None:
                raise RuntimeError(f"Failed to open base USD: {base_usd_path}")
        else:
            self.stage = Usd.Stage.CreateInMemory()
            self.stage.DefinePrim("/Root", "Xform")

        # Non-destructive editing: fallback layer for clients without an ID.
        self.edit_layer = self._create_edit_layer()

        # Per-department layers: clients in the same department share a layer
        # (last-write-wins within a department). Department ordering controls
        # strength between departments.
        self.client_layers: dict[str, Sdf.Layer] = {}  # client_id or dept → layer
        self._dept_layers: dict[str, Sdf.Layer] = {}  # department → shared layer
        self._client_departments: dict[str, str] = {}  # client_id → department
        self.department_priority: list[str] = list(department_priority or [])

        # Ordered session layer objects (strong → weak) for fast strength
        # checks. Rebuilt by _reorder_session_sublayers().
        self._ordered_session_layers: list = [self.edit_layer]

        self.stage_lock = threading.RLock()
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
        # Item: (payload_bytes, exclude_origin, target_origin)
        #   exclude_origin set → broadcast to all except that origin
        #   target_origin set  → send only to that origin (corrections)
        #   both None          → broadcast to everyone
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

    def shutdown(self):
        """Signal background threads to drain queued work and exit.

        Persist queue drains first (durability), then broadcast.
        """
        if self._persist_queue is not None:
            self._persist_queue.put(None)
            self._persist_thread.join(timeout=10.0)
        self._broadcast_queue.put(None)
        self._broadcast_thread.join(timeout=10.0)

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
        self, client_id: str, initial_time: float | None = None,
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

        Routes events to per-client layers based on stored client_id.
        Events without a client_id go to the shared edit_layer.
        Also populates _prim_paths for incremental prim tracking.
        """
        from ..event_apply import apply_events

        rows = self.store.get_all_asc()
        if not rows:
            return

        # Collect all events for prim tracking (both code paths need this).
        all_events: list[dict] = []

        if self.department_priority:
            # Per-client layers: route events to the correct client layer.
            by_client: dict[str | None, list[dict]] = {}
            for _seq, record_bin in rows:
                rec = message_to_dict(record_bin, numpy_arrays=True)
                ev = rec.get("event", rec)
                cid = rec.get("client_id")
                by_client.setdefault(cid, []).append(ev)
                all_events.append(ev)

            for cid, evts in by_client.items():
                layer = self.get_or_create_client_layer(cid) if cid else self.edit_layer
                self.stage.SetEditTarget(Usd.EditTarget(layer))
                apply_events(self.stage, evts, op_cache=self._op_cache_for(layer))

            total = sum(len(v) for v in by_client.values())
        else:
            # Legacy mode: all events to the shared edit_layer.
            events = []
            for _seq, record_bin in rows:
                rec = message_to_dict(record_bin, numpy_arrays=True)
                ev = rec.get("event", rec)
                events.append(ev)
                all_events.append(ev)
            apply_events(self.stage, events, op_cache=self._op_cache_for(self.edit_layer))
            total = len(events)

        # Populate incremental prim tracking from replayed events.
        for ev in all_events:
            self._track_prim_event(ev)

        LOG.info("Restored stage from event log: %d events", total)

    # -- TOFU authentication -------------------------------------------

    def authenticate(
        self, client_id: str | None, token: str | None, department: str | None = None
    ) -> tuple[bool, str | None]:
        """Authenticate a client using TOFU.

        Returns (accepted, issued_token).
        - First connect (no token stored): issues a new token → (True, new_token)
        - Reconnect with valid token: accepted → (True, None)
        - Reconnect with wrong/missing token: rejected → (False, None)
        - Token not required or no client_id: always accepted → (True, None)
        """
        if not self.require_token or not self.token_store or not client_id:
            return True, None

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
        with self.proposals_lock:
            p = self.proposals.get(proposal_id)
        if not p or p.status != "pending":
            return False

        target_layer = self._dept_layers.get(p.target_department)
        if not target_layer:
            LOG.warning(
                "Cannot approve proposal %s — target department '%s' has no layer",
                proposal_id,
                p.target_department,
            )
            return False

        if p.events:
            # Apply into the target department layer.
            self.apply_txn(p.events, layer=target_layer)

            # Persist and broadcast all events. Unlike normal txns we
            # don't filter by changed_set — proposals merge into a
            # department layer and all events should be visible to
            # receivers for log consistency. No corrections needed
            # since no active receiver has the proposal origin.
            records = []
            persist_tuples = []
            for ev in p.events:
                rec = {
                    "type": MSG_EVENT,
                    "seq": self.assign_seq(),
                    "event": ev,
                    "client_id": p.from_client,
                    "origin": f"proposal-{p.proposal_id}",
                }
                rec_bin = encode_message(rec)
                records.append(rec)
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
            for rec in records:
                self.broadcast(rec)

        # Remove proposal layer from session
        with self.stage_lock:
            self.stage.UnmuteLayer(p.layer.identifier)
            session = self.stage.GetSessionLayer()
            idx = list(session.subLayerPaths).index(p.layer.identifier)
            del session.subLayerPaths[idx]

        p.status = "approved"
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
        if not department:
            self.client_layers[client_id] = self.edit_layer
            return self.edit_layer

        self._client_departments[client_id] = department

        # Lock the whole lookup+create so two threads for the same
        # department don't both miss the check and create duplicate layers.
        with self.stage_lock:
            existing = self._dept_layers.get(department)
            if existing:
                self.client_layers[client_id] = existing
                return existing

            layer = self._create_edit_layer(label=f"dept-{department}")
            self._dept_layers[department] = layer
            self.client_layers[client_id] = layer
            self._reorder_session_sublayers()
        LOG.info("Created shared layer for department %s (client %s)", department, client_id)
        return layer

    def _reorder_session_sublayers(self):
        """Reorder session sublayers: department layers by priority, then edit_layer.

        Ordering (strongest → weakest):
        1. Department layers in ``department_priority`` order
        2. Shared ``edit_layer`` (fallback for non-department clients)
        """
        session = self.stage.GetSessionLayer()
        ordered_ids: list[str] = []
        ordered_layers: list = []

        for dept in self.department_priority:
            layer = self._dept_layers.get(dept)
            if layer:
                ordered_ids.append(layer.identifier)
                ordered_layers.append(layer)

        # edit_layer is always last (weakest)
        ordered_ids.append(self.edit_layer.identifier)
        ordered_layers.append(self.edit_layer)

        session.subLayerPaths.clear()
        for p in ordered_ids:
            session.subLayerPaths.append(p)

        # Re-attach pending proposal layers (weakest, still muted). Without
        # this a reorder mid-proposal detaches them, so approve/reject can't
        # find them by identifier and proposal edits have no layer to land in.
        with self.proposals_lock:
            for prop in self.proposals.values():
                if prop.status == "pending":
                    session.subLayerPaths.append(prop.layer.identifier)

        # Cache for _is_first_layer_with_spec — avoids Sdf.Layer.Find()
        # on every strength check. Proposal layers are excluded — they are
        # muted and never win a strength check.
        self._ordered_session_layers = ordered_layers

    def resolve_layer(self, key: str) -> Sdf.Layer | None:
        """Resolve a layer by client_id or department name."""
        return self._dept_layers.get(key) or self.client_layers.get(key)

    def mute_layer(self, key: str) -> bool:
        """Mute a layer by client_id or department — opinions hidden but preserved."""
        layer = self.resolve_layer(key)
        if not layer:
            return False
        with self.stage_lock:
            self.stage.MuteLayer(layer.identifier)
        return True

    def unmute_layer(self, key: str) -> bool:
        """Unmute a layer by client_id or department."""
        layer = self.resolve_layer(key)
        if not layer:
            return False
        with self.stage_lock:
            self.stage.UnmuteLayer(layer.identifier)
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
            session = self.stage.GetSessionLayer()
            idx = list(session.subLayerPaths).index(layer.identifier)
            del session.subLayerPaths[idx]
        self._cleanup_client_refs(client_id)
        LOG.info("Merged and removed layer for client %s", client_id)
        return True

    def delete_layer(self, client_id: str) -> bool:
        """Delete a client's layer and discard all opinions.

        Returns False for clients on the shared edit_layer (no-op).
        """
        layer = self.client_layers.get(client_id)
        if not layer or layer is self.edit_layer:
            return False
        with self.stage_lock:
            session = self.stage.GetSessionLayer()
            idx = list(session.subLayerPaths).index(layer.identifier)
            del session.subLayerPaths[idx]
        self._cleanup_client_refs(client_id)
        LOG.info("Deleted layer for client %s", client_id)
        return True

    def _cleanup_client_refs(self, client_id: str):
        """Remove a client from all tracking dicts.

        If this was the last client in a department, also removes the
        orphaned department layer reference.
        """
        self.client_layers.pop(client_id, None)
        dept = self._client_departments.pop(client_id, None)
        if dept and not any(d == dept for d in self._client_departments.values()):
            self._dept_layers.pop(dept, None)

    def set_department_priority(self, ordered_departments: list[str]) -> None:
        """Set department priority ordering (strongest first)."""
        with self.stage_lock:
            self.department_priority = list(ordered_departments)
            self._reorder_session_sublayers()

    def get_layer_stack_info(self) -> list[dict]:
        """Return ordered layer stack info for the dashboard.

        Department layers list all clients sharing them. Non-department
        layers list the single owning client.
        """
        with self.stage_lock:
            session = self.stage.GetSessionLayer()
            muted = set(self.stage.GetMutedLayers())
            sublayer_paths = list(session.subLayerPaths)
            dept_items = list(self._dept_layers.items())
            client_items = list(self.client_layers.items())
            dept_map = dict(self._client_departments)

        seen = set()
        result = []

        # Department layers
        for dept, layer in dept_items:
            if layer.identifier in seen:
                continue
            seen.add(layer.identifier)
            clients = [cid for cid, d in dept_map.items() if d == dept]
            result.append(
                {
                    "department": dept,
                    "clients": clients,
                    "identifier": layer.identifier,
                    "muted": layer.identifier in muted,
                }
            )

        # Non-department client layers
        for cid, layer in client_items:
            if layer.identifier in seen:
                continue
            seen.add(layer.identifier)
            result.append(
                {
                    "department": None,
                    "clients": [cid],
                    "identifier": layer.identifier,
                    "muted": layer.identifier in muted,
                }
            )

        # Sort by session sublayer order (strongest first)
        path_order = {p: i for i, p in enumerate(sublayer_paths)}
        result.sort(key=lambda r: path_order.get(r["identifier"], 999))
        return result

    def compact_log(self):
        """Compact the event log, keeping only the latest state per prim.

        For latest-wins events (TRS, visibility, etc.), only the final value
        is kept.  Partial TRS fields are merged.  delete_prim tombstones all
        prior events for that prim.  deactivate_prim is latest-wins (TRS
        preserved for payload reload).

        Two-phase design minimizes emitter blocking:
          Phase 1 (no lock): snapshot the log and build the compacted dict.
          Phase 2 (exclusive): merge any delta, rewrite store, resync.
        """
        # Phase 1 — snapshot + compute (no txn_barrier, emitters keep running)
        rows = self.store.get_all_asc()
        if not rows:
            return
        max_seq = rows[-1][0]
        latest, tombstoned = self._build_compacted(rows)
        original_count = len(rows)

        # Phase 2 — merge delta + commit (exclusive, emitters blocked)
        self.txn_barrier.acquire_exclusive()
        try:
            # Catch any events that arrived during phase 1
            delta = self.store.get_from_seq_asc(max_seq + 1)
            if delta:
                for _seq, record_bin in delta:
                    self._merge_event(latest, tombstoned, record_bin)
                original_count += len(delta)

            self._commit_compaction(latest, original_count)
        finally:
            self.txn_barrier.release_exclusive()

    @staticmethod
    def _merge_event(
        latest: dict[tuple[str, str, float | None], tuple[dict, dict]],
        tombstoned: set[str],
        record_bin: bytes,
    ):
        """Merge a single event record into the compacted state.

        Keys are ``(prim, kind, time)``: events at distinct time samples
        compact independently of each other and of the default-time
        opinion, so a keyframed log keeps one merged event per sample.
        """
        rec = message_to_dict(record_bin)
        ev = rec.get("event", rec)
        prim = ev.get("prim", "")
        k = ev.get("k", "")
        key = (prim, k, ev.get("time"))
        meta = {}
        for meta_key in ("origin", "client", "client_id"):
            val = rec.get(meta_key)
            if val:
                meta[meta_key] = val

        if k in (K_DELETE_PRIM, K_RENAME_PRIM):
            tombstoned.add(prim)
            to_remove = [existing for existing in latest if existing[0] == prim]
            for existing in to_remove:
                del latest[existing]
            latest[key] = (ev, meta)
            return

        if prim in tombstoned:
            return

        # load/unload are mutually exclusive — only the last one wins.
        if k == K_LOAD_PAYLOAD:
            latest.pop((prim, K_UNLOAD_PAYLOAD, None), None)
            latest[key] = (ev, meta)
            return
        if k == K_UNLOAD_PAYLOAD:
            latest.pop((prim, K_LOAD_PAYLOAD, None), None)
            latest[key] = (ev, meta)
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
                latest[key] = (prev, meta)
            else:
                latest[key] = (ev, meta)
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
                latest[key] = (prev, meta)
            else:
                latest[key] = (ev, meta)
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
                latest[key] = (prev, meta)
            else:
                latest[key] = (ev, meta)
        elif k == K_SET_POINT_INSTANCER:
            existing = latest.get(key)
            if existing:
                prev = existing[0]
                for f in ev.get("fields", []):
                    prev[f] = ev[f]
                    if f not in prev["fields"]:
                        prev["fields"].append(f)
                latest[key] = (prev, meta)
            else:
                latest[key] = (ev, meta)
        elif k == K_ENSURE_PRIM:
            # Union api_schemas across subsequent ensure_prim events for the
            # same prim (latest typeName wins; api_schemas accumulates) so
            # ShapingAPI added later doesn't clobber a previously-merged
            # ShadowAPI. Multi-apply names (e.g. "CollectionAPI:render") are
            # unique strings so set-union is correct for them too.
            existing = latest.get(key)
            if existing:
                prev = existing[0]
                if "typeName" in ev:
                    prev["typeName"] = ev["typeName"]
                merged = set(prev.get("api_schemas") or [])
                merged.update(ev.get("api_schemas") or [])
                if merged:
                    prev["api_schemas"] = list(merged)
                latest[key] = (prev, meta)
            else:
                latest[key] = (ev, meta)
        else:
            latest[key] = (ev, meta)

    @staticmethod
    def _build_compacted(
        rows: list[tuple[int, bytes]],
    ) -> tuple[dict[tuple[str, str, float | None], tuple[dict, dict]], set[str]]:
        """Build compacted event dict from raw log rows.

        Returns (latest, tombstoned) where:
          latest: {(prim, kind, time) → (event_dict, metadata_dict)}
          tombstoned: set of prim paths deleted/renamed
        """
        tombstoned: set[str] = set()
        latest: dict[tuple[str, str, float | None], tuple[dict, dict]] = {}
        for _seq, record_bin in rows:
            UsdSyncServer._merge_event(latest, tombstoned, record_bin)
        return latest, tombstoned

    def _commit_compaction(
        self,
        latest: dict[tuple[str, str, float | None], tuple[dict, dict]],
        original_count: int,
    ):
        """Commit compacted state: rewrite store, reset seqs, resync receivers.

        Must be called under exclusive txn_barrier.

        Default-time events sort before time-sampled ones for the same prim
        and kind, so replay establishes static state before samples.
        """
        sorted_entries = sorted(
            latest.values(),
            key=lambda entry: (
                entry[0]["prim"].count("/"),
                entry[0]["prim"],
                event_apply_tier(entry[0]["k"]),
                entry[0].get("time") is not None,
                entry[0].get("time") or 0.0,
            ),
        )

        with self._seq_lock:
            self._next_seq = 1
        records = []
        for ev, meta in sorted_entries:
            seq = self.assign_seq()
            rec = {"type": MSG_EVENT, "seq": seq, "event": ev}
            rec.update(meta)
            records.append(
                (seq, encode_message(rec), meta.get("client_id"), ev.get("k"), ev.get("prim"))
            )
        self.store.clear_and_rewrite(records)
        with self._seq_lock:
            self._event_count = len(records)

        self.op_cache.clear()
        self._op_cache_layer = None

        # Rebuild incremental prim tracking from compacted state.
        self._prim_paths.clear()
        self._instanceable_paths.clear()
        self._point_instancer_paths.clear()
        for ev, _meta in sorted_entries:
            self._track_prim_event(ev)

        LOG.info("Compacted event log: %d -> %d events", original_count, len(sorted_entries))

        # Tell connected receivers to reset and replay from the compacted log.
        self.broadcast({"type": MSG_RESYNC, "reason": "compact"})
        with self.clients_lock:
            targets = list(self.receivers)
        for handler in targets:
            with handler.send_lock:
                self.replay_from(handler, 1)

    def purge(self):
        """Clear all events, reset the edit layer, and resync receivers."""
        self.txn_barrier.acquire_exclusive()
        try:
            self._purge_inner()
        finally:
            self.txn_barrier.release_exclusive()

    def _purge_inner(self):
        self.store.clear_and_rewrite([])
        with self._seq_lock:
            self._event_count = 0
            self._next_seq = 1
        with self.stage_lock:
            self.edit_layer.Clear()
        self.op_cache.clear()
        self._op_cache_layer = None
        self._prim_paths.clear()
        self._instanceable_paths.clear()
        self._point_instancer_paths.clear()
        LOG.info("Purged event log and reset edit layer")
        self.broadcast({"type": MSG_RESYNC, "reason": "purge"})

    def build_correction(self, ev: dict) -> dict | None:
        """Build a correction event with the composed value for an overridden event.

        Returns a new event dict with the server's authoritative composed
        values, or None if no correction is needed.
        """
        k = ev.get("k")
        pp = ev.get("prim", "")
        if not pp:
            return None

        with self.stage_lock:
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
        # Store (ev, origin) tuples so replayed broadcasts can suppress
        # echo back to the original sender.
        latest: dict[tuple[str, str], tuple[dict, str | None]] = {}
        for blob in record_blobs:
            rec = message_to_dict(blob)
            ev = rec.get("event", rec)
            ep = ev.get("prim", "")
            ek = ev.get("k", "")
            if ep.startswith(prefix) and ek in replay_kinds:
                latest[(ep, ek)] = (ev, rec.get("origin"))

        if not latest:
            return

        # Order: ensure_prim → ensure_xform_ops → set_xform_trs → set_visibility
        sorted_events = sorted(
            latest.values(),
            key=lambda e: (e[0]["prim"], event_apply_tier(e[0]["k"])),
        )

        for ev, origin in sorted_events:
            rec = {"type": MSG_EVENT, "seq": self.assign_seq(), "event": ev}
            if origin:
                rec["origin"] = origin
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

    def broadcast(self, rec: dict, exclude_origin: str | None = None):
        """Broadcast a single record to all connected receivers."""
        self.broadcast_batch([rec], exclude_origin=exclude_origin)

    def broadcast_batch(
        self,
        records: list[dict],
        exclude_origin: str | None = None,
    ):
        """Enqueue records for async broadcast to all receivers.

        The actual network sends happen on the dedicated broadcast thread,
        so the calling emitter thread is never blocked by slow receivers.
        """
        if not records:
            return
        framed_payloads = [encode_message(rec) for rec in records]
        payload = frame_batch(framed_payloads)
        self._broadcast_queue.put((payload, exclude_origin, None))
        # Notify event listeners synchronously — these are in-process
        # callbacks (e.g. dashboard) that are fast and must see events
        # in order.
        for listener in list(self._event_listeners):
            for rec in records:
                try:
                    listener(rec)
                except Exception:
                    LOG.exception("Event listener failed, removing")
                    self._event_listeners.remove(listener)
                    break

    def broadcast_bytes(
        self,
        payload: bytes,
        records: list[dict],
        exclude_origin: str | None = None,
    ):
        """Enqueue pre-framed payload for broadcast and notify listeners."""
        self._broadcast_queue.put((payload, exclude_origin, None))
        for listener in list(self._event_listeners):
            for rec in records:
                try:
                    listener(rec)
                except Exception:
                    LOG.exception("Event listener failed, removing")
                    self._event_listeners.remove(listener)
                    break

    def send_to_origin(self, rec: dict, origin: str):
        """Enqueue a record for async send to receivers matching an origin."""
        payload = frame_batch([encode_message(rec)])
        self._broadcast_queue.put((payload, None, origin))

    def broadcast_message(self, msg: dict, exclude_origin: str | None = None):
        """Enqueue a one-off non-event message (PlaybackState, etc.) for broadcast.

        Bypasses the event listener path — playback messages are control-plane
        signals, not USD scene events, so they shouldn't appear in the
        dashboard event log.
        """
        payload = frame_batch([encode_message(msg)])
        self._broadcast_queue.put((payload, exclude_origin, None))

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
                    payload, exclude_origin, target_origin = remaining
                    try:
                        self._send_to_all(
                            payload,
                            exclude_origin=exclude_origin,
                            target_origin=target_origin,
                        )
                    except Exception:
                        LOG.exception("Error draining broadcast queue")
                return

            payload, exclude_origin, target_origin = item
            try:
                self._send_to_all(
                    payload,
                    exclude_origin=exclude_origin,
                    target_origin=target_origin,
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
        for layer in self._ordered_session_layers:
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
        for layer in self._ordered_session_layers:
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

        Events are authored into *layer* (defaults to ``self.edit_layer``).
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

        target = layer or self.edit_layer

        changed_indices = []
        with self.stage_lock:
            self.stage.SetEditTarget(Usd.EditTarget(target))
            apply_events(self.stage, events, op_cache=self._op_cache_for(target))

            # Single-layer mode: the edit layer is the only unmuted session
            # sublayer, and local session opinions are strongest in LIVRPS
            # order, so every event's opinion is composed-visible. Skips
            # the per-event GetPropertyStack/HasSpec strength checks.
            if target is self.edit_layer and len(self._ordered_session_layers) == 1:
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
        """
        changed = self.apply_txn(events, layer=layer)
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
            rec_bin = encode_message(rec)
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
                "prototype": (
                    prototype.GetPath().pathString if prototype else None
                ),
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

        Sends binary blobs directly from the store — no re-serialization.
        """
        _REPLAY_CHUNK = 65536
        try:
            blobs = self.store.get_from_seq_bin(seq_start)
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
        except Exception:
            LOG.exception("Failed to replay events")
