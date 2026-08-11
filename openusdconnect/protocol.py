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
    MSG_REPLAY_COMPLETE,
    MSG_TRANSACTION_RESULT,
    MSG_TXN,
    PROTOCOL_VERSION,
    LayerMode,
)

__all__ = [
    "make_hello",
    "make_quit",
    "make_txn",
    "make_claim_playback",
    "make_playback_control",
    "make_transaction_result",
    "make_replay_complete",
]


def make_hello(
    role: str,
    sync_from: int | None = None,
    client_id: str | None = None,
    origin: str | None = None,
    department: str | None = None,
    token: str | None = None,
    layered_replay: bool | None = None,
    layer_mode: LayerMode | str = LayerMode.MANAGED,
    producer_session_id: str | None = None,
) -> dict:
    """Build a hello message.

    Args:
        role: "emitter" or "receiver".
        sync_from: Sequence number to replay from (receivers only).
        client_id: Per-connection identifier.
        origin: Session-level identifier shared by all connections from the
            same DCC instance. Durable events still return to that origin as
            part of the complete commit stream; integrations use the value for
            attribution and local reconciliation.
        department: Optional department name (e.g. "animation", "lighting").
            Used by the server for layer ordering when per-client layers
            are enabled.
        token: Authentication token from a previous session (TOFU).
        layered_replay: Whether a receiver can reconstruct the logical
            authored-layer stack instead of consuming only the composed view.
            Defaults to true for receivers and false for other roles.
        layer_mode: Managed collaboration layers or the shared root-layer graph.
        producer_session_id: Ordered producer-session identity. Required for
            emitter connections that submit ordinary transactions.
    """
    mode = LayerMode(layer_mode)
    if mode is LayerMode.SHARED_STAGE and department is not None:
        raise ValueError("department routing is unavailable in shared-stage mode")
    if layered_replay is None:
        layered_replay = role == "receiver" and mode is LayerMode.MANAGED
    if mode is LayerMode.SHARED_STAGE and layered_replay:
        raise ValueError("managed layered replay is unavailable in shared-stage mode")

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
    if layered_replay:
        msg["layered_replay"] = True
    if mode is not LayerMode.MANAGED:
        msg["layer_mode"] = mode.value
    if producer_session_id is not None:
        msg["producer_session_id"] = producer_session_id
    return msg


def make_txn(
    events: list[Event],
    *,
    layer_key: str = "",
    txn_id: int = 0,
) -> dict:
    """Build a transaction message."""
    msg = {"type": MSG_TXN, "events": events}
    if layer_key:
        msg["layer_key"] = layer_key
    if txn_id:
        msg["txn_id"] = int(txn_id)
    return msg


def make_transaction_result(
    txn_id: int,
    *,
    status: str = "acknowledged",
    expected_txn_id: int = 0,
    rejection_code: str = "none",
    reason: str = "",
) -> dict:
    """Build a cumulative durable producer result."""
    msg = {
        "type": MSG_TRANSACTION_RESULT,
        "txn_id": int(txn_id),
        "status": status,
    }
    if expected_txn_id:
        msg["expected_txn_id"] = int(expected_txn_id)
    if rejection_code != "none":
        msg["rejection_code"] = rejection_code
    if reason:
        msg["reason"] = reason
    return msg


def make_replay_complete(head_seq: int, epoch: int) -> dict:
    """Build the receiver replay-to-live synchronization watermark."""
    if head_seq < 0 or epoch < 0:
        raise ValueError("replay watermark values cannot be negative")
    return {
        "type": MSG_REPLAY_COMPLETE,
        "head_seq": int(head_seq),
        "epoch": int(epoch),
    }


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
