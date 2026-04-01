"""Authoritative TCP sequencer server.

Maintains an in-memory Usd.Stage, accepts transactions from emitters,
applies them atomically, assigns monotonic sequence numbers, broadcasts
to all connected receivers, and logs events to a SQLite database for replay.

CLI usage:
    python -m openusdconnect.server --port 7200 --base test_scene.usda --log events.db
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import queue
import signal
import socketserver
import threading
import time
from dataclasses import dataclass, field

from pxr import Sdf, Usd, UsdGeom

from .emitter import as_matrix, decompose_trs_from_matrix
from .event_store import EventStore, SqliteEventStore
from .protocol import (
    EVENT_KIND_ORDER,
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_SET_GPRIM_ATTRS,
    K_SET_MATERIAL_BINDING,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_SHADER_CONNECTION,
    K_SET_SHADER_INPUT,
    K_SET_VARIANT_SELECTIONS,
    K_SET_VISIBILITY,
    K_SET_XFORM_MATRICES,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
    MSG_AUTH_REJECTED,
    MSG_COMPACT,
    MSG_CREATE_PROPOSAL,
    MSG_EVENT,
    MSG_HELLO,
    MSG_HELLO_OK,
    MSG_PROPOSAL_CREATED,
    MSG_QUIT,
    MSG_RESYNC,
    MSG_TXN,
)

LOG = logging.getLogger(__name__)

# Event kinds where only the latest event per prim matters.
LATEST_WINS_KINDS = frozenset({
    K_SET_XFORM_TRS,
    K_SET_XFORM_MATRICES,
    K_SET_VISIBILITY,
    K_SET_REFERENCE,
    K_SET_PAYLOAD,
    K_SET_VARIANT_SELECTIONS,
    K_SET_MATERIAL_BINDING,
    K_SET_SHADER_CONNECTION,
    K_DEACTIVATE_PRIM,
})


@dataclass
class ClientInfo:
    """Metadata for a connected client (emitter or receiver)."""

    role: str
    address: tuple
    client_id: str | None = None
    origin: str | None = None
    department: str | None = None
    connected_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    event_count: int = 0


@dataclass
class Proposal:
    """Metadata for a cross-department edit proposal."""

    proposal_id: str
    from_client: str
    from_department: str | None
    target_department: str
    description: str
    layer: Sdf.Layer
    status: str = "pending"  # pending, approved, rejected
    created_at: float = field(default_factory=time.time)
    events: list = field(default_factory=list)  # accumulated events for log persistence


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
        self._dept_layers: dict[str, Sdf.Layer] = {}   # department → shared layer
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

        # Pluggable event store — defaults to SQLite
        self.store: EventStore = event_store or SqliteEventStore(log_path)
        self._next_seq = self.store.get_max_seq() + 1
        self._event_count = self.store.get_count()

        # TOFU token authentication
        self.require_token = require_token
        self.token_store = None
        if require_token:
            from .token_store import TokenStore
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
        self._broadcast_queue: queue.Queue = queue.Queue()
        self._broadcast_thread = threading.Thread(
            target=self._broadcast_loop, daemon=True,
        )
        self._broadcast_thread.start()

        # Durability mode:
        #   "strict"   — persist to DB before broadcast (no lost events on crash)
        #   "realtime" — broadcast first, persist async (lower latency)
        self.durability = durability
        self._persist_queue: queue.Queue | None = None
        if durability == "realtime":
            self._persist_queue = queue.Queue()
            self._persist_thread = threading.Thread(
                target=self._persist_loop, daemon=True,
            )
            self._persist_thread.start()

        # LRU cache: prim_path → (translate_op, orient_op, scale_op).
        from cachetools import LRUCache
        self._op_cache: LRUCache = LRUCache(
            maxsize=op_cache_size or self.DEFAULT_OP_CACHE_SIZE,
        )

        # Rebuild stage from the event log so the composed stage matches
        # what receivers would get on replay.
        self._replay_log_into_stage()

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

    def _replay_log_into_stage(self):
        """Apply all events from the event store to restore stage on startup.

        Routes events to per-client layers based on stored client_id.
        Events without a client_id go to the shared edit_layer.
        """
        from .event_apply import apply_events

        rows = self.store.get_all_asc()
        if not rows:
            return

        if self.department_priority:
            # Per-client layers: route events to the correct client layer.
            by_client: dict[str | None, list[dict]] = {}
            for _seq, record_json in rows:
                rec = json.loads(record_json)
                ev = rec.get("event", rec)
                cid = rec.get("client_id")
                by_client.setdefault(cid, []).append(ev)

            for cid, evts in by_client.items():
                layer = self.get_or_create_client_layer(cid) if cid else self.edit_layer
                self.stage.SetEditTarget(Usd.EditTarget(layer))
                apply_events(self.stage, evts, op_cache=self._op_cache)

            total = sum(len(v) for v in by_client.values())
        else:
            # Legacy mode: all events to the shared edit_layer.
            events = []
            for _seq, record_json in rows:
                rec = json.loads(record_json)
                events.append(rec.get("event", rec))
            apply_events(self.stage, events, op_cache=self._op_cache)
            total = len(events)

        LOG.info("Restored stage from event log: %d events", total)

    # -- TOFU authentication -------------------------------------------

    def authenticate(self, client_id: str | None, token: str | None,
                     department: str | None = None) -> tuple[bool, str | None]:
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
        self, from_client: str, target_department: str, description: str = "",
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
            proposal_id, from_client, target_department, description,
        )
        return proposal_id

    def list_proposals(
        self, department: str | None = None, include_usda: bool = True,
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
                proposal_id, p.target_department,
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
            for ev in p.events:
                rec = {
                    "type": MSG_EVENT,
                    "seq": self.assign_seq(),
                    "event": ev,
                    "client_id": p.from_client,
                    "origin": f"proposal-{p.proposal_id}",
                }
                records.append(rec)
            self.append_log_batch(records)
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
        self, client_id: str, department: str | None = None,
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
        return (
            self._dept_layers.get(key)
            or self.client_layers.get(key)
        )

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
            result.append({
                "department": dept,
                "clients": clients,
                "identifier": layer.identifier,
                "muted": layer.identifier in muted,
            })

        # Non-department client layers
        for cid, layer in client_items:
            if layer.identifier in seen:
                continue
            seen.add(layer.identifier)
            result.append({
                "department": None,
                "clients": [cid],
                "identifier": layer.identifier,
                "muted": layer.identifier in muted,
            })

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
        """
        rows = self.store.get_all_asc()
        if not rows:
            return

        tombstoned: set[str] = set()
        # (ev, metadata) tuples where metadata holds origin/client/client_id.
        latest: dict[tuple[str, str], tuple[dict, dict]] = {}

        for _seq, event_json in rows:
            rec = json.loads(event_json)
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
                latest = {key: val for key, val in latest.items() if key[0] != prim}
                latest[(prim, k)] = (ev, meta)
                continue

            if prim in tombstoned:
                continue

            # load/unload are mutually exclusive — only the last one wins.
            if k == K_LOAD_PAYLOAD:
                latest.pop((prim, K_UNLOAD_PAYLOAD), None)
                latest[(prim, k)] = (ev, meta)
                continue
            if k == K_UNLOAD_PAYLOAD:
                latest.pop((prim, K_LOAD_PAYLOAD), None)
                latest[(prim, k)] = (ev, meta)
                continue

            if k == K_SET_XFORM_TRS:
                existing = latest.get((prim, k))
                if existing:
                    prev = existing[0]
                    for field in ("t", "r", "s"):
                        if field in ev.get("fields", []):
                            prev[field] = ev[field]
                            if field not in prev["fields"]:
                                prev["fields"].append(field)
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
            elif k == K_SET_SHADER_INPUT:
                existing = latest.get((prim, k))
                if existing:
                    prev = existing[0]
                    prev.setdefault("inputs", {}).update(ev.get("inputs", {}))
                    prev.setdefault("input_types", {}).update(
                        ev.get("input_types", {}),
                    )
                    if ev.get("shader_id"):
                        prev["shader_id"] = ev["shader_id"]
                    latest[(prim, k)] = (prev, meta)
                else:
                    latest[(prim, k)] = (ev, meta)
            else:
                latest[(prim, k)] = (ev, meta)

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
            records.append((seq, json.dumps(rec), meta.get("client_id")))
        self.store.clear_and_rewrite(records)
        self._event_count = len(records)

        self._op_cache.clear()
        LOG.info("Compacted event log: %d -> %d events", len(rows), len(sorted_entries))

        # Tell connected receivers to reset and replay from the compacted log.
        self.broadcast({"type": MSG_RESYNC, "reason": "compact"})
        with self.clients_lock:
            for handler in self.receivers:
                self.replay_from(handler, 1)

    def purge(self):
        """Clear all events, reset the edit layer, and resync receivers.

        Wipes the event store, clears all opinions from the server's edit
        layer (restoring the base scene), resets the sequence counter, and
        sends a resync to all connected receivers so they start fresh.
        """
        self.store.clear_and_rewrite([])
        self._event_count = 0
        with self._seq_lock:
            self._next_seq = 1
        with self.stage_lock:
            self.edit_layer.Clear()
        self._op_cache.clear()
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
                    "k": K_SET_XFORM_TRS, "prim": pp,
                    "fields": ["t", "r", "s"], "t": t, "r": r, "s": s,
                }

            if k == K_SET_VISIBILITY:
                img = UsdGeom.Imageable(prim)
                vis = img.GetVisibilityAttr().Get()
                return {
                    "k": K_SET_VISIBILITY, "prim": pp,
                    "visible": vis != "invisible",
                }

            if k == K_DEACTIVATE_PRIM:
                return {
                    "k": K_DEACTIVATE_PRIM, "prim": pp,
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
        self.store.append(rec["seq"], json.dumps(rec))
        self._event_count += 1

    def append_log_batch(self, records: list[dict]):
        """Persist multiple event records.

        In strict mode, writes synchronously (caller blocks until DB commit).
        In realtime mode, enqueues for async write (caller returns immediately).
        """
        if self._persist_queue is not None:
            self._persist_queue.put(records)
        else:
            tuples = [
                (rec["seq"], json.dumps(rec), rec.get("client_id"))
                for rec in records
            ]
            self.store.append_batch(tuples)
            self._event_count += len(records)

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
            K_ENSURE_PRIM, K_ENSURE_XFORM_OPS, K_SET_XFORM_TRS, K_SET_VISIBILITY,
            K_SET_MATERIAL_BINDING, K_SET_SHADER_INPUT, K_SET_SHADER_CONNECTION,
        }

        record_jsons = self.store.search_like(f'%"prim": "{prefix}%')

        # Collect the latest event of each relevant kind per child prim.
        # Store (ev, origin) tuples so replayed broadcasts can suppress
        # echo back to the original sender.
        latest: dict[tuple[str, str], tuple[dict, str | None]] = {}
        for event_json in record_jsons:
            rec = json.loads(event_json)
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
        self, address: tuple, role: str,
        client_id: str | None = None, origin: str | None = None,
        department: str | None = None,
    ):
        """Register a connected client for tracking."""
        key = f"{address[0]}:{address[1]}"
        with self.clients_lock:
            self.clients[key] = ClientInfo(
                role=role, address=address, client_id=client_id,
                origin=origin, department=department,
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
        self, records: list[dict], exclude_origin: str | None = None,
    ):
        """Enqueue records for async broadcast to all receivers.

        The actual network sends happen on the dedicated broadcast thread,
        so the calling emitter thread is never blocked by slow receivers.
        """
        if not records:
            return
        payload = "".join(
            json.dumps(rec) + "\n" for rec in records
        ).encode("utf-8")
        self._broadcast_queue.put((payload, exclude_origin, None))
        # Notify event listeners synchronously — these are in-process
        # callbacks (e.g. dashboard) that are fast and must see events
        # in order.
        for listener in list(self._event_listeners):
            for rec in records:
                try:
                    listener(rec)
                except Exception:
                    LOG.debug("Event listener failed, removing")
                    self._event_listeners.remove(listener)
                    break

    def send_to_origin(self, rec: dict, origin: str):
        """Enqueue a record for async send to receivers matching an origin."""
        line = (json.dumps(rec) + "\n").encode("utf-8")
        self._broadcast_queue.put((line, None, origin))

    def _broadcast_loop(self):
        """Dedicated thread: drain the broadcast queue and send to receivers."""
        while True:
            payload, exclude_origin, target_origin = self._broadcast_queue.get()
            try:
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
                        h.request.sendall(payload)
                    except OSError:
                        LOG.debug("Broadcast failed for %s, marking as dead",
                                  h.client_address)
                        dead.append(h)
                if dead:
                    with self.clients_lock:
                        for h in dead:
                            self.receivers.discard(h)
            except Exception:
                LOG.exception("Unexpected error in broadcast loop")
            finally:
                self._broadcast_queue.task_done()

    def _persist_loop(self):
        """Dedicated thread for realtime durability: drain persistence queue
        and write to SQLite without blocking emitter threads."""
        while True:
            records = self._persist_queue.get()
            try:
                tuples = [
                    (rec["seq"], json.dumps(rec), rec.get("client_id"))
                    for rec in records
                ]
                self.store.append_batch(tuples)
                self._event_count += len(records)
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
            "xformOp:translate", "xformOp:orient", "xformOp:scale",
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
                    prim_path, target, "active",
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
        self, prim_path: str, target, field: str,
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
        """
        from .event_apply import apply_events

        target = layer or self.edit_layer

        # Phase 1: Mutate stage (must be locked — SetEditTarget is global).
        with self.stage_lock:
            self.stage.SetEditTarget(Usd.EditTarget(target))
            apply_events(self.stage, events, op_cache=self._op_cache)

        # Phase 2: Strength checking (separate lock scope so reads can
        # interleave between mutation and strength-check).
        changed_indices = []
        with self.stage_lock:
            for i, ev in enumerate(events):
                k = ev.get("k")
                if k in (K_ENSURE_PRIM, K_DELETE_PRIM):
                    self._prim_count_dirty = True
                pp = ev.get("prim", "")
                if not pp:
                    changed_indices.append(i)
                    continue
                prim = self.stage.GetPrimAtPath(pp)
                if not prim or not prim.IsValid():
                    changed_indices.append(i)
                    continue
                if self._is_layer_winning(prim, target, ev):
                    changed_indices.append(i)

        return changed_indices

    def get_prim_count(self) -> int:
        """Return the number of prims on the composed stage (thread-safe, cached)."""
        if self._prim_count_dirty:
            with self.stage_lock:
                self._prim_count = sum(1 for _ in self.stage.Traverse())
            self._prim_count_dirty = False
        return self._prim_count

    def get_tracked_prim_count(self) -> int:
        """Return the number of prims tracked in the event log.

        Counts distinct prim paths from ensure_prim events minus
        delete_prim tombstones.
        """
        rows = self.store.get_all_asc()
        prims: set[str] = set()
        for _seq, record_json in rows:
            rec = json.loads(record_json)
            ev = rec.get("event", {})
            k = ev.get("k")
            path = ev.get("prim", "")
            if k == K_ENSURE_PRIM:
                prims.add(path)
            elif k == K_DELETE_PRIM:
                prims.discard(path)
        return len(prims)

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
        record_jsons, count = self.store.query(
            offset=offset, limit=limit,
            kind=kind, prim_contains=prim_contains,
        )
        return [json.loads(r) for r in record_jsons], count

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
        """Reconstruct the prim tree from the event log.

        Reads ensure_prim and delete_prim events from the store — no
        stage access or stage_lock needed.
        """
        rows = self.store.get_all_asc()
        prims: dict[str, str] = {}  # path → typeName
        for _seq, record_json in rows:
            rec = json.loads(record_json)
            ev = rec.get("event", {})
            k = ev.get("k")
            path = ev.get("prim", "")
            if k == K_ENSURE_PRIM:
                prims[path] = ev.get("typeName", "Xform")
            elif k == K_DELETE_PRIM:
                prims.pop(path, None)

        # Build tree structure from flat paths
        result = []
        for path in sorted(prims):
            parent = path.rsplit("/", 1)[0] or "/"
            depth = path.count("/")
            has_children = any(
                p.startswith(path + "/") and p.count("/") == depth + 1
                for p in prims
            )
            result.append({
                "path": path,
                "typeName": prims[path],
                "parent": parent,
                "depth": depth,
                "has_children": has_children,
            })
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
        """
        try:
            record_jsons = self.store.get_from_seq(seq_start)
            for record_json in record_jsons:
                handler.request.sendall((record_json + "\n").encode("utf-8"))
        except Exception:
            LOG.exception("Failed to replay events")


