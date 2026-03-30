"""OpenUSDConnect integration for Unreal Engine.

Bridges the OpenUSDConnect network protocol to USDStageActor's live USD stage.
Leverages Epic's full USD support — USDStageActor handles all actor spawning,
transform conversion, and scene management. We only touch the USD stage.

Usage (from UE Python console):
    >>> import sys; sys.path.insert(0, r"D:\gamedev\OpenUSDConnect")
    >>> from integrations.unreal.usd_connect import start, stop
    >>> start("127.0.0.1", 7200, "test_scene.usda")
    >>> stop()

Or add the project root to UE's Additional Python Paths (Project Settings →
Plugins → Python → Additional Paths) and skip the sys.path line.
Once imported, the module self-bootstraps — no init_unreal.py needed.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
import uuid

# Self-bootstrap: ensure the project root is on sys.path so that
# `openusdconnect` is importable without any external setup.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

LOG = logging.getLogger("openusdconnect.unreal")

# -- Module state --------------------------------------------------------

_receiver = None        # ReceiverThread
_emitter = None         # NoticeEmitter
_sender_sock = None     # TCP socket for emitter txn messages
_tick_handle = None     # Slate tick callback handle
_last_seq: int = 0      # Highest sequence number received
_stage = None           # Cached Usd.Stage reference
_root_layer_id = ""     # Root layer identifier for staleness detection
_root_layer_name = ""   # For stage re-lookup on lifecycle events
_client_id: str = ""    # Per-session identifier
_origin: str = ""       # Shared origin for echo suppression
_host: str = ""
_port: int = 0
_coalesce_seconds: float = 0.1
_last_send_time: float = 0.0
_running: bool = False


# -- Stage lookup --------------------------------------------------------

def _find_stage(root_layer_name: str):
    """Find a stage in UsdUtils.StageCache matching the root layer name.

    Returns the stage or None.
    """
    from pxr import UsdUtils

    target = os.path.basename(root_layer_name)
    cache = UsdUtils.StageCache.Get()
    for s in cache.GetAllStages():
        root = s.GetRootLayer()
        if root and root.GetDisplayName() == target:
            return s
    return None


def _is_stage_alive(stage) -> bool:
    """Check if a cached stage reference is still valid."""
    try:
        root = stage.GetRootLayer()
        return root is not None and bool(root.GetDisplayName())
    except Exception:
        return False


def _ensure_stage():
    """Validate cached stage, re-lookup if stale.

    Called at the top of every tick. Returns the stage or None.
    Re-attaches the emitter to the new stage on reconnection.
    """
    global _stage, _root_layer_id

    if _stage is not None and _is_stage_alive(_stage):
        # Check that the root layer identifier hasn't changed (stage replaced)
        current_id = _stage.GetRootLayer().GetDisplayName()
        if current_id == _root_layer_id:
            return _stage
        LOG.info("Stage root layer changed (%s → %s), re-attaching...",
                 _root_layer_id, current_id)

    # Stage missing or stale — try to find it again
    stage = _find_stage(_root_layer_name)
    if stage is None:
        if _stage is not None:
            LOG.warning(
                "Stage '%s' no longer in cache — waiting for reopen",
                _root_layer_name,
            )
            _stage = None
            _root_layer_id = ""
        return None

    # (Re-)attached to a stage
    new_id = stage.GetRootLayer().GetDisplayName()
    if _stage is None or new_id != _root_layer_id:
        LOG.info("Attached to stage '%s' (%s)", _root_layer_name, new_id)
        _stage = stage
        _root_layer_id = new_id
        _reattach_emitter(stage)

    return _stage


def _reattach_emitter(stage):
    """Create or replace the NoticeEmitter on a (possibly new) stage."""
    global _emitter

    if _emitter is not None:
        _emitter.cleanup()
        _emitter = None

    if _sender_sock is None:
        return  # Emitter not requested

    from openusdconnect.emitter import NoticeEmitter

    _emitter = NoticeEmitter(stage)
    # Seed caches for existing prims so we don't dump full state
    root = stage.GetPseudoRoot()
    for child in root.GetChildren():
        _emitter.seed_prim_cache(stage, str(child.GetPath()))
    LOG.info("NoticeEmitter attached to stage")


# -- Tick callback -------------------------------------------------------

def _on_tick(delta_seconds: float):
    """Slate post-tick callback. Drains receiver queue and flushes emitter."""
    global _last_seq, _last_send_time

    stage = _ensure_stage()
    if stage is None:
        return

    # -- Receiver: drain queue and apply events --------------------------
    if _receiver is not None:
        lines = _receiver.drain_queue()
        if lines:
            _apply_received_lines(stage, lines)

    # -- Emitter: flush dirty events -------------------------------------
    if _emitter is not None and _sender_sock is not None:
        now = time.time()
        if (now - _last_send_time) >= _coalesce_seconds:
            _flush_emitter()
            _last_send_time = now


def _apply_received_lines(stage, lines: list[str]):
    """Parse, deduplicate, and apply received JSON lines to the stage."""
    global _last_seq

    from openusdconnect.event_apply import apply_events
    from openusdconnect.protocol import MSG_EVENT, MSG_RESYNC

    events_to_apply = []

    for raw_line in lines:
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError:
            LOG.warning("Malformed JSON line: %.100s...", raw_line)
            continue

        msg_type = msg.get("type")

        if msg_type == MSG_RESYNC:
            LOG.info("Server requested resync — resetting sequence")
            _last_seq = 0
            events_to_apply.clear()
            continue

        seq = msg.get("seq")
        if seq is not None:
            seq_int = int(seq)
            if seq_int <= _last_seq:
                continue
            _last_seq = seq_int

        if msg_type == MSG_EVENT:
            ev = msg.get("event")
            if ev:
                events_to_apply.append(ev)

    if not events_to_apply:
        return

    # Suppress emitter to prevent echoing received events back
    if _emitter is not None:
        _emitter.suppress()
    try:
        apply_events(stage, events_to_apply)
    except Exception:
        LOG.exception("Error applying %d events to stage", len(events_to_apply))
    finally:
        if _emitter is not None:
            _emitter.unsuppress()


def _flush_emitter():
    """Build events from dirty prims and send to server."""
    global _sender_sock

    events = _emitter.build_events_for_dirty(include_matrices=False)
    if not events:
        return

    from openusdconnect.protocol import make_txn
    from openusdconnect.transport import send_line

    txn = make_txn(_client_id, events)
    try:
        send_line(_sender_sock, txn)
    except (OSError, BrokenPipeError):
        LOG.warning("Emitter socket lost — closing")
        try:
            _sender_sock.close()
        except Exception:
            pass
        _sender_sock = None


# -- Public API ----------------------------------------------------------

def start(
    host: str = "127.0.0.1",
    port: int = 7200,
    root_layer_name: str = "test_scene.usda",
    *,
    receive: bool = True,
    emit: bool = True,
    coalesce: float = 0.1,
    client_id: str | None = None,
    origin: str | None = None,
):
    """Start the OpenUSDConnect sync bridge.

    Args:
        host: Server hostname.
        port: Server port.
        root_layer_name: Display name of the root layer opened in USDStageActor
            (e.g. "test_scene.usda"). Used to find the stage in the cache.
        receive: Enable receiving events from the server.
        emit: Enable emitting local changes to the server.
        coalesce: Minimum seconds between emitter flushes.
        client_id: Optional client identifier (auto-generated if None).
        origin: Optional origin identifier for echo suppression.
    """
    global _receiver, _sender_sock, _emitter, _tick_handle
    global _root_layer_name, _client_id, _origin, _host, _port
    global _coalesce_seconds, _last_send_time, _running, _last_seq

    if _running:
        LOG.warning("Already running — call stop() first")
        return

    import unreal  # noqa: F811 — only available inside UE

    _root_layer_name = root_layer_name
    _host = host
    _port = port
    _client_id = client_id or f"ue-{uuid.uuid4().hex[:8]}"
    _origin = origin or f"ue-{uuid.uuid4().hex[:8]}"
    _coalesce_seconds = coalesce
    _last_send_time = 0.0
    _last_seq = 0

    # Find the stage up front
    stage = _find_stage(root_layer_name)
    if stage is None:
        LOG.error(
            "No stage with root layer '%s' found in UsdUtils.StageCache. "
            "Make sure a USDStageActor has opened the file first.",
            root_layer_name,
        )
        return

    global _stage, _root_layer_id
    _stage = stage
    _root_layer_id = stage.GetRootLayer().GetDisplayName()
    LOG.info("Found stage '%s' (%s)", root_layer_name, _root_layer_id)

    # -- Start receiver --------------------------------------------------
    if receive:
        from openusdconnect.receiver import ReceiverThread

        _receiver = ReceiverThread(
            host=host,
            port=port,
            sync_from=1,
            client_id=_client_id,
            origin=_origin,
        )
        _receiver.start()
        LOG.info("Receiver started → %s:%d", host, port)

    # -- Start emitter ---------------------------------------------------
    if emit:
        from openusdconnect.protocol import make_hello
        from openusdconnect.transport import send_line

        try:
            _sender_sock = socket.create_connection((host, port), timeout=5.0)
            hello = make_hello("emitter", client_id=_client_id, origin=_origin)
            send_line(_sender_sock, hello)
            LOG.info("Emitter connected → %s:%d", host, port)
        except OSError as exc:
            LOG.error("Failed to connect emitter socket: %s", exc)
            _sender_sock = None

        _reattach_emitter(stage)

    # -- Register tick callback ------------------------------------------
    _tick_handle = unreal.register_slate_post_tick_callback(_on_tick)
    _running = True
    LOG.info("OpenUSDConnect sync active (receive=%s, emit=%s)", receive, emit)


def stop():
    """Stop the OpenUSDConnect sync bridge and clean up."""
    global _receiver, _emitter, _sender_sock, _tick_handle
    global _stage, _root_layer_id, _running, _last_seq

    if not _running:
        return

    import unreal

    # Unregister tick
    if _tick_handle is not None:
        unreal.unregister_slate_post_tick_callback(_tick_handle)
        _tick_handle = None

    # Stop receiver
    if _receiver is not None:
        _receiver.stop()
        _receiver = None

    # Clean up emitter
    if _emitter is not None:
        _emitter.cleanup()
        _emitter = None

    # Close sender socket
    if _sender_sock is not None:
        try:
            _sender_sock.close()
        except Exception:
            pass
        _sender_sock = None

    _stage = None
    _root_layer_id = ""
    _last_seq = 0
    _running = False
    LOG.info("OpenUSDConnect sync stopped")


def status() -> dict:
    """Return current sync status for diagnostics."""
    return {
        "running": _running,
        "root_layer": _root_layer_name,
        "stage_attached": _stage is not None,
        "receiver_connected": _receiver.connected if _receiver else False,
        "emitter_connected": _sender_sock is not None,
        "last_seq": _last_seq,
        "client_id": _client_id,
        "origin": _origin,
    }
