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

# Event kind constants - use these instead of raw string literals.
K_ENSURE_PRIM = "ensure_prim"
K_ENSURE_XFORM_OPS = "ensure_xform_ops"
K_SET_XFORM_TRS = "set_xform_trs"
K_SET_XFORM_MATRICES = "set_xform_matrices"
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
K_SET_SHADER_INPUT = "set_shader_input"
K_SET_SHADER_CONNECTION = "set_shader_connection"

PRIMVAR_PREFIX = "primvars:"
REL_MATERIAL_BINDING = "material:binding"

EVENT_KEYS = frozenset(
    {
        K_ENSURE_PRIM,
        K_ENSURE_XFORM_OPS,
        K_SET_XFORM_TRS,
        K_SET_XFORM_MATRICES,
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
        K_SET_SHADER_INPUT,
        K_SET_SHADER_CONNECTION,
    }
)

# Event application order - structural first, then composition arcs
# (V before R before P per LIVERPS), then local value opinions (L),
# destructive last.  This is dependency order (prim must exist before
# values can be set), not strength order.  LIVERPS strength (L strongest,
# S weakest) is handled by USD's composition engine, not by event ordering.
# fmt: off
_EVENT_KIND_SEQUENCE = [
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_SET_VARIANT_SELECTIONS,  # V in LIVERPS - before R
    K_SET_REFERENCE,
    K_SET_PAYLOAD,
    K_LOAD_PAYLOAD,
    K_SET_XFORM_TRS,
    K_SET_VISIBILITY,
    K_SET_GPRIM_ATTRS,
    K_SET_SHADER_INPUT,
    K_SET_SHADER_CONNECTION,
    K_SET_XFORM_MATRICES,
    K_SET_MATERIAL_BINDING,
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_RENAME_PRIM,
    K_UNLOAD_PAYLOAD,
]
# fmt: on
EVENT_KIND_ORDER: dict[str, int] = {k: i for i, k in enumerate(_EVENT_KIND_SEQUENCE)}

# Events that must be applied outside a ChangeBlock.
#
# The historical name is kept for API compatibility. Some entries are not
# structural in the USD schema sense, but must still run before the batched
# value-setting ChangeBlock because they create specs or affect composition.
STRUCTURAL_EVENT_KINDS = frozenset(
    {
        K_ENSURE_PRIM,
        K_ENSURE_XFORM_OPS,
        K_SET_VARIANT_SELECTIONS,
        K_SET_REFERENCE,
        K_SET_PAYLOAD,
        K_LOAD_PAYLOAD,
        K_UNLOAD_PAYLOAD,
        K_SET_MATERIAL_BINDING,
        K_SET_SHADER_INPUT,
        K_SET_SHADER_CONNECTION,
    }
)

TRS_FIELDS = frozenset({"t", "r", "s"})
