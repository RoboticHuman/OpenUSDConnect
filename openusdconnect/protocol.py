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
from .protocol_constants import (
    MSG_CLAIM_PLAYBACK,
    MSG_HELLO,
    MSG_PLAYBACK_CONTROL,
    MSG_QUIT,
    MSG_TXN,
    PROTOCOL_VERSION,
)

__all__ = [
    "make_hello",
    "make_quit",
    "make_txn",
    "make_claim_playback",
    "make_playback_control",
]


def make_hello(
    role: str,
    sync_from: int | None = None,
    client_id: str | None = None,
    origin: str | None = None,
    department: str | None = None,
    token: str | None = None,
    layered_replay: bool | None = None,
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
        layered_replay: Whether a receiver can reconstruct the logical
            authored-layer stack instead of consuming only the composed view.
            Defaults to true for receivers and false for other roles.
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
    if layered_replay is None:
        layered_replay = role == "receiver"
    if layered_replay:
        msg["layered_replay"] = True
    return msg


def make_txn(client_id: str, events: list[Event], proposal_id: str = "") -> dict:
    """Build a transaction message.

    With *proposal_id*, the server routes the edits to that proposal's muted
    layer instead of the client's live layer.
    """
    msg = {"type": MSG_TXN, "client_id": client_id, "events": events}
    if proposal_id:
        msg["proposal_id"] = proposal_id
    return msg


def make_quit() -> dict:
    """Build a quit message."""
    return {"type": MSG_QUIT}


def make_claim_playback(client_id: str, time: float | None = None) -> dict:
    """Build a ClaimPlayback message requesting the playback-leader role.

    ``time`` (optional) is the claimer's current timecode; the server sets the
    shared playhead atomically with the grant so followers don't snap to a
    stale value.
    """
    msg: dict = {"type": MSG_CLAIM_PLAYBACK, "client_id": client_id}
    if time is not None:
        msg["time"] = float(time)
    return msg


def make_playback_control(
    action: str, *, time: float | None = None, rate: float | None = None
) -> dict:
    """Build a PlaybackControl message.

    ``action`` is one of ``"play"``, ``"pause"``, ``"stop"``, ``"set_time"``,
    ``"set_rate"``. ``time`` carries the new timecode for ``set_time``;
    ``rate`` the playback rate for ``set_rate``.
    """
    msg: dict = {"type": MSG_PLAYBACK_CONTROL, "action": action}
    if time is not None:
        msg["time"] = float(time)
    if rate is not None:
        msg["rate"] = float(rate)
    return msg
