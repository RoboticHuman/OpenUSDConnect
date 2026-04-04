"""Blender receiver — applies incoming network events to Blender objects.

Uses ReceiverThread from openusdconnect.receiver and drains the queue
on the main thread via bpy.app.timers.
"""

from __future__ import annotations

import logging

try:
    import bpy
    from bpy.props import BoolProperty, IntProperty, StringProperty

    BPY_AVAILABLE = True
except Exception:
    BPY_AVAILABLE = False

from openusdconnect.codec import (
    PayloadType,
    decode_envelope,
    event_to_dict,
    resolve_payload,
)
from openusdconnect.protocol import (
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
    MSG_EVENT,
    MSG_RESYNC,
)
from openusdconnect.receiver import ReceiverThread

from . import SESSION_ORIGIN as _ORIGIN
from .blender_adapter import BlenderAdapter

LOG = logging.getLogger(__name__)

_RECEIVER: ReceiverThread | None = None
_ADAPTER: BlenderAdapter | None = None
_QUEUE_TIMER_REGISTERED = False
# Prim paths that need emitter cache seeding after view_layer.update().
_pending_seed_paths: set[str] = set()
# Feedback loop guard: set True while applying remote events
_APPLYING_REMOTE = False
# Track last sequence number across reconnects
_LAST_SEQ: int = 0
# Lazy-cached reference to the capture module (avoids per-call import overhead)
_capture_mod = None


def _get_capture_mod():
    global _capture_mod
    if _capture_mod is None:
        try:
            from . import capture

            _capture_mod = capture
        except ImportError:
            pass
    return _capture_mod


def _ensure_scene_props():
    if not BPY_AVAILABLE:
        return
    S = bpy.types.Scene
    if not hasattr(S, "usd_connect_recv_host"):
        S.usd_connect_recv_host = StringProperty(name="Host", default="127.0.0.1")
    if not hasattr(S, "usd_connect_recv_port"):
        S.usd_connect_recv_port = IntProperty(name="Port", default=7200, min=1, max=65535)
    if not hasattr(S, "usd_connect_recv_running"):
        S.usd_connect_recv_running = BoolProperty(name="Receiver Running", default=False)
    if not hasattr(S, "usd_connect_recv_last_seq"):
        S.usd_connect_recv_last_seq = IntProperty(name="Last Sequence", default=0)


# Module-level dispatch table — method names resolved at call time.
_DISPATCH_TABLE: dict[str, str] = {
    K_ENSURE_PRIM: "ensure_prim",
    K_ENSURE_XFORM_OPS: "ensure_xform_ops",
    K_SET_XFORM_TRS: "set_xform_trs",
    K_SET_XFORM_MATRICES: "set_xform_matrices",
    K_DELETE_PRIM: "delete_prim",
    K_DEACTIVATE_PRIM: "deactivate_prim",
    K_RENAME_PRIM: "rename_prim",
    K_SET_VISIBILITY: "set_visibility",
    K_SET_GPRIM_ATTRS: "set_gprim_attrs",
    K_SET_REFERENCE: "set_reference",
    K_SET_VARIANT_SELECTIONS: "set_variant_selections",
    K_SET_PAYLOAD: "set_payload",
    K_LOAD_PAYLOAD: "load_payload",
    K_UNLOAD_PAYLOAD: "unload_payload",
    K_SET_MATERIAL_BINDING: "set_material_binding",
    K_SET_SHADER_INPUT: "set_shader_input",
    K_SET_SHADER_CONNECTION: "set_shader_connection",
}