class ConnectionHandler(socketserver.StreamRequestHandler):
    """Handles a single client connection (emitter or receiver)."""

    server: ThreadedTCPServer

    def handle(self):
        sync_server = self.server.sync_server

        # Read hello
        line = self.rfile.readline()
        if not line:
            return
        try:
            hello = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            LOG.warning("Failed to parse hello message: %s", e)
            return

        if hello.get("type") != MSG_HELLO:
            return

        role = hello.get("role")
        client_id = hello.get("client_id")
        self._origin = hello.get("origin")
        self._department = hello.get("department")
        self._client_id = client_id
        self._addr_key = f"{self.client_address[0]}:{self.client_address[1]}"

        # TOFU authentication
        from .transport import send_line

        accepted, issued_token = sync_server.authenticate(
            client_id, hello.get("token"), self._department,
        )
        if not accepted:
            send_line(self.request, {
                "type": MSG_AUTH_REJECTED,
                "reason": "invalid or missing token",
            })
            LOG.warning("Rejected %s from %s", client_id, self.client_address)
            return

        # Send hello_ok with token (issued on first connect, None on reconnect)
        hello_ok = {"type": MSG_HELLO_OK}
        if issued_token:
            hello_ok["token"] = issued_token
        send_line(self.request, hello_ok)

        LOG.info(
            "Client connected: role=%s origin=%s dept=%s from %s",
            role, self._origin, self._department, self.client_address,
        )
        sync_server.register_client(
            self.client_address, role, client_id,
            origin=self._origin, department=self._department,
        )

        # Create per-client layer only when department ordering is enabled.
        # Without departments, all clients share edit_layer (last-write-wins).
        self._client_layer = None
        if role == "emitter" and client_id and sync_server.department_priority:
            self._client_layer = sync_server.get_or_create_client_layer(
                client_id, department=self._department,
            )

        if role == "receiver":
            sync_from = int(hello.get("sync_from", 1))

            # If sync_from is beyond the current log (e.g., after compaction
            # reset seq numbers), send resync so the receiver resets its
            # sequence counter, then replay the full log.
            max_seq = sync_server.store.get_max_seq()
            if sync_from > max_seq > 0:
                send_line(self.request, {"type": MSG_RESYNC, "reason": "seq_overflow"})
                sync_from = 1

            # Hold clients_lock during replay AND add to prevent race condition.
            # This blocks broadcasts during replay, ensuring no events slip through
            # the gap between replay finishing and being added to broadcast set.
            with sync_server.clients_lock:
                sync_server.replay_from(self, sync_from)
                sync_server.receivers.add(self)

        try:
            self._read_loop(sync_server)
        finally:
            with sync_server.clients_lock:
                sync_server.receivers.discard(self)
            sync_server.unregister_client(self.client_address)
            LOG.info("Client disconnected: %s", self.client_address)

    def _read_loop(self, sync_server: UsdSyncServer):
        while True:
            line = self.rfile.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                LOG.warning("Failed to parse message: %s", e)
                continue

            if msg.get("type") == MSG_QUIT:
                break

            if msg.get("type") == MSG_COMPACT:
                LOG.info("Compact requested by %s", self.client_address)
                sync_server.compact_log()
                continue

            if msg.get("type") == MSG_CREATE_PROPOSAL:
                self._handle_create_proposal(sync_server, msg)
                continue

            if msg.get("type") != MSG_TXN:
                continue

            events = msg.get("events", [])
            if not isinstance(events, list) or not events:
                continue

            # Check if txn targets a proposal (no broadcast, just apply to muted layer)
            proposal_id = msg.get("proposal_id")
            if proposal_id:
                self._handle_proposal_txn(sync_server, proposal_id, events)
                continue

            # Apply to the client's layer. Returns indices of events
            # that actually changed the composed view.
            changed = sync_server.apply_txn(events, layer=self._client_layer)
            changed_set = set(changed)

            # Sequence and persist ALL events (even overridden ones —
            # they're in the client's layer for mute/merge/review).
            records = []
            for ev in events:
                rec = {
                    "type": MSG_EVENT,
                    "seq": sync_server.assign_seq(),
                    "event": ev,
                    "client": self._addr_key,
                    "client_id": self._client_id,
                }
                if self._origin:
                    rec["origin"] = self._origin
                records.append(rec)
            sync_server.append_log_batch(records)

            # Broadcast events that changed the composed view to everyone
            # except the sender (origin suppression — sender already has
            # the correct value locally).
            # For overridden events, send a correction back to the sender
            # with the composed value so their DCC snaps to the authoritative
            # state.  This ensures flatten parity across all clients.
            changed_records = []
            for i, rec in enumerate(records):
                if i in changed_set:
                    changed_records.append(rec)
                else:
                    correction = sync_server.build_correction(events[i])
                    if correction:
                        correction_rec = {
                            "type": MSG_EVENT,
                            "seq": sync_server.assign_seq(),
                            "event": correction,
                        }
                        sync_server.send_to_origin(
                            correction_rec, self._origin,
                        )
            if changed_records:
                sync_server.broadcast_batch(
                    changed_records, exclude_origin=self._origin,
                )
            # Update client activity tracking.
            with sync_server.clients_lock:
                info = sync_server.clients.get(self._addr_key)
            if info:
                info.last_activity = time.time()
                info.event_count += len(events)

            # After load_payload, re-broadcast latest child state so
            # receivers re-apply authoritative TRS after re-import.
            for ev in events:
                if ev.get("k") == K_LOAD_PAYLOAD:
                    sync_server.replay_children_after_load(ev["prim"])

    def _handle_create_proposal(self, sync_server: UsdSyncServer, msg: dict):
        """Handle a create_proposal message from an emitter."""
        from .transport import send_line

        target = msg.get("target_department", "")
        desc = msg.get("description", "")
        if not target:
            return
        pid = sync_server.create_proposal(
            self._client_id or "", target, desc,
        )
        send_line(self.request, {
            "type": MSG_PROPOSAL_CREATED,
            "proposal_id": pid,
        })

    def _handle_proposal_txn(
        self, sync_server: UsdSyncServer, proposal_id: str, events: list[dict],
    ):
        """Apply a txn to a proposal's muted layer (no broadcast).

        Muted layers can't be SetEditTarget — temporarily unmute during
        the write, then re-mute so opinions stay invisible to composition.
        Events are accumulated on the proposal for log persistence on approval.
        """
        p = sync_server.proposals.get(proposal_id)
        if not p or p.status != "pending":
            return
        from .event_apply import apply_events

        with sync_server.stage_lock:
            sync_server.stage.UnmuteLayer(p.layer.identifier)
            sync_server.stage.SetEditTarget(Usd.EditTarget(p.layer))
            apply_events(sync_server.stage, events, op_cache=sync_server._op_cache)
            sync_server.stage.MuteLayer(p.layer.identifier)
        p.events.extend(events)
        LOG.debug("Applied %d events to proposal %s", len(events), proposal_id)


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True

    def __init__(self, server_address, handler_class, sync_server: UsdSyncServer):
        self.sync_server = sync_server
        super().__init__(server_address, handler_class)


