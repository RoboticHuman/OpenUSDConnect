"""Blender receiver — applies incoming network events to Blender objects.

Uses ReceiverThread from openusdconnect.receiver and drains the queue
on the main thread via bpy.app.timers.
"""

from __future__ import annotations

import json
import logging

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
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_VISIBILITY,
    K_SET_XFORM_MATRICES,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
)
from openusdconnect.receiver import ReceiverThread

from .blender_adapter import BlenderAdapter

LOG = logging.getLogger(__name__)

_RECEIVER: ReceiverThread | None = None
_ADAPTER: BlenderAdapter | None = None
_QUEUE_TIMER_REGISTERED = False
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
    K_SET_PAYLOAD: "set_payload",
    K_LOAD_PAYLOAD: "load_payload",
    K_UNLOAD_PAYLOAD: "unload_payload",
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
    if k == K_SET_PAYLOAD:
        return (prim_path, ev.get("payloads", [])), {}
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


def _process_event(ev: dict):
    """Dispatch a single event to the BlenderAdapter.

    NOTE: caller is responsible for setting _APPLYING_REMOTE around the
    entire batch — this function only dispatches, it does not toggle the
    feedback guard.
    """
    global _ADAPTER
    if _ADAPTER is None:
        _ADAPTER = BlenderAdapter()

    k = ev.get("k")
    prim_path = ev.get("prim", "")
    extra = ""
    if k == K_SET_VISIBILITY:
        extra = f" visible={ev.get('visible')}"
    elif k == K_DEACTIVATE_PRIM:
        extra = f" active={ev.get('active')}"
    LOG.debug("event: k=%s prim=%s%s", k, prim_path, extra)

    _dispatch_event(_ADAPTER, k, prim_path, ev)

    # Keep the emitter's stage in sync with payload and reference arcs so
    # its composed view matches what the receiver imported/removed.  This
    # mirrors how the base-file case works: the emitter's stage has the
    # composition arc, so children compose naturally when loaded.
    if k in (K_SET_PAYLOAD, K_LOAD_PAYLOAD, K_UNLOAD_PAYLOAD, K_SET_REFERENCE):
        cap = _get_capture_mod()
        if cap is not None and cap._state.author is not None:
            from openusdconnect.event_apply import apply_event as _apply_ev

            try:
                stage = cap._state.author.stage
                _apply_ev(stage, ev)

                ne = cap._state.notice_emitter
                prefix = prim_path + "/"

                if k == K_UNLOAD_PAYLOAD and ne is not None:
                    # Purge emitter caches for unloaded children so they're
                    # treated as fresh first-encounters after the next load.
                    # Without this, stale _known_prims / last_sent_visibility
                    # cause wrong events after reload.
                    to_purge = [p for p in list(ne._known_prims) if p.startswith(prefix)]
                    for p in to_purge:
                        ne._purge_caches(p)

                elif k == K_LOAD_PAYLOAD:
                    # Clear stale SetActive(False) opinions left by
                    # _detect_deletions during the previous unload cycle.
                    from pxr import Usd

                    prim = stage.GetPrimAtPath(prim_path)
                    if prim and prim.IsValid():
                        for child in Usd.PrimRange(prim):
                            if not child.IsActive():
                                child.SetActive(True)
            except Exception:
                LOG.debug("Could not apply %s to emitter stage", k)


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
    global _LAST_SEQ
    for raw_line in lines:
        try:
            msg = json.loads(raw_line)
            seq = msg.get("seq")
            if seq is not None:
                seq_int = int(seq)
                if seq_int <= _LAST_SEQ:
                    continue
                _LAST_SEQ = seq_int
            if msg.get("type") == "event":
                _process_event(msg.get("event", {}))
        except Exception:
            LOG.exception("Error processing received line")


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
            _RECEIVER = ReceiverThread(host=host, port=port, sync_from=sync_from)
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
