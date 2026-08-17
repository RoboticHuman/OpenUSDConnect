"""Protocol constants and event ordering for OpenUSDConnect."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

PROTOCOL_VERSION = 12


class LayerMode(StrEnum):
    """How authored layer identity is represented by one server."""

    MANAGED = "managed"
    SHARED_STAGE = "shared_stage"


# Message type constants
MSG_HELLO = "hello"
MSG_HELLO_OK = "hello_ok"
MSG_AUTH_REJECTED = "auth_rejected"
MSG_HELLO_REJECTED = "hello_rejected"
MSG_TXN = "txn"
MSG_EVENT = "event"
MSG_RESYNC = "resync"
MSG_COMPACT = "compact"
MSG_PING = "ping"
MSG_QUIT = "quit"
MSG_RATE_LIMITED = "rate_limited"
MSG_CLAIM_PLAYBACK = "claim_playback"
MSG_PLAYBACK_CLAIMED = "playback_claimed"
MSG_PLAYBACK_REJECTED = "playback_rejected"
MSG_PLAYBACK_CONTROL = "playback_control"
MSG_PLAYBACK_STATE = "playback_state"
MSG_LAYER_STACK_STATE = "layer_stack_state"
MSG_LAYER_GRAPH_STATE = "layer_graph_state"
MSG_TRANSACTION_RESULT = "transaction_result"
MSG_REPLAY_COMPLETE = "replay_complete"

# Event kind constants - use these instead of raw string literals.
K_ENSURE_PRIM = "ensure_prim"
K_ENSURE_XFORM_OPS = "ensure_xform_ops"
K_SET_XFORM_TRS = "set_xform_trs"
K_DELETE_PRIM = "delete_prim"
K_DEACTIVATE_PRIM = "deactivate_prim"
K_RENAME_PRIM = "rename_prim"
K_SET_VISIBILITY = "set_visibility"
K_SET_GPRIM_ATTRS = "set_gprim_attrs"
K_SET_REFERENCE = "set_reference"
K_SET_PAYLOAD = "set_payload"
K_LOAD_PAYLOAD = "load_payload"
K_UNLOAD_PAYLOAD = "unload_payload"
K_SET_VARIANT_SELECTIONS = "set_variant_selections"
K_SET_MATERIAL_BINDING = "set_material_binding"
K_SET_CONNECTABLE_INPUT = "set_connectable_input"
K_SET_CONNECTABLE_CONNECTION = "set_connectable_connection"
K_SET_STAGE_METADATA = "set_stage_metadata"
K_SET_INSTANCEABLE = "set_instanceable"
K_SET_POINT_INSTANCER = "set_point_instancer"
K_SET_SDF_SPEC_FIELDS = "set_sdf_spec_fields"
K_REPLACE_SDF_LAYER_CONTENT = "replace_sdf_layer_content"
K_SET_SUBLAYERS = "set_sublayers"

SDF_SPEC_KIND_LAYER = "layer"
SDF_SPEC_KIND_PRIM = "prim"
SDF_SPEC_KIND_ATTRIBUTE = "attribute"
SDF_SPEC_KIND_RELATIONSHIP = "relationship"
SDF_SPEC_KIND_VARIANT_SET = "variant_set"
SDF_SPEC_KIND_VARIANT = "variant"
SDF_SPEC_KIND_PROPERTY = "property"
SDF_SPEC_KINDS = (
    SDF_SPEC_KIND_LAYER,
    SDF_SPEC_KIND_PRIM,
    SDF_SPEC_KIND_ATTRIBUTE,
    SDF_SPEC_KIND_RELATIONSHIP,
    SDF_SPEC_KIND_VARIANT_SET,
    SDF_SPEC_KIND_VARIANT,
    SDF_SPEC_KIND_PROPERTY,
)
SDF_LAYER_TOPOLOGY_FIELDS = frozenset({"subLayers", "subLayerOffsets"})

# Sdf list-op buckets carried by reference and payload arc entries.
ARC_LIST_POSITIONS = frozenset(
    {
        "explicit",
        "added",
        "prepended",
        "appended",
        "deleted",
        "ordered",
    }
)

# Array/relationship fields carried by a SetPointInstancer event, in wire
# bitmask order. Authoritative list: the codec, adapter dispatch, and
# emitter diff path all derive from it.
POINT_INSTANCER_FIELDS = (
    "prototypes",
    "proto_indices",
    "positions",
    "orientations",
    "scales",
    "velocities",
    "accelerations",
    "angular_velocities",
    "ids",
    "invisible_ids",
    "inactive_ids",
)

# Fields carried by a SetStageMetadata event. Authoritative list used by
# the codec, adapter dispatch, and emitter diff path.
STAGE_METADATA_KEYS = (
    "timeCodesPerSecond",
    "framesPerSecond",
    "startTimeCode",
    "endTimeCode",
    "metersPerUnit",
    "upAxis",
)

PRIMVAR_PREFIX = "primvars:"
REL_MATERIAL_BINDING = "material:binding"


class EventTarget(StrEnum):
    """Destination domain for an event's effect."""

    COLLABORATION_LAYER = "collaboration_layer"
    SESSION_LAYER = "session_layer"
    STAGE_STATE = "stage_state"