# Per-event-type argument builders (returns kwargs for the adapter method).
def _dispatch_args(k: str, prim_path: str, ev: dict) -> tuple[tuple, dict]:
    """Return (args, kwargs) for the adapter method identified by *k*."""
    if k == K_ENSURE_PRIM:
        return (prim_path, ev.get("typeName", "Xform")), {}
    if k == K_DEACTIVATE_PRIM:
        return (prim_path, ev.get("active", False)), {}
    if k == K_RENAME_PRIM:
        return (prim_path, ev.get("new_name", "")), {}
    if k == K_SET_VISIBILITY:
        return (prim_path, ev.get("visible", True)), {}
    if k == K_SET_GPRIM_ATTRS:
        return (prim_path, ev.get("attrs", {})), {}
    if k == K_SET_REFERENCE:
        return (prim_path, ev.get("refs", [])), {}
    if k == K_SET_VARIANT_SELECTIONS:
        return (prim_path, ev.get("selections", {})), {}
    if k == K_SET_PAYLOAD:
        return (prim_path, ev.get("payloads", [])), {}
    if k == K_SET_MATERIAL_BINDING:
        return (prim_path, ev.get("material_path", "")), {}
    if k == K_SET_SHADER_INPUT:
        return (
            prim_path,
            ev.get("shader_id", ""),
            ev.get("inputs", {}),
            ev.get("input_types", {}),
        ), {}
    if k == K_SET_SHADER_CONNECTION:
        return (
            prim_path,
            ev.get("connections", {}),
            ev.get("disconnections", []),
        ), {}
    # set_xform_trs, set_xform_matrices, ensure_xform_ops, delete_prim
    if k in (K_SET_XFORM_TRS, K_SET_XFORM_MATRICES):
        return (prim_path, ev), {}
    return (prim_path,), {}


def _dispatch_event(adapter, k, prim_path, ev):
    """Route an event to the appropriate adapter method."""
    method_name = _DISPATCH_TABLE.get(k)
    if method_name is None:
        return
    method = getattr(adapter, method_name, None)
    if method is None:
        return
    args, kwargs = _dispatch_args(k, prim_path, ev)
    method(*args, **kwargs)


def _arc_changed(stage, ev, k):
    """Check if a composition arc event differs from the emitter's stage."""
    from openusdconnect.emitter import (
        _read_payloads,
        _read_references,
        _read_variant_selections,
    )

    prim_path = ev.get("prim", "")

    def _normalize_arcs(arcs):
        return [(p.replace("\\", "/"), r) for p, r in arcs]

    if k == K_SET_REFERENCE:
        current = _normalize_arcs(_read_references(stage, prim_path))
        incoming = _normalize_arcs([
            (e.get("asset_path", ""), e.get("prim_path", ""))
            for e in ev.get("refs", [])
        ])
        changed = current != incoming
        LOG.debug("arc ref %s changed=%s", prim_path, changed)
        return changed
    if k == K_SET_PAYLOAD:
        current = _normalize_arcs(_read_payloads(stage, prim_path))
        incoming = _normalize_arcs([
            (e.get("asset_path", ""), e.get("prim_path", ""))
            for e in ev.get("payloads", [])
        ])
        changed = current != incoming
        LOG.debug("arc payload %s changed=%s", prim_path, changed)
        return changed
    if k == K_SET_VARIANT_SELECTIONS:
        current = dict(_read_variant_selections(stage, prim_path))
        incoming = ev.get("selections", {})
        changed = current != incoming
        LOG.debug("arc variant %s changed=%s", prim_path, changed)
        return changed
    return True


def _ensure_adapter():
    """Lazily create the BlenderAdapter on first use."""
    global _ADAPTER
    if _ADAPTER is not None:
        return _ADAPTER
    up_axis = "Y"  # default — most USD scenes are Y-up
    cap = _get_capture_mod()
    if cap is not None and getattr(cap, "_state", None) is not None:
        author = getattr(cap._state, "author", None)
        if author is not None:
            from pxr import UsdGeom

            up_axis = UsdGeom.GetStageUpAxis(author.stage)
    _ADAPTER = BlenderAdapter(scene_up_axis=up_axis)
    return _ADAPTER


