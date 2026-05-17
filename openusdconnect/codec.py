"""FlatBuffers codec for the OpenUSDConnect wire protocol.

FlatBuffers is the canonical wire and storage format.  Zero-copy access
is used wherever possible:

  * **Receiver queue** — stores raw bytes; decode on drain.
  * **Broadcast relay** — pre-framed binary sent to all receivers.
  * **Event store** — binary blobs written/read without re-serialization.

The server's event-processing path (apply_txn) still operates on Python
dicts — events are decoded once on ingestion and the dict is passed to
event_apply.  This is the primary conversion boundary.

Primary API (zero-copy path):
    encode_message(msg_dict)     -> bytes          # dict  → FB binary
    decode_envelope(buf)         -> Envelope        # FB binary → typed FB object
    resolve_payload(envelope)    -> (msg_type, obj) # Envelope → (str, typed FB table)
    resolve_event(event_wrapper) -> (kind, obj)     # EventWrapper → (str, typed FB table)

Debug / compaction API (copies):
    message_to_dict(buf)  -> dict    # FB binary → Python dict
    event_to_dict(ew)     -> dict    # EventWrapper → Python dict

Fast checks:
    is_ping(buf)          -> bool    # single-byte read, no alloc
    payload_type(buf)     -> int     # raw union tag
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field

import flatbuffers
import numpy as np

from . import events as _events
from .events import Event, register_decoder, register_encoder
from .generated import messages_generated as _fb
from .protocol_constants import (
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_SET_CONNECTABLE_CONNECTION,
    K_SET_CONNECTABLE_INPUT,
    K_SET_GPRIM_ATTRS,
    K_SET_MATERIAL_BINDING,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_STAGE_METADATA,
    K_SET_VARIANT_SELECTIONS,
    K_SET_VISIBILITY,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
    MSG_AUTH_REJECTED,
    MSG_CLAIM_PLAYBACK,
    MSG_COMPACT,
    MSG_CREATE_PROPOSAL,
    MSG_EVENT,
    MSG_HELLO,
    MSG_HELLO_OK,
    MSG_PING,
    MSG_PLAYBACK_CLAIMED,
    MSG_PLAYBACK_CONTROL,
    MSG_PLAYBACK_REJECTED,
    MSG_PLAYBACK_STATE,
    MSG_PROPOSAL_CREATED,
    MSG_QUIT,
    MSG_RATE_LIMITED,
    MSG_RESYNC,
    MSG_TXN,
)

# Re-export generated classes so consumers import from codec, not generated path.
# These are the typed FB table classes the rest of the codebase works with.
Envelope = _fb.Envelope
Hello = _fb.Hello
HelloOk = _fb.HelloOk
AuthRejected = _fb.AuthRejected
Txn = _fb.Txn
BroadcastEvent = _fb.BroadcastEvent
Resync = _fb.Resync
Compact = _fb.Compact
Ping = _fb.Ping
Quit = _fb.Quit
CreateProposal = _fb.CreateProposal
ProposalCreated = _fb.ProposalCreated
RateLimited = _fb.RateLimited
EventWrapper = _fb.EventWrapper
EnsurePrim = _fb.EnsurePrim
EnsureXformOps = _fb.EnsureXformOps
SetXformTrs = _fb.SetXformTrs
DeletePrim = _fb.DeletePrim
DeactivatePrim = _fb.DeactivatePrim
RenamePrim = _fb.RenamePrim
SetVisibility = _fb.SetVisibility
SetGprimAttrs = _fb.SetGprimAttrs
SetReference = _fb.SetReference
SetPayload = _fb.SetPayload
LoadPayload = _fb.LoadPayload
UnloadPayload = _fb.UnloadPayload
SetVariantSelections = _fb.SetVariantSelections
SetMaterialBinding = _fb.SetMaterialBinding
SetConnectableInput = _fb.SetConnectableInput
SetConnectableConnection = _fb.SetConnectableConnection
SetStageMetadata = _fb.SetStageMetadata
ClaimPlayback = _fb.ClaimPlayback
PlaybackClaimed = _fb.PlaybackClaimed
PlaybackRejected = _fb.PlaybackRejected
PlaybackControl = _fb.PlaybackControl
PlaybackState = _fb.PlaybackState
NamedAttr = _fb.NamedAttr
AttrValue = _fb.AttrValue
AttrValueType = _fb.AttrValueType
PrimvarMeta = _fb.PrimvarMeta
AttrInterp = _fb.AttrInterp
ConnectableInputValue = _fb.ConnectableInputValue
Connection = _fb.Connection
ConnectableInputValueType = _fb.ConnectableInputValueType
ArcEntry = _fb.ArcEntry
StringPair = _fb.StringPair
PayloadType = _fb.Payload
EventPayloadType = _fb.EventPayload

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Mapping tables
# ---------------------------------------------------------------------------

_MSG_TYPE_TO_PAYLOAD = {
    MSG_HELLO: PayloadType.Hello,
    MSG_HELLO_OK: PayloadType.HelloOk,
    MSG_AUTH_REJECTED: PayloadType.AuthRejected,
    MSG_TXN: PayloadType.Txn,
    MSG_EVENT: PayloadType.BroadcastEvent,
    MSG_RESYNC: PayloadType.Resync,
    MSG_COMPACT: PayloadType.Compact,
    MSG_PING: PayloadType.Ping,
    MSG_QUIT: PayloadType.Quit,
    MSG_CREATE_PROPOSAL: PayloadType.CreateProposal,
    MSG_PROPOSAL_CREATED: PayloadType.ProposalCreated,
    MSG_RATE_LIMITED: PayloadType.RateLimited,
    MSG_CLAIM_PLAYBACK: PayloadType.ClaimPlayback,
    MSG_PLAYBACK_CLAIMED: PayloadType.PlaybackClaimed,
    MSG_PLAYBACK_REJECTED: PayloadType.PlaybackRejected,
    MSG_PLAYBACK_CONTROL: PayloadType.PlaybackControl,
    MSG_PLAYBACK_STATE: PayloadType.PlaybackState,
}

_PAYLOAD_TO_MSG_TYPE = {v: k for k, v in _MSG_TYPE_TO_PAYLOAD.items()}

_PAYLOAD_TO_CLASS = {
    PayloadType.Hello: Hello,
    PayloadType.HelloOk: HelloOk,
    PayloadType.AuthRejected: AuthRejected,
    PayloadType.Txn: Txn,
    PayloadType.BroadcastEvent: BroadcastEvent,
    PayloadType.Resync: Resync,
    PayloadType.Compact: Compact,
    PayloadType.Ping: Ping,
    PayloadType.Quit: Quit,
    PayloadType.CreateProposal: CreateProposal,
    PayloadType.ProposalCreated: ProposalCreated,
    PayloadType.RateLimited: RateLimited,
    PayloadType.ClaimPlayback: ClaimPlayback,
    PayloadType.PlaybackClaimed: PlaybackClaimed,
    PayloadType.PlaybackRejected: PlaybackRejected,
    PayloadType.PlaybackControl: PlaybackControl,
    PayloadType.PlaybackState: PlaybackState,
}

# Stage metadata bitmask bits — order must match SetStageMetadata in events.fbs.
_STAGE_META_BITS = {
    "timeCodesPerSecond": 1,
    "framesPerSecond": 2,
    "startTimeCode": 4,
    "endTimeCode": 8,
    "metersPerUnit": 16,
    "upAxis": 32,
}

_TRS_BITS = {"t": 1, "r": 2, "s": 4}

_BUILDER_SIZE_HINT: dict[str, int] = {
    MSG_EVENT: 4096,
    MSG_TXN: 4096,
}
_DEFAULT_BUILDER_SIZE = 512


# ===================================================================
# ZERO-COPY DECODE API  (primary path)
# ===================================================================


def decode_envelope(buf: bytes | bytearray) -> Envelope:
    """Decode wire bytes to a FlatBuffers Envelope (zero-copy)."""
    return Envelope.GetRootAs(buf, 0)


def resolve_payload(envelope: Envelope):
    """Resolve envelope union to (msg_type_str, typed_fb_object).

    Returns e.g. ("hello", Hello) or ("event", BroadcastEvent).
    The typed object shares the same underlying buffer — zero-copy.
    """
    tag = envelope.PayloadType()
    msg_type = _PAYLOAD_TO_MSG_TYPE.get(tag)
    if msg_type is None:
        raise ValueError(f"unknown payload type {tag}")
    cls = _PAYLOAD_TO_CLASS[tag]
    raw_table = envelope.Payload()
    obj = cls()
    obj.Init(raw_table.Bytes, raw_table.Pos)
    return msg_type, obj


def resolve_event(event_wrapper: EventWrapper):
    """Resolve EventWrapper union to (kind_str, typed_fb_object).

    Returns e.g. ("set_xform_trs", SetXformTrs) or
    ("set_gprim_attrs", SetGprimAttrs).
    """
    tag = event_wrapper.EventType()
    spec = _events.by_tag(tag)
    if spec is None:
        raise ValueError(f"unknown event payload type {tag}")
    raw_table = event_wrapper.Event()
    obj = spec.fb_class()
    obj.Init(raw_table.Bytes, raw_table.Pos)
    return spec.kind, obj


def is_ping(buf: bytes | bytearray) -> bool:
    """Check if a buffer is a Ping message — single byte read, no alloc."""
    return decode_envelope(buf).PayloadType() == PayloadType.Ping


def payload_type(buf: bytes | bytearray) -> int:
    """Read the raw Payload union tag from a buffer."""
    return decode_envelope(buf).PayloadType()


# ===================================================================
# ENCODE API  (dict -> FlatBuffers bytes)
# ===================================================================


def encode_message(msg: dict) -> bytes:
    """Encode a protocol dict to FlatBuffers wire bytes."""
    msg_type = msg["type"]
    builder = flatbuffers.Builder(_BUILDER_SIZE_HINT.get(msg_type, _DEFAULT_BUILDER_SIZE))

    payload_tag = _MSG_TYPE_TO_PAYLOAD[msg_type]
    payload_offset = _ENCODE_DISPATCH[msg_type](builder, msg)

    _fb.EnvelopeStart(builder)
    _fb.EnvelopeAddPayloadType(builder, payload_tag)
    _fb.EnvelopeAddPayload(builder, payload_offset)
    _fb.EnvelopeAddSchemaVersion(builder, SCHEMA_VERSION)
    envelope = _fb.EnvelopeEnd(builder)

    builder.Finish(envelope)
    return bytes(builder.Output())


# --- Per-message-type encoders ---


def _encode_hello(b, msg):
    role = b.CreateString(msg["role"])
    client_id = b.CreateString(msg["client_id"]) if msg.get("client_id") else None
    origin = b.CreateString(msg["origin"]) if msg.get("origin") else None
    department = b.CreateString(msg["department"]) if msg.get("department") else None
    token = b.CreateString(msg["token"]) if msg.get("token") else None

    _fb.HelloStart(b)
    _fb.HelloAddRole(b, role)
    _fb.HelloAddProtocolVersion(b, msg.get("protocol_version", 0))
    if msg.get("sync_from") is not None:
        _fb.HelloAddSyncFrom(b, msg["sync_from"])
    if client_id:
        _fb.HelloAddClientId(b, client_id)
    if origin:
        _fb.HelloAddOrigin(b, origin)
    if department:
        _fb.HelloAddDepartment(b, department)
    if token:
        _fb.HelloAddToken(b, token)
    return _fb.HelloEnd(b)


def _encode_stage_metadata_table(b, meta: dict | None, *, force: bool = False) -> int | None:
    """Build a SetStageMetadata table from a sparse dict.

    Returns ``None`` when ``meta`` is empty and ``force`` is False — caller
    omits the optional field rather than writing an empty table.
    """
    if not meta and not force:
        return None
    meta = meta or {}
    bitmask = 0
    up_axis_off = None
    if meta.get("upAxis"):
        up_axis_off = b.CreateString(meta["upAxis"])
    _fb.SetStageMetadataStart(b)
    for key, bit in _STAGE_META_BITS.items():
        if key == "upAxis":
            if up_axis_off is not None:
                bitmask |= bit
                _fb.SetStageMetadataAddUpAxis(b, up_axis_off)
        elif key in meta:
            bitmask |= bit
            value = float(meta[key])
            if key == "timeCodesPerSecond":
                _fb.SetStageMetadataAddTimeCodesPerSecond(b, value)
            elif key == "framesPerSecond":
                _fb.SetStageMetadataAddFramesPerSecond(b, value)
            elif key == "startTimeCode":
                _fb.SetStageMetadataAddStartTimeCode(b, value)
            elif key == "endTimeCode":
                _fb.SetStageMetadataAddEndTimeCode(b, value)
            elif key == "metersPerUnit":
                _fb.SetStageMetadataAddMetersPerUnit(b, value)
    _fb.SetStageMetadataAddFields(b, bitmask)
    return _fb.SetStageMetadataEnd(b)


def _decode_stage_metadata_table(sm) -> dict:
    """Read a SetStageMetadata FB table into a sparse dict (only flagged fields)."""
    out: dict = {}
    fields = sm.Fields()
    if fields & _STAGE_META_BITS["timeCodesPerSecond"]:
        out["timeCodesPerSecond"] = sm.TimeCodesPerSecond()
    if fields & _STAGE_META_BITS["framesPerSecond"]:
        out["framesPerSecond"] = sm.FramesPerSecond()
    if fields & _STAGE_META_BITS["startTimeCode"]:
        out["startTimeCode"] = sm.StartTimeCode()
    if fields & _STAGE_META_BITS["endTimeCode"]:
        out["endTimeCode"] = sm.EndTimeCode()
    if fields & _STAGE_META_BITS["metersPerUnit"]:
        out["metersPerUnit"] = sm.MetersPerUnit()
    if fields & _STAGE_META_BITS["upAxis"]:
        out["upAxis"] = _str(sm.UpAxis()) or ""
    return out


def _encode_hello_ok(b, msg):
    token = b.CreateString(msg["token"]) if msg.get("token") else None
    sm_off = _encode_stage_metadata_table(b, msg.get("stage_metadata"))
    _fb.HelloOkStart(b)
    if token:
        _fb.HelloOkAddToken(b, token)
    if sm_off is not None:
        _fb.HelloOkAddStageMetadata(b, sm_off)
    return _fb.HelloOkEnd(b)


def _encode_auth_rejected(b, msg):
    reason = b.CreateString(msg.get("reason", ""))
    _fb.AuthRejectedStart(b)
    _fb.AuthRejectedAddReason(b, reason)
    return _fb.AuthRejectedEnd(b)


def _encode_txn(b, msg):
    client_id = b.CreateString(msg["client_id"])
    event_offsets = [_encode_event_wrapper(b, ev) for ev in msg["events"]]
    _fb.TxnStartEventsVector(b, len(event_offsets))
    for off in reversed(event_offsets):
        b.PrependUOffsetTRelative(off)
    events_vec = b.EndVector()
    _fb.TxnStart(b)
    _fb.TxnAddClientId(b, client_id)
    _fb.TxnAddEvents(b, events_vec)
    return _fb.TxnEnd(b)


def _encode_broadcast_event(b, msg):
    ev_offset = _encode_event_wrapper(b, msg["event"])
    origin = b.CreateString(msg["origin"]) if msg.get("origin") else None
    client_id = b.CreateString(msg["client_id"]) if msg.get("client_id") else None
    client = b.CreateString(msg["client"]) if msg.get("client") else None
    _fb.BroadcastEventStart(b)
    _fb.BroadcastEventAddSeq(b, msg["seq"])
    _fb.BroadcastEventAddEvent(b, ev_offset)
    if origin:
        _fb.BroadcastEventAddOrigin(b, origin)
    if client_id:
        _fb.BroadcastEventAddClientId(b, client_id)
    if client:
        _fb.BroadcastEventAddClient(b, client)
    return _fb.BroadcastEventEnd(b)


def _encode_resync(b, _msg):
    _fb.ResyncStart(b)
    return _fb.ResyncEnd(b)


def _encode_compact(b, _msg):
    _fb.CompactStart(b)
    return _fb.CompactEnd(b)


def _encode_ping(b, _msg):
    _fb.PingStart(b)
    return _fb.PingEnd(b)


def _encode_quit(b, _msg):
    _fb.QuitStart(b)
    return _fb.QuitEnd(b)


def _encode_create_proposal(b, msg):
    target = b.CreateString(msg["target_department"])
    desc = b.CreateString(msg.get("description", ""))
    events = msg.get("events", [])
    events_vec = None
    if events:
        event_offsets = [_encode_event_wrapper(b, ev) for ev in events]
        _fb.CreateProposalStartEventsVector(b, len(event_offsets))
        for off in reversed(event_offsets):
            b.PrependUOffsetTRelative(off)
        events_vec = b.EndVector()
    _fb.CreateProposalStart(b)
    _fb.CreateProposalAddTargetDepartment(b, target)
    if events_vec is not None:
        _fb.CreateProposalAddEvents(b, events_vec)
    _fb.CreateProposalAddDescription(b, desc)
    return _fb.CreateProposalEnd(b)


def _encode_proposal_created(b, msg):
    pid = b.CreateString(msg["proposal_id"])
    _fb.ProposalCreatedStart(b)
    _fb.ProposalCreatedAddProposalId(b, pid)
    return _fb.ProposalCreatedEnd(b)


def _encode_rate_limited(b, msg):
    _fb.RateLimitedStart(b)
    _fb.RateLimitedAddRetryAfter(b, msg["retry_after"])
    return _fb.RateLimitedEnd(b)


def _encode_claim_playback(b, msg):
    client_id = b.CreateString(msg.get("client_id", ""))
    _fb.ClaimPlaybackStart(b)
    _fb.ClaimPlaybackAddClientId(b, client_id)
    if msg.get("time") is not None:
        _fb.ClaimPlaybackAddTime(b, float(msg["time"]))
    return _fb.ClaimPlaybackEnd(b)


def _encode_playback_claimed(b, msg):
    leader = b.CreateString(msg.get("leader_client_id", ""))
    _fb.PlaybackClaimedStart(b)
    _fb.PlaybackClaimedAddLeaderClientId(b, leader)
    return _fb.PlaybackClaimedEnd(b)


def _encode_playback_rejected(b, msg):
    reason = b.CreateString(msg.get("reason", ""))
    current = b.CreateString(msg.get("current_leader_client_id", ""))
    _fb.PlaybackRejectedStart(b)
    _fb.PlaybackRejectedAddReason(b, reason)
    _fb.PlaybackRejectedAddCurrentLeaderClientId(b, current)
    return _fb.PlaybackRejectedEnd(b)


def _encode_playback_control(b, msg):
    action = b.CreateString(msg.get("action", ""))
    _fb.PlaybackControlStart(b)
    _fb.PlaybackControlAddAction(b, action)
    if "time" in msg:
        _fb.PlaybackControlAddTime(b, float(msg["time"]))
    if "rate" in msg:
        _fb.PlaybackControlAddRate(b, float(msg["rate"]))
    return _fb.PlaybackControlEnd(b)


def _encode_playback_state(b, msg):
    leader = b.CreateString(msg.get("leader_client_id", ""))
    _fb.PlaybackStateStart(b)
    _fb.PlaybackStateAddTime(b, float(msg.get("time", 0.0)))
    _fb.PlaybackStateAddPlaying(b, bool(msg.get("playing", False)))
    _fb.PlaybackStateAddRate(b, float(msg.get("rate", 1.0)))
    _fb.PlaybackStateAddLeaderClientId(b, leader)
    return _fb.PlaybackStateEnd(b)


_ENCODE_DISPATCH = {
    MSG_HELLO: _encode_hello,
    MSG_HELLO_OK: _encode_hello_ok,
    MSG_AUTH_REJECTED: _encode_auth_rejected,
    MSG_TXN: _encode_txn,
    MSG_EVENT: _encode_broadcast_event,
    MSG_RESYNC: _encode_resync,
    MSG_COMPACT: _encode_compact,
    MSG_PING: _encode_ping,
    MSG_QUIT: _encode_quit,
    MSG_CREATE_PROPOSAL: _encode_create_proposal,
    MSG_PROPOSAL_CREATED: _encode_proposal_created,
    MSG_RATE_LIMITED: _encode_rate_limited,
    MSG_CLAIM_PLAYBACK: _encode_claim_playback,
    MSG_PLAYBACK_CLAIMED: _encode_playback_claimed,
    MSG_PLAYBACK_REJECTED: _encode_playback_rejected,
    MSG_PLAYBACK_CONTROL: _encode_playback_control,
    MSG_PLAYBACK_STATE: _encode_playback_state,
}


# --- Event wrapper encoder ---


def _encode_event_wrapper(b, ev: dict) -> int:
    kind = ev["k"]
    spec = _events.get(kind)
    if spec is None or spec.encode is None or spec.fb_tag is None:
        raise KeyError(f"no registered encoder for event kind {kind!r}")
    event_offset = spec.encode(b, ev)
    _fb.EventWrapperStart(b)
    _fb.EventWrapperAddEventType(b, spec.fb_tag)
    _fb.EventWrapperAddEvent(b, event_offset)
    return _fb.EventWrapperEnd(b)


# --- Per-event-kind encoders ---


def _create_float_vector(b, values):
    """Create a FlatBuffers float32 vector via numpy bulk copy."""
    return b.CreateNumpyVector(np.asarray(values, dtype=np.float32))


def _create_int_vector(b, values):
    """Create a FlatBuffers int32 vector via numpy bulk copy."""
    return b.CreateNumpyVector(np.asarray(values, dtype=np.int32))


@register_encoder(K_ENSURE_PRIM, fb_tag=EventPayloadType.EnsurePrim, fb_class=EnsurePrim)
def _encode_ensure_prim(b, ev):
    prim = b.CreateString(ev["prim"])
    tn = b.CreateString(ev["typeName"])
    api_schemas = ev.get("api_schemas") or []
    api_offsets = [b.CreateString(s) for s in api_schemas]
    api_vec = None
    if api_offsets:
        _fb.EnsurePrimStartApiSchemasVector(b, len(api_offsets))
        for off in reversed(api_offsets):
            b.PrependUOffsetTRelative(off)
        api_vec = b.EndVector()
    _fb.EnsurePrimStart(b)
    _fb.EnsurePrimAddPrim(b, prim)
    _fb.EnsurePrimAddTypeName(b, tn)
    if api_vec is not None:
        _fb.EnsurePrimAddApiSchemas(b, api_vec)
    return _fb.EnsurePrimEnd(b)


@register_encoder(
    K_ENSURE_XFORM_OPS,
    fb_tag=EventPayloadType.EnsureXformOps,
    fb_class=EnsureXformOps,
)
def _encode_ensure_xform_ops(b, ev):
    prim = b.CreateString(ev["prim"])
    _fb.EnsureXformOpsStart(b)
    _fb.EnsureXformOpsAddPrim(b, prim)
    return _fb.EnsureXformOpsEnd(b)


@register_encoder(K_SET_XFORM_TRS, fb_tag=EventPayloadType.SetXformTrs, fb_class=SetXformTrs)
def _encode_set_xform_trs(b, ev):
    prim = b.CreateString(ev["prim"])
    fields = ev.get("fields", [])
    bitmask = 0
    for f in fields:
        bitmask |= _TRS_BITS.get(f, 0)

    t_vec = _create_float_vector(b, ev["t"]) if "t" in fields else None
    r_vec = _create_float_vector(b, ev["r"]) if "r" in fields else None
    s_vec = _create_float_vector(b, ev["s"]) if "s" in fields else None

    _fb.SetXformTrsStart(b)
    _fb.SetXformTrsAddPrim(b, prim)
    _fb.SetXformTrsAddFields(b, bitmask)
    if t_vec is not None:
        _fb.SetXformTrsAddT(b, t_vec)
    if r_vec is not None:
        _fb.SetXformTrsAddR(b, r_vec)
    if s_vec is not None:
        _fb.SetXformTrsAddS(b, s_vec)
    if ev.get("time") is not None:
        _fb.SetXformTrsAddTime(b, float(ev["time"]))
    return _fb.SetXformTrsEnd(b)


@register_encoder(K_DELETE_PRIM, fb_tag=EventPayloadType.DeletePrim, fb_class=DeletePrim)
def _encode_delete_prim(b, ev):
    prim = b.CreateString(ev["prim"])
    _fb.DeletePrimStart(b)
    _fb.DeletePrimAddPrim(b, prim)
    return _fb.DeletePrimEnd(b)


@register_encoder(
    K_DEACTIVATE_PRIM,
    fb_tag=EventPayloadType.DeactivatePrim,
    fb_class=DeactivatePrim,
)
def _encode_deactivate_prim(b, ev):
    prim = b.CreateString(ev["prim"])
    _fb.DeactivatePrimStart(b)
    _fb.DeactivatePrimAddPrim(b, prim)
    _fb.DeactivatePrimAddActive(b, ev["active"])
    return _fb.DeactivatePrimEnd(b)


@register_encoder(K_RENAME_PRIM, fb_tag=EventPayloadType.RenamePrim, fb_class=RenamePrim)
def _encode_rename_prim(b, ev):
    prim = b.CreateString(ev["prim"])
    new_name = b.CreateString(ev["new_name"])
    _fb.RenamePrimStart(b)
    _fb.RenamePrimAddPrim(b, prim)
    _fb.RenamePrimAddNewName(b, new_name)
    return _fb.RenamePrimEnd(b)


@register_encoder(K_SET_VISIBILITY, fb_tag=EventPayloadType.SetVisibility, fb_class=SetVisibility)
def _encode_set_visibility(b, ev):
    prim = b.CreateString(ev["prim"])
    _fb.SetVisibilityStart(b)
    _fb.SetVisibilityAddPrim(b, prim)
    _fb.SetVisibilityAddVisible(b, ev["visible"])
    if ev.get("time") is not None:
        _fb.SetVisibilityAddTime(b, float(ev["time"]))
    return _fb.SetVisibilityEnd(b)


def _encode_attr_value(b, name: str, value) -> int:
    """Encode a single named attribute value into a NamedAttr table."""
    name_off = b.CreateString(name)
    av_off = _encode_attr_value_inner(b, value)
    _fb.NamedAttrStart(b)
    _fb.NamedAttrAddName(b, name_off)
    _fb.NamedAttrAddValue(b, av_off)
    return _fb.NamedAttrEnd(b)


def _encode_attr_value_inner(b, value) -> int:
    """Encode a Python value into an AttrValue table offset."""
    if isinstance(value, bool):
        _fb.AttrValueStart(b)
        _fb.AttrValueAddValueType(b, AttrValueType.ScalarBool)
        _fb.AttrValueAddScalarBool(b, value)
        return _fb.AttrValueEnd(b)
    if isinstance(value, int):
        _fb.AttrValueStart(b)
        _fb.AttrValueAddValueType(b, AttrValueType.ScalarInt)
        _fb.AttrValueAddScalarInt(b, value)
        return _fb.AttrValueEnd(b)
    if isinstance(value, float):
        _fb.AttrValueStart(b)
        _fb.AttrValueAddValueType(b, AttrValueType.ScalarFloat)
        _fb.AttrValueAddScalarFloat(b, value)
        return _fb.AttrValueEnd(b)
    if isinstance(value, str):
        str_off = b.CreateString(value)
        _fb.AttrValueStart(b)
        _fb.AttrValueAddValueType(b, AttrValueType.ScalarString)
        _fb.AttrValueAddScalarString(b, str_off)
        return _fb.AttrValueEnd(b)
    # numpy arrays — bulk encode via CreateNumpyVector (zero-copy path)
    if isinstance(value, np.ndarray):
        return _encode_attr_value_numpy(b, value)
    if isinstance(value, list):
        return _encode_attr_value_list(b, value)
    # Fallback: JSON
    json_off = b.CreateString(json.dumps(value))
    _fb.AttrValueStart(b)
    _fb.AttrValueAddValueType(b, AttrValueType.NestedList)
    _fb.AttrValueAddNestedJson(b, json_off)
    return _fb.AttrValueEnd(b)


def _encode_attr_value_numpy(b, arr: np.ndarray) -> int:
    """Encode a numpy array into an AttrValue — direct bulk copy."""
    if arr.size == 0:
        json_off = b.CreateString("[]")
        _fb.AttrValueStart(b)
        _fb.AttrValueAddValueType(b, AttrValueType.NestedList)
        _fb.AttrValueAddNestedJson(b, json_off)
        return _fb.AttrValueEnd(b)

    # Determine stride from array shape (e.g. (N,3) → stride=3, (N,) → stride=1)
    if arr.ndim == 1:
        stride = 1
    elif arr.ndim == 2:
        stride = arr.shape[1]
    else:
        # 3D+ arrays — flatten and use stride from last dim
        stride = arr.shape[-1]

    # Flatten to 1D for FlatBuffers vector
    flat = arr.ravel()

    if np.issubdtype(arr.dtype, np.floating):
        vec = b.CreateNumpyVector(flat.astype(np.float32, copy=False))
        _fb.AttrValueStart(b)
        _fb.AttrValueAddValueType(b, AttrValueType.FloatArray)
        _fb.AttrValueAddFloatArray(b, vec)
        _fb.AttrValueAddStride(b, stride)
        return _fb.AttrValueEnd(b)

    if np.issubdtype(arr.dtype, np.integer):
        vec = b.CreateNumpyVector(flat.astype(np.int32, copy=False))
        _fb.AttrValueStart(b)
        _fb.AttrValueAddValueType(b, AttrValueType.IntArray)
        _fb.AttrValueAddIntArray(b, vec)
        _fb.AttrValueAddStride(b, stride)
        return _fb.AttrValueEnd(b)

    # Fallback for unusual dtypes
    json_off = b.CreateString(json.dumps(arr.tolist()))
    _fb.AttrValueStart(b)
    _fb.AttrValueAddValueType(b, AttrValueType.NestedList)
    _fb.AttrValueAddNestedJson(b, json_off)
    return _fb.AttrValueEnd(b)


def _encode_attr_value_list(b, value: list) -> int:
    """Encode a list value — detect element type, use typed vectors."""
    if not value:
        json_off = b.CreateString("[]")
        _fb.AttrValueStart(b)
        _fb.AttrValueAddValueType(b, AttrValueType.NestedList)
        _fb.AttrValueAddNestedJson(b, json_off)
        return _fb.AttrValueEnd(b)

    first = value[0]

    # Flat float list
    if isinstance(first, float):
        vec = _create_float_vector(b, value)
        _fb.AttrValueStart(b)
        _fb.AttrValueAddValueType(b, AttrValueType.FloatArray)
        _fb.AttrValueAddFloatArray(b, vec)
        _fb.AttrValueAddStride(b, 1)
        return _fb.AttrValueEnd(b)

    # Flat int list
    if isinstance(first, int) and not isinstance(first, bool):
        vec = _create_int_vector(b, value)
        _fb.AttrValueStart(b)
        _fb.AttrValueAddValueType(b, AttrValueType.IntArray)
        _fb.AttrValueAddIntArray(b, vec)
        _fb.AttrValueAddStride(b, 1)
        return _fb.AttrValueEnd(b)

    # Nested list of numbers (Vec3fArray → [[x,y,z], ...]) → flattened with stride
    if isinstance(first, list) and first and isinstance(first[0], (int, float)):
        stride = len(first)
        flat = []
        for sub in value:
            flat.extend(float(v) for v in sub)
        vec = _create_float_vector(b, flat)
        _fb.AttrValueStart(b)
        _fb.AttrValueAddValueType(b, AttrValueType.FloatArray)
        _fb.AttrValueAddFloatArray(b, vec)
        _fb.AttrValueAddStride(b, stride)
        return _fb.AttrValueEnd(b)

    # Fallback
    json_off = b.CreateString(json.dumps(value))
    _fb.AttrValueStart(b)
    _fb.AttrValueAddValueType(b, AttrValueType.NestedList)
    _fb.AttrValueAddNestedJson(b, json_off)
    return _fb.AttrValueEnd(b)


@register_encoder(K_SET_GPRIM_ATTRS, fb_tag=EventPayloadType.SetGprimAttrs, fb_class=SetGprimAttrs)
def _encode_set_gprim_attrs(b, ev):
    prim = b.CreateString(ev["prim"])
    attrs = ev.get("attrs", {})
    primvar_meta = ev.get("primvar_meta", {})
    attr_interp_map = ev.get("attr_interp", {})

    attr_offsets = [_encode_attr_value(b, name, val) for name, val in attrs.items()]
    _fb.SetGprimAttrsStartAttrsVector(b, len(attr_offsets))
    for off in reversed(attr_offsets):
        b.PrependUOffsetTRelative(off)
    attrs_vec = b.EndVector()

    pvm_offsets = []
    for aname, meta in primvar_meta.items():
        an = b.CreateString(aname)
        tn = b.CreateString(meta.get("typeName", ""))
        interp = b.CreateString(meta["interpolation"]) if meta.get("interpolation") else None
        _fb.PrimvarMetaStart(b)
        _fb.PrimvarMetaAddAttrName(b, an)
        _fb.PrimvarMetaAddTypeName(b, tn)
        if interp:
            _fb.PrimvarMetaAddInterpolation(b, interp)
        pvm_offsets.append(_fb.PrimvarMetaEnd(b))

    pvm_vec = None
    if pvm_offsets:
        _fb.SetGprimAttrsStartPrimvarMetaVector(b, len(pvm_offsets))
        for off in reversed(pvm_offsets):
            b.PrependUOffsetTRelative(off)
        pvm_vec = b.EndVector()

    ai_offsets = []
    for aname, interp in attr_interp_map.items():
        an = b.CreateString(aname)
        iv = b.CreateString(interp)
        _fb.AttrInterpStart(b)
        _fb.AttrInterpAddAttrName(b, an)
        _fb.AttrInterpAddInterpolation(b, iv)
        ai_offsets.append(_fb.AttrInterpEnd(b))

    ai_vec = None
    if ai_offsets:
        _fb.SetGprimAttrsStartAttrInterpVector(b, len(ai_offsets))
        for off in reversed(ai_offsets):
            b.PrependUOffsetTRelative(off)
        ai_vec = b.EndVector()

    _fb.SetGprimAttrsStart(b)
    _fb.SetGprimAttrsAddPrim(b, prim)
    _fb.SetGprimAttrsAddAttrs(b, attrs_vec)
    if pvm_vec is not None:
        _fb.SetGprimAttrsAddPrimvarMeta(b, pvm_vec)
    if ai_vec is not None:
        _fb.SetGprimAttrsAddAttrInterp(b, ai_vec)
    if ev.get("time") is not None:
        _fb.SetGprimAttrsAddTime(b, float(ev["time"]))
    return _fb.SetGprimAttrsEnd(b)


def _encode_arc_entries(b, entries):
    offsets = []
    for entry in entries:
        ap = b.CreateString(entry["asset_path"]) if entry.get("asset_path") else None
        pp = b.CreateString(entry["prim_path"]) if entry.get("prim_path") else None
        _fb.ArcEntryStart(b)
        if ap:
            _fb.ArcEntryAddAssetPath(b, ap)
        if pp:
            _fb.ArcEntryAddPrimPath(b, pp)
        offsets.append(_fb.ArcEntryEnd(b))
    return offsets


@register_encoder(K_SET_REFERENCE, fb_tag=EventPayloadType.SetReference, fb_class=SetReference)
def _encode_set_reference(b, ev):
    prim = b.CreateString(ev["prim"])
    arc_offsets = _encode_arc_entries(b, ev["refs"])
    _fb.SetReferenceStartRefsVector(b, len(arc_offsets))
    for off in reversed(arc_offsets):
        b.PrependUOffsetTRelative(off)
    refs_vec = b.EndVector()
    _fb.SetReferenceStart(b)
    _fb.SetReferenceAddPrim(b, prim)
    _fb.SetReferenceAddRefs(b, refs_vec)
    return _fb.SetReferenceEnd(b)


@register_encoder(K_SET_PAYLOAD, fb_tag=EventPayloadType.SetPayload, fb_class=SetPayload)
def _encode_set_payload(b, ev):
    prim = b.CreateString(ev["prim"])
    arc_offsets = _encode_arc_entries(b, ev["payloads"])
    _fb.SetPayloadStartPayloadsVector(b, len(arc_offsets))
    for off in reversed(arc_offsets):
        b.PrependUOffsetTRelative(off)
    payloads_vec = b.EndVector()
    _fb.SetPayloadStart(b)
    _fb.SetPayloadAddPrim(b, prim)
    _fb.SetPayloadAddPayloads(b, payloads_vec)
    return _fb.SetPayloadEnd(b)


@register_encoder(K_LOAD_PAYLOAD, fb_tag=EventPayloadType.LoadPayload, fb_class=LoadPayload)
def _encode_load_payload(b, ev):
    prim = b.CreateString(ev["prim"])
    _fb.LoadPayloadStart(b)
    _fb.LoadPayloadAddPrim(b, prim)
    return _fb.LoadPayloadEnd(b)


@register_encoder(K_UNLOAD_PAYLOAD, fb_tag=EventPayloadType.UnloadPayload, fb_class=UnloadPayload)
def _encode_unload_payload(b, ev):
    prim = b.CreateString(ev["prim"])
    _fb.UnloadPayloadStart(b)
    _fb.UnloadPayloadAddPrim(b, prim)
    return _fb.UnloadPayloadEnd(b)


@register_encoder(
    K_SET_VARIANT_SELECTIONS,
    fb_tag=EventPayloadType.SetVariantSelections,
    fb_class=SetVariantSelections,
)
def _encode_set_variant_selections(b, ev):
    prim = b.CreateString(ev["prim"])
    sp_offsets = []
    for key, val in ev["selections"].items():
        k = b.CreateString(key)
        v = b.CreateString(val)
        _fb.StringPairStart(b)
        _fb.StringPairAddKey(b, k)
        _fb.StringPairAddValue(b, v)
        sp_offsets.append(_fb.StringPairEnd(b))
    _fb.SetVariantSelectionsStartSelectionsVector(b, len(sp_offsets))
    for off in reversed(sp_offsets):
        b.PrependUOffsetTRelative(off)
    sel_vec = b.EndVector()
    _fb.SetVariantSelectionsStart(b)
    _fb.SetVariantSelectionsAddPrim(b, prim)
    _fb.SetVariantSelectionsAddSelections(b, sel_vec)
    return _fb.SetVariantSelectionsEnd(b)


@register_encoder(
    K_SET_MATERIAL_BINDING,
    fb_tag=EventPayloadType.SetMaterialBinding,
    fb_class=SetMaterialBinding,
)
def _encode_set_material_binding(b, ev):
    prim = b.CreateString(ev["prim"])
    mp = b.CreateString(ev["material_path"])
    _fb.SetMaterialBindingStart(b)
    _fb.SetMaterialBindingAddPrim(b, prim)
    _fb.SetMaterialBindingAddMaterialPath(b, mp)
    return _fb.SetMaterialBindingEnd(b)


@register_encoder(
    K_SET_CONNECTABLE_INPUT,
    fb_tag=EventPayloadType.SetConnectableInput,
    fb_class=SetConnectableInput,
)
def _encode_set_connectable_input(b, ev):
    prim = b.CreateString(ev["prim"])
    info_id = b.CreateString(ev["info_id"])
    inputs = ev.get("inputs", {})
    input_types = ev.get("input_types", {})

    civ_offsets = []
    for name, value in inputs.items():
        n = b.CreateString(name)
        tn = b.CreateString(input_types.get(name, ""))
        str_off = None
        float_vec = None
        if isinstance(value, str):
            str_off = b.CreateString(value)
        elif isinstance(value, list):
            float_vec = _create_float_vector(b, [float(v) for v in value])

        _fb.ConnectableInputValueStart(b)
        _fb.ConnectableInputValueAddName(b, n)
        _fb.ConnectableInputValueAddTypeName(b, tn)
        if isinstance(value, bool):
            _fb.ConnectableInputValueAddValueType(b, ConnectableInputValueType.ScalarBool)
            _fb.ConnectableInputValueAddScalarBool(b, value)
        elif isinstance(value, int) and not isinstance(value, bool):
            _fb.ConnectableInputValueAddValueType(b, ConnectableInputValueType.ScalarInt)
            _fb.ConnectableInputValueAddScalarInt(b, value)
        elif isinstance(value, float):
            _fb.ConnectableInputValueAddValueType(b, ConnectableInputValueType.ScalarFloat)
            _fb.ConnectableInputValueAddScalarFloat(b, value)
        elif str_off is not None:
            _fb.ConnectableInputValueAddValueType(b, ConnectableInputValueType.ScalarString)
            _fb.ConnectableInputValueAddScalarString(b, str_off)
        elif float_vec is not None:
            _fb.ConnectableInputValueAddValueType(b, ConnectableInputValueType.FloatArray)
            _fb.ConnectableInputValueAddFloatArray(b, float_vec)
        civ_offsets.append(_fb.ConnectableInputValueEnd(b))

    _fb.SetConnectableInputStartInputsVector(b, len(civ_offsets))
    for off in reversed(civ_offsets):
        b.PrependUOffsetTRelative(off)
    inputs_vec = b.EndVector()

    _fb.SetConnectableInputStart(b)
    _fb.SetConnectableInputAddPrim(b, prim)
    _fb.SetConnectableInputAddInfoId(b, info_id)
    _fb.SetConnectableInputAddInputs(b, inputs_vec)
    if ev.get("time") is not None:
        _fb.SetConnectableInputAddTime(b, float(ev["time"]))
    return _fb.SetConnectableInputEnd(b)


@register_encoder(
    K_SET_CONNECTABLE_CONNECTION,
    fb_tag=EventPayloadType.SetConnectableConnection,
    fb_class=SetConnectableConnection,
)
def _encode_set_connectable_connection(b, ev):
    prim = b.CreateString(ev["prim"])
    connections = ev.get("connections", {})
    disconnections = ev.get("disconnections", [])

    conn_offsets = []
    for local_attr, conn in connections.items():
        la_off = b.CreateString(local_attr)
        sp_off = b.CreateString(conn["source_prim"])
        sa_off = b.CreateString(conn["source_attr"])
        _fb.ConnectionStart(b)
        _fb.ConnectionAddLocalAttr(b, la_off)
        _fb.ConnectionAddSourcePrim(b, sp_off)
        _fb.ConnectionAddSourceAttr(b, sa_off)
        conn_offsets.append(_fb.ConnectionEnd(b))

    _fb.SetConnectableConnectionStartConnectionsVector(b, len(conn_offsets))
    for off in reversed(conn_offsets):
        b.PrependUOffsetTRelative(off)
    conn_vec = b.EndVector()

    disc_str_offsets = [b.CreateString(d) for d in disconnections]
    _fb.SetConnectableConnectionStartDisconnectionsVector(b, len(disc_str_offsets))
    for off in reversed(disc_str_offsets):
        b.PrependUOffsetTRelative(off)
    disc_vec = b.EndVector()

    _fb.SetConnectableConnectionStart(b)
    _fb.SetConnectableConnectionAddPrim(b, prim)
    _fb.SetConnectableConnectionAddConnections(b, conn_vec)
    _fb.SetConnectableConnectionAddDisconnections(b, disc_vec)
    return _fb.SetConnectableConnectionEnd(b)


@register_encoder(
    K_SET_STAGE_METADATA,
    fb_tag=EventPayloadType.SetStageMetadata,
    fb_class=SetStageMetadata,
)
def _encode_set_stage_metadata(b, ev):
    return _encode_stage_metadata_table(b, {k: v for k, v in ev.items() if k != "k"}, force=True)


# ===================================================================
# DEBUG / COMPACTION API  (FlatBuffers -> dict, copies everything)
# ===================================================================


def message_to_dict(buf: bytes | bytearray, *, numpy_arrays: bool = False) -> dict:
    """Decode FlatBuffers wire bytes to a Python dict.

    When *numpy_arrays* is True, geometry array attributes are returned as
    numpy arrays (zero-copy views into the FlatBuffer) for efficient
    Vt.*Array.FromNumpy() conversion in event_apply.  Default (False) returns
    plain Python lists for JSON-safe compatibility.
    """
    envelope = decode_envelope(buf)
    msg_type, obj = resolve_payload(envelope)
    if msg_type in (MSG_TXN, MSG_EVENT, MSG_CREATE_PROPOSAL):
        return _DICT_DECODE_DISPATCH[msg_type](obj, msg_type, numpy_arrays=numpy_arrays)
    return _DICT_DECODE_DISPATCH[msg_type](obj, msg_type)


def event_to_dict(ew: EventWrapper, *, numpy_arrays: bool = False) -> dict:
    """Decode an EventWrapper FB object to a Python dict.

    See *message_to_dict* for the meaning of *numpy_arrays*.
    """
    kind, obj = resolve_event(ew)
    if kind == K_SET_GPRIM_ATTRS:
        return _dict_set_gprim_attrs(obj, kind, numpy_arrays=numpy_arrays)
    spec = _events.get(kind)
    if spec is None or spec.decode is None:
        raise KeyError(f"no registered decoder for event kind {kind!r}")
    return spec.decode(obj, kind)


@dataclass(slots=True)
class DecodeResult:
    """Outcome of decoding one batch of wire messages."""

    received: list[Event] = field(default_factory=list)
    last_seq: int = 0
    resync_requested: bool = False
    rate_limited_retry_after: float | None = None
    errors: list[Exception] = field(default_factory=list)


def decode_messages(
    raw_messages: Iterable[bytes],
    *,
    last_seq: int = 0,
    numpy_arrays: bool = False,
    clear_on_resync: bool = False,
) -> DecodeResult:
    """Decode a batch of wire messages with sequence dedup and resync handling.

    Per-message decode failures are captured into ``result.errors``
    rather than raised.
    """
    result = DecodeResult(last_seq=last_seq)
    for raw in raw_messages:
        try:
            msg = message_to_dict(raw, numpy_arrays=numpy_arrays)
        except Exception as exc:  # noqa: BLE001 — surfaced via result.errors
            result.errors.append(exc)
            continue

        msg_type = msg.get("type")
        if msg_type == MSG_RESYNC:
            result.last_seq = 0
            result.resync_requested = True
            if clear_on_resync:
                result.received.clear()
            continue

        if msg_type == MSG_RATE_LIMITED:
            retry_after = msg.get("retry_after")
            if isinstance(retry_after, (int, float)):
                result.rate_limited_retry_after = float(retry_after)
            continue

        if msg_type != MSG_EVENT:
            continue

        seq = int(msg.get("seq") or 0)
        if seq and seq <= result.last_seq:
            continue
        if seq:
            result.last_seq = seq
        event = msg.get("event")
        if event:
            result.received.append(event)

    return result


# --- Helpers ---


def _str(val) -> str | None:
    if val is None:
        return None
    if isinstance(val, bytes):
        return val.decode("utf-8")
    return val


# --- Per-message dict decoders ---


def _dict_hello(h, msg_type):
    msg = {"type": msg_type, "role": _str(h.Role()), "protocol_version": h.ProtocolVersion()}
    sf = h.SyncFrom()
    if sf:
        msg["sync_from"] = sf
    for key, getter in [
        ("client_id", h.ClientId),
        ("origin", h.Origin),
        ("department", h.Department),
        ("token", h.Token),
    ]:
        v = _str(getter())
        if v:
            msg[key] = v
    return msg


def _dict_hello_ok(h, msg_type):
    msg = {"type": msg_type}
    token = _str(h.Token())
    if token:
        msg["token"] = token
    sm = h.StageMetadata()
    if sm is not None:
        meta = _decode_stage_metadata_table(sm)
        if meta:
            msg["stage_metadata"] = meta
    return msg


def _dict_claim_playback(cp, msg_type):
    msg = {"type": msg_type, "client_id": _str(cp.ClientId()) or ""}
    t = cp.Time()
    if t is not None:
        msg["time"] = t
    return msg


def _dict_playback_claimed(pc, msg_type):
    return {"type": msg_type, "leader_client_id": _str(pc.LeaderClientId()) or ""}


def _dict_playback_rejected(pr, msg_type):
    return {
        "type": msg_type,
        "reason": _str(pr.Reason()) or "",
        "current_leader_client_id": _str(pr.CurrentLeaderClientId()) or "",
    }


def _dict_playback_control(pc, msg_type):
    return {
        "type": msg_type,
        "action": _str(pc.Action()) or "",
        "time": pc.Time(),
        "rate": pc.Rate(),
    }


def _dict_playback_state(ps, msg_type):
    return {
        "type": msg_type,
        "time": ps.Time(),
        "playing": ps.Playing(),
        "rate": ps.Rate(),
        "leader_client_id": _str(ps.LeaderClientId()) or "",
    }


def _dict_auth_rejected(h, msg_type):
    return {"type": msg_type, "reason": _str(h.Reason()) or ""}


def _dict_txn(t, msg_type, numpy_arrays=False):
    events = [
        event_to_dict(t.Events(i), numpy_arrays=numpy_arrays) for i in range(t.EventsLength())
    ]
    return {"type": msg_type, "client_id": _str(t.ClientId()), "events": events}


def _dict_broadcast_event(be, msg_type, numpy_arrays=False):
    ew = be.Event()
    event_dict = event_to_dict(ew, numpy_arrays=numpy_arrays) if ew else {}
    msg = {"type": msg_type, "seq": be.Seq(), "event": event_dict}
    for key, getter in [("origin", be.Origin), ("client_id", be.ClientId), ("client", be.Client)]:
        v = _str(getter())
        if v:
            msg[key] = v
    return msg


def _dict_empty(_obj, msg_type):
    return {"type": msg_type}


def _dict_create_proposal(cp, msg_type, numpy_arrays=False):
    events = [
        event_to_dict(cp.Events(i), numpy_arrays=numpy_arrays) for i in range(cp.EventsLength())
    ]
    return {
        "type": msg_type,
        "target_department": _str(cp.TargetDepartment()),
        "events": events,
        "description": _str(cp.Description()) or "",
    }


def _dict_proposal_created(pc, msg_type):
    return {"type": msg_type, "proposal_id": _str(pc.ProposalId())}


def _dict_rate_limited(rl, msg_type):
    return {"type": msg_type, "retry_after": rl.RetryAfter()}


_DICT_DECODE_DISPATCH = {
    MSG_HELLO: _dict_hello,
    MSG_HELLO_OK: _dict_hello_ok,
    MSG_AUTH_REJECTED: _dict_auth_rejected,
    MSG_TXN: _dict_txn,
    MSG_EVENT: _dict_broadcast_event,
    MSG_RESYNC: _dict_empty,
    MSG_COMPACT: _dict_empty,
    MSG_PING: _dict_empty,
    MSG_QUIT: _dict_empty,
    MSG_CREATE_PROPOSAL: _dict_create_proposal,
    MSG_PROPOSAL_CREATED: _dict_proposal_created,
    MSG_RATE_LIMITED: _dict_rate_limited,
    MSG_CLAIM_PLAYBACK: _dict_claim_playback,
    MSG_PLAYBACK_CLAIMED: _dict_playback_claimed,
    MSG_PLAYBACK_REJECTED: _dict_playback_rejected,
    MSG_PLAYBACK_CONTROL: _dict_playback_control,
    MSG_PLAYBACK_STATE: _dict_playback_state,
}


# --- Per-event dict decoders ---


@register_decoder(K_ENSURE_PRIM)
def _dict_ensure_prim(ep, kind):
    ev = {"k": kind, "prim": _str(ep.Prim())}
    tn = _str(ep.TypeName())
    # Preserve empty typeName ("" means untyped def in USD, distinct from
    # missing-key legacy behavior which defaults to Xform at apply time).
    if tn is not None:
        ev["typeName"] = tn
    api_len = ep.ApiSchemasLength()
    if api_len:
        ev["api_schemas"] = [_str(ep.ApiSchemas(i)) for i in range(api_len)]
    return ev


@register_decoder(K_ENSURE_XFORM_OPS)
def _dict_ensure_xform_ops(exo, kind):
    return {"k": kind, "prim": _str(exo.Prim())}


@register_decoder(K_SET_XFORM_TRS)
def _dict_set_xform_trs(trs, kind):
    bitmask = trs.Fields()
    fields = []
    ev = {"k": kind, "prim": _str(trs.Prim())}
    if bitmask & 1:
        fields.append("t")
        ev["t"] = [trs.T(i) for i in range(trs.TLength())]
    if bitmask & 2:
        fields.append("r")
        ev["r"] = [trs.R(i) for i in range(trs.RLength())]
    if bitmask & 4:
        fields.append("s")
        ev["s"] = [trs.S(i) for i in range(trs.SLength())]
    ev["fields"] = fields
    t = trs.Time()
    if t is not None:
        ev["time"] = t
    return ev


@register_decoder(K_DELETE_PRIM)
def _dict_delete_prim(dp, kind):
    return {"k": kind, "prim": _str(dp.Prim())}


@register_decoder(K_DEACTIVATE_PRIM)
def _dict_deactivate_prim(dp, kind):
    return {"k": kind, "prim": _str(dp.Prim()), "active": dp.Active()}


@register_decoder(K_RENAME_PRIM)
def _dict_rename_prim(rp, kind):
    return {"k": kind, "prim": _str(rp.Prim()), "new_name": _str(rp.NewName())}


@register_decoder(K_SET_VISIBILITY)
def _dict_set_visibility(sv, kind):
    ev = {"k": kind, "prim": _str(sv.Prim()), "visible": sv.Visible()}
    t = sv.Time()
    if t is not None:
        ev["time"] = t
    return ev


def _attr_value_to_python(av, numpy_arrays: bool = False):
    """Convert an AttrValue FB object to a Python value.

    When *numpy_arrays* is True, array types return numpy arrays (zero-copy
    view into the FlatBuffer) so downstream consumers like event_apply can
    pass them directly to Vt.*Array.FromNumpy() without intermediate lists.
    When False (default), arrays are returned as plain Python lists for
    JSON-safe compatibility (dashboard, tests, compaction).
    """
    vt = av.ValueType()
    if vt == AttrValueType.ScalarFloat:
        return av.ScalarFloat()
    if vt == AttrValueType.ScalarInt:
        return av.ScalarInt()
    if vt == AttrValueType.ScalarBool:
        return av.ScalarBool()
    if vt == AttrValueType.ScalarString:
        return _str(av.ScalarString())
    if vt == AttrValueType.FloatArray:
        if numpy_arrays:
            arr = av.FloatArrayAsNumpy()
            stride = av.Stride()
            if stride > 1:
                arr = arr.reshape(-1, stride)
            return arr
        stride = av.Stride()
        length = av.FloatArrayLength()
        if stride <= 1:
            return [av.FloatArray(i) for i in range(length)]
        return [[av.FloatArray(i + j) for j in range(stride)] for i in range(0, length, stride)]
    if vt == AttrValueType.IntArray:
        if numpy_arrays:
            arr = av.IntArrayAsNumpy()
            stride = av.Stride()
            if stride > 1:
                arr = arr.reshape(-1, stride)
            return arr
        stride = av.Stride()
        length = av.IntArrayLength()
        if stride <= 1:
            return [av.IntArray(i) for i in range(length)]
        return [[av.IntArray(i + j) for j in range(stride)] for i in range(0, length, stride)]
    if vt == AttrValueType.NestedList:
        return json.loads(_str(av.NestedJson()))
    return None


@register_decoder(K_SET_GPRIM_ATTRS)
def _dict_set_gprim_attrs(sg, kind, numpy_arrays=False):
    attrs = {}
    for i in range(sg.AttrsLength()):
        na = sg.Attrs(i)
        attrs[_str(na.Name())] = _attr_value_to_python(na.Value(), numpy_arrays)

    ev = {"k": kind, "prim": _str(sg.Prim()), "attrs": attrs}

    if sg.PrimvarMetaLength():
        primvar_meta = {}
        for i in range(sg.PrimvarMetaLength()):
            pm = sg.PrimvarMeta(i)
            meta = {"typeName": _str(pm.TypeName())}
            interp = _str(pm.Interpolation())
            if interp:
                meta["interpolation"] = interp
            primvar_meta[_str(pm.AttrName())] = meta
        ev["primvar_meta"] = primvar_meta

    if sg.AttrInterpLength():
        attr_interp = {}
        for i in range(sg.AttrInterpLength()):
            ai = sg.AttrInterp(i)
            attr_interp[_str(ai.AttrName())] = _str(ai.Interpolation())
        ev["attr_interp"] = attr_interp

    t = sg.Time()
    if t is not None:
        ev["time"] = t
    return ev


@register_decoder(K_SET_REFERENCE)
def _dict_set_reference(sr, kind):
    refs = []
    for i in range(sr.RefsLength()):
        arc = sr.Refs(i)
        entry = {}
        ap = _str(arc.AssetPath())
        if ap:
            entry["asset_path"] = ap
        pp = _str(arc.PrimPath())
        if pp:
            entry["prim_path"] = pp
        refs.append(entry)
    return {"k": kind, "prim": _str(sr.Prim()), "refs": refs}


@register_decoder(K_SET_PAYLOAD)
def _dict_set_payload(sp, kind):
    payloads = []
    for i in range(sp.PayloadsLength()):
        arc = sp.Payloads(i)
        entry = {}
        ap = _str(arc.AssetPath())
        if ap:
            entry["asset_path"] = ap
        pp = _str(arc.PrimPath())
        if pp:
            entry["prim_path"] = pp
        payloads.append(entry)
    return {"k": kind, "prim": _str(sp.Prim()), "payloads": payloads}


@register_decoder(K_LOAD_PAYLOAD)
def _dict_load_payload(lp, kind):
    return {"k": kind, "prim": _str(lp.Prim())}


@register_decoder(K_UNLOAD_PAYLOAD)
def _dict_unload_payload(up, kind):
    return {"k": kind, "prim": _str(up.Prim())}


@register_decoder(K_SET_VARIANT_SELECTIONS)
def _dict_set_variant_selections(sv, kind):
    selections = {}
    for i in range(sv.SelectionsLength()):
        sp = sv.Selections(i)
        selections[_str(sp.Key())] = _str(sp.Value())
    return {"k": kind, "prim": _str(sv.Prim()), "selections": selections}


@register_decoder(K_SET_MATERIAL_BINDING)
def _dict_set_material_binding(mb, kind):
    return {"k": kind, "prim": _str(mb.Prim()), "material_path": _str(mb.MaterialPath())}


@register_decoder(K_SET_CONNECTABLE_INPUT)
def _dict_set_connectable_input(sci, kind):
    inputs = {}
    input_types = {}
    for i in range(sci.InputsLength()):
        civ = sci.Inputs(i)
        name = _str(civ.Name())
        input_types[name] = _str(civ.TypeName())
        vt = civ.ValueType()
        if vt == ConnectableInputValueType.FloatArray:
            inputs[name] = [civ.FloatArray(j) for j in range(civ.FloatArrayLength())]
        elif vt == ConnectableInputValueType.ScalarString:
            inputs[name] = _str(civ.ScalarString())
        elif vt == ConnectableInputValueType.ScalarBool:
            inputs[name] = civ.ScalarBool()
        elif vt == ConnectableInputValueType.ScalarInt:
            inputs[name] = civ.ScalarInt()
        else:
            inputs[name] = civ.ScalarFloat()
    ev = {
        "k": kind,
        "prim": _str(sci.Prim()),
        "info_id": _str(sci.InfoId()),
        "inputs": inputs,
        "input_types": input_types,
    }
    t = sci.Time()
    if t is not None:
        ev["time"] = t
    return ev


@register_decoder(K_SET_STAGE_METADATA)
def _dict_set_stage_metadata(sm, kind):
    ev = {"k": kind}
    ev.update(_decode_stage_metadata_table(sm))
    return ev


@register_decoder(K_SET_CONNECTABLE_CONNECTION)
def _dict_set_connectable_connection(scc, kind):
    connections = {}
    for i in range(scc.ConnectionsLength()):
        c = scc.Connections(i)
        connections[_str(c.LocalAttr())] = {
            "source_prim": _str(c.SourcePrim()),
            "source_attr": _str(c.SourceAttr()),
        }
    disconnections = [_str(scc.Disconnections(i)) for i in range(scc.DisconnectionsLength())]
    ev = {"k": kind, "prim": _str(scc.Prim()), "connections": connections}
    if disconnections:
        ev["disconnections"] = disconnections
    return ev
