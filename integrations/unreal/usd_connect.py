"""OpenUSDConnect integration for Unreal Engine.

Bridges the OpenUSDConnect network protocol to USDStageActor's live USD stage.
Leverages Epic's full USD support — USDStageActor handles all actor spawning,
transform conversion, and scene management. We only touch the USD stage.

Usage (from UE Python console):
    >>> import sys; sys.path.insert(0, r"D:\\gamedev\\OpenUSDConnect")
    >>> from integrations.unreal.usd_connect import reconnect_emitter, start, stop
    >>> start("127.0.0.1", 7200, "test_scene.usda")
    >>> reconnect_emitter()  # after a reported emitter connection loss
    >>> stop()

Or add the project root to UE's Additional Python Paths (Project Settings →
Plugins → Python → Additional Paths) and skip the sys.path line.
Once imported, the module self-bootstraps — no init_unreal.py needed.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import uuid

# Self-bootstrap: ensure the project root is on sys.path so that
# `openusdconnect` is importable without any external setup.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from openusdconnect.defaults import DEFAULT_HOST, DEFAULT_SYNC_PORT  # noqa: E402

LOG = logging.getLogger("openusdconnect.unreal")

# -- Module state --------------------------------------------------------

_receiver = None  # ReceiverThread
_emitter = None  # NoticeEmitter
_sender = None  # EventSender
_dispatcher = None  # EventDispatcher
_tick_handle = None  # Slate tick callback handle
_stage = None  # Cached Usd.Stage reference
_root_layer_id = ""  # Root layer identifier for staleness detection
_root_layer_name = ""  # For stage re-lookup on lifecycle events
_client_id: str = ""  # Per-session identifier
_origin: str = ""  # Shared origin for attribution and reconciliation
_host: str = ""
_port: int = 0
_live_metadata: dict | None = None
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


def _read_live_metadata(stage) -> dict | None:
    """Read OpenUSDConnect live metadata from the stage root layer."""
    try:
        root = stage.GetRootLayer()
        meta = (root.customLayerData or {}).get("openusdconnect") if root else None
    except Exception:
        LOG.exception("Failed reading OpenUSDConnect live metadata")
        return None
    if not isinstance(meta, dict) or not meta.get("live"):
        return None
    host = str(meta.get("host") or "").strip()
    try:
        port = int(meta.get("port") or 0)
        snapshot_seq = int(meta.get("snapshot_seq") or 0)
    except (TypeError, ValueError):
        LOG.warning("OpenUSDConnect live metadata has invalid port or snapshot_seq")
        return None
    if not host or not (1 <= port <= 65535):
        LOG.warning("OpenUSDConnect live metadata is missing a valid host/port")
        return None
    return {
        "host": host,
        "port": port,
        "snapshot_seq": max(0, snapshot_seq),
        "scene_id": meta.get("scene_id", ""),
        "vfs_url": meta.get("vfs_url", ""),
        "requires_token": bool(meta.get("requires_token", False)),
    }


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
        LOG.info("Stage root layer changed (%s → %s), re-attaching...", _root_layer_id, current_id)

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
        _reattach_to_stage(stage)

    return _stage


def _reattach_to_stage(stage):
    """Rebind emitter and dispatcher adapter to a (possibly new) stage."""
    global _emitter

    if _emitter is not None:
        _emitter.cleanup()
        _emitter = None

    if _sender is not None:
        from openusdconnect.emitter import NoticeEmitter

        _emitter = NoticeEmitter(stage)
        # Seed caches for existing prims so we don't dump full state
        root = stage.GetPseudoRoot()
        for child in root.GetChildren():
            _emitter.seed_prim_cache(stage, str(child.GetPath()))
        LOG.info("NoticeEmitter attached to stage")

    # Repoint the dispatcher's adapter at the new stage and refresh its
    # emitter reference.  The dispatcher's invalidate path requires the
    # emitter and adapter to share the same stage.
    if _dispatcher is not None:
        from openusdconnect.adapters import UsdStageAdapter

        _dispatcher.adapter = UsdStageAdapter(stage)
        _dispatcher.emitter = _emitter


# -- Tick callback -------------------------------------------------------


def _on_tick(delta_seconds: float):
    """Slate post-tick callback. Drains receiver queue and flushes emitter."""
    global _last_send_time

    stage = _ensure_stage()
    if stage is None:
        return

    # -- Receiver: drive the dispatcher's drain-and-apply pipeline -------
    if _dispatcher is not None:
        _dispatcher.drain_and_apply()

    # -- Emitter: flush dirty events -------------------------------------
    if _emitter is not None and _sender is not None and _sender.connected:
        now = time.time()
        if (now - _last_send_time) >= _coalesce_seconds:
            _flush_emitter()
            _last_send_time = now


def _flush_emitter() -> bool:
    """Build events from dirty prims and send to server."""
    events = _emitter.prepare_events_for_send()
    if not events:
        return True
    if _sender.send_events(events):
        _emitter.mark_prepared_events_sent(events)
        return True
    LOG.warning(
        "Emitter send failed; retaining %d events. Call reconnect_emitter() to retry",
        len(events),
    )
    return False


# -- Public API ----------------------------------------------------------


def reconnect_emitter() -> bool:
    """Reconnect the emitter and retry its retained batch.

    This call is synchronous and should be initiated by the user after a
    connection failure rather than from the Slate tick callback.
    """
    if not _running or _sender is None or _emitter is None:
        LOG.error("Emitter is not initialized; call start(..., emit=True) first")
        return False
    if _sender.connected:
        return True
    if _sender.auth_rejected:
        LOG.error("Emitter authentication was rejected")
        return False
    if not _sender.connect():
        LOG.error("Failed to reconnect emitter")
        return False
    if not _flush_emitter():
        return False
    LOG.info("Emitter reconnected")
    return True


def start(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_SYNC_PORT,
    root_layer_name: str = "test_scene.usda",
    *,
    receive: bool = True,
    emit: bool = True,
    coalesce: float = 0.1,
    use_live_metadata: bool = True,
    persist_tokens: bool = True,
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
        use_live_metadata: When the opened stage has OpenUSDConnect metadata,
            use its host/port and snapshot_seq for live sync.
        persist_tokens: Save and reuse TOFU auth tokens for token-required servers.
        client_id: Optional client identifier (auto-generated if None).
        origin: Optional session origin for attribution and reconciliation.
    """
    global _receiver, _sender, _dispatcher, _tick_handle
    global _root_layer_name, _client_id, _origin, _host, _port
    global _live_metadata, _coalesce_seconds, _last_send_time, _running

    if _running:
        LOG.warning("Already running — call stop() first")
        return

    import unreal  # noqa: F811 — only available inside UE

    _root_layer_name = root_layer_name
    _host = host
    _port = port
    from openusdconnect.client_id import make_stable_client_id

    _client_id = client_id or make_stable_client_id("ue")
    _origin = origin or f"ue-{uuid.uuid4().hex[:8]}"
    _coalesce_seconds = coalesce
    _last_send_time = 0.0

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

    sync_from = 1
    _live_metadata = _read_live_metadata(stage) if use_live_metadata else None
    requires_token = False
    if _live_metadata:
        host = _live_metadata["host"]
        port = _live_metadata["port"]
        sync_from = _live_metadata["snapshot_seq"] + 1
        requires_token = bool(_live_metadata.get("requires_token"))
        _host = host
        _port = port
        LOG.info(
            "Using OpenUSDConnect live metadata: %s:%d sync_from=%d vfs_url=%s",
            host,
            port,
            sync_from,
            _live_metadata.get("vfs_url", ""),
        )
    saved_token = None
    if persist_tokens:
        from openusdconnect import token_client

        saved_token = token_client.load_token(host, port)

    token_for_clients = saved_token

    def _remember_token(token: str) -> None:
        nonlocal token_for_clients
        token_for_clients = token
        if persist_tokens:
            token_client.save_token(host, port, token)

    # -- Start receiver --------------------------------------------------
    if receive:
        from openusdconnect.receiver import ReceiverThread

        _receiver = ReceiverThread(
            host=host,
            port=port,
            sync_from=sync_from,
            client_id=_client_id,
            origin=_origin,
            token=token_for_clients,
            on_token_issued=_remember_token,
            layered_replay=False,
        )
        _receiver.start()
        LOG.info("Receiver started → %s:%d", host, port)

    if emit and receive and requires_token and not token_for_clients:
        # First-use TOFU: let the receiver obtain the token before
        # opening the emitter socket with the same client_id.
        deadline = time.time() + 5.0
        while time.time() < deadline and not token_for_clients:
            token = getattr(_receiver, "token", None)
            if token:
                _remember_token(token)
                break
            time.sleep(0.05)

    # -- Start emitter ---------------------------------------------------
    if emit:
        from openusdconnect.sender import EventSender

        _sender = EventSender(
            host,
            port,
            token=token_for_clients,
            on_token_issued=_remember_token,
            client_id=_client_id,
            origin=_origin,
        )
        if _sender.connect():
            LOG.info("Emitter connected → %s:%d", host, port)
        else:
            LOG.error("Failed to connect emitter")
            _sender = None

        _reattach_to_stage(stage)

    # -- Build dispatcher ------------------------------------------------
    # The receive→apply pipeline is driven by EventDispatcher.  The
    # adapter writes to the stage directly (UsdStageAdapter), so no
    # separate mirror_stage is needed — the adapter and the emitter
    # share the same stage.
    if _receiver is not None:
        from openusdconnect.adapters import UsdStageAdapter
        from openusdconnect.dispatcher import EventDispatcher

        _dispatcher = EventDispatcher(
            receiver=_receiver,
            adapter=UsdStageAdapter(stage),
            emitter=_emitter,
        )

    # -- Register tick callback ------------------------------------------
    _tick_handle = unreal.register_slate_post_tick_callback(_on_tick)
    _running = True
    LOG.info("OpenUSDConnect sync active (receive=%s, emit=%s)", receive, emit)


def stop():
    """Stop the OpenUSDConnect sync bridge and clean up."""
    global _receiver, _emitter, _sender, _dispatcher, _tick_handle
    global _stage, _root_layer_id, _live_metadata, _running

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

    # Drop dispatcher (no separate state to flush — last_seq dies with it)
    _dispatcher = None

    # Clean up emitter
    if _emitter is not None:
        _emitter.cleanup()
        _emitter = None

    # Close sender
    if _sender is not None:
        _sender.disconnect()
        _sender = None

    _stage = None
    _root_layer_id = ""
    _live_metadata = None
    _running = False
    LOG.info("OpenUSDConnect sync stopped")


def status() -> dict:
    """Return current sync status for diagnostics."""
    return {
        "running": _running,
        "root_layer": _root_layer_name,
        "stage_attached": _stage is not None,
        "receiver_connected": _receiver.connected if _receiver else False,
        "emitter_connected": _sender.connected if _sender else False,
        "last_seq": _dispatcher.last_seq if _dispatcher else 0,
        "live_metadata": _live_metadata,
        "client_id": _client_id,
        "origin": _origin,
        "pending_emitter_events": _emitter.prepared_event_count if _emitter else 0,
    }