# Event types that are synced to the emitter's USD stage.
# Transforms go directly to the BlenderAdapter — the emitter stage
# only needs composition arcs and materials to keep its composed view
# in sync with what the receiver imported.
_STAGE_SYNC_KINDS = frozenset({
    K_SET_REFERENCE, K_SET_PAYLOAD, K_LOAD_PAYLOAD, K_UNLOAD_PAYLOAD,
    K_SET_VARIANT_SELECTIONS,
    K_SET_MATERIAL_BINDING, K_SET_SHADER_INPUT, K_SET_SHADER_CONNECTION,
})


def _commit_to_stage(events: list[dict], stage) -> None:
    """Apply composition arc and material events to the emitter's stage.

    Rolls back on failure via atomic_apply.  Also reactivates payload
    children that were deactivated during a previous unload cycle.
    """
    from pxr import Usd

    from openusdconnect.event_apply import apply_events, atomic_apply

    stage_events = [ev for ev in events if ev.get("k") in _STAGE_SYNC_KINDS]
    if not stage_events:
        return
    with atomic_apply(stage):
        apply_events(stage, stage_events)
        for ev in stage_events:
            if ev.get("k") != K_LOAD_PAYLOAD:
                continue
            prim = stage.GetPrimAtPath(ev["prim"])
            if prim and prim.IsValid():
                for child in Usd.PrimRange(prim, Usd.PrimAllPrimsPredicate):
                    if not child.IsActive():
                        child.SetActive(True)


def _dispatch_to_adapter(events: list[dict], skip_indices: set[int]) -> None:
    """Phase 4: Dispatch events to the BlenderAdapter.

    Only called after the stage commit succeeds.  Events whose index is
    in *skip_indices* (unchanged arcs) are skipped.
    """
    adapter = _ensure_adapter()
    for i, ev in enumerate(events):
        if i in skip_indices:
            continue
        k = ev.get("k")
        prim_path = ev.get("prim", "")
        LOG.debug("event: k=%s prim=%s", k, prim_path)
        _dispatch_event(adapter, k, prim_path, ev)
        # Track import events for post-batch cache seeding.
        if k in (K_LOAD_PAYLOAD, K_SET_REFERENCE):
            _pending_seed_paths.add(prim_path)


def _update_emitter_caches(events: list[dict], stage, skip_indices: set[int]) -> None:
    """Phase 5: Update emitter caches from committed stage state.

    Reads from the stage AFTER all events are committed, so the stage
    is in a consistent state.
    """
    cap = _get_capture_mod()
    if cap is None or cap._state.author is None:
        return
    ne = cap._state.notice_emitter

    for i, ev in enumerate(events):
        if i in skip_indices:
            continue
        k = ev.get("k")
        prim_path = ev.get("prim", "")
        prefix = prim_path + "/"

        if k == K_UNLOAD_PAYLOAD:
            cap._state.author._unloaded_payload_roots.add(prim_path)
            if ne is not None:
                to_purge = [p for p in list(ne._known_prims) if p.startswith(prefix)]
                for p in to_purge:
                    ne._purge_caches(p)

        elif k == K_LOAD_PAYLOAD:
            cap._state.author._unloaded_payload_roots.discard(prim_path)

        elif k == K_SET_REFERENCE:
            if ne is not None:
                from pxr import Usd

                from openusdconnect.emitter import (
                    _C_REFERENCES,
                    _C_VARIANT_SELECTIONS,
                    _read_references,
                    _read_variant_selections,
                )
                pc = ne._prim_cache.setdefault(prim_path, {})
                pc[_C_REFERENCES] = _read_references(stage, prim_path)
                pc[_C_VARIANT_SELECTIONS] = _read_variant_selections(
                    stage, prim_path,
                )
                prim = stage.GetPrimAtPath(prim_path)
                if prim and prim.IsValid():
                    for child in Usd.PrimRange(prim):
                        cp = str(child.GetPath())
                        if cp == prim_path:
                            continue
                        cvs = _read_variant_selections(stage, cp)
                        if cvs:
                            cpc = ne._prim_cache.setdefault(cp, {})
                            cpc[_C_VARIANT_SELECTIONS] = cvs

        elif k == K_SET_VARIANT_SELECTIONS:
            if ne is not None:
                from openusdconnect.emitter import (
                    _C_VARIANT_SELECTIONS,
                    _read_variant_selections,
                )
                pc = ne._prim_cache.setdefault(prim_path, {})
                pc[_C_VARIANT_SELECTIONS] = _read_variant_selections(
                    stage, prim_path,
                )
                to_purge = [
                    p for p in list(ne._known_prims)
                    if p.startswith(prefix)
                ]
                for p in to_purge:
                    ne._purge_caches(p)
            cap._state.author.purge_prim_refs(prefix)

        elif k == K_SET_SHADER_INPUT:
            if ne is not None:
                from openusdconnect.emitter import _C_SHADER_INPUTS, _read_shader_inputs

                pc = ne._prim_cache.setdefault(prim_path, {})
                sid, inps, _, _ = _read_shader_inputs(stage, prim_path)
                if sid:
                    pc[_C_SHADER_INPUTS] = {"shader_id": sid, "inputs": inps}


