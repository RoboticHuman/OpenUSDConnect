"""Event schema, message types, and validation for JSON Lines over TCP protocol.

Message types:
  hello:   {"type":"hello","role":"emitter"|"receiver","sync_from":<int optional>}
  txn:     {"type":"txn","client_id":"...", "events":[ <event>, ... ]}
  event:   {"type":"event","seq":123,"event":{...}}   (server broadcasts)
  resync:  {"type":"resync"}  (server broadcasts after log compaction —
           receivers must reset their sequence counter and expect a full replay)
  compact: {"type":"compact"}  (client requests server to compact event log;
           triggers resync broadcast to all receivers)
  quit:    {"type":"quit"}

Event types (inside txn.events):
  ensure_prim:        {"k":"ensure_prim","prim":"/World/Sphere","typeName":"Xform"}
  ensure_xform_ops:   {"k":"ensure_xform_ops","prim":"/World/Sphere"}
  set_xform_trs:      {"k":"set_xform_trs","prim":"/World/Sphere","fields":["t","r","s"],
                        "t":[x,y,z], "r":[w,x,y,z], "s":[x,y,z]}
  set_xform_matrices: {"k":"set_xform_matrices","prim":"/World/Sphere",
                        "local_m":[16 floats], "world_m":[16 floats]}
  deactivate_prim:    {"k":"deactivate_prim","prim":"/World/Sphere","active":false}
  rename_prim:        {"k":"rename_prim","prim":"/World/OldName","new_name":"NewName"}
  set_visibility:     {"k":"set_visibility","prim":"/World/Sphere","visible":false}
  set_gprim_attrs:    {"k":"set_gprim_attrs","prim":"/World/Sphere/Geom",
                        "attrs":{"radius":2.0}}
  set_reference:      {"k":"set_reference","prim":"/World/Chair",
                        "refs":[{"asset_path":"./chair.usd","prim_path":"/Model"}]}
  set_payload:        {"k":"set_payload","prim":"/World/Asset",
                        "payloads":[{"asset_path":"./payload.usda","prim_path":"/Model"}]}
  load_payload:       {"k":"load_payload","prim":"/World/Asset"}
  unload_payload:     {"k":"unload_payload","prim":"/World/Asset"}
  set_variant_selections: {"k":"set_variant_selections","prim":"/World/Car",
                        "selections":{"wheels":"wheelWide","color":"red"}}
"""

from __future__ import annotations

PROTOCOL_VERSION = 1

# Message type constants
MSG_HELLO = "hello"
MSG_TXN = "txn"
MSG_EVENT = "event"
MSG_RESYNC = "resync"
MSG_COMPACT = "compact"
MSG_QUIT = "quit"

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
    }
)

# Event application order — structural/compositional first, values second,
# destructive last.  Follows the LIVERPS composition strength model from
# the OpenUSD spec (section 10.4).
EVENT_KIND_ORDER: dict[str, int] = {
    K_ENSURE_PRIM: 0,
    K_ENSURE_XFORM_OPS: 1,
    K_SET_VARIANT_SELECTIONS: 2,  # V in LIVERPS — before R
    K_SET_REFERENCE: 3,
    K_SET_PAYLOAD: 4,
    K_LOAD_PAYLOAD: 5,
    K_SET_XFORM_TRS: 6,
    K_SET_VISIBILITY: 7,
    K_SET_GPRIM_ATTRS: 8,
    K_SET_SHADER_INPUT: 9,
    K_SET_XFORM_MATRICES: 10,
    K_SET_MATERIAL_BINDING: 11,
    K_DEACTIVATE_PRIM: 12,
    K_DELETE_PRIM: 13,
    K_RENAME_PRIM: 14,
    K_UNLOAD_PAYLOAD: 15,
}

# Events that must be applied outside a ChangeBlock (structural ops).
STRUCTURAL_EVENT_KINDS = frozenset({
    K_ENSURE_PRIM, K_ENSURE_XFORM_OPS, K_SET_VARIANT_SELECTIONS,
    K_SET_REFERENCE, K_SET_PAYLOAD, K_LOAD_PAYLOAD, K_UNLOAD_PAYLOAD,
    K_SET_MATERIAL_BINDING, K_SET_SHADER_INPUT,
})

# Valid TRS field names
TRS_FIELDS = frozenset({"t", "r", "s"})


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
    role: str, sync_from: int | None = None, client_id: str | None = None,
) -> dict:
    """Build a hello message."""
    msg = {"type": MSG_HELLO, "role": role, "protocol_version": PROTOCOL_VERSION}
    if sync_from is not None:
        msg["sync_from"] = sync_from
    if client_id is not None:
        msg["client_id"] = client_id
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
    if k == K_DEACTIVATE_PRIM:
        if not isinstance(ev.get("active"), bool):
            return False
    if k == K_RENAME_PRIM:
        new_name = ev.get("new_name")
        if not isinstance(new_name, str) or not new_name:
            return False
    if k == K_SET_VISIBILITY:
        if not isinstance(ev.get("visible"), bool):
            return False
    if k == K_SET_GPRIM_ATTRS:
        attrs = ev.get("attrs")
        if not isinstance(attrs, dict):
            return False
        if not all(isinstance(key, str) for key in attrs):
            return False
    if k == K_SET_REFERENCE:
        refs = ev.get("refs")
        if not isinstance(refs, list):
            return False
        for entry in refs:
            if not isinstance(entry, dict):
                return False
            ap = entry.get("asset_path")
            pp = entry.get("prim_path")
            # At least one of asset_path or prim_path must be present
            if ap is None and pp is None:
                return False
            if ap is not None:
                if not isinstance(ap, str) or not ap:
                    return False
            if pp is not None:
                if not isinstance(pp, str) or not pp.startswith("/"):
                    return False
    # load_payload and unload_payload require only "prim" (already validated above)

    if k == K_SET_PAYLOAD:
        payloads = ev.get("payloads")
        if not isinstance(payloads, list):
            return False
        for entry in payloads:
            if not isinstance(entry, dict):
                return False
            ap = entry.get("asset_path")
            pp = entry.get("prim_path")
            if ap is None and pp is None:
                return False
            if ap is not None:
                if not isinstance(ap, str) or not ap:
                    return False
            if pp is not None:
                if not isinstance(pp, str) or not pp.startswith("/"):
                    return False
    if k == K_SET_VARIANT_SELECTIONS:
        selections = ev.get("selections")
        if not isinstance(selections, dict):
            return False
        if not all(isinstance(k, str) for k in selections):
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
        shader_id = ev.get("shader_id")
        if not isinstance(shader_id, str) or not shader_id:
            return False
        inputs = ev.get("inputs")
        if not isinstance(inputs, dict):
            return False
        if not all(isinstance(key, str) for key in inputs):
            return False
        input_types = ev.get("input_types")
        if not isinstance(input_types, dict):
            return False
    return True
