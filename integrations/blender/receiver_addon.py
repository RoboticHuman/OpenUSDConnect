"""Blender receiver applies incoming network events to Blender objects.

Uses ReceiverThread from openusdconnect.receiver and drains the queue
on the main thread via bpy.app.timers.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass

try:
    import bpy
    from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty

    BPY_AVAILABLE = True
except Exception:
    BPY_AVAILABLE = False
    BoolProperty = FloatProperty = IntProperty = StringProperty = None  # type: ignore[assignment]

from openusdconnect import token_client
from openusdconnect.dispatcher import EventDispatcher
from openusdconnect.protocol_constants import (
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_SET_GPRIM_ATTRS,
    K_SET_XFORM_TRS,
)
from openusdconnect.receiver import ReceiverThread

from . import SESSION_ORIGIN as _ORIGIN
from .blender_adapter import BlenderAdapter, apply_stage_metadata_to_scene

LOG = logging.getLogger(__name__)

_RECEIVER: ReceiverThread | None = None
_DISPATCHER: EventDispatcher | None = None
_ADAPTER: BlenderAdapter | None = None
_MIRROR_STAGE = None
_MIRROR_SOURCE = ""
_MIRROR_ENDPOINT: tuple[str, int] | None = None
_MIRROR_LAYERED_REPLAY: bool | None = None
_RECEIVER_TIMER_REGISTERED = False
# Imported prim roots whose emitter caches need seeding after view_layer.update().
_pending_import_seed_paths: set[str] = set()
# Applied prim paths whose evaluated transform baselines must be refreshed.
_pending_object_baseline_paths: set[str] = set()
# Shader prim paths whose reverse-sync state must be refreshed after apply.
_pending_shader_baseline_paths: set[str] = set()
# Feedback loop guard: set True while applying remote events
_APPLYING_REMOTE = False
# Track last sequence number across reconnects
_LAST_SEQ: int = 0
# Lazy-cached reference to the capture module (avoids per-call import overhead)
_capture_module = None


def _get_capture_module():
    global _capture_module
    if _capture_module is None:
        try:
            from . import capture

            _capture_module = capture
        except ImportError:
            pass
    return _capture_module


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
    if not hasattr(S, "usd_connect_department"):
        S.usd_connect_department = StringProperty(name="Department", default="")
    # Playback synchronization state populated from server messages.
    if not hasattr(S, "usd_connect_playback_is_leader"):
        S.usd_connect_playback_is_leader = BoolProperty(
            name="Local Is Playback Leader", default=False,
        )
    if not hasattr(S, "usd_connect_playback_leader_id"):
        S.usd_connect_playback_leader_id = StringProperty(
            name="Playback Leader", default="",
        )
    if not hasattr(S, "usd_connect_playback_time"):
        S.usd_connect_playback_time = FloatProperty(
            name="Playback Time (timecode)", default=0.0,
        )
    if not hasattr(S, "usd_connect_playback_playing"):
        S.usd_connect_playback_playing = BoolProperty(
            name="Playback Playing", default=False,
        )
    # USD's timeCodesPerSecond from the server stage; needed for the
    # timecode↔frame conversion. Defaults to 24 the USD spec default.
    if not hasattr(S, "usd_connect_tcps"):
        S.usd_connect_tcps = FloatProperty(
            name="Stage timeCodesPerSecond", default=24.0,
        )


def _ensure_adapter():
    """Lazily create the BlenderAdapter on first use."""
    global _ADAPTER
    if _ADAPTER is not None:
        return _ADAPTER
    up_axis = "Y"  # default most USD scenes are Y-up
    source_stage = _MIRROR_STAGE
    if source_stage is None:
        capture_module = _get_capture_module()
        if capture_module is not None and getattr(capture_module, "_state", None) is not None:
            author = getattr(capture_module._state, "author", None)
            source_stage = author.stage if author is not None else None
    if source_stage is not None:
        from pxr import UsdGeom

        up_axis = UsdGeom.GetStageUpAxis(source_stage)
    _ADAPTER = BlenderAdapter(scene_up_axis=up_axis)
    return _ADAPTER


def _configured_base_path() -> str:
    capture_module = _get_capture_module()
    if capture_module is not None and getattr(capture_module, "_state", None) is not None:
        author = getattr(capture_module._state, "author", None)
        if author is not None:
            return os.path.abspath(author.base_usd_path)
    scene = bpy.context.scene
    path = str(getattr(scene, "usd_connect_base_usd_path", "") or "").strip()
    return os.path.abspath(bpy.path.abspath(path)) if path else ""


@dataclass(frozen=True, slots=True)
class _ReplayPlan:
    sync_from: int
    baseline_seq: int
    layered_replay: bool


def _live_snapshot_metadata(source: str) -> dict | None:
    if not source:
        return None
    from .live_discovery import read_live_metadata

    return read_live_metadata(source)


def _replay_plan(
    *,
    last_seq: int,
    mirror_reusable: bool,
    live_metadata: dict | None,
) -> _ReplayPlan:
    if mirror_reusable:
        return _ReplayPlan(
            sync_from=max(1, last_seq + 1),
            baseline_seq=max(0, last_seq),
            layered_replay=live_metadata is None,
        )
    if live_metadata is not None:
        snapshot_seq = max(0, int(live_metadata.get("snapshot_seq") or 0))
        return _ReplayPlan(
            sync_from=snapshot_seq + 1,
            baseline_seq=snapshot_seq,
            layered_replay=False,
        )
    return _ReplayPlan(sync_from=1, baseline_seq=0, layered_replay=True)


def _ensure_mirror_stage(*, reset: bool = False):
    """Return a receiver-owned stage with an independent session layer."""
    global _MIRROR_SOURCE, _MIRROR_STAGE

    source = _configured_base_path()
    if not reset and _MIRROR_STAGE is not None and source == _MIRROR_SOURCE:
        return _MIRROR_STAGE

    from pxr import Sdf, Usd

    if source:
        root_layer = Sdf.Layer.FindOrOpen(source)
        if root_layer is None:
            raise RuntimeError(f"Failed to open receiver base USD layer: {source}")
        stage = Usd.Stage.Open(root_layer)
        if stage is None:
            raise RuntimeError(f"Failed to open receiver USD stage: {source}")
    else:
        stage = Usd.Stage.CreateInMemory()

    stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))
    _MIRROR_SOURCE = source
    _MIRROR_STAGE = stage
    return stage


def _discard_replay_state() -> None:
    """Release retained replay state before changing the receiver baseline."""
    global _ADAPTER, _DISPATCHER, _MIRROR_ENDPOINT, _MIRROR_LAYERED_REPLAY
    global _MIRROR_SOURCE, _MIRROR_STAGE

    if _DISPATCHER is not None:
        _DISPATCHER.close()
    _DISPATCHER = None
    _ADAPTER = None
    _MIRROR_STAGE = None
    _MIRROR_SOURCE = ""
    _MIRROR_ENDPOINT = None
    _MIRROR_LAYERED_REPLAY = None
    _pending_import_seed_paths.clear()
    _pending_object_baseline_paths.clear()
    _pending_shader_baseline_paths.clear()


def _set_remote_apply_guard(value: bool):
    """Set the feedback-loop guard on both receiver and emitter modules."""
    global _APPLYING_REMOTE
    _APPLYING_REMOTE = value
    capture_module = _get_capture_module()
    if capture_module is not None:
        capture_module.set_emitter_feedback_guard(value)


def _stop_receiver_thread(receiver: ReceiverThread) -> None:
    """Stop a receiver and tolerate joining a thread that never started."""
    receiver.stop()
    try:
        receiver.join(timeout=2.0)
    except RuntimeError:
        LOG.debug("Receiver thread could not be joined", exc_info=True)


def _store_last_sequence(scene, value: int) -> None:
    """Persist replay progress when the Blender scene is still writable."""
    try:
        scene.usd_connect_recv_last_seq = value
    except (AttributeError, ReferenceError, RuntimeError):
        LOG.debug("Could not persist receiver sequence", exc_info=True)


def _refresh_shader_reverse_sync_state(author, adapter, prim_path: str):
    """Seed the author's reverse-sync state for shaders at or under prim_path.

    Multi-node shaders: copy the adapter's cached ``input_map`` socket dicts
    (usd_name -> socket) to the author, which needs them for reverse reading,
    and seed the ``_last_shader_values`` baseline from the sockets.
    Single-node shaders (PBR, textures, primvar readers): locate the tagged
    Blender node and seed the baseline from the mapper's reverse read.
    A seeded baseline means the first local edit diffs against the synced
    state instead of being swallowed as a first-encounter seed.
    """
    prefix = prim_path + "/"
    for shader_path, sc in adapter._registry.iter_shaders():
        if shader_path != prim_path and not shader_path.startswith(prefix):
            continue
        shader_id = sc.get("shader_id")
        mapper = author._shader_registry.get(shader_id) if shader_id else None
        if "input_map" in sc:
            input_map = sc["input_map"]
            # Cached sockets go stale when their material is removed (e.g.
            # importer duplicates cleaned up after rebinding). A raising
            # timer callback gets unregistered by Blender, killing the
            # receiver, so dead maps are dropped instead of read.
            try:
                values = (
                    mapper.read_all_inputs(input_map=input_map)
                    if mapper and mapper.is_multi_node
                    else {}
                )
            except ReferenceError:
                author._shader_input_maps.pop(shader_path, None)
                continue
            author._shader_input_maps[shader_path] = input_map
            if values:
                author._last_shader_values[shader_path] = values
                LOG.debug(
                    "seeded shader map %s with %d inputs",
                    shader_path,
                    len(values),
                )
            continue
        if not mapper or mapper.is_multi_node or not hasattr(mapper, "read_all_inputs"):
            continue
        try:
            mat = adapter._find_material_for_shader(shader_path)
            if mat is None or not getattr(mat, "node_tree", None):
                continue
            node = adapter._find_shader_node(mat.node_tree, shader_path)
            if node is None:
                continue
            values = mapper.read_all_inputs(node)
        except ReferenceError:
            continue
        if values:
            author._last_shader_values[shader_path] = values
            LOG.debug(
                "seeded shader baseline %s with %d inputs",
                shader_path,
                len(values),
            )


# Latest-wins PlaybackState handoff: written by the receiver thread,
# read-and-cleared by Blender's main-thread timer. The lock keeps the
# read-then-clear pair atomic so an update arriving in between can't be
# silently wiped under the GIL this is rare in practice, under
# free-threaded Python it's frequent (see scripts/bench_playback_state_sync.py).
# Python's free-threading HOWTO explicitly recommends threading.Lock
# over relying on built-in containers' internal locks.
_PLAYBACK_LOCK = threading.Lock()
_LATEST_PLAYBACK_STATE: dict | None = None


def _on_playback_state(state: dict) -> None:
    """Receive an authoritative PlaybackState broadcast.

    Stashes the payload for the next timer tick to apply on the Blender
    main thread (callbacks run on the receiver thread).
    """
    global _LATEST_PLAYBACK_STATE
    snapshot = dict(state)
    with _PLAYBACK_LOCK:
        _LATEST_PLAYBACK_STATE = snapshot


def _on_stage_metadata(meta: dict) -> None:
    """Receive a stage_metadata snapshot from hello_ok or live broadcast.

    Off-main-thread safe: schedules the actual scene mutation through
    bpy.app.timers so we never touch Blender state from the network thread.
    """
    if not BPY_AVAILABLE:
        return

    def _apply():
        if _ADAPTER is not None:
            _ADAPTER.set_stage_metadata(**meta)
        else:
            apply_stage_metadata_to_scene(bpy.context.scene, **meta)
        return None

    try:
        bpy.app.timers.register(_apply, first_interval=0.0)
    except RuntimeError:
        LOG.exception("Failed to schedule stage_metadata apply")


def _on_playback_claimed(msg: dict) -> None:
    """We just got granted the playback-leader role."""
    if not BPY_AVAILABLE:
        return

    def _apply():
        scene = bpy.context.scene
        if scene is not None:
            scene.usd_connect_playback_is_leader = True
            scene.usd_connect_playback_leader_id = msg.get("leader_client_id", "")
        return None

    try:
        bpy.app.timers.register(_apply, first_interval=0.0)
    except RuntimeError:
        pass


def _on_playback_rejected(msg: dict) -> None:
    LOG.info(
        "Playback control rejected: %s (current leader: %s)",
        msg.get("reason", ""),
        msg.get("current_leader_client_id", ""),
    )


def _apply_pending_playback_state() -> None:
    """Drain the latest PlaybackState onto the Blender timeline.

    Called from the queue timer (main thread) under the
    ``_set_remote_apply_guard`` so the resulting depsgraph_update_post
    callbacks don't echo the frame change back to the server.
    """
    global _LATEST_PLAYBACK_STATE
    with _PLAYBACK_LOCK:
        state = _LATEST_PLAYBACK_STATE
        _LATEST_PLAYBACK_STATE = None
    if state is None or not BPY_AVAILABLE:
        return
    scene = bpy.context.scene
    if scene is None:
        return
    leader = state.get("leader_client_id", "") or ""
    scene.usd_connect_playback_leader_id = leader
    try:
        from . import STABLE_CLIENT_ID
    except ImportError:
        STABLE_CLIENT_ID = ""
    scene.usd_connect_playback_is_leader = bool(leader) and leader == STABLE_CLIENT_ID
    scene.usd_connect_playback_time = float(state.get("time", 0.0))
    scene.usd_connect_playback_playing = bool(state.get("playing", False))
    # Vacant-leader broadcast (server clears the seat on disconnect): the
    # message is a UI notification only, not a drive command. Followers
    # shouldn't be yanked back to time=0 just because the leader left.
    if not leader:
        return
    # The leader's own client mustn't echo its own state back into frame_set
    # the user's local scrub already moved the playhead.
    if scene.usd_connect_playback_is_leader:
        return
    fps = max(1.0, float(scene.render.fps or 24.0))
    tcps = max(1.0, float(getattr(scene, "usd_connect_tcps", 24.0)))
    target_frame = int(round(float(state.get("time", 0.0)) / tcps * fps))
    # Skip the (expensive) depsgraph eval when we'd land on the same frame
    # the leader resending the same timecode shouldn't burn cycles.
    if target_frame == scene.frame_current:
        return
    try:
        scene.frame_set(target_frame)
    except RuntimeError:
        LOG.exception("frame_set failed for playback state")


def _on_imported(prim_paths: list[str]) -> None:
    """Defer emitter cache seeding until after view_layer.update() runs.

    The dispatcher fires this inside its suppress block, before any
    Blender post-tick evaluation.  Stash the paths and seed them later
    in the timer cycle (after view_layer.update) so matrices reflect
    the final evaluated state.
    """
    _pending_import_seed_paths.update(prim_paths)


def _on_applied(prim_paths: list[str]) -> None:
    """Collect applied shader paths for post-evaluation baseline refresh.

    Shader baseline work is filtered to paths known by the adapter registry.
    """
    if _ADAPTER is None:
        return
    registry = _ADAPTER._registry
    _pending_shader_baseline_paths.update(
        prim_path for prim_path in prim_paths if registry.get_shader(prim_path)
    )


_OBJECT_DIRTYING_EVENT_KINDS = frozenset(
    {K_ENSURE_XFORM_OPS, K_SET_XFORM_TRS, K_SET_GPRIM_ATTRS},
)


def _on_applied_events(events: list[dict]) -> None:
    """Collect object paths that may fire delayed depsgraph callbacks.

    Filtering here keeps the post-evaluation refresh O(changed object paths)
    even for transactions containing large material or shader networks.
    """
    for event in events:
        kind = event.get("k")
        prim_path = event.get("prim")
        if not prim_path:
            continue
        if kind in _OBJECT_DIRTYING_EVENT_KINDS:
            _pending_object_baseline_paths.add(prim_path)
            continue
        if kind == K_ENSURE_PRIM:
            type_name = event.get("typeName", "Xform")
            if type_name != "DomeLight" and type_name not in BlenderAdapter._NON_SCENE_TYPES:
                _pending_object_baseline_paths.add(prim_path)


def _on_resync() -> None:
    """Reset the adapter so all caches (including _imported_refs) are clean."""
    global _ADAPTER
    LOG.info("Server requested resync resetting adapter")
    _pending_import_seed_paths.clear()
    _pending_object_baseline_paths.clear()
    _pending_shader_baseline_paths.clear()
    mirror_stage = _ensure_mirror_stage(reset=True)
    _ADAPTER = None
    if _DISPATCHER is not None:
        _DISPATCHER.adapter = _ensure_adapter()
        _DISPATCHER.mirror_stage = mirror_stage
        _DISPATCHER.adapter.mirror_stage = mirror_stage


def _build_dispatcher(receiver: ReceiverThread) -> EventDispatcher:
    """Construct a dispatcher over the receiver-owned layered USD mirror."""
    mirror_stage = _ensure_mirror_stage()
    adapter = _ensure_adapter()
    adapter.mirror_stage = mirror_stage
    return EventDispatcher(
        receiver=receiver,
        adapter=adapter,
        mirror_stage=mirror_stage,
        on_imported=_on_imported,
        on_resync=_on_resync,
        on_applied=_on_applied,
        on_applied_events=_on_applied_events,
    )


def _drain_and_apply_remote_events() -> int:
    """Apply one queued batch through the receiver's layered USD mirror."""
    global _LAST_SEQ, _DISPATCHER
    if _RECEIVER is None:
        return 0
    if _DISPATCHER is None:
        _DISPATCHER = _build_dispatcher(_RECEIVER)
        _DISPATCHER.last_seq = _LAST_SEQ

    mirror_stage = _ensure_mirror_stage()
    if mirror_stage is not _DISPATCHER.mirror_stage:
        _DISPATCHER.mirror_stage = mirror_stage
        _DISPATCHER.bind_layered_stage(mirror_stage)
    _DISPATCHER.adapter.mirror_stage = mirror_stage

    _DISPATCHER.last_seq = _LAST_SEQ
    applied_count = _DISPATCHER.drain_and_apply()
    _LAST_SEQ = _DISPATCHER.last_seq
    return applied_count