def _set_applying_remote(value: bool):
    """Set the feedback-loop guard on both receiver and emitter modules."""
    global _APPLYING_REMOTE
    _APPLYING_REMOTE = value
    cap = _get_capture_mod()
    if cap is not None:
        try:
            cap.set_emitter_feedback_guard(value)
        except Exception:
            pass


def _parse_events(lines) -> list[dict]:
    """Parse and deduplicate raw JSON lines into a list of event dicts.

    Handles resync messages (resets sequence and adapter).  Returns the
    collected events for the caller to process in phases.
    """
    global _LAST_SEQ, _ADAPTER
    events = []
    for raw_buf in lines:
        try:
            env = decode_envelope(raw_buf)
            pt = env.PayloadType()

            if pt == PayloadType.Resync:
                LOG.info("Server requested resync — resetting sequence and adapter")
                _LAST_SEQ = 0
                _ADAPTER = None
                events.clear()
                continue

            if pt == PayloadType.BroadcastEvent:
                _, be = resolve_payload(env)
                seq_int = be.Seq()
                if seq_int <= _LAST_SEQ:
                    continue
                _LAST_SEQ = seq_int
                ew = be.Event()
                if ew:
                    events.append(event_to_dict(ew))
        except Exception:
            LOG.exception("Error parsing received message")
    return events


def _seed_multi_node_shader_maps(cap, prim_path: str):
    """Copy multi-node shader input maps from adapter to author.

    The adapter caches ``input_map`` dicts (usd_name -> socket) during
    forward application.  The author needs them for reverse reading
    (Blender -> USD).  Also seeds ``_last_shader_values`` baseline so
    the first depsgraph tick doesn't emit all inputs as changes.
    """
    author = cap._state.author
    prefix = prim_path + "/"
    for shader_path, sc in _ADAPTER._registry.iter_shaders():
        if "input_map" not in sc:
            continue
        if shader_path != prim_path and not shader_path.startswith(prefix):
            continue
        author._shader_input_maps[shader_path] = sc["input_map"]
        shader_id = sc.get("shader_id")
        if not shader_id:
            continue
        mapper = author._shader_registry.get(shader_id)
        if mapper and mapper.is_multi_node:
            values = mapper.read_all_inputs(input_map=sc["input_map"])
            if values:
                author._last_shader_values[shader_path] = values
                LOG.debug(
                    "seeded shader map %s with %d inputs",
                    shader_path, len(values),
                )


