"""Blender receiver — applies incoming network events to Blender objects.

Uses ReceiverThread from openusdconnect.receiver and drains the queue
on the main thread via bpy.app.timers.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

try:
    import bpy
    from bpy.props import IntProperty, StringProperty, BoolProperty
    BPY_AVAILABLE = True
except Exception:
    BPY_AVAILABLE = False

from openusdconnect.receiver import ReceiverThread
from .blender_adapter import BlenderAdapter

LOG = logging.getLogger(__name__)

_RECEIVER: Optional[ReceiverThread] = None
_ADAPTER: Optional[BlenderAdapter] = None
_QUEUE_TIMER_REGISTERED = False
# Feedback loop guard: set True while applying remote events
_APPLYING_REMOTE = False
# Track last sequence number across reconnects
_LAST_SEQ: int = 0


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


def _process_event(ev: dict):
    """Dispatch a single event to the BlenderAdapter."""
    global _ADAPTER, _APPLYING_REMOTE
    if _ADAPTER is None:
        _ADAPTER = BlenderAdapter()

    k = ev.get("k")
    prim_path = ev.get("prim", "")

    _APPLYING_REMOTE = True
    try:
        # Also set feedback guard on capture module if available
        try:
            from . import capture
            if capture._NET_EMITTER is not None:
                capture._NET_EMITTER._applying_remote = True
        except Exception:
            pass

        if k == "ensure_prim":
            _ADAPTER.ensure_prim(prim_path, ev.get("typeName", "Xform"))
        elif k == "ensure_xform_ops":
            _ADAPTER.ensure_xform_ops(prim_path)
        elif k == "set_xform_trs":
            _ADAPTER.set_xform_trs(prim_path, ev)
        elif k == "set_xform_matrices":
            _ADAPTER.set_xform_matrices(prim_path, ev)
        elif k == "delete_prim":
            _ADAPTER.delete_prim(prim_path)
        elif k == "deactivate_prim":
            _ADAPTER.deactivate_prim(prim_path, ev.get("active", False))
        elif k == "rename_prim":
            _ADAPTER.rename_prim(prim_path, ev.get("new_name", ""))
    finally:
        _APPLYING_REMOTE = False
        try:
            from . import capture
            if capture._NET_EMITTER is not None:
                capture._NET_EMITTER._applying_remote = False
        except Exception:
            pass


def _process_queue_timer():
    """Drain receiver queue on Blender main thread."""
    global _RECEIVER, _LAST_SEQ
    if _RECEIVER is None:
        return None  # Unregister timer

    lines = _RECEIVER.drain_queue()
    for raw_line in lines:
        try:
            msg = json.loads(raw_line)
            seq = msg.get("seq")
            
            # Skip already-processed events (deduplication)
            if seq is not None:
                seq_int = int(seq)
                if seq_int <= _LAST_SEQ:
                    continue
                _LAST_SEQ = seq_int
            
            if msg.get("type") == "event":
                ev = msg.get("event", {})
                _process_event(ev)
        except Exception:
            LOG.exception("Error processing received line")

    # Persist to scene property after processing all
    if lines:
        try:
            scene = bpy.context.scene
            if scene:
                scene.usd_connect_recv_last_seq = _LAST_SEQ
        except Exception:
            pass

    return 0.01  # Run again in 10ms


class USD_CONNECT_OT_start_receiver(bpy.types.Operator):
    bl_idname = "usd_connect.start_receiver"
    bl_label = "Start Receiver"
    bl_description = "Connect to sync server and start receiving transform events"

    def execute(self, context):
        global _RECEIVER, _QUEUE_TIMER_REGISTERED, _LAST_SEQ
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
            remaining = receiver.drain_queue()
            for raw_line in remaining:
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
                    pass
            
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
        global _LAST_SEQ
        _LAST_SEQ = 0
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
    for prop_name in ("usd_connect_recv_host", "usd_connect_recv_port", "usd_connect_recv_running", "usd_connect_recv_last_seq"):
        if hasattr(bpy.types.Scene, prop_name):
            try:
                delattr(bpy.types.Scene, prop_name)
            except Exception:
                pass
