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
    ARC_LIST_POSITIONS,
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_REPLACE_SDF_LAYER_CONTENT,
    K_SET_CONNECTABLE_CONNECTION,
    K_SET_CONNECTABLE_INPUT,
    K_SET_GPRIM_ATTRS,
    K_SET_INSTANCEABLE,
    K_SET_MATERIAL_BINDING,
    K_SET_PAYLOAD,
    K_SET_POINT_INSTANCER,
    K_SET_REFERENCE,
    K_SET_SDF_SPEC_FIELDS,
    K_SET_STAGE_METADATA,
    K_SET_SUBLAYERS,
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
    MSG_HELLO_REJECTED,
    MSG_LAYER_GRAPH_STATE,
    MSG_LAYER_STACK_STATE,
    MSG_PING,
    MSG_PLAYBACK_CLAIMED,
    MSG_PLAYBACK_CONTROL,
    MSG_PLAYBACK_REJECTED,
    MSG_PLAYBACK_STATE,
    MSG_PROPOSAL_CREATED,
    MSG_QUIT,
    MSG_RATE_LIMITED,
    MSG_REPLAY_COMPLETE,
    MSG_RESYNC,
    MSG_TRANSACTION_RESULT,
    MSG_TXN,
    POINT_INSTANCER_FIELDS,
    SDF_SPEC_KINDS,
    LayerMode,
)

# Re-export generated classes so consumers import from codec, not generated path.
# These are the typed FB table classes the rest of the codebase works with.
Envelope = _fb.Envelope
Hello = _fb.Hello
HelloOk = _fb.HelloOk
AuthRejected = _fb.AuthRejected
HelloRejectionCode = _fb.HelloRejectionCode
HelloRejected = _fb.HelloRejected
Txn = _fb.Txn
TransactionResult = _fb.TransactionResult
TransactionStatus = _fb.TransactionStatus
TransactionRejectionCode = _fb.TransactionRejectionCode
ReplayComplete = _fb.ReplayComplete
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
SetInstanceable = _fb.SetInstanceable
SetPointInstancer = _fb.SetPointInstancer
SetSdfSpecFields = _fb.SetSdfSpecFields
ReplaceSdfLayerContent = _fb.ReplaceSdfLayerContent
SetSublayers = _fb.SetSublayers
ClaimPlayback = _fb.ClaimPlayback
PlaybackClaimed = _fb.PlaybackClaimed
PlaybackRejected = _fb.PlaybackRejected
PlaybackControl = _fb.PlaybackControl
PlaybackState = _fb.PlaybackState
LogicalLayerState = _fb.LogicalLayerState
LayerStackState = _fb.LayerStackState
LayerGraphState = _fb.LayerGraphState
SharedLayerState = _fb.SharedLayerState
SublayerEntry = _fb.SublayerEntry
NamedAttr = _fb.NamedAttr
AttrValue = _fb.AttrValue
AttrValueType = _fb.AttrValueType
PrimvarMeta = _fb.PrimvarMeta
AttrInterp = _fb.AttrInterp
ConnectableInputValue = _fb.ConnectableInputValue
Connection = _fb.Connection
ConnectableInputValueType = _fb.ConnectableInputValueType
ArcEntry = _fb.ArcEntry
ArcListPositionType = _fb.ArcListPosition
StringPair = _fb.StringPair
PayloadType = _fb.Payload
EventPayloadType = _fb.EventPayload
SdfSpecKindType = _fb.SdfSpecKind
LayerModeType = _fb.LayerMode

SCHEMA_VERSION = 8

# ---------------------------------------------------------------------------
# Mapping tables
# ---------------------------------------------------------------------------

_MSG_TYPE_TO_PAYLOAD = {
    MSG_HELLO: PayloadType.Hello,
    MSG_HELLO_OK: PayloadType.HelloOk,
    MSG_AUTH_REJECTED: PayloadType.AuthRejected,
    MSG_HELLO_REJECTED: PayloadType.HelloRejected,
    MSG_TXN: PayloadType.Txn,
    MSG_TRANSACTION_RESULT: PayloadType.TransactionResult,
    MSG_REPLAY_COMPLETE: PayloadType.ReplayComplete,
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
    MSG_LAYER_STACK_STATE: PayloadType.LayerStackState,
    MSG_LAYER_GRAPH_STATE: PayloadType.LayerGraphState,
}

_LAYER_MODE_TO_FB = {
    LayerMode.MANAGED.value: LayerModeType.Managed,
    LayerMode.SHARED_STAGE.value: LayerModeType.SharedStage,
}
_FB_TO_LAYER_MODE = {value: key for key, value in _LAYER_MODE_TO_FB.items()}

_ARC_POSITION_TO_FB = {
    "explicit": ArcListPositionType.Explicit,
    "added": ArcListPositionType.Added,
    "prepended": ArcListPositionType.Prepended,
    "appended": ArcListPositionType.Appended,
    "deleted": ArcListPositionType.Deleted,
    "ordered": ArcListPositionType.Ordered,
}
assert frozenset(_ARC_POSITION_TO_FB) == ARC_LIST_POSITIONS
_FB_TO_ARC_POSITION = {value: key for key, value in _ARC_POSITION_TO_FB.items()}

_SDF_SPEC_KIND_TO_FB = {
    "layer": SdfSpecKindType.Layer,
    "prim": SdfSpecKindType.Prim,
    "attribute": SdfSpecKindType.Attribute,
    "relationship": SdfSpecKindType.Relationship,
    "variant_set": SdfSpecKindType.VariantSet,
    "variant": SdfSpecKindType.Variant,
    "property": SdfSpecKindType.Property,
}
assert tuple(_SDF_SPEC_KIND_TO_FB) == SDF_SPEC_KINDS
_FB_TO_SDF_SPEC_KIND = {value: key for key, value in _SDF_SPEC_KIND_TO_FB.items()}