def _process_queue_timer():
    """Drain receiver queue on Blender main thread.

    Stage-first architecture:
      Phase 1 — Parse JSON lines into event list
      Phase 2 — Collect skip decisions (unchanged arcs) from pre-commit state
      Phase 3 — Commit all events to the emitter's USD stage (atomic rollback)
      Phase 4 — Dispatch to BlenderAdapter (only after stage commit succeeds)
      Phase 5 — Update emitter caches from committed stage state
    """
    if _RECEIVER is None:
        return None  # Unregister timer

    lines = _RECEIVER.drain_queue()
    if not lines:
        return 0.01

    # Hold the feedback guard for the entire batch INCLUDING view_layer.update()
    # so the depsgraph handler doesn't echo received changes back to the server.
    _set_applying_remote(True)
    try:
        # Phase 1: Parse and collect events
        events = _parse_events(lines)
        if not events:
            return 0.01

        cap = _get_capture_mod()
        has_stage = (
            cap is not None
            and getattr(cap, "_state", None) is not None
            and cap._state.author is not None
        )
        stage = cap._state.author.stage if has_stage else None

        # Phase 2: Collect skip decisions BEFORE stage commit.
        # arc_changed reads pre-commit state to avoid unnecessary re-imports.
        # Unchanged arcs are excluded from stage commit too — ClearReferences()
        # followed by re-adding identical references triggers recomposition
        # even when the final state is identical.
        skip_indices: set[int] = set()
        if stage is not None:
            for i, ev in enumerate(events):
                k = ev.get("k")
                if k in (K_SET_VARIANT_SELECTIONS, K_SET_REFERENCE, K_SET_PAYLOAD):
                    if not _arc_changed(stage, ev, k):
                        skip_indices.add(i)

        # Phase 3: Commit stage-sync events to the emitter's USD stage
        # (atomic).  Unchanged arcs (skip_indices) are excluded to avoid
        # spurious recomposition from ClearReferences + re-add.
        if stage is not None:
            sync_events = [
                ev for i, ev in enumerate(events)
                if i not in skip_indices
            ]
            _commit_to_stage(sync_events, stage)

        # Phase 4: Dispatch to BlenderAdapter (stage is committed)
        _dispatch_to_adapter(events, skip_indices)

        # Phase 5: Update emitter caches from committed stage state
        if stage is not None:
            _update_emitter_caches(events, stage, skip_indices)

        # Refresh viewport once after processing all events (not per-event)
        try:
            if bpy.context.view_layer:
                bpy.context.view_layer.update()
        except RuntimeError:
            pass

        # Seed emitter caches AFTER view_layer.update() so matrices reflect
        # final evaluated state (parenting, constraints, etc.).  Seeding
        # before the update captures stale matrices that won't match the
        # next depsgraph evaluation, causing the emitter to re-process
        # every imported object.
        if _pending_seed_paths:
            if cap is not None:
                for pp in _pending_seed_paths:
                    cap.seed_emitter_caches_for_import(pp)
                # Seed multi-node shader input maps and baselines so the
                # emitter can read values back from multi-node networks.
                if cap._state.author is not None and _ADAPTER is not None:
                    for pp in _pending_seed_paths:
                        _seed_multi_node_shader_maps(cap, pp)
            _pending_seed_paths.clear()
    finally:
        _set_applying_remote(False)

    try:
        scene = bpy.context.scene
        if scene:
            scene.usd_connect_recv_last_seq = _LAST_SEQ
    except (AttributeError, RuntimeError):
        pass

    return 0.01  # Run again in 10ms


