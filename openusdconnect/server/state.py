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
from ..emitter import as_matrix, decompose_trs_from_matrix
from ..event_store import EventStore, SqliteEventStore
from ..framing import frame_batch
from ..protocol_constants import (
    EVENT_KIND_ORDER,
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_SET_CONNECTABLE_CONNECTION,
    K_SET_CONNECTABLE_INPUT,
    K_SET_GPRIM_ATTRS,
    K_SET_MATERIAL_BINDING,
    K_SET_VISIBILITY,
    K_SET_XFORM_MATRICES,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
    MSG_EVENT,
    MSG_PING,
    MSG_RESYNC,
)
from ._txn_barrier import _TxnBarrier
from .types import ClientInfo, Proposal

LOG = logging.getLogger(__name__)

# Bounded queue limits — provides natural backpressure when receivers or
# persistence can't keep up, preventing unbounded memory growth.
_BROADCAST_QUEUE_MAX = 10_000
_PERSIST_QUEUE_MAX = 10_000
_PING_INTERVAL = 30.0  # seconds between heartbeat pings during idle


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

        # LRU cache: prim_path → (translate_op, orient_op, scale_op).
        from cachetools import LRUCache

        self.op_cache: LRUCache = LRUCache(
            maxsize=op_cache_size or self.DEFAULT_OP_CACHE_SIZE,
        )

        # Incremental prim tracking — avoids full log scans on dashboard polls.
        self._prim_paths: dict[str, str] = {}  # prim_path → typeName

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

    def _track_prim_event(self, ev: dict):
        """Update _prim_paths from a single event (ensure/delete/rename)."""
        k = ev.get("k")
        prim = ev.get("prim", "")
        if k == K_ENSURE_PRIM:
            self._prim_paths[prim] = ev["typeName"]
        elif k == K_DELETE_PRIM:
            self._prim_paths.pop(prim, None)
        elif k == K_RENAME_PRIM:
            type_name = self._prim_paths.pop(prim, "Xform")
            new_name = ev.get("new_name", "")
            if new_name:
                parent = prim.rsplit("/", 1)[0] or "/"
                new_path = f"{parent}/{new_name}" if parent != "/" else f"/{new_name}"
                self._prim_paths[new_path] = type_name

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
                apply_events(self.stage, evts, op_cache=self.op_cache)

            total = sum(len(v) for v in by_client.values())
        else:
            # Legacy mode: all events to the shared edit_layer.
            events = []
            for _seq, record_bin in rows:
                rec = message_to_dict(record_bin, numpy_arrays=True)
                ev = rec.get("event", rec)
                events.append(ev)
                all_events.append(ev)
            apply_events(self.stage, events, op_cache=self.op_cache)
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

        # Cache for _is_first_layer_with_spec — avoids Sdf.Layer.Find()
        # on every strength check.
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
        latest: dict[tuple[str, str], tuple[dict, dict]],
        tombstoned: set[str],
        record_bin: bytes,
    ):
        """Merge a single event record into the compacted state."""
        rec = message_to_dict(record_bin)
        ev = rec.get("event", rec)
        prim = ev.get("prim", "")
        k = ev.get("k", "")
        meta = {}
        for meta_key in ("origin", "client", "client_id"):
            val = rec.get(meta_key)
            if val:
                meta[meta_key] = val

        if k in (K_DELETE_PRIM, K_RENAME_PRIM):
            tombstoned.add(prim)
            to_remove = [key for key in latest if key[0] == prim]
            for key in to_remove:
                del latest[key]
            latest[(prim, k)] = (ev, meta)
            return

        if prim in tombstoned:
            return

        # load/unload are mutually exclusive — only the last one wins.
        if k == K_LOAD_PAYLOAD:
            latest.pop((prim, K_UNLOAD_PAYLOAD), None)
            latest[(prim, k)] = (ev, meta)
            return
        if k == K_UNLOAD_PAYLOAD:
            latest.pop((prim, K_LOAD_PAYLOAD), None)
            latest[(prim, k)] = (ev, meta)
            return

        if k == K_SET_XFORM_TRS:
            existing = latest.get((prim, k))
            if existing:
                prev = existing[0]
                for comp in ("t", "r", "s"):
                    if comp in ev.get("fields", []):
                        prev[comp] = ev[comp]
                        if comp not in prev["fields"]:
                            prev["fields"].append(comp)
                latest[(prim, k)] = (prev, meta)
            else:
                latest[(prim, k)] = (ev, meta)
        elif k == K_SET_GPRIM_ATTRS:
            existing = latest.get((prim, k))
            if existing:
                prev = existing[0]
                prev.setdefault("attrs", {}).update(ev.get("attrs", {}))
                new_meta = ev.get("primvar_meta", {})
                if new_meta:
                    prev.setdefault("primvar_meta", {}).update(new_meta)
                new_interp = ev.get("attr_interp", {})
                if new_interp:
                    prev.setdefault("attr_interp", {}).update(new_interp)
                latest[(prim, k)] = (prev, meta)
            else:
                latest[(prim, k)] = (ev, meta)
        elif k == K_SET_CONNECTABLE_INPUT:
            existing = latest.get((prim, k))
            if existing:
                prev = existing[0]
                prev.setdefault("inputs", {}).update(ev.get("inputs", {}))
                prev.setdefault("input_types", {}).update(
                    ev.get("input_types", {}),
                )
                if ev.get("info_id"):
                    prev["info_id"] = ev["info_id"]
                latest[(prim, k)] = (prev, meta)
            else:
                latest[(prim, k)] = (ev, meta)
        elif k == K_ENSURE_PRIM:
            # Union api_schemas across subsequent ensure_prim events for the
            # same prim (latest typeName wins; api_schemas accumulates) so
            # ShapingAPI added later doesn't clobber a previously-merged
            # ShadowAPI. Multi-apply names (e.g. "CollectionAPI:render") are
            # unique strings so set-union is correct for them too.
            existing = latest.get((prim, k))
            if existing:
                prev = existing[0]
                if "typeName" in ev:
                    prev["typeName"] = ev["typeName"]
                merged = set(prev.get("api_schemas") or [])
                merged.update(ev.get("api_schemas") or [])
                if merged:
                    prev["api_schemas"] = list(merged)
                latest[(prim, k)] = (prev, meta)
            else:
                latest[(prim, k)] = (ev, meta)
        else:
            latest[(prim, k)] = (ev, meta)

    @staticmethod
    def _build_compacted(
        rows: list[tuple[int, bytes]],
    ) -> tuple[dict[tuple[str, str], tuple[dict, dict]], set[str]]:
        """Build compacted event dict from raw log rows.

        Returns (latest, tombstoned) where:
          latest: {(prim, kind) → (event_dict, metadata_dict)}
          tombstoned: set of prim paths deleted/renamed
        """
        tombstoned: set[str] = set()
        latest: dict[tuple[str, str], tuple[dict, dict]] = {}
        for _seq, record_bin in rows:
            UsdSyncServer._merge_event(latest, tombstoned, record_bin)
        return latest, tombstoned

    def _commit_compaction(
        self,
        latest: dict[tuple[str, str], tuple[dict, dict]],
        original_count: int,
    ):
        """Commit compacted state: rewrite store, reset seqs, resync receivers.

        Must be called under exclusive txn_barrier.
        """
        sorted_entries = sorted(
            latest.values(),
            key=lambda entry: (
                entry[0]["prim"].count("/"),
                entry[0]["prim"],
                EVENT_KIND_ORDER[entry[0]["k"]],
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

        # Rebuild incremental prim tracking from compacted state.
        self._prim_paths.clear()
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
        self._prim_paths.clear()
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

        record_blobs = self.store.search_like_decoded(
            lambda d: d.get("event", d).get("prim", "").startswith(prefix)
        )

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
            key=lambda e: (e[0]["prim"], EVENT_KIND_ORDER[e[0]["k"]]),
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
        """Send payload to matching receivers, removing dead ones."""
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
        if dead:
            with self.clients_lock:
                for h in dead:
                    self.receivers.discard(h)

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

    # Maps event kind -> list of attribute names (or 'meta:active' for
    # prim metadata) that the event writes.  Used by apply_txn to check
    # per-attribute whether the target layer's opinion is the winner.
    # Events not in this map are always broadcast (conservative default).
    _EVENT_ATTR_MAP: dict[str, list[str]] = {
        K_ENSURE_PRIM: [],  # structural — check prim definer
        K_ENSURE_XFORM_OPS: [],
        K_SET_XFORM_TRS: [
            "xformOp:translate",
            "xformOp:orient",
            "xformOp:scale",
        ],
        K_SET_XFORM_MATRICES: [],
        K_SET_VISIBILITY: ["visibility"],
        K_DEACTIVATE_PRIM: ["meta:active"],
        K_DELETE_PRIM: [],  # structural
        K_RENAME_PRIM: [],  # structural
        K_SET_MATERIAL_BINDING: ["rel:material:binding"],
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
            apply_events(self.stage, events, op_cache=self.op_cache)

            # Cache winning results per (prim_path, event_kind) — the
            # layer stack is frozen for the duration of this txn so the
            # result is stable across events touching the same prim.
            winning_cache: dict[tuple[str, str], bool] = {}

            for i, ev in enumerate(events):
                k = ev.get("k")
                if k in (K_ENSURE_PRIM, K_DELETE_PRIM, K_RENAME_PRIM):
                    self._prim_count_dirty = True
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
        """
        prims = dict(self._prim_paths)  # snapshot

        result = []
        for path in sorted(prims):
            parent = path.rsplit("/", 1)[0] or "/"
            depth = path.count("/")
            has_children = any(
                p.startswith(path + "/") and p.count("/") == depth + 1 for p in prims
            )
            result.append(
                {
                    "path": path,
                    "typeName": prims[path],
                    "parent": parent,
                    "depth": depth,
                    "has_children": has_children,
                }
            )
        return result

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