def _apply_received_events_timer():
    """Apply queued events and refresh Blender under the feedback guard."""
    global _RECEIVER_TIMER_REGISTERED, _RECEIVER
    if _RECEIVER is None:
        return None  # Unregister timer

    if _RECEIVER.hello_rejected:
        reason = _RECEIVER.rejection_reason or "receiver connection rejected"
        LOG.error("OpenUSDConnect receiver rejected: %s", reason)
        receiver = _RECEIVER
        _RECEIVER = None
        _stop_receiver_thread(receiver)
        _RECEIVER_TIMER_REGISTERED = False
        scene = bpy.context.scene
        if scene is not None:
            scene.usd_connect_recv_running = False
        _set_remote_apply_guard(False)
        return None

    # Hold the feedback guard for the entire batch INCLUDING view_layer.update()
    # so the depsgraph handler doesn't echo received changes back to the server.
    _set_remote_apply_guard(True)
    try:
        applied_count = _drain_and_apply_remote_events()
        # Apply the latest PlaybackState (if any) under the same guard so
        # frame_set's recursive depsgraph eval stays suppressed.
        _apply_pending_playback_state()

        if applied_count > 0:
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
            if (
                _pending_import_seed_paths
                or _pending_object_baseline_paths
                or _pending_shader_baseline_paths
            ):
                capture_module = _get_capture_module()
                if capture_module is not None:
                    changed_objects_by_prim = {}
                    if _ADAPTER is not None:
                        registry = _ADAPTER._registry
                        for prim_path in _pending_object_baseline_paths:
                            obj = registry.find(prim_path)
                            if obj is not None:
                                changed_objects_by_prim[prim_path] = obj
                    capture_module.refresh_transform_baselines_after_remote_apply(
                        changed_objects_by_prim,
                    )
                    for prim_path in _pending_import_seed_paths:
                        capture_module.seed_emitter_caches_for_import(prim_path)
                    # Seed reverse-sync shader state (input maps + value
                    # baselines) for imported subtrees and applied shader
                    # prims so the emitter can read values back.
                    if (
                        capture_module._state.author is not None
                        and _ADAPTER is not None
                    ):
                        for prim_path in (
                            _pending_import_seed_paths
                            | _pending_shader_baseline_paths
                        ):
                            _refresh_shader_reverse_sync_state(
                                capture_module._state.author,
                                _ADAPTER,
                                prim_path,
                            )
                _pending_import_seed_paths.clear()
                _pending_object_baseline_paths.clear()
                _pending_shader_baseline_paths.clear()
    finally:
        _set_remote_apply_guard(False)

    scene = bpy.context.scene
    if scene:
        _store_last_sequence(scene, _LAST_SEQ)

    return 0.01  # Run again in 10ms