class NativeProjectionMode(StrEnum):
    """How a layered receiver maps an event into a non-USD adapter."""

    PROJECT = "project"
    DIRECT = "direct"
    FIELD_ROUTED = "field_routed"


@dataclass(frozen=True)
class EventKindInfo:
    """Classification flags for one event kind.

    The single declaration site for everything that is a property of the
    kind itself: apply ordering and receive-side categorization derive from
    this table. Behavior (encoder, decoder, applier, adapter method, emitter
    invalidator) is registered at the function definition sites.

    create: a prim must exist before anything can author on it; applied
        first, ancestors before descendants.
    structural: creates specs or affects composition, so it must run
        outside the batched value-setting ChangeBlock.
    stage_sync: mutates USD scene description that a receive-side mirror
        stage must track on its own (composition arcs, materials,
        shaders, stage metadata).
    arc: re-applying identical state would still trigger recomposition
        (ClearReferences plus re-add, variant re-select), so receivers
        skip-detect it against the mirror's composed state.
    imports: application brings new content into the consumer; fires the
        dispatcher's on_imported callback.
    native_projection: how a layered receiver maps the event into a non-USD
        adapter after applying it to the receiver-owned USD mirror.
    modes: protocol layer modes in which the event is valid. Some exact Sdf
        fallback events are intentionally valid in both managed and shared
        stage mode.
    target: whether the event authors into a collaboration layer, authors
        shared stage metadata into the primary session layer, or changes
        non-authored stage state such as payload load rules.
    """

    native_projection: NativeProjectionMode
    modes: frozenset[LayerMode] = frozenset({LayerMode.MANAGED})
    create: bool = False
    structural: bool = False
    stage_sync: bool = False
    arc: bool = False
    imports: bool = False
    target: EventTarget = EventTarget.COLLABORATION_LAYER


EVENT_KIND_INFO: dict[str, EventKindInfo] = {
    K_ENSURE_PRIM: EventKindInfo(
        native_projection=NativeProjectionMode.PROJECT,
        create=True,
        structural=True,
    ),
    K_ENSURE_XFORM_OPS: EventKindInfo(
        native_projection=NativeProjectionMode.PROJECT,
        structural=True,
    ),
    K_SET_XFORM_TRS: EventKindInfo(
        native_projection=NativeProjectionMode.PROJECT,
    ),
    K_DELETE_PRIM: EventKindInfo(
        native_projection=NativeProjectionMode.PROJECT,
    ),
    K_DEACTIVATE_PRIM: EventKindInfo(
        native_projection=NativeProjectionMode.PROJECT,
    ),
    K_RENAME_PRIM: EventKindInfo(
        native_projection=NativeProjectionMode.PROJECT,
    ),
    K_SET_VISIBILITY: EventKindInfo(
        native_projection=NativeProjectionMode.PROJECT,
    ),
    K_SET_GPRIM_ATTRS: EventKindInfo(native_projection=NativeProjectionMode.PROJECT),
    K_SET_REFERENCE: EventKindInfo(
        native_projection=NativeProjectionMode.PROJECT,
        structural=True,
        stage_sync=True,
        arc=True,
        imports=True,
    ),
    K_SET_PAYLOAD: EventKindInfo(
        native_projection=NativeProjectionMode.PROJECT,
        structural=True,
        stage_sync=True,
        arc=True,
    ),
    K_LOAD_PAYLOAD: EventKindInfo(
        native_projection=NativeProjectionMode.DIRECT,
        structural=True,
        stage_sync=True,
        imports=True,
        target=EventTarget.STAGE_STATE,
    ),
    K_UNLOAD_PAYLOAD: EventKindInfo(
        native_projection=NativeProjectionMode.DIRECT,
        structural=True,
        stage_sync=True,
        target=EventTarget.STAGE_STATE,
    ),
    K_SET_VARIANT_SELECTIONS: EventKindInfo(
        native_projection=NativeProjectionMode.PROJECT,
        structural=True,
        stage_sync=True,
        arc=True,
    ),
    K_SET_MATERIAL_BINDING: EventKindInfo(
        native_projection=NativeProjectionMode.PROJECT,
        structural=True,
        stage_sync=True,
    ),
    K_SET_CONNECTABLE_INPUT: EventKindInfo(
        native_projection=NativeProjectionMode.PROJECT,
        structural=True,
        stage_sync=True,
    ),
    K_SET_CONNECTABLE_CONNECTION: EventKindInfo(
        native_projection=NativeProjectionMode.PROJECT,
        structural=True,
        stage_sync=True,
    ),
    K_SET_STAGE_METADATA: EventKindInfo(
        native_projection=NativeProjectionMode.DIRECT,
        create=True,
        structural=True,
        stage_sync=True,
        target=EventTarget.SESSION_LAYER,
    ),
    K_SET_INSTANCEABLE: EventKindInfo(
        native_projection=NativeProjectionMode.PROJECT,
        structural=True,
        stage_sync=True,
    ),
    K_SET_POINT_INSTANCER: EventKindInfo(native_projection=NativeProjectionMode.PROJECT),
    K_SET_SDF_SPEC_FIELDS: EventKindInfo(
        native_projection=NativeProjectionMode.FIELD_ROUTED,
        modes=frozenset({LayerMode.MANAGED, LayerMode.SHARED_STAGE}),
        structural=True,
        stage_sync=True,
    ),
    K_REPLACE_SDF_LAYER_CONTENT: EventKindInfo(
        native_projection=NativeProjectionMode.DIRECT,
        modes=frozenset({LayerMode.SHARED_STAGE}),
        structural=True,
        stage_sync=True,
    ),
    K_SET_SUBLAYERS: EventKindInfo(
        native_projection=NativeProjectionMode.DIRECT,
        modes=frozenset({LayerMode.SHARED_STAGE}),
        structural=True,
        stage_sync=True,
    ),
}