_TRANSACTION_STATUS_TO_FB = {
    "acknowledged": TransactionStatus.Acknowledged,
    "rejected": TransactionStatus.Rejected,
}
_TRANSACTION_REJECTION_TO_FB = {
    "none": TransactionRejectionCode.None_,
    "invalid_identity": TransactionRejectionCode.InvalidIdentity,
    "unexpected_id": TransactionRejectionCode.UnexpectedId,
    "invalid_transaction": TransactionRejectionCode.InvalidTransaction,
}

_PAYLOAD_TO_MSG_TYPE = {v: k for k, v in _MSG_TYPE_TO_PAYLOAD.items()}

_PAYLOAD_TO_CLASS = {
    PayloadType.Hello: Hello,
    PayloadType.HelloOk: HelloOk,
    PayloadType.AuthRejected: AuthRejected,
    PayloadType.HelloRejected: HelloRejected,
    PayloadType.Txn: Txn,
    PayloadType.TransactionResult: TransactionResult,
    PayloadType.ReplayComplete: ReplayComplete,
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
    PayloadType.LayerStackState: LayerStackState,
    PayloadType.LayerGraphState: LayerGraphState,
}

# Stage metadata numeric fields paired with their FB Add* setters.
# upAxis is a string and handled separately.
_STAGE_META_FB_NUMERIC = (
    ("timeCodesPerSecond", _fb.SetStageMetadataAddTimeCodesPerSecond),
    ("framesPerSecond", _fb.SetStageMetadataAddFramesPerSecond),
    ("startTimeCode", _fb.SetStageMetadataAddStartTimeCode),
    ("endTimeCode", _fb.SetStageMetadataAddEndTimeCode),
    ("metersPerUnit", _fb.SetStageMetadataAddMetersPerUnit),
)

_TRS_BITS = {"t": 1, "r": 2, "s": 4}

_PI_BITS = {name: 1 << i for i, name in enumerate(POINT_INSTANCER_FIELDS)}

# field -> (FB add fn, FB accessor base, element stride, numpy dtype).
# None stride = flat scalar vector; others reshape to (N, stride) on decode.
_PI_ARRAYS = {
    "proto_indices": (_fb.SetPointInstancerAddProtoIndices, "ProtoIndices", None, np.int32),
    "positions": (_fb.SetPointInstancerAddPositions, "Positions", 3, np.float32),
    "orientations": (_fb.SetPointInstancerAddOrientations, "Orientations", 4, np.float32),
    "scales": (_fb.SetPointInstancerAddScales, "Scales", 3, np.float32),
    "velocities": (_fb.SetPointInstancerAddVelocities, "Velocities", 3, np.float32),
    "accelerations": (_fb.SetPointInstancerAddAccelerations, "Accelerations", 3, np.float32),
    "angular_velocities": (
        _fb.SetPointInstancerAddAngularVelocities,
        "AngularVelocities",
        3,
        np.float32,
    ),
    "ids": (_fb.SetPointInstancerAddIds, "Ids", None, np.int64),
    "invisible_ids": (_fb.SetPointInstancerAddInvisibleIds, "InvisibleIds", None, np.int64),
    "inactive_ids": (_fb.SetPointInstancerAddInactiveIds, "InactiveIds", None, np.int64),
}

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
    envelope = Envelope.GetRootAs(buf, 0)
    version = envelope.SchemaVersion()
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported schema version {version}; expected {SCHEMA_VERSION}",
        )
    return envelope


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
    producer_session_id = (
        b.CreateString(msg["producer_session_id"])
        if msg.get("producer_session_id")
        else None
    )

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
    if msg.get("layered_replay"):
        _fb.HelloAddLayeredReplay(b, True)
    _fb.HelloAddLayerMode(
        b,
        _LAYER_MODE_TO_FB[msg.get("layer_mode", LayerMode.MANAGED.value)],
    )
    if producer_session_id:
        _fb.HelloAddProducerSessionId(b, producer_session_id)
    return _fb.HelloEnd(b)


def _encode_stage_metadata_table(b, meta: dict | None, *, force: bool = False) -> int | None:
    """Build a SetStageMetadata table from a sparse dict.

    Returns ``None`` when ``meta`` is empty and ``force`` is False — caller
    omits the optional field rather than writing an empty table.
    """
    if not meta and not force:
        return None
    meta = meta or {}
    up_axis_off = b.CreateString(meta["upAxis"]) if meta.get("upAxis") else None
    _fb.SetStageMetadataStart(b)
    for key, add_fn in _STAGE_META_FB_NUMERIC:
        if key in meta:
            add_fn(b, float(meta[key]))
    if up_axis_off is not None:
        _fb.SetStageMetadataAddUpAxis(b, up_axis_off)
    return _fb.SetStageMetadataEnd(b)


def _decode_stage_metadata_table(sm) -> dict:
    """Read a SetStageMetadata FB table into a sparse dict (only authored fields)."""
    out: dict = {}
    for key, _add_fn in _STAGE_META_FB_NUMERIC:
        # Each numeric getter returns None when the FB optional is unset.
        val = getattr(sm, key[0].upper() + key[1:])()
        if val is not None:
            out[key] = val
    up_axis = _str(sm.UpAxis())
    if up_axis:
        out["upAxis"] = up_axis
    return out


def _encode_hello_ok(b, msg):
    token = b.CreateString(msg["token"]) if msg.get("token") else None
    sm_off = _encode_stage_metadata_table(b, msg.get("stage_metadata"))
    _fb.HelloOkStart(b)
    if token:
        _fb.HelloOkAddToken(b, token)
    if sm_off is not None:
        _fb.HelloOkAddStageMetadata(b, sm_off)
    if msg.get("layered_replay"):
        _fb.HelloOkAddLayeredReplay(b, True)
    _fb.HelloOkAddLayerMode(
        b,
        _LAYER_MODE_TO_FB[msg.get("layer_mode", LayerMode.MANAGED.value)],
    )
    if msg.get("committed_through"):
        _fb.HelloOkAddCommittedThrough(b, int(msg["committed_through"]))
    return _fb.HelloOkEnd(b)


