"""Protocol constants and event ordering for OpenUSDConnect."""

from __future__ import annotations

from dataclasses import dataclass

PROTOCOL_VERSION = 2

# Message type constants
MSG_HELLO = "hello"
MSG_HELLO_OK = "hello_ok"
MSG_AUTH_REJECTED = "auth_rejected"
MSG_TXN = "txn"
MSG_EVENT = "event"
MSG_RESYNC = "resync"
MSG_COMPACT = "compact"
MSG_PING = "ping"
MSG_QUIT = "quit"
MSG_CREATE_PROPOSAL = "create_proposal"
MSG_PROPOSAL_CREATED = "proposal_created"
MSG_RATE_LIMITED = "rate_limited"
MSG_CLAIM_PLAYBACK = "claim_playback"
MSG_PLAYBACK_CLAIMED = "playback_claimed"
MSG_PLAYBACK_REJECTED = "playback_rejected"
MSG_PLAYBACK_CONTROL = "playback_control"
MSG_PLAYBACK_STATE = "playback_state"

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
K_SET_SDF_PROPERTY_FIELDS = "set_sdf_property_fields"

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

# Fields carried by a SetStageMetadata event. Authoritative list — used by
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

@dataclass(frozen=True)
class EventKindInfo:
    """Classification flags for one event kind.

    The single declaration site for everything that is a property of the
    kind itself: apply ordering, receive-side categorization, and the
    server's layer-strength checks all derive from this table. Behavior
    (encoder, decoder, applier, adapter method, emitter invalidator) is
    registered at the function definition sites.

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
    strength_attrs: what the server inspects for the per-client layer
        strength check. None means not checked (always broadcast), an
        empty tuple means strongest-spec check (structural ops), names
        mean a per-attribute check ("meta:X" is a prim metadata field,
        "rel:X" a relationship).
    """

    create: bool = False
    structural: bool = False
    stage_sync: bool = False
    arc: bool = False
    imports: bool = False
    strength_attrs: tuple[str, ...] | None = None


EVENT_KIND_INFO: dict[str, EventKindInfo] = {
    K_ENSURE_PRIM: EventKindInfo(create=True, structural=True, strength_attrs=()),
    K_ENSURE_XFORM_OPS: EventKindInfo(structural=True, strength_attrs=()),
    K_SET_XFORM_TRS: EventKindInfo(
        strength_attrs=("xformOp:translate", "xformOp:orient", "xformOp:scale"),
    ),
    K_DELETE_PRIM: EventKindInfo(strength_attrs=()),
    K_DEACTIVATE_PRIM: EventKindInfo(strength_attrs=("meta:active",)),
    K_RENAME_PRIM: EventKindInfo(strength_attrs=()),
    K_SET_VISIBILITY: EventKindInfo(strength_attrs=("visibility",)),
    K_SET_GPRIM_ATTRS: EventKindInfo(),
    K_SET_REFERENCE: EventKindInfo(
        structural=True, stage_sync=True, arc=True, imports=True,
    ),
    K_SET_PAYLOAD: EventKindInfo(structural=True, stage_sync=True, arc=True),
    K_LOAD_PAYLOAD: EventKindInfo(structural=True, stage_sync=True, imports=True),
    K_UNLOAD_PAYLOAD: EventKindInfo(structural=True, stage_sync=True),
    K_SET_VARIANT_SELECTIONS: EventKindInfo(
        structural=True, stage_sync=True, arc=True,
    ),
    K_SET_MATERIAL_BINDING: EventKindInfo(
        structural=True, stage_sync=True, strength_attrs=("rel:material:binding",),
    ),
    K_SET_CONNECTABLE_INPUT: EventKindInfo(structural=True, stage_sync=True),
    K_SET_CONNECTABLE_CONNECTION: EventKindInfo(structural=True, stage_sync=True),
    K_SET_STAGE_METADATA: EventKindInfo(create=True, structural=True, stage_sync=True),
    K_SET_INSTANCEABLE: EventKindInfo(structural=True, stage_sync=True),
    K_SET_POINT_INSTANCER: EventKindInfo(),
    K_SET_SDF_PROPERTY_FIELDS: EventKindInfo(structural=True, stage_sync=True),
}

EVENT_KEYS = frozenset(EVENT_KIND_INFO)
STRUCTURAL_EVENT_KINDS = frozenset(
    k for k, i in EVENT_KIND_INFO.items() if i.structural
)
CREATE_KINDS = frozenset(k for k, i in EVENT_KIND_INFO.items() if i.create)
STAGE_SYNC_KINDS = frozenset(k for k, i in EVENT_KIND_INFO.items() if i.stage_sync)
ARC_KINDS = frozenset(k for k, i in EVENT_KIND_INFO.items() if i.arc)
IMPORT_KINDS = frozenset(k for k, i in EVENT_KIND_INFO.items() if i.imports)


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
