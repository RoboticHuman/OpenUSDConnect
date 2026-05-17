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

from openusdconnect.codec import decode_messages
from openusdconnect.dispatcher import EventDispatcher
from openusdconnect.receiver import ReceiverThread

from . import SESSION_ORIGIN as _ORIGIN
from .blender_adapter import BlenderAdapter

LOG = logging.getLogger(__name__)

_RECEIVER: ReceiverThread | None = None
_DISPATCHER: EventDispatcher | None = None
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
                    shader_path,
                    len(values),
                )


def _on_imported(prim_paths: list[str]) -> None:
    """Defer emitter cache seeding until after view_layer.update() runs.

    The dispatcher fires this inside its suppress block, before any
    Blender post-tick evaluation.  Stash the paths and seed them later
    in the timer cycle (after view_layer.update) so matrices reflect
    the final evaluated state.
    """
    _pending_seed_paths.update(prim_paths)


def _on_resync() -> None:
    """Reset the adapter so all caches (including _imported_refs) are clean."""
    global _ADAPTER
    LOG.info("Server requested resync — resetting adapter")
    _ADAPTER = None
    if _DISPATCHER is not None:
        _DISPATCHER.adapter = _ensure_adapter()


def _build_dispatcher(receiver: ReceiverThread) -> EventDispatcher:
    """Construct an EventDispatcher wired to the current capture state."""
    cap = _get_capture_mod()
    mirror_stage = None
    emitter = None
    if cap is not None and getattr(cap, "_state", None) is not None:
        author = getattr(cap._state, "author", None)
        if author is not None:
            mirror_stage = author.stage
            emitter = cap._state.notice_emitter
    adapter = _ensure_adapter()
    adapter.mirror_stage = mirror_stage
    return EventDispatcher(
        receiver=receiver,
        adapter=adapter,
        mirror_stage=mirror_stage,
        emitter=emitter,
        on_imported=_on_imported,
        on_resync=_on_resync,
    )


def _drain_and_process() -> int:
    """Run one drain-and-apply cycle through the dispatcher.

    Returns the count of events applied.  Lazily constructs the
    dispatcher if a receiver exists but the dispatcher does not (used
    by test scripts that bypass the start_receiver operator).  Re-binds
    mirror_stage and emitter on every call so capture toggling
    mid-session is picked up.

    The 5 phases (Parse, Skip-detect, Stage commit, Adapter dispatch,
    Cache invalidation) all run inside ``EventDispatcher.drain_and_apply``
    — see the per-phase commentary in ``openusdconnect/dispatcher.py``.
    """
    global _LAST_SEQ, _DISPATCHER
    if _RECEIVER is None:
        return 0
    if _DISPATCHER is None:
        _DISPATCHER = _build_dispatcher(_RECEIVER)
        _DISPATCHER.last_seq = _LAST_SEQ

    cap = _get_capture_mod()
    if cap is not None and cap._state.author is not None:
        _DISPATCHER.mirror_stage = cap._state.author.stage
        _DISPATCHER.emitter = cap._state.notice_emitter
    else:
        _DISPATCHER.mirror_stage = None
        _DISPATCHER.emitter = None
    _DISPATCHER.adapter.mirror_stage = _DISPATCHER.mirror_stage

    _DISPATCHER.last_seq = _LAST_SEQ
    applied = _DISPATCHER.drain_and_apply()
    _LAST_SEQ = _DISPATCHER.last_seq
    return applied


def _process_queue_timer():
    """Drain receiver queue on Blender main thread.

    Stage-first architecture (5 phases inside ``EventDispatcher.drain_and_apply``):
      Phase 1 — Parse FlatBuffers frames into event list
      Phase 2 — Collect skip decisions (unchanged arcs) from pre-commit state.
                arc_changed reads pre-commit state to avoid unnecessary
                re-imports.  Unchanged arcs are excluded from stage commit too —
                ClearReferences() followed by re-adding identical references
                triggers recomposition even when the final state is identical.
      Phase 3 — Commit all stage-affecting events to the emitter's USD stage
                atomically.  If atomic_apply raises, the adapter is never
                touched — Blender stays untouched on a failed batch.
      Phase 4 — Dispatch to BlenderAdapter (only after stage commit succeeds).
                Stage idempotence and DCC idempotence are not identical: a USD
                stage arc can already match before Blender has imported the
                corresponding objects, so adapter dispatch is only skipped when
                composed children are already present in the DCC scene.
      Phase 5 — Invalidate emitter caches against the now-mutated stage so the
                local Blender does not re-emit an event it just received
                (feedback-loop guard).

    Post-tick work (viewport refresh, post-import seeding) runs here in this
    function, still under the ``_set_applying_remote`` guard so the depsgraph
    handler doesn't echo received changes back to the server.
    """
    if _RECEIVER is None:
        return None  # Unregister timer

    # Hold the feedback guard for the entire batch INCLUDING view_layer.update()
    # so the depsgraph handler doesn't echo received changes back to the server.
    _set_applying_remote(True)
    try:
        applied = _drain_and_process()

        if applied > 0:
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
        global _RECEIVER, _DISPATCHER, _QUEUE_TIMER_REGISTERED, _LAST_SEQ, _ADAPTER
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
                host=host,
                port=port,
                sync_from=sync_from,
                client_id=STABLE_CLIENT_ID,
                origin=_ORIGIN,
            )
            _RECEIVER.start()
            _DISPATCHER = _build_dispatcher(_RECEIVER)
            _DISPATCHER.last_seq = _LAST_SEQ
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
        global _RECEIVER, _DISPATCHER, _QUEUE_TIMER_REGISTERED, _LAST_SEQ
        if _RECEIVER is not None:
            # Grab reference and null out global FIRST to stop timer from processing
            receiver = _RECEIVER
            _RECEIVER = None  # Timer will exit on next tick
            _DISPATCHER = None

            # Stop receiver thread (stops adding to queue)
            receiver.stop()
            try:
                receiver.join(timeout=2.0)
            except Exception:
                pass

            # NOW drain any remaining queued events (thread dead, timer stopped) —
            # decode/dedup so _LAST_SEQ matches the receiver's perspective; events
            # are not applied (the user clicked stop).
            remaining = receiver.drain_queue()
            if remaining:
                result = decode_messages(remaining, last_seq=_LAST_SEQ, clear_on_resync=True)
                for exc in result.errors:
                    LOG.exception(
                        "Error parsing received message at stop",
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
                _LAST_SEQ = result.last_seq

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
        if _DISPATCHER is not None:
            _DISPATCHER.last_seq = 0
            _DISPATCHER.adapter = _ensure_adapter()
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
    global _RECEIVER, _DISPATCHER, _QUEUE_TIMER_REGISTERED
    if _RECEIVER is not None:
        _RECEIVER.stop()
        _RECEIVER = None
    _DISPATCHER = None
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
