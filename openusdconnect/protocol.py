"""Event schema, message types, and validation for JSON Lines over TCP protocol.

Message types:
  hello:  {"type":"hello","role":"emitter"|"receiver","sync_from":<int optional>}
  txn:    {"type":"txn","client_id":"...", "events":[ <event>, ... ]}
  event:  {"type":"event","seq":123,"event":{...}}   (server broadcasts)
  quit:   {"type":"quit"}

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
                        "asset_path":"./assets/chair.usd","prim_path":"/Chair"}
"""

from __future__ import annotations

PROTOCOL_VERSION = 1

# Valid event keys
EVENT_KEYS = frozenset(
    {
        "ensure_prim",
        "ensure_xform_ops",
        "set_xform_trs",
        "set_xform_matrices",
        "delete_prim",
        "deactivate_prim",
        "rename_prim",
        "set_visibility",
        "set_gprim_attrs",
        "set_reference",
    }
)

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


def make_hello(role: str, sync_from: int = None) -> dict:
    """Build a hello message."""
    msg = {"type": "hello", "role": role, "protocol_version": PROTOCOL_VERSION}
    if sync_from is not None:
        msg["sync_from"] = sync_from
    return msg


def make_txn(client_id: str, events: list[dict]) -> dict:
    """Build a transaction message."""
    return {"type": "txn", "client_id": client_id, "events": events}


def make_quit() -> dict:
    """Build a quit message."""
    return {"type": "quit"}


def validate_event(ev: dict) -> bool:
    """Basic validation that an event dict has required fields."""
    k = ev.get("k")
    if k not in EVENT_KEYS:
        return False
    if "prim" not in ev:
        return False
    if k == "set_xform_trs":
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
    if k == "set_xform_matrices":
        if not is_mat16_valid(ev.get("local_m", [])):
            return False
        if not is_mat16_valid(ev.get("world_m", [])):
            return False
    if k == "deactivate_prim":
        if not isinstance(ev.get("active"), bool):
            return False
    if k == "rename_prim":
        new_name = ev.get("new_name")
        if not isinstance(new_name, str) or not new_name:
            return False
    if k == "set_visibility":
        if not isinstance(ev.get("visible"), bool):
            return False
    if k == "set_gprim_attrs":
        attrs = ev.get("attrs")
        if not isinstance(attrs, dict):
            return False
        if not all(isinstance(key, str) for key in attrs):
            return False
    if k == "set_reference":
        asset_path = ev.get("asset_path")
        if not isinstance(asset_path, str) or not asset_path:
            return False
        prim_path = ev.get("prim_path")
        if prim_path is not None:
            if not isinstance(prim_path, str) or not prim_path.startswith("/"):
                return False
    return True