class USD_CONNECT_OT_start_receiver(bpy.types.Operator):
    bl_idname = "usd_connect.start_receiver"
    bl_label = "Start Receiver"
    bl_description = "Connect to sync server and start receiving transform events"

    def execute(self, context):
        global _RECEIVER, _QUEUE_TIMER_REGISTERED, _LAST_SEQ, _ADAPTER
        if _RECEIVER is not None:
            self.report({"INFO"}, "Receiver already running")
            return {"CANCELLED"}
        scene = context.scene
        host = scene.usd_connect_recv_host
        port = scene.usd_connect_recv_port

        # Load persisted sequence from scene property
        if _LAST_SEQ == 0 and hasattr(scene, "usd_connect_recv_last_seq"):
            _LAST_SEQ = scene.usd_connect_recv_last_seq

        # Request replay from last known sequence + 1 (or 1 if never connected)
        sync_from = _LAST_SEQ + 1 if _LAST_SEQ > 0 else 1

        # Full replay — reset adapter so all caches (including _imported_refs) are clean
        if sync_from == 1:
            _ADAPTER = None

        try:
            from . import STABLE_CLIENT_ID

            _RECEIVER = ReceiverThread(
                host=host, port=port, sync_from=sync_from,
                client_id=STABLE_CLIENT_ID,
                origin=_ORIGIN,
            )
            _RECEIVER.start()
            if not _QUEUE_TIMER_REGISTERED:
                bpy.app.timers.register(_process_queue_timer, first_interval=0.01)
                _QUEUE_TIMER_REGISTERED = True
            scene.usd_connect_recv_running = True
            self.report({"INFO"}, f"Receiver started ({host}:{port}), sync from seq={sync_from}")
        except Exception as e:
            self.report({"ERROR"}, f"Failed to start receiver: {e}")
            return {"CANCELLED"}
        return {"FINISHED"}


class USD_CONNECT_OT_stop_receiver(bpy.types.Operator):
    bl_idname = "usd_connect.stop_receiver"
    bl_label = "Stop Receiver"

    def execute(self, context):
        global _RECEIVER, _QUEUE_TIMER_REGISTERED, _LAST_SEQ
        if _RECEIVER is not None:
            # Grab reference and null out global FIRST to stop timer from processing
            receiver = _RECEIVER
            _RECEIVER = None  # Timer will exit on next tick

            # Stop receiver thread (stops adding to queue)
            receiver.stop()
            try:
                receiver.join(timeout=2.0)
            except Exception:
                pass

            # NOW drain any remaining queued events (thread dead, timer stopped)
            _parse_events(receiver.drain_queue())

            # Persist to scene property
            try:
                context.scene.usd_connect_recv_last_seq = _LAST_SEQ
            except Exception:
                pass
        _QUEUE_TIMER_REGISTERED = False
        context.scene.usd_connect_recv_running = False
        self.report({"INFO"}, f"Receiver stopped at seq={_LAST_SEQ}")
        return {"FINISHED"}


class USD_CONNECT_OT_reset_receiver_seq(bpy.types.Operator):
    bl_idname = "usd_connect.reset_receiver_seq"
    bl_label = "Reset Seq"
    bl_description = "Reset sequence counter to force full replay on next connect"

    def execute(self, context):
        global _LAST_SEQ, _ADAPTER
        _LAST_SEQ = 0
        _ADAPTER = None
        try:
            context.scene.usd_connect_recv_last_seq = 0
        except Exception:
            pass
        self.report({"INFO"}, "Receiver sequence reset to 0")
        return {"FINISHED"}


_RECEIVER_CLASSES = (
    USD_CONNECT_OT_start_receiver,
    USD_CONNECT_OT_stop_receiver,
    USD_CONNECT_OT_reset_receiver_seq,
)


def register():
    _ensure_scene_props()
    if BPY_AVAILABLE:
        for c in _RECEIVER_CLASSES:
            bpy.utils.register_class(c)


def unregister():
    global _RECEIVER, _QUEUE_TIMER_REGISTERED
    if _RECEIVER is not None:
        _RECEIVER.stop()
        _RECEIVER = None
    _QUEUE_TIMER_REGISTERED = False
    if BPY_AVAILABLE:
        for c in reversed(_RECEIVER_CLASSES):
            bpy.utils.unregister_class(c)
    recv_props = (
        "usd_connect_recv_host",
        "usd_connect_recv_port",
        "usd_connect_recv_running",
        "usd_connect_recv_last_seq",
    )
    for prop_name in recv_props:
        if hasattr(bpy.types.Scene, prop_name):
            try:
                delattr(bpy.types.Scene, prop_name)
            except Exception:
                pass