def _encode_auth_rejected(b, msg):
    reason = b.CreateString(msg.get("reason", ""))
    _fb.AuthRejectedStart(b)
    _fb.AuthRejectedAddReason(b, reason)
    return _fb.AuthRejectedEnd(b)


def _encode_hello_rejected(b, msg):
    code = int(msg.get("code", HelloRejectionCode.Unspecified))
    if code == HelloRejectionCode.Unspecified:
        raise ValueError("hello_rejected code must be specified")
    reason = b.CreateString(msg.get("reason", ""))
    _fb.HelloRejectedStart(b)
    _fb.HelloRejectedAddCode(b, code)
    _fb.HelloRejectedAddReason(b, reason)
    return _fb.HelloRejectedEnd(b)


def _encode_txn(b, msg):
    proposal_id = b.CreateString(msg["proposal_id"]) if msg.get("proposal_id") else None
    layer_key = b.CreateString(msg["layer_key"]) if msg.get("layer_key") else None
    event_offsets = [_encode_event_wrapper(b, ev) for ev in msg["events"]]
    _fb.TxnStartEventsVector(b, len(event_offsets))
    for off in reversed(event_offsets):
        b.PrependUOffsetTRelative(off)
    events_vec = b.EndVector()
    _fb.TxnStart(b)
    _fb.TxnAddEvents(b, events_vec)
    if proposal_id is not None:
        _fb.TxnAddProposalId(b, proposal_id)
    if layer_key is not None:
        _fb.TxnAddLayerKey(b, layer_key)
    if msg.get("txn_id"):
        _fb.TxnAddTxnId(b, int(msg["txn_id"]))
    return _fb.TxnEnd(b)


def _encode_transaction_result(b, msg):
    reason = b.CreateString(msg["reason"]) if msg.get("reason") else None
    _fb.TransactionResultStart(b)
    _fb.TransactionResultAddTxnId(b, int(msg["txn_id"]))
    status = msg["status"]
    if isinstance(status, str):
        status = _TRANSACTION_STATUS_TO_FB[status]
    rejection_code = msg.get("rejection_code", 0)
    if isinstance(rejection_code, str):
        rejection_code = _TRANSACTION_REJECTION_TO_FB[rejection_code]
    _fb.TransactionResultAddStatus(b, int(status))
    _fb.TransactionResultAddExpectedTxnId(b, int(msg.get("expected_txn_id", 0)))
    _fb.TransactionResultAddRejectionCode(b, int(rejection_code))
    if reason is not None:
        _fb.TransactionResultAddReason(b, reason)
    return _fb.TransactionResultEnd(b)


def _encode_replay_complete(b, msg):
    _fb.ReplayCompleteStart(b)
    _fb.ReplayCompleteAddHeadSeq(b, int(msg.get("head_seq", 0)))
    _fb.ReplayCompleteAddEpoch(b, int(msg.get("epoch", 0)))
    return _fb.ReplayCompleteEnd(b)


def _encode_broadcast_event(b, msg):
    ev_offset = _encode_event_wrapper(b, msg["event"])
    origin = b.CreateString(msg["origin"]) if msg.get("origin") else None
    client_id = b.CreateString(msg["client_id"]) if msg.get("client_id") else None
    client = b.CreateString(msg["client"]) if msg.get("client") else None
    layer_key = b.CreateString(msg["layer_key"]) if msg.get("layer_key") else None
    _fb.BroadcastEventStart(b)
    _fb.BroadcastEventAddSeq(b, msg["seq"])
    _fb.BroadcastEventAddEvent(b, ev_offset)
    if origin:
        _fb.BroadcastEventAddOrigin(b, origin)
    if client_id:
        _fb.BroadcastEventAddClientId(b, client_id)
    if client:
        _fb.BroadcastEventAddClient(b, client)
    if layer_key:
        _fb.BroadcastEventAddLayerKey(b, layer_key)
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
    if msg.get("time") is not None:
        _fb.PlaybackControlAddTime(b, float(msg["time"]))
    if msg.get("rate") is not None:
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


def _encode_layer_stack_state(b, msg):
    generation = b.CreateString(msg.get("generation", ""))
    layer_offsets = []
    for state in msg.get("layers", ()):
        layer_key = b.CreateString(state["layer_key"])
        label = b.CreateString(state["label"]) if state.get("label") else None
        _fb.LogicalLayerStateStart(b)
        _fb.LogicalLayerStateAddLayerKey(b, layer_key)
        if state.get("muted"):
            _fb.LogicalLayerStateAddMuted(b, True)
        if label is not None:
            _fb.LogicalLayerStateAddLabel(b, label)
        layer_offsets.append(_fb.LogicalLayerStateEnd(b))

    layers = None
    if layer_offsets:
        _fb.LayerStackStateStartLayersVector(b, len(layer_offsets))
        for offset in reversed(layer_offsets):
            b.PrependUOffsetTRelative(offset)
        layers = b.EndVector()

    _fb.LayerStackStateStart(b)
    _fb.LayerStackStateAddGeneration(b, generation)
    _fb.LayerStackStateAddRevision(b, int(msg.get("revision", 0)))
    if layers is not None:
        _fb.LayerStackStateAddLayers(b, layers)
    return _fb.LayerStackStateEnd(b)


def _encode_sublayer_entries(b, entries):
    offsets = []
    for entry in entries:
        authored_path = b.CreateString(entry["authored_path"])
        layer_key = b.CreateString(entry["layer_key"]) if entry.get("layer_key") else None
        _fb.SublayerEntryStart(b)
        _fb.SublayerEntryAddAuthoredPath(b, authored_path)
        _fb.SublayerEntryAddOffset(b, float(entry.get("offset", 0.0)))
        _fb.SublayerEntryAddScale(b, float(entry.get("scale", 1.0)))
        if layer_key is not None:
            _fb.SublayerEntryAddLayerKey(b, layer_key)
        offsets.append(_fb.SublayerEntryEnd(b))
    return offsets