EVENT_KEYS = frozenset(EVENT_KIND_INFO)
STRUCTURAL_EVENT_KINDS = frozenset(k for k, i in EVENT_KIND_INFO.items() if i.structural)
CREATE_KINDS = frozenset(k for k, i in EVENT_KIND_INFO.items() if i.create)
STAGE_SYNC_KINDS = frozenset(k for k, i in EVENT_KIND_INFO.items() if i.stage_sync)
NATIVE_PROJECTED_KINDS = frozenset(
    k for k, i in EVENT_KIND_INFO.items() if i.native_projection == NativeProjectionMode.PROJECT
)
NATIVE_DIRECT_KINDS = frozenset(
    k for k, i in EVENT_KIND_INFO.items() if i.native_projection == NativeProjectionMode.DIRECT
)
NATIVE_FIELD_ROUTED_KINDS = frozenset(
    k
    for k, i in EVENT_KIND_INFO.items()
    if i.native_projection == NativeProjectionMode.FIELD_ROUTED
)
COLLABORATION_LAYER_KINDS = frozenset(
    k for k, i in EVENT_KIND_INFO.items() if i.target == EventTarget.COLLABORATION_LAYER
)
SESSION_LAYER_KINDS = frozenset(
    k for k, i in EVENT_KIND_INFO.items() if i.target == EventTarget.SESSION_LAYER
)
STAGE_RUNTIME_KINDS = frozenset(
    k for k, i in EVENT_KIND_INFO.items() if i.target == EventTarget.STAGE_STATE
)
NON_COLLABORATION_KINDS = SESSION_LAYER_KINDS | STAGE_RUNTIME_KINDS
ARC_KINDS = frozenset(k for k, i in EVENT_KIND_INFO.items() if i.arc)
IMPORT_KINDS = frozenset(k for k, i in EVENT_KIND_INFO.items() if i.imports)
MANAGED_KINDS = frozenset(k for k, i in EVENT_KIND_INFO.items() if LayerMode.MANAGED in i.modes)
SHARED_STAGE_EVENT_KINDS = frozenset(
    k for k, i in EVENT_KIND_INFO.items() if LayerMode.SHARED_STAGE in i.modes
)
SHARED_STAGE_ONLY_KINDS = SHARED_STAGE_EVENT_KINDS - MANAGED_KINDS
assert MANAGED_KINDS | SHARED_STAGE_EVENT_KINDS == EVENT_KEYS, "every event kind must have a mode"
assert all(info.modes for info in EVENT_KIND_INFO.values()), "every event kind needs a mode"


def event_apply_tier(kind: str) -> int:
    """Coarse apply tier for deterministic ordering: 0 = create (prims first),
    1 = structural modify (arcs/connections on existing prims), 2 = value +
    destructive. Replaces the old fine-grained per-kind sequence."""
    if kind in CREATE_KINDS:
        return 0
    if kind in STRUCTURAL_EVENT_KINDS:
        return 1
    return 2


TRS_FIELDS = frozenset({"t", "r", "s"})
