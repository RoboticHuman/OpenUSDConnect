"""Protocol message builders.

Message and event constants live in protocol_constants.py.  Event validation
lives in protocol_validation.py.  UsdShade input/output attribute helpers live
in connectable_attrs.py.

The wire format is length-prefixed FlatBuffers -- see codec.py and framing.py.
The FlatBuffers schema (schema/events.fbs, schema/messages.fbs) is the
canonical reference for per-event field definitions; the codec handles
dict <-> FlatBuffers conversion.
"""

from __future__ import annotations

from .events import Event
from .protocol_constants import MSG_HELLO, MSG_QUIT, MSG_TXN, PROTOCOL_VERSION

__all__ = ["make_hello", "make_quit", "make_txn"]


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
            same DCC instance.  The server uses this to suppress echo --
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


def make_txn(client_id: str, events: list[Event]) -> dict:
    """Build a transaction message."""
    return {"type": MSG_TXN, "client_id": client_id, "events": events}


def make_quit() -> dict:
    """Build a quit message."""
    return {"type": MSG_QUIT}