def _encode_layer_graph_state(b, msg):
    generation = b.CreateString(msg["generation"])
    root_layer_key = b.CreateString(msg["root_layer_key"])
    layer_offsets = []
    for state in msg.get("layers", ()):
        layer_key = b.CreateString(state["layer_key"])
        sublayer_offsets = _encode_sublayer_entries(b, state.get("sublayers", ()))
        sublayers = None
        if sublayer_offsets:
            _fb.SharedLayerStateStartSublayersVector(b, len(sublayer_offsets))
            for offset in reversed(sublayer_offsets):
                b.PrependUOffsetTRelative(offset)
            sublayers = b.EndVector()
        _fb.SharedLayerStateStart(b)
        _fb.SharedLayerStateAddLayerKey(b, layer_key)
        if sublayers is not None:
            _fb.SharedLayerStateAddSublayers(b, sublayers)
        layer_offsets.append(_fb.SharedLayerStateEnd(b))

    layers = None
    if layer_offsets:
        _fb.LayerGraphStateStartLayersVector(b, len(layer_offsets))
        for offset in reversed(layer_offsets):
            b.PrependUOffsetTRelative(offset)
        layers = b.EndVector()

    _fb.LayerGraphStateStart(b)
    _fb.LayerGraphStateAddSeq(b, int(msg.get("seq", 0)))
    _fb.LayerGraphStateAddGeneration(b, generation)
    _fb.LayerGraphStateAddRevision(b, int(msg["revision"]))
    _fb.LayerGraphStateAddRootLayerKey(b, root_layer_key)
    if layers is not None:
        _fb.LayerGraphStateAddLayers(b, layers)
    return _fb.LayerGraphStateEnd(b)


