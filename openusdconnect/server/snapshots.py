"""Validate and prepare full-file VFS saves without changing authoritative state.

The caller captures SnapshotState under the stage lock and holds the exclusive
transaction barrier until the replacement has been committed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import NoReturn

from pxr import Sdf, Usd

from ..emitter import NoticeEmitter
from ..protocol_constants import K_DEACTIVATE_PRIM, K_SET_SDF_SPEC_FIELDS, NON_COLLABORATION_KINDS
from . import inspection
from .types import (
    AmbiguousVfsWriteError,
    InvalidVfsWriteError,
    StaleVfsWriteError,
    UnsupportedVfsWriteError,
    VfsWriteAnalysis,
    VfsWriteRejectedError,
)


@dataclass(frozen=True, slots=True)
class SnapshotMetadata:
    scene_id: str
    epoch: int
    seq: int


@dataclass(frozen=True, slots=True)
class SnapshotState:
    scene_id: str
    epoch: int
    seq: int
    prim_types: dict[str, str]
    department_layers: list[str]
    additional_layers: list[str]


@dataclass(frozen=True, slots=True)
class PreparedSnapshot:
    events: list[dict]
    edit_layer: Sdf.Layer
    session_events: list[dict]


def read_snapshot_metadata(stage: Usd.Stage) -> SnapshotMetadata | None:
    """Read and validate the optional identity carried by a VFS snapshot."""
    layer_data = stage.GetRootLayer().customLayerData or {}
    if "openusdconnect" not in layer_data:
        return None
    metadata = layer_data["openusdconnect"]
    if not isinstance(metadata, dict):
        raise InvalidVfsWriteError("uploaded openusdconnect metadata must be a dictionary")
    scene_id = metadata.get("scene_id")
    if not isinstance(scene_id, str) or not scene_id:
        raise InvalidVfsWriteError(
            "uploaded openusdconnect metadata field 'scene_id' must be a non-empty string"
        )
    return SnapshotMetadata(
        scene_id=scene_id,
        epoch=_metadata_int(metadata, "epoch"),
        seq=_metadata_int(metadata, "snapshot_seq"),
    )


def _metadata_int(metadata: dict, key: str) -> int:
    value = metadata.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidVfsWriteError(
            f"uploaded openusdconnect metadata field {key!r} must be a non-negative integer"
        )
    return value


def validate_stage_snapshot(
    uploaded_stage: Usd.Stage,
    uploaded_meta: SnapshotMetadata | None,
    current: SnapshotState,
    *,
    reject_stale: bool,
    reject_ambiguous: bool,
) -> VfsWriteAnalysis:
    """Analyze a full-file save; rejection errors carry the analysis for diagnostics."""
    uploaded_scene_id = uploaded_meta.scene_id if uploaded_meta else None
    uploaded_epoch = uploaded_meta.epoch if uploaded_meta else None
    uploaded_seq = uploaded_meta.seq if uploaded_meta else None
    current_epoch, current_seq = current.epoch, current.seq
    department_layers = current.department_layers
    additional_layers = current.additional_layers
    before_types = current.prim_types
    uploaded_types = inspection.read_prim_types(uploaded_stage)
    before_paths = set(before_types)
    uploaded_paths = set(uploaded_types)
    created_paths = sorted(uploaded_paths - before_paths)
    removed_paths = sorted(
        before_paths - uploaded_paths,
        key=lambda p: p.count("/"),
        reverse=True,
    )
    type_changed_paths = sorted(
        p for p in before_paths & uploaded_paths if before_types[p] != uploaded_types[p]
    )

    analysis = VfsWriteAnalysis(
        status="translated",
        current_epoch=current_epoch,
        current_seq=current_seq,
        uploaded_epoch=uploaded_epoch,
        uploaded_seq=uploaded_seq,
        before_prim_count=len(before_paths),
        uploaded_prim_count=len(uploaded_paths),
        created_prims=created_paths,
        removed_prims=removed_paths,
        type_changed_prims=type_changed_paths,
    )

    def reject(status: str, note: str, error: VfsWriteRejectedError) -> NoReturn:
        error.analysis = replace(analysis, status=status, notes=[note])
        raise error

    if department_layers or additional_layers:
        details = []
        if department_layers:
            details.append(f"department layers: {', '.join(department_layers)}")
        if additional_layers:
            details.append(f"collaboration layers: {', '.join(additional_layers)}")
        reject(
            "unsupported_rejected",
            "translate write fallback is disabled while non-default "
            f"collaboration layers are active ({'; '.join(details)})",
            UnsupportedVfsWriteError(
                "VFS translate writes are disabled while non-default "
                "collaboration layers are active"
            ),
        )

    if uploaded_meta is None:
        reject(
            "metadata_rejected",
            "uploaded snapshot is missing openusdconnect metadata",
            InvalidVfsWriteError("uploaded VFS snapshot is missing openusdconnect metadata"),
        )

    if uploaded_scene_id != current.scene_id:
        reject(
            "metadata_rejected",
            "uploaded snapshot belongs to a different live scene",
            InvalidVfsWriteError(
                "uploaded VFS snapshot scene_id does not match this server: "
                f"file={uploaded_scene_id!r}, server={current.scene_id!r}"
            ),
        )

    uploaded_token = (uploaded_epoch, uploaded_seq)
    current_token = (current_epoch, current_seq)
    if uploaded_token != current_token:
        if uploaded_token < current_token and not reject_stale:
            analysis = replace(
                analysis,
                notes=[
                    "stale snapshot token accepted because stale-write rejection was disabled"
                ],
            )
        else:
            relation = "older" if uploaded_token < current_token else "newer"
            reject(
                "stale_rejected" if relation == "older" else "future_rejected",
                f"uploaded snapshot is {relation} than the current live server state",
                StaleVfsWriteError(
                    "uploaded VFS snapshot token does not match the current server: "
                    f"file epoch/seq={uploaded_epoch}/{uploaded_seq}, "
                    f"server epoch/seq={current_epoch}/{current_seq}"
                ),
            )

    if uploaded_stage.GetEditTarget().GetLayer().subLayerPaths:
        reject(
            "unsupported_rejected",
            "uploaded snapshot contains sublayer topology, which cannot "
            "be mapped into the managed collaboration layer stack",
            UnsupportedVfsWriteError(
                "uploaded VFS snapshot contains unsupported sublayer topology"
            ),
        )

    if reject_ambiguous and _looks_destructively_incomplete(analysis):
        reject(
            "ambiguous_rejected",
            "uploaded snapshot removes a root-level prim or most of the scene; "
            "refusing automatic fallback translation",
            AmbiguousVfsWriteError(
                "uploaded VFS snapshot looks destructively incomplete; "
                f"removed {len(removed_paths)} of {len(before_paths)} prims"
            ),
        )

    return analysis


def _looks_destructively_incomplete(analysis: VfsWriteAnalysis) -> bool:
    removed_fraction = len(analysis.removed_prims) / max(1, analysis.before_prim_count)
    removes_rootish_prim = any(
        path.count("/") <= 1 and path != "/Root" for path in analysis.removed_prims
    )
    return bool(analysis.removed_prims) and (
        analysis.uploaded_prim_count == 0
        or removes_rootish_prim
        or (analysis.before_prim_count >= 10 and removed_fraction >= 0.8)
    )


def create_replacement_stage(stage: Usd.Stage) -> Usd.Stage:
    """Open an isolated edit/session stack; the caller must hold the stage lock."""
    edit_layer = Sdf.Layer.CreateAnonymous("vfs-replacement-edits")
    session_layer = Sdf.Layer.CreateAnonymous("vfs-replacement-session")
    session_layer.subLayerPaths = [edit_layer.identifier]
    replacement = Usd.Stage.Open(
        stage.GetRootLayer(), session_layer, stage.GetPathResolverContext(),
    )
    if replacement is None:
        raise RuntimeError("failed to create the VFS replacement stage")
    replacement.SetEditTarget(Usd.EditTarget(edit_layer))
    return replacement


def prepare_stage_snapshot(
    uploaded_stage: Usd.Stage,
    removed_paths: list[str],
    *,
    replacement_stage: Usd.Stage,
) -> PreparedSnapshot:
    """Translate and validate a replacement without changing live USD state."""
    emitter = NoticeEmitter(uploaded_stage)
    try:
        events = emitter.snapshot_events()
    finally:
        emitter.cleanup()

    # Hide prims that existed in the previous composed stage but are absent
    # from the uploaded snapshot. This lets full-file saves express deletes
    # even when the original prim lives in the immutable base layer.
    for prim_path in removed_paths:
        events.append({"k": K_DEACTIVATE_PRIM, "prim": prim_path, "active": False})

    # Build the complete replacement off-stage first. This validates
    # every generated event and gives us an authored layer that can be
    # installed without incrementally mutating authoritative state.
    from ..event_apply import apply_events
    from ..sdf_spec_delta import validate_spec_delta

    for event in events:
        if event.get("k") == K_SET_SDF_SPEC_FIELDS:
            validate_spec_delta(event)

    replacement_layer = replacement_stage.GetEditTarget().GetLayer()
    replacement_events = [
        event for event in events if event.get("k") not in NON_COLLABORATION_KINDS
    ]
    session_events = [event for event in events if event.get("k") in NON_COLLABORATION_KINDS]
    if replacement_events:
        apply_events(replacement_stage, replacement_events, prevalidated=True)
    if session_events:
        replacement_stage.SetEditTarget(Usd.EditTarget(replacement_stage.GetSessionLayer()))
        apply_events(replacement_stage, session_events, prevalidated=True)

    return PreparedSnapshot(events, replacement_layer, session_events)
