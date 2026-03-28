"""Blender receiver — applies incoming network events to Blender objects.

Uses ReceiverThread from openusdconnect.receiver and drains the queue
on the main thread via bpy.app.timers.
"""

from __future__ import annotations

import json
import logging
import os

try:
    import bpy
    from bpy.props import BoolProperty, IntProperty, StringProperty

    BPY_AVAILABLE = True
except Exception:
    BPY_AVAILABLE = False

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


def _process_event(ev: dict):
    """Dispatch a single event to the BlenderAdapter.

    NOTE: caller is responsible for setting _APPLYING_REMOTE around the
    entire batch — this function only dispatches, it does not toggle the
    feedback guard.
    """
    global _ADAPTER
    if _ADAPTER is None:
        up_axis = "Y"  # default — most USD scenes are Y-up
        cap = _get_capture_mod()
        if cap is not None and getattr(cap, "_state", None) is not None:
            author = getattr(cap._state, "author", None)
            if author is not None:
                from pxr import UsdGeom

                up_axis = UsdGeom.GetStageUpAxis(author.stage)
        _ADAPTER = BlenderAdapter(scene_up_axis=up_axis)

    k = ev.get("k")
    prim_path = ev.get("prim", "")
    extra = ""
    if k == K_SET_VISIBILITY:
        extra = f" visible={ev.get('visible')}"
    elif k == K_DEACTIVATE_PRIM:
        extra = f" active={ev.get('active')}"
    LOG.debug("event: k=%s prim=%s%s", k, prim_path, extra)

    # Skip adapter dispatch for variant/reference events that haven't
    # changed — prevents unnecessary remove + re-import on resync.
    if k in (K_SET_VARIANT_SELECTIONS, K_SET_REFERENCE):
        cap = _get_capture_mod()
        if cap is not None and cap._state.author is not None:
            stage = cap._state.author.stage
            if not _arc_changed(stage, ev, k):
                LOG.debug("skip adapter dispatch %s %s (unchanged)", k, prim_path)
                return

    _dispatch_event(_ADAPTER, k, prim_path, ev)

    # Track import events for post-batch cache seeding.  Must happen here
    # (before the emitter stage sync block) because the sync block can
    # return early when the payload is already loaded on the emitter's
    # stage (USD auto-loads payloads with the default LoadAll policy).
    if k in (K_LOAD_PAYLOAD, K_SET_REFERENCE):
        _pending_seed_paths.add(prim_path)

    # Keep the emitter's stage in sync with payload and reference arcs so
    # its composed view matches what the receiver imported/removed.  This
    # mirrors how the base-file case works: the emitter's stage has the
    # composition arc, so children compose naturally when loaded.
    if k in (
        K_SET_PAYLOAD, K_LOAD_PAYLOAD, K_UNLOAD_PAYLOAD,
        K_SET_REFERENCE, K_SET_VARIANT_SELECTIONS,
    ):
        cap = _get_capture_mod()
        if cap is not None and cap._state.author is not None:
            from openusdconnect.event_apply import apply_event as _apply_ev

            try:
                stage = cap._state.author.stage

                # Skip re-applying composition arcs that already match
                # the emitter's stage — avoids unnecessary recomposition
                # notices that trigger the emitter to re-emit everything.
                if k in (K_SET_REFERENCE, K_SET_PAYLOAD, K_SET_VARIANT_SELECTIONS):
                    if not _arc_changed(stage, ev, k):
                        LOG.debug("skip sync %s %s", k, prim_path)
                        return
                if k == K_LOAD_PAYLOAD:
                    prim = stage.GetPrimAtPath(prim_path)
                    if prim and prim.IsValid() and prim.IsLoaded():
                        LOG.debug("skip sync %s %s (loaded)", k, prim_path)
                        return
                LOG.debug("apply to emitter: %s %s", k, prim_path)
                _apply_ev(stage, ev)

                ne = cap._state.notice_emitter
                prefix = prim_path + "/"

                if k == K_UNLOAD_PAYLOAD:
                    # Track unloaded payload root so emitter skips children
                    cap._state.author._unloaded_payload_roots.add(prim_path)
                    # Purge emitter caches for unloaded children so they're
                    # treated as fresh first-encounters after the next load.
                    if ne is not None:
                        to_purge = [p for p in list(ne._known_prims) if p.startswith(prefix)]
                        for p in to_purge:
                            ne._purge_caches(p)

                elif k == K_LOAD_PAYLOAD:
                    cap._state.author._unloaded_payload_roots.discard(prim_path)
                    # Clear stale SetActive(False) opinions left by
                    # _detect_deletions during the previous unload cycle.
                    from pxr import Usd

                    prim = stage.GetPrimAtPath(prim_path)
                    if prim and prim.IsValid():
                        for child in Usd.PrimRange(prim, Usd.PrimAllPrimsPredicate):
                            if not child.IsActive():
                                child.SetActive(True)

                elif k == K_SET_REFERENCE:
                    # Update emitter's caches so it doesn't re-emit
                    # the reference or composed variant selections.
                    if ne is not None:
                        from pxr import Usd  # noqa: E402

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
                        # Also cache variants for composed children
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
                    # Update the emitter's variant cache so it doesn't
                    # re-emit the variant change on the next dirty cycle.
                    if ne is not None:
                        pc = ne._prim_cache.setdefault(prim_path, {})
                        from openusdconnect.emitter import (
                            _C_VARIANT_SELECTIONS,
                            _read_variant_selections,
                        )
                        pc[_C_VARIANT_SELECTIONS] = _read_variant_selections(
                            stage, prim_path,
                        )
                        # Purge known prims for children so re-imported
                        # objects are treated as fresh first-encounters.
                        to_purge = [
                            p for p in list(ne._known_prims)
                            if p.startswith(prefix)
                        ]
                        for p in to_purge:
                            ne._purge_caches(p)
                    # Purge BlenderStageAuthor refs for old children
                    cap._state.author.purge_prim_refs(prefix)

            except RuntimeError:
                LOG.warning("Could not apply %s to emitter stage for %s", k, prim_path)

    # Sync incoming shader/material events to the emitter's stage so
    # the emitter's diff baseline matches the latest network state.
    if k in (K_SET_SHADER_INPUT, K_SET_SHADER_CONNECTION, K_SET_MATERIAL_BINDING):
        cap = _get_capture_mod()
        if cap is not None and cap._state.author is not None:
            from openusdconnect.event_apply import apply_event as _apply_ev

            stage = cap._state.author.stage
            ne = cap._state.notice_emitter
            _apply_ev(stage, ev)
            if ne is not None and k == K_SET_SHADER_INPUT:
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