_ENCODE_DISPATCH = {
    MSG_HELLO: _encode_hello,
    MSG_HELLO_OK: _encode_hello_ok,
    MSG_AUTH_REJECTED: _encode_auth_rejected,
    MSG_HELLO_REJECTED: _encode_hello_rejected,
    MSG_TXN: _encode_txn,
    MSG_TRANSACTION_RESULT: _encode_transaction_result,
    MSG_REPLAY_COMPLETE: _encode_replay_complete,
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
    MSG_LAYER_STACK_STATE: _encode_layer_stack_state,
    MSG_LAYER_GRAPH_STATE: _encode_layer_graph_state,
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


def _encode_arc_entries(b, entries, *, explicit):
    offsets = []
    for entry in entries:
        ap = b.CreateString(entry["asset_path"]) if entry.get("asset_path") else None
        pp = b.CreateString(entry["prim_path"]) if entry.get("prim_path") else None
        custom_data = (
            b.CreateString(entry["custom_data_fragment"])
            if entry.get("custom_data_fragment")
            else None
        )
        position = entry.get(
            "list_position",
            "explicit" if explicit else "prepended",
        )
        _fb.ArcEntryStart(b)
        if ap:
            _fb.ArcEntryAddAssetPath(b, ap)
        if pp:
            _fb.ArcEntryAddPrimPath(b, pp)
        _fb.ArcEntryAddListPosition(b, _ARC_POSITION_TO_FB[position])
        if entry.get("layer_offset") is not None:
            _fb.ArcEntryAddLayerOffset(b, float(entry["layer_offset"]))
        if entry.get("layer_scale") is not None:
            _fb.ArcEntryAddLayerScale(b, float(entry["layer_scale"]))
        if custom_data:
            _fb.ArcEntryAddCustomDataFragment(b, custom_data)
        offsets.append(_fb.ArcEntryEnd(b))
    return offsets


@register_encoder(K_SET_REFERENCE, fb_tag=EventPayloadType.SetReference, fb_class=SetReference)
def _encode_set_reference(b, ev):
    prim = b.CreateString(ev["prim"])
    explicit = bool(ev.get("list_op_explicit", False))
    authored = bool(ev.get("list_op_authored", ev["refs"] or explicit))
    arc_offsets = _encode_arc_entries(b, ev["refs"], explicit=explicit)
    _fb.SetReferenceStartRefsVector(b, len(arc_offsets))
    for off in reversed(arc_offsets):
        b.PrependUOffsetTRelative(off)
    refs_vec = b.EndVector()
    _fb.SetReferenceStart(b)
    _fb.SetReferenceAddPrim(b, prim)
    _fb.SetReferenceAddRefs(b, refs_vec)
    _fb.SetReferenceAddListOpAuthored(b, authored)
    _fb.SetReferenceAddListOpExplicit(b, explicit)
    return _fb.SetReferenceEnd(b)


@register_encoder(K_SET_PAYLOAD, fb_tag=EventPayloadType.SetPayload, fb_class=SetPayload)
def _encode_set_payload(b, ev):
    prim = b.CreateString(ev["prim"])
    explicit = bool(ev.get("list_op_explicit", False))
    authored = bool(ev.get("list_op_authored", ev["payloads"] or explicit))
    arc_offsets = _encode_arc_entries(b, ev["payloads"], explicit=explicit)
    _fb.SetPayloadStartPayloadsVector(b, len(arc_offsets))
    for off in reversed(arc_offsets):
        b.PrependUOffsetTRelative(off)
    payloads_vec = b.EndVector()
    _fb.SetPayloadStart(b)
    _fb.SetPayloadAddPrim(b, prim)
    _fb.SetPayloadAddPayloads(b, payloads_vec)
    _fb.SetPayloadAddListOpAuthored(b, authored)
    _fb.SetPayloadAddListOpExplicit(b, explicit)
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
    purpose = ev.get("material_purpose", "") or ""
    mpur = b.CreateString(purpose) if purpose else None
    _fb.SetMaterialBindingStart(b)
    _fb.SetMaterialBindingAddPrim(b, prim)
    _fb.SetMaterialBindingAddMaterialPath(b, mp)
    if mpur is not None:
        _fb.SetMaterialBindingAddMaterialPurpose(b, mpur)
    return _fb.SetMaterialBindingEnd(b)


# Declared USD types whose numeric payloads travel in the float wire slots.
# The emitting language's literal type must not leak into the wire: a JSON
# "1" or "[1, 1, 1]" for a float-typed input encodes as float, so receivers
# can trust that the payload slot matches the declared type.
_FLOAT_WIRE_TYPES = frozenset(
    {
        "float",
        "double",
        "color3f",
        "float3",
        "normal3f",
        "point3f",
        "vector3f",
        "color3d",
        "double3",
        "normal3d",
        "point3d",
        "vector3d",
        "float2",
        "texCoord2f",
        "double2",
        "float4",
        "color4f",
        "double4",
        "matrix2d",
        "matrix3d",
        "matrix4d",
        "float[]",
    }
)


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
        type_name = input_types.get(name, "")
        tn = b.CreateString(type_name)
        float_declared = type_name in _FLOAT_WIRE_TYPES

        # Coerce numeric sequences (incl. numpy arrays) into a flat float
        # vector; numpy arrays are not list-typed, but iterate fine.
        as_seq = None
        if isinstance(value, np.ndarray) or (
            isinstance(value, list) and not isinstance(value, str)
        ):
            as_seq = list(value) if isinstance(value, np.ndarray) else value

        str_off = None
        float_vec = None
        int_vec = None
        string_vec = None
        if isinstance(value, str):
            str_off = b.CreateString(value)
        elif as_seq is not None and len(as_seq) > 0 and all(isinstance(v, str) for v in as_seq):
            str_offs = [b.CreateString(v) for v in as_seq]
            _fb.ConnectableInputValueStartStringArrayVector(b, len(str_offs))
            for off in reversed(str_offs):
                b.PrependUOffsetTRelative(off)
            string_vec = b.EndVector()
        elif (
            as_seq is not None
            and not float_declared
            and all(isinstance(v, (int, np.integer)) and not isinstance(v, bool) for v in as_seq)
        ):
            int_vec = _create_int_vector(b, [int(v) for v in as_seq])
        elif as_seq is not None:
            float_vec = _create_float_vector(b, [float(v) for v in as_seq])

        _fb.ConnectableInputValueStart(b)
        _fb.ConnectableInputValueAddName(b, n)
        _fb.ConnectableInputValueAddTypeName(b, tn)
        if isinstance(value, bool):
            _fb.ConnectableInputValueAddValueType(b, ConnectableInputValueType.ScalarBool)
            _fb.ConnectableInputValueAddScalarBool(b, value)
        elif isinstance(value, int) and not isinstance(value, bool):
            if float_declared:
                _fb.ConnectableInputValueAddValueType(b, ConnectableInputValueType.ScalarFloat)
                _fb.ConnectableInputValueAddScalarFloat(b, float(value))
            else:
                _fb.ConnectableInputValueAddValueType(b, ConnectableInputValueType.ScalarInt)
                _fb.ConnectableInputValueAddScalarInt(b, value)
        elif isinstance(value, float):
            _fb.ConnectableInputValueAddValueType(b, ConnectableInputValueType.ScalarFloat)
            _fb.ConnectableInputValueAddScalarFloat(b, value)
        elif str_off is not None:
            _fb.ConnectableInputValueAddValueType(b, ConnectableInputValueType.ScalarString)
            _fb.ConnectableInputValueAddScalarString(b, str_off)
        elif string_vec is not None:
            _fb.ConnectableInputValueAddValueType(b, ConnectableInputValueType.StringArray)
            _fb.ConnectableInputValueAddStringArray(b, string_vec)
        elif int_vec is not None:
            _fb.ConnectableInputValueAddValueType(b, ConnectableInputValueType.IntArray)
            _fb.ConnectableInputValueAddIntArray(b, int_vec)
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


@register_encoder(
    K_SET_INSTANCEABLE,
    fb_tag=EventPayloadType.SetInstanceable,
    fb_class=SetInstanceable,
)
def _encode_set_instanceable(b, ev):
    prim = b.CreateString(ev["prim"])
    _fb.SetInstanceableStart(b)
    _fb.SetInstanceableAddPrim(b, prim)
    _fb.SetInstanceableAddInstanceable(b, bool(ev["instanceable"]))
    return _fb.SetInstanceableEnd(b)


@register_encoder(
    K_SET_POINT_INSTANCER,
    fb_tag=EventPayloadType.SetPointInstancer,
    fb_class=SetPointInstancer,
)
def _encode_set_point_instancer(b, ev):
    prim = b.CreateString(ev["prim"])
    fields = ev.get("fields", [])
    bitmask = 0
    for f in fields:
        bitmask |= _PI_BITS.get(f, 0)

    proto_vec = None
    if "prototypes" in fields:
        offsets = [b.CreateString(p) for p in ev["prototypes"]]
        _fb.SetPointInstancerStartPrototypesVector(b, len(offsets))
        for off in reversed(offsets):
            b.PrependUOffsetTRelative(off)
        proto_vec = b.EndVector()

    array_vecs: dict[str, int] = {}
    for name, (_add, _base, _stride, dtype) in _PI_ARRAYS.items():
        if name in fields:
            array_vecs[name] = b.CreateNumpyVector(np.asarray(ev[name], dtype=dtype).ravel())

    _fb.SetPointInstancerStart(b)
    _fb.SetPointInstancerAddPrim(b, prim)
    _fb.SetPointInstancerAddFields(b, bitmask)
    if proto_vec is not None:
        _fb.SetPointInstancerAddPrototypes(b, proto_vec)
    for name, vec in array_vecs.items():
        _PI_ARRAYS[name][0](b, vec)
    if ev.get("time") is not None:
        _fb.SetPointInstancerAddTime(b, float(ev["time"]))
    return _fb.SetPointInstancerEnd(b)


@register_encoder(
    K_SET_SDF_SPEC_FIELDS,
    fb_tag=EventPayloadType.SetSdfSpecFields,
    fb_class=SetSdfSpecFields,
)
def _encode_set_sdf_spec_fields(b, ev):
    prim = b.CreateString(ev["prim"])
    spec_path = b.CreateString(ev["spec_path"])
    fragment = b.CreateString(ev.get("fragment", ""))
    field_offsets = [b.CreateString(field) for field in ev.get("fields", ())]
    _fb.SetSdfSpecFieldsStartFieldsVector(b, len(field_offsets))
    for offset in reversed(field_offsets):
        b.PrependUOffsetTRelative(offset)
    fields = b.EndVector()

    _fb.SetSdfSpecFieldsStart(b)
    _fb.SetSdfSpecFieldsAddPrim(b, prim)
    _fb.SetSdfSpecFieldsAddSpecPath(b, spec_path)
    _fb.SetSdfSpecFieldsAddSpecKind(b, _SDF_SPEC_KIND_TO_FB[ev["spec_kind"]])
    _fb.SetSdfSpecFieldsAddFields(b, fields)
    _fb.SetSdfSpecFieldsAddFragment(b, fragment)
    _fb.SetSdfSpecFieldsAddRemoved(b, bool(ev.get("removed", False)))
    return _fb.SetSdfSpecFieldsEnd(b)


@register_encoder(
    K_REPLACE_SDF_LAYER_CONTENT,
    fb_tag=EventPayloadType.ReplaceSdfLayerContent,
    fb_class=ReplaceSdfLayerContent,
)
def _encode_replace_sdf_layer_content(b, ev):
    prim = b.CreateString(ev["prim"])
    fragment = b.CreateString(ev["fragment"])
    _fb.ReplaceSdfLayerContentStart(b)
    _fb.ReplaceSdfLayerContentAddPrim(b, prim)
    _fb.ReplaceSdfLayerContentAddFragment(b, fragment)
    return _fb.ReplaceSdfLayerContentEnd(b)


@register_encoder(
    K_SET_SUBLAYERS,
    fb_tag=EventPayloadType.SetSublayers,
    fb_class=SetSublayers,
)
def _encode_set_sublayers(b, ev):
    prim = b.CreateString(ev["prim"])
    generation = b.CreateString(ev["generation"])
    entry_offsets = _encode_sublayer_entries(b, ev.get("sublayers", ()))
    sublayers = None
    if entry_offsets:
        _fb.SetSublayersStartSublayersVector(b, len(entry_offsets))
        for offset in reversed(entry_offsets):
            b.PrependUOffsetTRelative(offset)
        sublayers = b.EndVector()
    _fb.SetSublayersStart(b)
    _fb.SetSublayersAddPrim(b, prim)
    _fb.SetSublayersAddGeneration(b, generation)
    _fb.SetSublayersAddRevision(b, int(ev.get("revision", 0)))
    if sublayers is not None:
        _fb.SetSublayersAddSublayers(b, sublayers)
    return _fb.SetSublayersEnd(b)


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
    if kind == K_SET_POINT_INSTANCER:
        return _dict_set_point_instancer(obj, kind, numpy_arrays=numpy_arrays)
    spec = _events.get(kind)
    if spec is None or spec.decode is None:
        raise KeyError(f"no registered decoder for event kind {kind!r}")
    return spec.decode(obj, kind)


@dataclass(slots=True)
class ReceivedEvent:
    """One event together with its broadcast routing envelope."""

    seq: int
    event: Event
    layer_key: str | None = None
    origin: str | None = None
    client_id: str | None = None
    client: str | None = None


class SequenceGapError(ValueError):
    """A lossless replay stream skipped the next required sequence."""

    def __init__(self, expected: int, received: int):
        self.expected = expected
        self.received = received
        super().__init__(
            f"sequence gap: expected {expected}, received {received}",
        )


@dataclass(slots=True)
class DecodeResult:
    """Outcome of decoding one batch of wire messages."""

    received: list[Event] = field(default_factory=list)
    received_records: list[ReceivedEvent] = field(default_factory=list)
    layer_stack_states: list[dict] = field(default_factory=list)
    layer_graph_states: list[dict] = field(default_factory=list)
    last_seq: int = 0
    resync_requested: bool = False
    rate_limited_retry_after: float | None = None
    replay_complete: tuple[int, int] | None = None
    errors: list[Exception] = field(default_factory=list)


def decode_messages(
    raw_messages: Iterable[bytes],
    *,
    last_seq: int = 0,
    numpy_arrays: bool = False,
    clear_on_resync: bool = False,
    preserve_envelopes: bool = False,
    require_contiguous: bool = False,
) -> DecodeResult:
    """Decode a batch of wire messages with sequence dedup and resync handling.

    The first decode failure is captured in ``result.errors`` rather than
    raised. Decoding then stops so the caller can apply the valid prefix and
    request replay from ``result.last_seq + 1`` without skipping later events.
    """
    result = DecodeResult(last_seq=last_seq)
    for raw in raw_messages:
        try:
            msg = message_to_dict(raw, numpy_arrays=numpy_arrays)
        except Exception as exc:  # noqa: BLE001 — surfaced via result.errors
            result.errors.append(exc)
            break

        msg_type = msg.get("type")
        if msg_type == MSG_RESYNC:
            result.last_seq = 0
            result.resync_requested = True
            if clear_on_resync:
                result.received.clear()
                result.received_records.clear()
                result.layer_stack_states.clear()
                result.layer_graph_states.clear()
            continue

        if msg_type == MSG_LAYER_STACK_STATE:
            result.layer_stack_states.append(msg)
            continue

        if msg_type == MSG_LAYER_GRAPH_STATE:
            seq = int(msg.get("seq") or 0)
            if seq and seq <= result.last_seq:
                continue
            if require_contiguous and seq and seq != result.last_seq + 1:
                result.errors.append(SequenceGapError(result.last_seq + 1, seq))
                break
            if seq:
                result.last_seq = seq
            result.layer_graph_states.append(msg)
            continue

        if msg_type == MSG_RATE_LIMITED:
            retry_after = msg.get("retry_after")
            if isinstance(retry_after, (int, float)):
                result.rate_limited_retry_after = float(retry_after)
            continue

        if msg_type == MSG_REPLAY_COMPLETE:
            result.replay_complete = (
                int(msg.get("head_seq", 0)),
                int(msg.get("epoch", 0)),
            )
            continue

        if msg_type != MSG_EVENT:
            continue

        seq = int(msg.get("seq") or 0)
        if seq and seq <= result.last_seq:
            continue
        if require_contiguous and seq and seq != result.last_seq + 1:
            result.errors.append(SequenceGapError(result.last_seq + 1, seq))
            break
        if seq:
            result.last_seq = seq
        event = msg.get("event")
        if event:
            result.received.append(event)
            if preserve_envelopes:
                result.received_records.append(
                    ReceivedEvent(
                        seq=seq,
                        event=event,
                        layer_key=msg.get("layer_key"),
                        origin=msg.get("origin"),
                        client_id=msg.get("client_id"),
                        client=msg.get("client"),
                    )
                )

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
        ("producer_session_id", h.ProducerSessionId),
    ]:
        v = _str(getter())
        if v:
            msg[key] = v
    if h.LayeredReplay():
        msg["layered_replay"] = True
    mode = _FB_TO_LAYER_MODE[h.LayerMode()]
    if mode != LayerMode.MANAGED.value:
        msg["layer_mode"] = mode
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
    if h.LayeredReplay():
        msg["layered_replay"] = True
    if h.CommittedThrough():
        msg["committed_through"] = int(h.CommittedThrough())
    mode = _FB_TO_LAYER_MODE[h.LayerMode()]
    if mode != LayerMode.MANAGED.value:
        msg["layer_mode"] = mode
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
    msg = {"type": msg_type, "action": _str(pc.Action()) or ""}
    # Time / rate are FB-nullable; absent fields stay absent in the dict so
    # callers can distinguish "no opinion" from "explicit zero".
    t = pc.Time()
    if t is not None:
        msg["time"] = t
    r = pc.Rate()
    if r is not None:
        msg["rate"] = r
    return msg