def run_server(
    host: str = "127.0.0.1",
    port: int = 7200,
    base_usd_path: str | None = None,
    log_path: str = "usd_events.db",
    compact: bool = False,
    export_diff: str | None = None,
    dashboard_port: int | None = None,
    op_cache_size: int | None = None,
    department_priority: list[str] | None = None,
    require_token: bool = False,
    durability: str = "strict",
):
    """Start the server (blocking)."""
    sync_server = UsdSyncServer(
        base_usd_path=base_usd_path,
        log_path=log_path,
        op_cache_size=op_cache_size,
        department_priority=department_priority,
        require_token=require_token,
        durability=durability,
    )

    if compact:
        sync_server.compact_log()

    if dashboard_port:
        from integrations.dashboard import run_dashboard

        run_dashboard(sync_server, dashboard_port)
        LOG.info("Dashboard running on http://localhost:%d", dashboard_port)

    server = ThreadedTCPServer((host, port), ConnectionHandler, sync_server)

    _cleaned_up = False

    def _cleanup():
        nonlocal _cleaned_up
        if _cleaned_up:
            return
        _cleaned_up = True
        if export_diff:
            sync_server.export_edit_layer(export_diff)
        try:
            sync_server.store.close()
            LOG.info("Event store closed")
        except Exception:
            LOG.exception("Failed to close event store")

    atexit.register(_cleanup)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: server.shutdown())

    LOG.info("Server listening on %s:%s (PID %d) durability=%s",
             host, port, os.getpid(), sync_server.durability)
    LOG.info("Event log: %s", log_path)
    if base_usd_path:
        LOG.info("Base USD: %s", base_usd_path)
    if export_diff:
        LOG.info("Will export diff to %s on shutdown", export_diff)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Server shutting down")
    finally:
        server.shutdown()
        _cleanup()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    ap = argparse.ArgumentParser(description="OpenUSDConnect sync server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7200)
    ap.add_argument("--base", default=None, help="Base USD file to load")
    ap.add_argument("--log", default="usd_events.db", help="SQLite event log file path")
    ap.add_argument("--compact", action="store_true", help="Compact event log on startup")
    ap.add_argument(
        "--export-diff", default=None, metavar="PATH",
        help="Export the override layer as USDA on shutdown",
    )
    ap.add_argument(
        "--dashboard", type=int, default=None, metavar="PORT",
        help="Start admin dashboard on this port (e.g. --dashboard 8080)",
    )
    ap.add_argument(
        "--op-cache-size", type=int, default=None, metavar="N",
        help=f"Max xform op cache entries (default: {UsdSyncServer.DEFAULT_OP_CACHE_SIZE})",
    )
    ap.add_argument(
        "--departments", default=None, metavar="LIST",
        help="Comma-separated department priority (strongest first). "
             "Enables per-client layer ordering by department. "
             "Example: --departments lighting,fx,animation,layout",
    )
    ap.add_argument(
        "--require-token", action="store_true",
        help="Enable TOFU token authentication. Clients are issued a token "
             "on first connect and must present it on reconnect.",
    )
    ap.add_argument(
        "--durability", choices=["strict", "realtime"], default="strict",
        help="strict: persist to DB before broadcast (no lost events). "
             "realtime: broadcast first, persist async (lower latency).",
    )
    args = ap.parse_args()
    dept_list = args.departments.split(",") if args.departments else None
    run_server(
        host=args.host,
        port=args.port,
        base_usd_path=args.base,
        log_path=args.log,
        compact=args.compact,
        export_diff=args.export_diff,
        dashboard_port=args.dashboard,
        op_cache_size=args.op_cache_size,
        department_priority=dept_list,
        require_token=args.require_token,
        durability=args.durability,
    )


if __name__ == "__main__":
    main()