def _drain_and_process(lines):
    """Parse, deduplicate, and apply a batch of raw JSON lines."""
    global _LAST_SEQ, _ADAPTER
    for raw_line in lines:
        try:
            msg = json.loads(raw_line)

            if msg.get("type") == MSG_RESYNC:
                LOG.info("Server requested resync — resetting sequence and adapter")
                _LAST_SEQ = 0
                _ADAPTER = None
                continue

            seq = msg.get("seq")
            if seq is not None:
                seq_int = int(seq)
                if seq_int <= _LAST_SEQ:
                    continue
                _LAST_SEQ = seq_int
            if msg.get("type") == MSG_EVENT:
                _process_event(msg.get("event", {}))
        except Exception:
            LOG.exception("Error processing received line")


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
    """Drain receiver queue on Blender main thread."""
    if _RECEIVER is None:
        return None  # Unregister timer

    lines = _RECEIVER.drain_queue()
    if not lines:
        return 0.01

    # Hold the feedback guard for the entire batch INCLUDING view_layer.update()
    # so the depsgraph handler doesn't echo received changes back to the server.
    _set_applying_remote(True)
    try:
        _drain_and_process(lines)

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
            cap = _get_capture_mod()
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
            _RECEIVER = ReceiverThread(
                host=host, port=port, sync_from=sync_from,
                client_id=f"blender-receiver-{os.getpid()}",
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
            _drain_and_process(receiver.drain_queue())

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
