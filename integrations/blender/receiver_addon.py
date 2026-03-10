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
_TIMER_INSTALLED = False
# Feedback loop guard: set True while applying remote events
_APPLYING_REMOTE = False


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
    global _RECEIVER
    if _RECEIVER is None:
        return None  # Unregister timer

    lines = _RECEIVER.drain_queue()
    processed = 0
    for raw_line in lines:
        try:
            msg = json.loads(raw_line)
            if msg.get("type") == "event":
                ev = msg.get("event", {})
                _process_event(ev)
                processed += 1
        except Exception:
            LOG.exception("Error processing received line")
        if processed >= 50:
            break  # Yield to Blender, process rest next tick

    return 0.01  # Run again in 10ms


class USD_CONNECT_OT_start_receiver(bpy.types.Operator):
    bl_idname = "usd_connect.start_receiver"
    bl_label = "Start Receiver"
    bl_description = "Connect to sync server and start receiving transform events"

    def execute(self, context):
        global _RECEIVER, _TIMER_INSTALLED
        if _RECEIVER is not None:
            self.report({"INFO"}, "Receiver already running")
            return {"CANCELLED"}
        scene = context.scene
        host = scene.usd_connect_recv_host
        port = scene.usd_connect_recv_port
        try:
            _RECEIVER = ReceiverThread(host=host, port=port)
            _RECEIVER.start()
            if not _TIMER_INSTALLED:
                bpy.app.timers.register(_process_queue_timer, first_interval=0.01)
                _TIMER_INSTALLED = True
            scene.usd_connect_recv_running = True
            self.report({"INFO"}, f"Receiver started ({host}:{port})")
        except Exception as e:
            self.report({"ERROR"}, f"Failed to start receiver: {e}")
            return {"CANCELLED"}
        return {"FINISHED"}


class USD_CONNECT_OT_stop_receiver(bpy.types.Operator):
    bl_idname = "usd_connect.stop_receiver"
    bl_label = "Stop Receiver"

    def execute(self, context):
        global _RECEIVER, _TIMER_INSTALLED
        if _RECEIVER is not None:
            _RECEIVER.stop()
            try:
                _RECEIVER.join(timeout=2.0)
            except Exception:
                pass
            _RECEIVER = None
        _TIMER_INSTALLED = False
        context.scene.usd_connect_recv_running = False
        self.report({"INFO"}, "Receiver stopped")
        return {"FINISHED"}


_RECEIVER_CLASSES = (
    USD_CONNECT_OT_start_receiver,
    USD_CONNECT_OT_stop_receiver,
)


def register():
    _ensure_scene_props()
    if BPY_AVAILABLE:
        for c in _RECEIVER_CLASSES:
            bpy.utils.register_class(c)


def unregister():
    global _RECEIVER, _TIMER_INSTALLED
    if _RECEIVER is not None:
        _RECEIVER.stop()
        _RECEIVER = None
    _TIMER_INSTALLED = False
    if BPY_AVAILABLE:
        for c in reversed(_RECEIVER_CLASSES):
            bpy.utils.unregister_class(c)
    for prop_name in ("usd_connect_recv_host", "usd_connect_recv_port", "usd_connect_recv_running"):
        if hasattr(bpy.types.Scene, prop_name):
            try:
                delattr(bpy.types.Scene, prop_name)
            except Exception:
                pass