def _unregister_receiver_timer() -> None:
    global _RECEIVER_TIMER_REGISTERED
    if (
        BPY_AVAILABLE
        and _RECEIVER_TIMER_REGISTERED
        and bpy.app.timers.is_registered(_apply_received_events_timer)
    ):
        bpy.app.timers.unregister(_apply_received_events_timer)
    _RECEIVER_TIMER_REGISTERED = False


class USD_CONNECT_OT_start_receiver(bpy.types.Operator):
    bl_idname = "usd_connect.start_receiver"
    bl_label = "Start Receiver"
    bl_description = "Connect to the sync server and receive scene edits"

    def execute(self, context):
        global _RECEIVER, _DISPATCHER, _RECEIVER_TIMER_REGISTERED, _LAST_SEQ, _ADAPTER
        global _MIRROR_ENDPOINT, _MIRROR_LAYERED_REPLAY
        if _RECEIVER is not None:
            self.report({"INFO"}, "Receiver already running")
            return {"CANCELLED"}
        scene = context.scene
        host = scene.usd_connect_recv_host
        port = scene.usd_connect_recv_port

        # Load persisted sequence from scene property
        if _LAST_SEQ == 0 and hasattr(scene, "usd_connect_recv_last_seq"):
            _LAST_SEQ = scene.usd_connect_recv_last_seq

        source = _configured_base_path()
        live_metadata = _live_snapshot_metadata(source)
        layered_replay = live_metadata is None
        mirror_reusable = (
            _MIRROR_STAGE is not None
            and _MIRROR_SOURCE == source
            and _MIRROR_ENDPOINT == (host, port)
            and _MIRROR_LAYERED_REPLAY is layered_replay
            and _DISPATCHER is not None
        )
        plan = _replay_plan(
            last_seq=_LAST_SEQ,
            mirror_reusable=mirror_reusable,
            live_metadata=live_metadata,
        )

        if not mirror_reusable:
            if _DISPATCHER is not None:
                _DISPATCHER.close()
                _DISPATCHER = None
            _LAST_SEQ = plan.baseline_seq
            _ADAPTER = None
            _ensure_mirror_stage(reset=True)

        try:
            from . import STABLE_CLIENT_ID

            _RECEIVER = ReceiverThread(
                host=host,
                port=port,
                sync_from=plan.sync_from,
                client_id=STABLE_CLIENT_ID,
                origin=_ORIGIN,
                department=scene.usd_connect_department or None,
                token=token_client.load_token(host, port),
                on_token_issued=lambda token: token_client.save_token(host, port, token),
                on_stage_metadata=_on_stage_metadata,
                on_playback_state=_on_playback_state,
                on_playback_claimed=_on_playback_claimed,
                on_playback_rejected=_on_playback_rejected,
                layered_replay=plan.layered_replay,
            )
            _RECEIVER.start()
            if _DISPATCHER is None:
                _DISPATCHER = _build_dispatcher(_RECEIVER)
            else:
                _DISPATCHER.receiver = _RECEIVER
            _DISPATCHER.last_seq = _LAST_SEQ
            _MIRROR_ENDPOINT = (host, port)
            _MIRROR_LAYERED_REPLAY = plan.layered_replay
            if not _RECEIVER_TIMER_REGISTERED:
                bpy.app.timers.register(_apply_received_events_timer, first_interval=0.01)
                _RECEIVER_TIMER_REGISTERED = True
            scene.usd_connect_recv_running = True
            self.report(
                {"INFO"},
                f"Receiver started ({host}:{port}), sync from seq={plan.sync_from}",
            )
        except Exception as exc:
            LOG.exception("Failed to start receiver")
            if _RECEIVER is not None and getattr(_RECEIVER, "auth_rejected", False):
                token_client.delete_token(host, port)
            if _RECEIVER is not None:
                _stop_receiver_thread(_RECEIVER)
                _RECEIVER = None
            _set_remote_apply_guard(False)
            _unregister_receiver_timer()
            scene.usd_connect_recv_running = False
            self.report({"ERROR"}, f"Failed to start receiver: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class USD_CONNECT_OT_stop_receiver(bpy.types.Operator):
    bl_idname = "usd_connect.stop_receiver"
    bl_label = "Stop Receiver"

    def execute(self, context):
        global _RECEIVER
        if _RECEIVER is not None:
            receiver = _RECEIVER
            _RECEIVER = None
            _stop_receiver_thread(receiver)
            _store_last_sequence(context.scene, _LAST_SEQ)
        _unregister_receiver_timer()
        _set_remote_apply_guard(False)
        context.scene.usd_connect_recv_running = False
        self.report({"INFO"}, f"Receiver stopped at seq={_LAST_SEQ}")
        return {"FINISHED"}


def _get_active_sender():
    """Locate the EventSender owned by the capture module, if any."""
    capture_module = _get_capture_module()
    if capture_module is None:
        return None
    state = getattr(capture_module, "_state", None)
    if state is None:
        return None
    return getattr(state, "sender", None)


def _frame_change_to_playback_control(scene):
    """Leader broadcasts its current frame as a PlaybackControl.

    Bound to ``bpy.app.handlers.frame_change_post`` fires once per
    timeline frame change, covering both autonomous playback (Spacebar)
    and manual scrubs. Only the local playback leader emits; followers
    skip. ``_APPLYING_REMOTE`` short-circuits the case where a frame
    change is itself the consequence of a received ``PlaybackState``
    (the receiver path calls ``scene.frame_set``, which fires this
    handler without the guard we'd echo our own snapshot back to
    the server).
    """
    if _APPLYING_REMOTE:
        return
    if not getattr(scene, "usd_connect_playback_is_leader", False):
        return
    sender = _get_active_sender()
    if sender is None or not sender.connected:
        return
    fps = max(1.0, float(scene.render.fps or 24.0))
    tcps = max(1.0, float(getattr(scene, "usd_connect_tcps", 24.0)))
    timecode = float(scene.frame_current) / fps * tcps
    sender.send_playback_control("set_time", time=timecode)


class USD_CONNECT_OT_claim_playback(bpy.types.Operator):
    bl_idname = "usd_connect.claim_playback"
    bl_label = "Claim Playback"
    bl_description = "Take the playback-leader role on the server"

    def execute(self, context):
        sender = _get_active_sender()
        if sender is None or not sender.connected:
            self.report({"WARNING"}, "Emitter must be connected to claim playback")
            return {"CANCELLED"}
        # Include our current frame as the initial timecode so the server
        # sets the shared playhead to our position atomically with the
        # grant avoids followers snapping to the server's stale value
        # (typically 0) on a fresh claim.
        scene = context.scene
        fps = max(1.0, float(scene.render.fps or 24.0))
        tcps = max(1.0, float(getattr(scene, "usd_connect_tcps", 24.0)))
        timecode = float(scene.frame_current) / fps * tcps
        if not sender.claim_playback(time=timecode):
            self.report({"ERROR"}, "Failed to send claim_playback")
            return {"CANCELLED"}
        self.report({"INFO"}, "Playback claim sent")
        return {"FINISHED"}


class USD_CONNECT_OT_release_playback(bpy.types.Operator):
    bl_idname = "usd_connect.release_playback"
    bl_label = "Release Playback"
    bl_description = "Release the playback-leader role (server clears it on disconnect)"

    def execute(self, context):
        # The protocol doesn't ship an explicit Release message: the server
        # releases when the client disconnects or another client claims.
        # For now we just flip the local UI flag; full release is implicit.
        context.scene.usd_connect_playback_is_leader = False
        self.report({"INFO"}, "Local leader flag cleared (server still tracks on disconnect)")
        return {"FINISHED"}


class USD_CONNECT_OT_playback_play(bpy.types.Operator):
    bl_idname = "usd_connect.playback_play"
    bl_label = "Play"

    def execute(self, context):
        sender = _get_active_sender()
        if sender is None or not sender.connected:
            self.report({"WARNING"}, "Not connected")
            return {"CANCELLED"}
        sender.send_playback_control("play")
        return {"FINISHED"}


class USD_CONNECT_OT_playback_pause(bpy.types.Operator):
    bl_idname = "usd_connect.playback_pause"
    bl_label = "Pause"

    def execute(self, context):
        sender = _get_active_sender()
        if sender is None or not sender.connected:
            self.report({"WARNING"}, "Not connected")
            return {"CANCELLED"}
        sender.send_playback_control("pause")
        return {"FINISHED"}


class USD_CONNECT_OT_playback_set_time(bpy.types.Operator):
    bl_idname = "usd_connect.playback_set_time"
    bl_label = "Scrub Playhead"
    bl_description = "Push the current Blender frame to the server as a timecode"

    def execute(self, context):
        sender = _get_active_sender()
        if sender is None or not sender.connected:
            self.report({"WARNING"}, "Not connected")
            return {"CANCELLED"}
        scene = context.scene
        fps = max(1.0, float(scene.render.fps or 24.0))
        tcps = max(1.0, float(getattr(scene, "usd_connect_tcps", 24.0)))
        timecode = float(scene.frame_current) / fps * tcps
        sender.send_playback_control("set_time", time=timecode)
        return {"FINISHED"}


class USD_CONNECT_OT_reset_receiver_seq(bpy.types.Operator):
    bl_idname = "usd_connect.reset_receiver_seq"
    bl_label = "Rebuild Replay"
    bl_description = "Discard retained receiver state and rebuild from the configured USD base"

    def execute(self, context):
        global _LAST_SEQ
        _LAST_SEQ = 0
        _discard_replay_state()
        _ensure_mirror_stage(reset=True)
        _store_last_sequence(context.scene, 0)
        self.report({"INFO"}, "Receiver replay state reset")
        return {"FINISHED"}


_RECEIVER_CLASSES = (
    USD_CONNECT_OT_start_receiver,
    USD_CONNECT_OT_stop_receiver,
    USD_CONNECT_OT_reset_receiver_seq,
    USD_CONNECT_OT_claim_playback,
    USD_CONNECT_OT_release_playback,
    USD_CONNECT_OT_playback_play,
    USD_CONNECT_OT_playback_pause,
    USD_CONNECT_OT_playback_set_time,
)


def register():
    _ensure_scene_props()
    if BPY_AVAILABLE:
        for c in _RECEIVER_CLASSES:
            bpy.utils.register_class(c)
        if _frame_change_to_playback_control not in bpy.app.handlers.frame_change_post:
            bpy.app.handlers.frame_change_post.append(_frame_change_to_playback_control)


def unregister():
    global _RECEIVER
    if _RECEIVER is not None:
        receiver = _RECEIVER
        _RECEIVER = None
        _stop_receiver_thread(receiver)
    _discard_replay_state()
    _unregister_receiver_timer()
    if BPY_AVAILABLE:
        if _frame_change_to_playback_control in bpy.app.handlers.frame_change_post:
            bpy.app.handlers.frame_change_post.remove(_frame_change_to_playback_control)
        for c in reversed(_RECEIVER_CLASSES):
            bpy.utils.unregister_class(c)
    recv_props = (
        "usd_connect_recv_host",
        "usd_connect_recv_port",
        "usd_connect_recv_running",
        "usd_connect_recv_last_seq",
        "usd_connect_playback_is_leader",
        "usd_connect_playback_leader_id",
        "usd_connect_playback_time",
        "usd_connect_playback_playing",
        "usd_connect_tcps",
    )
    for prop_name in recv_props:
        if hasattr(bpy.types.Scene, prop_name):
            try:
                delattr(bpy.types.Scene, prop_name)
            except (AttributeError, RuntimeError):
                LOG.debug(
                    "Could not unregister Blender scene property %s",
                    prop_name,
                    exc_info=True,
                )