def _dict_playback_state(ps, msg_type):
    return {
        "type": msg_type,
        "time": ps.Time(),
        "playing": ps.Playing(),
        "rate": ps.Rate(),
        "leader_client_id": _str(ps.LeaderClientId()) or "",
    }


def _dict_layer_stack_state(state, msg_type):
    layers = []
    for index in range(state.LayersLength()):
        item = state.Layers(index)
        layers.append(
            {
                "layer_key": _str(item.LayerKey()) or "",
                "label": _str(item.Label()) or "",
                "muted": bool(item.Muted()),
            }
        )
    return {
        "type": msg_type,
        "generation": _str(state.Generation()) or "",
        "revision": int(state.Revision()),
        "layers": layers,
    }


def _dict_layer_graph_state(state, msg_type):
    layers = []
    for index in range(state.LayersLength()):
        item = state.Layers(index)
        layers.append(
            {
                "layer_key": _str(item.LayerKey()) or "",
                "sublayers": [
                    _dict_sublayer_entry(item.Sublayers(i)) for i in range(item.SublayersLength())
                ],
            }
        )
    return {
        "type": msg_type,
        "seq": int(state.Seq()),
        "generation": _str(state.Generation()) or "",
        "revision": int(state.Revision()),
        "root_layer_key": _str(state.RootLayerKey()) or "",
        "layers": layers,
    }


