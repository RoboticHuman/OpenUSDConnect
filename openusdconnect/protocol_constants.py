"""Protocol constants and event ordering for OpenUSDConnect."""

from __future__ import annotations

PROTOCOL_VERSION = 1

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

EVENT_KEYS = frozenset(
    {
        K_ENSURE_PRIM,
        K_ENSURE_XFORM_OPS,
        K_SET_XFORM_TRS,
        K_DELETE_PRIM,
        K_DEACTIVATE_PRIM,
        K_RENAME_PRIM,
        K_SET_VISIBILITY,
        K_SET_GPRIM_ATTRS,
        K_SET_REFERENCE,
        K_SET_PAYLOAD,
        K_LOAD_PAYLOAD,
        K_UNLOAD_PAYLOAD,
        K_SET_VARIANT_SELECTIONS,
        K_SET_MATERIAL_BINDING,
        K_SET_CONNECTABLE_INPUT,
        K_SET_CONNECTABLE_CONNECTION,
        K_SET_STAGE_METADATA,
    }
)

# Events that must be applied outside a ChangeBlock.
#
# The historical name is kept for API compatibility. Some entries are not
# structural in the USD schema sense, but must still run before the batched
# value-setting ChangeBlock because they create specs or affect composition.
STRUCTURAL_EVENT_KINDS = frozenset(
    {
        K_SET_STAGE_METADATA,
        K_ENSURE_PRIM,
        K_ENSURE_XFORM_OPS,
        K_SET_VARIANT_SELECTIONS,
        K_SET_REFERENCE,
        K_SET_PAYLOAD,
        K_LOAD_PAYLOAD,
        K_UNLOAD_PAYLOAD,
        K_SET_MATERIAL_BINDING,
        K_SET_CONNECTABLE_INPUT,
        K_SET_CONNECTABLE_CONNECTION,
    }
)

# Prim-creating kinds. The one hard apply-ordering requirement is that a prim
# exists before anything authors on it, so these are applied first (ancestors
# before descendants). Beyond that, USD composition + dangling connection /
# relationship targets make event order irrelevant; there is deliberately no
# fine-grained per-kind ordering table to maintain.
CREATE_KINDS = frozenset({K_SET_STAGE_METADATA, K_ENSURE_PRIM})


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
