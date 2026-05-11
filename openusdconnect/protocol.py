"""Protocol constants, dict builders, and validation.

String constants for message types (MSG_*) and event kinds (K_*), plus
helpers for constructing well-formed protocol dicts (make_hello, make_txn,
etc.) and validating inbound events.

The wire format is length-prefixed FlatBuffers — see codec.py and framing.py.
The FlatBuffers schema (schema/events.fbs, schema/messages.fbs) is the
canonical reference for per-event field definitions.  This module defines
the Python-side constants and dict-level contracts; the codec handles
dict ↔ FlatBuffers conversion.
"""

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

# Event kind constants — use these instead of raw string literals.
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

# Valid event keys
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

# Event application order — structural first, then composition arcs
# (V before R before P per LIVERPS), then local value opinions (L),
# destructive last.  This is dependency order (prim must exist before
# values can be set), not strength order.  LIVERPS strength (L strongest,
# S weakest) is handled by USD's composition engine, not by event ordering.
_EVENT_KIND_SEQUENCE = [
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_SET_VARIANT_SELECTIONS,   # V in LIVERPS — before R
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
EVENT_KIND_ORDER: dict[str, int] = {
    k: i for i, k in enumerate(_EVENT_KIND_SEQUENCE)
}

# Events that must be applied outside a ChangeBlock (structural ops).
STRUCTURAL_EVENT_KINDS = frozenset({
    K_ENSURE_PRIM, K_ENSURE_XFORM_OPS, K_SET_VARIANT_SELECTIONS,
    K_SET_REFERENCE, K_SET_PAYLOAD, K_LOAD_PAYLOAD, K_UNLOAD_PAYLOAD,
    K_SET_MATERIAL_BINDING, K_SET_SHADER_INPUT, K_SET_SHADER_CONNECTION,
})

# Valid TRS field names
TRS_FIELDS = frozenset({"t", "r", "s"})


def _is_arc_list_valid(arcs: list | None) -> bool:
    """Validate a list of composition arc entries (refs or payloads)."""
    if not isinstance(arcs, list):
        return False
    for entry in arcs:
        if not isinstance(entry, dict):
            return False
        ap = entry.get("asset_path")
        pp = entry.get("prim_path")
        if ap is None and pp is None:
            return False
        if ap is not None and (not isinstance(ap, str) or not ap):
            return False
        if pp is not None and (not isinstance(pp, str) or not pp.startswith("/")):
            return False
    return True


def is_quat_valid(q: list[float]) -> bool:
    """Check that q is a 4-element list of numbers [w, x, y, z]."""
    return isinstance(q, list) and len(q) == 4 and all(isinstance(v, (int, float)) for v in q)


def is_vec3_valid(v: list[float]) -> bool:
    """Check that v is a 3-element list of numbers [x, y, z]."""
    return isinstance(v, list) and len(v) == 3 and all(isinstance(x, (int, float)) for x in v)


def is_mat16_valid(m: list[float]) -> bool:
    """Check that m is a 16-element list of numbers (row-major 4x4 matrix)."""
    return isinstance(m, list) and len(m) == 16 and all(isinstance(x, (int, float)) for x in m)


def clamp_fields(fields: list[str]) -> list[str]:
    """Filter fields list to only valid TRS field names."""
    return [f for f in fields if f in TRS_FIELDS]


def make_hello(
    role: str,
    sync_from: int | None = None,
    client_id: str | None = None,
    origin: str | None = None,
    department: str | None = None,
    token: str | None = None,
) -> dict:
    """Build a hello message.

    Args:
        role: "emitter" or "receiver".
        sync_from: Sequence number to replay from (receivers only).
        client_id: Per-connection identifier.
        origin: Session-level identifier shared by all connections from the
            same DCC instance.  The server uses this to suppress echo —
            events are not broadcast back to receivers with matching origin.
        department: Optional department name (e.g. "animation", "lighting").
            Used by the server for layer ordering when per-client layers
            are enabled.
        token: Authentication token from a previous session (TOFU).
    """
    msg = {"type": MSG_HELLO, "role": role, "protocol_version": PROTOCOL_VERSION}
    if sync_from is not None:
        msg["sync_from"] = sync_from
    if client_id is not None:
        msg["client_id"] = client_id
    if origin is not None:
        msg["origin"] = origin
    if department is not None:
        msg["department"] = department
    if token is not None:
        msg["token"] = token
    return msg


def make_txn(client_id: str, events: list[dict]) -> dict:
    """Build a transaction message."""
    return {"type": MSG_TXN, "client_id": client_id, "events": events}


def make_quit() -> dict:
    """Build a quit message."""
    return {"type": MSG_QUIT}


def validate_event(ev: dict) -> bool:
    """Basic validation that an event dict has required fields."""
    k = ev.get("k")
    if k not in EVENT_KEYS:
        return False
    if "prim" not in ev:
        return False
    if k == K_SET_XFORM_TRS:
        fields = ev.get("fields", [])
        if not isinstance(fields, list):
            return False
        for f in fields:
            if f not in TRS_FIELDS:
                return False
            if f == "t" and not is_vec3_valid(ev.get("t", [])):
                return False
            if f == "r" and not is_quat_valid(ev.get("r", [])):
                return False
            if f == "s" and not is_vec3_valid(ev.get("s", [])):
                return False
    if k == K_SET_XFORM_MATRICES:
        if not is_mat16_valid(ev.get("local_m", [])):
            return False
        if not is_mat16_valid(ev.get("world_m", [])):
            return False
    if k == K_DEACTIVATE_PRIM and not isinstance(ev.get("active"), bool):
        return False
    if k == K_RENAME_PRIM:
        new_name = ev.get("new_name")
        if not isinstance(new_name, str) or not new_name:
            return False
    if k == K_SET_VISIBILITY and not isinstance(ev.get("visible"), bool):
        return False
    if k == K_SET_GPRIM_ATTRS:
        attrs = ev.get("attrs")
        if not isinstance(attrs, dict):
            return False
        if not all(isinstance(key, str) for key in attrs):
            return False
    if k == K_SET_REFERENCE and not _is_arc_list_valid(ev.get("refs")):
        return False
    # load_payload and unload_payload require only "prim" (already validated above)
    if k == K_SET_PAYLOAD and not _is_arc_list_valid(ev.get("payloads")):
        return False
    if k == K_SET_VARIANT_SELECTIONS:
        selections = ev.get("selections")
        if not isinstance(selections, dict):
            return False
        if not all(isinstance(key, str) for key in selections):
            return False
        if not all(isinstance(v, str) for v in selections.values()):
            return False
    if k == K_SET_MATERIAL_BINDING:
        material_path = ev.get("material_path")
        if not isinstance(material_path, str):
            return False
        if material_path and not material_path.startswith("/"):
            return False
    if k == K_SET_SHADER_INPUT:
        # shader_id may be empty for container prims that carry interface
        # inputs without an info:id (NodeGraph, Material).  Apply skips
        # CreateIdAttr when empty.
        shader_id = ev.get("shader_id")
        if not isinstance(shader_id, str):
            return False
        inputs = ev.get("inputs")
        if not isinstance(inputs, dict):
            return False
        if not all(isinstance(key, str) for key in inputs):
            return False
        input_types = ev.get("input_types")
        if not isinstance(input_types, dict):
            return False
    if k == K_SET_SHADER_CONNECTION:
        connections = ev.get("connections", {})
        if not isinstance(connections, dict):
            return False
        for conn in connections.values():
            if not isinstance(conn, dict):
                return False
            if not isinstance(conn.get("source_prim"), str):
                return False
            if not isinstance(conn.get("source_output"), str):
                return False
        disconnections = ev.get("disconnections", [])
        if not isinstance(disconnections, list):
            return False
    return True