def _dict_auth_rejected(h, msg_type):
    return {"type": msg_type, "reason": _str(h.Reason()) or ""}


def _dict_hello_rejected(h, msg_type):
    return {
        "type": msg_type,
        "code": int(h.Code()),
        "reason": _str(h.Reason()) or "",
    }


def _dict_txn(t, msg_type, numpy_arrays=False):
    events = [
        event_to_dict(t.Events(i), numpy_arrays=numpy_arrays) for i in range(t.EventsLength())
    ]
    msg = {"type": msg_type, "events": events}
    pid = _str(t.ProposalId())
    if pid:
        msg["proposal_id"] = pid
    layer_key = _str(t.LayerKey())
    if layer_key:
        msg["layer_key"] = layer_key
    txn_id = int(t.TxnId())
    if txn_id:
        msg["txn_id"] = txn_id
    return msg


def _dict_transaction_result(result, msg_type):
    msg = {
        "type": msg_type,
        "txn_id": int(result.TxnId()),
        "status": int(result.Status()),
        "expected_txn_id": int(result.ExpectedTxnId()),
        "rejection_code": int(result.RejectionCode()),
    }
    reason = _str(result.Reason())
    if reason:
        msg["reason"] = reason
    return msg


def _dict_replay_complete(complete, msg_type):
    return {
        "type": msg_type,
        "head_seq": int(complete.HeadSeq()),
        "epoch": int(complete.Epoch()),
    }


def _dict_broadcast_event(be, msg_type, numpy_arrays=False):
    ew = be.Event()
    event_dict = event_to_dict(ew, numpy_arrays=numpy_arrays) if ew else {}
    msg = {"type": msg_type, "seq": be.Seq(), "event": event_dict}
    for key, getter in [
        ("origin", be.Origin),
        ("client_id", be.ClientId),
        ("client", be.Client),
        ("layer_key", be.LayerKey),
    ]:
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
    MSG_HELLO_REJECTED: _dict_hello_rejected,
    MSG_TXN: _dict_txn,
    MSG_TRANSACTION_RESULT: _dict_transaction_result,
    MSG_REPLAY_COMPLETE: _dict_replay_complete,
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
    MSG_LAYER_STACK_STATE: _dict_layer_stack_state,
    MSG_LAYER_GRAPH_STATE: _dict_layer_graph_state,
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
    explicit = sr.ListOpExplicit()
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
        position = _FB_TO_ARC_POSITION[arc.ListPosition()]
        default_position = "explicit" if explicit else "prepended"
        if position != default_position:
            entry["list_position"] = position
        if arc.LayerOffset() != 0.0:
            entry["layer_offset"] = arc.LayerOffset()
        if arc.LayerScale() != 1.0:
            entry["layer_scale"] = arc.LayerScale()
        custom_data = _str(arc.CustomDataFragment())
        if custom_data:
            entry["custom_data_fragment"] = custom_data
        refs.append(entry)
    return {
        "k": kind,
        "prim": _str(sr.Prim()),
        "refs": refs,
        "list_op_authored": sr.ListOpAuthored(),
        "list_op_explicit": explicit,
    }


@register_decoder(K_SET_PAYLOAD)
def _dict_set_payload(sp, kind):
    explicit = sp.ListOpExplicit()
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
        position = _FB_TO_ARC_POSITION[arc.ListPosition()]
        default_position = "explicit" if explicit else "prepended"
        if position != default_position:
            entry["list_position"] = position
        if arc.LayerOffset() != 0.0:
            entry["layer_offset"] = arc.LayerOffset()
        if arc.LayerScale() != 1.0:
            entry["layer_scale"] = arc.LayerScale()
        custom_data = _str(arc.CustomDataFragment())
        if custom_data:
            entry["custom_data_fragment"] = custom_data
        payloads.append(entry)
    return {
        "k": kind,
        "prim": _str(sp.Prim()),
        "payloads": payloads,
        "list_op_authored": sp.ListOpAuthored(),
        "list_op_explicit": explicit,
    }


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
    out: dict = {
        "k": kind,
        "prim": _str(mb.Prim()),
        "material_path": _str(mb.MaterialPath()),
    }
    purpose = _str(mb.MaterialPurpose())
    if purpose:
        out["material_purpose"] = purpose
    return out


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
        elif vt == ConnectableInputValueType.IntArray:
            inputs[name] = [civ.IntArray(j) for j in range(civ.IntArrayLength())]
        elif vt == ConnectableInputValueType.StringArray:
            inputs[name] = [_str(civ.StringArray(j)) for j in range(civ.StringArrayLength())]
        elif vt == ConnectableInputValueType.ScalarString:
            inputs[name] = _str(civ.ScalarString())
        elif vt == ConnectableInputValueType.ScalarBool:
            inputs[name] = civ.ScalarBool()
        elif vt == ConnectableInputValueType.ScalarInt:
            inputs[name] = civ.ScalarInt()
        elif vt == ConnectableInputValueType.ScalarFloat:
            inputs[name] = civ.ScalarFloat()
        # else: value type None or unrecognized — drop the input entry so the
        # applier doesn't author a spurious default. type_name is preserved so
        # the receiver can log the unsupported kind without silently corrupting.
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


@register_decoder(K_SET_INSTANCEABLE)
def _dict_set_instanceable(si, kind):
    return {
        "k": kind,
        "prim": _str(si.Prim()),
        "instanceable": bool(si.Instanceable()),
    }


@register_decoder(K_SET_POINT_INSTANCER)
def _dict_set_point_instancer(spi, kind, numpy_arrays=False):
    bitmask = spi.Fields()
    ev: dict = {"k": kind, "prim": _str(spi.Prim())}
    fields: list[str] = []
    if bitmask & _PI_BITS["prototypes"]:
        fields.append("prototypes")
        ev["prototypes"] = [_str(spi.Prototypes(i)) for i in range(spi.PrototypesLength())]
    for name, (_add, base, stride, _dtype) in _PI_ARRAYS.items():
        if not bitmask & _PI_BITS[name]:
            continue
        fields.append(name)
        if numpy_arrays:
            arr = getattr(spi, base + "AsNumpy")()
            if stride is not None:
                arr = arr.reshape(-1, stride)
            ev[name] = arr
        else:
            n = getattr(spi, base + "Length")()
            get = getattr(spi, base)
            flat = [get(i) for i in range(n)]
            if stride is not None:
                flat = [flat[i : i + stride] for i in range(0, n, stride)]
            ev[name] = flat
    ev["fields"] = fields
    t = spi.Time()
    if t is not None:
        ev["time"] = t
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


@register_decoder(K_SET_SDF_SPEC_FIELDS)
def _dict_set_sdf_spec_fields(sss, kind):
    return {
        "k": kind,
        "prim": _str(sss.Prim()),
        "spec_path": _str(sss.SpecPath()),
        "spec_kind": _FB_TO_SDF_SPEC_KIND[sss.SpecKind()],
        "fields": [_str(sss.Fields(i)) for i in range(sss.FieldsLength())],
        "fragment": _str(sss.Fragment()),
        "removed": bool(sss.Removed()),
    }


@register_decoder(K_REPLACE_SDF_LAYER_CONTENT)
def _dict_replace_sdf_layer_content(replacement, kind):
    return {
        "k": kind,
        "prim": _str(replacement.Prim()) or "",
        "fragment": _str(replacement.Fragment()) or "",
    }


def _dict_sublayer_entry(entry) -> dict:
    result = {
        "authored_path": _str(entry.AuthoredPath()) or "",
        "offset": float(entry.Offset()),
        "scale": float(entry.Scale()),
    }
    layer_key = _str(entry.LayerKey())
    if layer_key:
        result["layer_key"] = layer_key
    return result


@register_decoder(K_SET_SUBLAYERS)
def _dict_set_sublayers(ss, kind):
    return {
        "k": kind,
        "prim": _str(ss.Prim()) or "",
        "generation": _str(ss.Generation()) or "",
        "revision": int(ss.Revision()),
        "sublayers": [_dict_sublayer_entry(ss.Sublayers(i)) for i in range(ss.SublayersLength())],
    }
