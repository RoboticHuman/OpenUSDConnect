"""Merge event payloads without moving writes across destructive edits."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from heapq import merge
from itertools import chain
from typing import NamedTuple

from ..codec import (
    ReceivedEvent,
    decode_envelope,
    decode_received_event,
    resolve_payload,
)
from ..event_store import EventStore
from ..protocol_constants import (
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_ERASE_TIME_SAMPLES,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_REPLACE_SDF_LAYER_CONTENT,
    K_SET_CONNECTABLE_CONNECTION,
    K_SET_CONNECTABLE_INPUT,
    K_SET_GPRIM_ATTRS,
    K_SET_MATERIAL_BINDING,
    K_SET_POINT_INSTANCER,
    K_SET_SDF_SPEC_FIELDS,
    K_SET_STAGE_METADATA,
    K_SET_SUBLAYERS,
    K_SET_VARIANT_SELECTIONS,
    K_SET_VISIBILITY,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
    MSG_EVENT,
    MSG_LAYER_GRAPH_STATE,
    NON_COLLABORATION_KINDS,
    STAGE_METADATA_KEYS,
    event_apply_tier,
)
from ..sdf_spec_delta import merge_spec_events
from ..time_sample_delta import is_sample_history_barrier
from ..usd_state import POINT_INSTANCER_USD_TO_WIRE

_SAMPLE_VALUE_KINDS = frozenset(
    {
        K_SET_XFORM_TRS,
        K_SET_GPRIM_ATTRS,
        K_SET_CONNECTABLE_INPUT,
        K_SET_VISIBILITY,
        K_SET_POINT_INSTANCER,
    }
)
_EXACT_LAYER_KINDS = frozenset(
    {
        K_SET_SDF_SPEC_FIELDS,
        K_ERASE_TIME_SAMPLES,
        K_REPLACE_SDF_LAYER_CONTENT,
    }
)
_TRS_ATTRIBUTES = {"t": "xformOp:translate", "r": "xformOp:orient", "s": "xformOp:scale"}
_INSTANCER_ATTRIBUTES = {wire: usd for usd, wire in POINT_INSTANCER_USD_TO_WIRE.items()}

_CHILD_REPLAY_KINDS = frozenset({
    K_ENSURE_PRIM, K_ENSURE_XFORM_OPS, K_SET_XFORM_TRS, K_SET_VISIBILITY,
    K_SET_MATERIAL_BINDING, K_SET_CONNECTABLE_INPUT, K_SET_CONNECTABLE_CONNECTION,
    K_DELETE_PRIM, K_RENAME_PRIM, K_ERASE_TIME_SAMPLES, K_SET_SDF_SPEC_FIELDS,
})


def child_replay_records(store: EventStore, prim_path: str) -> list[ReceivedEvent]:
    """Reduce child opinions without merging across deleted prims or samples."""
    prefix = prim_path.rstrip("/") + "/"

    def decode(blobs: Iterable[bytes]) -> Iterable[ReceivedEvent]:
        for blob in blobs:
            _message_type, broadcast = resolve_payload(decode_envelope(blob))
            yield decode_received_event(broadcast, numpy_arrays=True)

    descendants = decode(store.get_by_prim_prefix(prefix, _CHILD_REPLAY_KINDS))
    # Deleting the payload root or one of its ancestors also removes earlier
    # child opinions. Read these small lifecycle records without scanning the
    # geometry or shader-array histories of unrelated prims.
    ancestors = (
        record for record in decode(
            store.get_by_prim_prefix("/", {K_DELETE_PRIM, K_RENAME_PRIM})
        )
        if prim_path == record.event["prim"]
        or prim_path.startswith(record.event["prim"].rstrip("/") + "/")
    )
    compaction = LogCompaction()
    for record in merge(descendants, ancestors, key=lambda record: record.seq):
        compaction.add_event(record)

    ordered: list[ReceivedEvent] = []
    segment: list[ReceivedEvent] = []

    def flush_segment() -> None:
        # Creates precede bindings to siblings; parents precede descendants.
        ordered.extend(sorted(
            segment, key=lambda record: (event_apply_tier(record.event["k"]), record.event["prim"]),
        ))
        segment.clear()

    for record in compaction.replay_records():
        event = record.event
        if (
            event["k"] in (K_DELETE_PRIM, K_RENAME_PRIM)
            or is_sample_history_barrier(event)
        ):
            # Erasures must stay after the values they clear and before later
            # partial writes. Sorting the entire replay by tier revives samples.
            flush_segment()
            if event["prim"].startswith(prefix):
                ordered.append(record)
        elif event["prim"].startswith(prefix):
            segment.append(record)
    flush_segment()
    return ordered


class _MergeKey(NamedTuple):
    prim: str
    kind: str
    time: float | None
    layer_key: str
    spec_path: str = ""
    material_purpose: str = ""

    @classmethod
    def from_event(cls, event: dict, layer_key: str) -> _MergeKey:
        kind = event["k"]
        prim = event.get("prim", "")
        time = event.get("time")
        if kind in NON_COLLABORATION_KINDS:
            layer_key = ""
        spec_path = (
            event["spec_path"] if kind in (K_SET_SDF_SPEC_FIELDS, K_ERASE_TIME_SAMPLES) else ""
        )
        material_purpose = (
            (event.get("material_purpose") or "") if kind == K_SET_MATERIAL_BINDING else ""
        )
        return cls(prim, kind, time, layer_key, spec_path, material_purpose)


class _PreservedEvent(NamedTuple):
    key: _MergeKey
    record: ReceivedEvent


@dataclass
class LogCompaction:
    """Pending entries can merge; preserved entries keep their replay position."""

    _pending: dict[_MergeKey, ReceivedEvent] = field(default_factory=dict)
    _preserved: list[_PreservedEvent] = field(default_factory=list)

    def add_record(self, sequence: int, record_bin: bytes) -> None:
        # Keep geometry arrays as buffer views instead of expanding them into lists.
        message_type, payload = resolve_payload(decode_envelope(record_bin))
        if message_type == MSG_LAYER_GRAPH_STATE:
            return
        if message_type != MSG_EVENT:
            raise ValueError("event log contains an unsupported record")
        record = decode_received_event(payload, numpy_arrays=True)
        record.seq = sequence
        self.add_event(record)

    def add_event(self, record: ReceivedEvent) -> None:
        """Take ownership of a decoded record, retaining its routing through merges."""
        event = record.event
        kind = event["k"]
        if kind == K_SET_SUBLAYERS:
            return
        key = _MergeKey.from_event(event, record.layer_key or "")
        self._preserve_sample_order(key, event)

        if _is_exact_deletion(event):
            if kind == K_SET_SDF_SPEC_FIELDS:
                self._discard(
                    lambda old: old.layer_key == key.layer_key and old.spec_path == key.spec_path
                )
            self._preserved.append(_PreservedEvent(key, record))
            return
        if kind in (K_DELETE_PRIM, K_RENAME_PRIM):
            self._discard(
                lambda old: (
                    old.layer_key == key.layer_key
                    and (old.prim == key.prim or old.prim.startswith(key.prim + "/"))
                )
            )
        elif kind == K_REPLACE_SDF_LAYER_CONTENT:
            self._discard(
                lambda old: old.layer_key == key.layer_key and old.kind in _EXACT_LAYER_KINDS
            )
        elif kind in (K_LOAD_PAYLOAD, K_UNLOAD_PAYLOAD):
            opposite = K_UNLOAD_PAYLOAD if kind == K_LOAD_PAYLOAD else K_LOAD_PAYLOAD
            self._pending.pop(_MergeKey(key.prim, opposite, None, key.layer_key), None)

        previous = self._pending.get(key)
        if previous and kind == K_ENSURE_XFORM_OPS:
            return
        record.event = _merge_payload(previous.event if previous else None, event)
        if previous and kind == K_ENSURE_PRIM:
            # Creates must still replay before events that use the prim.
            record.seq = previous.seq
        self._pending[key] = record

    def replay_records(self) -> list[ReceivedEvent]:
        return sorted(
            chain((entry.record for entry in self._preserved), self._pending.values()),
            key=lambda record: record.seq,
        )

    def _preserve(self, key: _MergeKey) -> None:
        self._preserved.append(_PreservedEvent(key, self._pending.pop(key)))

    def _discard(self, matches: Callable[[_MergeKey], bool]) -> None:
        self._pending = {key: entry for key, entry in self._pending.items() if not matches(key)}
        self._preserved = [entry for entry in self._preserved if not matches(entry.key)]

    def _preserve_sample_order(self, key: _MergeKey, event: dict) -> None:
        # A later metadata merge must not move an old sample table past this write.
        for name in _sampled_attribute_names(event):
            spec_key = _MergeKey(
                key.prim,
                K_SET_SDF_SPEC_FIELDS,
                None,
                key.layer_key,
                spec_path=f"{key.prim}.{name}",
            )
            previous = self._pending.get(spec_key)
            if previous and "timeSamples" in previous.event.get("fields", ()):
                self._preserve(spec_key)

        if not is_sample_history_barrier(event):
            return
        kinds = _SAMPLE_VALUE_KINDS
        if _is_exact_deletion(event):
            kinds = kinds | {K_SET_SDF_SPEC_FIELDS}
        # Partial writes can bundle several attributes. Keep their pre-deletion
        # values here so merging a later field cannot revive an erased sample.
        for pending_key in list(self._pending):
            if (
                pending_key.prim == key.prim
                and pending_key.layer_key == key.layer_key
                and pending_key.kind in kinds
            ):
                self._preserve(pending_key)


def _is_exact_deletion(event: dict) -> bool:
    return event["k"] == K_ERASE_TIME_SAMPLES or (
        event["k"] == K_SET_SDF_SPEC_FIELDS and bool(event.get("removed"))
    )


def _sampled_attribute_names(event: dict) -> Iterable[str]:
    if event.get("time") is None:
        return ()
    kind = event["k"]
    if kind == K_SET_GPRIM_ATTRS:
        return event["attrs"]
    if kind == K_SET_XFORM_TRS:
        return [_TRS_ATTRIBUTES[name] for name in event["fields"]]
    if kind == K_SET_CONNECTABLE_INPUT:
        return [f"inputs:{name}" for name in event["inputs"]]
    if kind == K_SET_VISIBILITY:
        return ("visibility",)
    if kind == K_SET_POINT_INSTANCER:
        return [
            _INSTANCER_ATTRIBUTES[name] for name in event["fields"] if name in _INSTANCER_ATTRIBUTES
        ]
    return ()


def _merge_connections(previous: dict | None, event: dict) -> dict:
    merged = previous if previous is not None else dict(event)
    connections = merged.setdefault("connections", {})
    disconnections = dict.fromkeys(merged.get("disconnections", ()))
    for name, source in event.get("connections", {}).items():
        connections[name] = source
        disconnections.pop(name, None)
    # Keep the source's declaration/type context even if the connection is removed.
    for name in event.get("disconnections", ()):
        disconnections[name] = None
    if disconnections:
        merged["disconnections"] = list(disconnections)
    else:
        merged.pop("disconnections", None)
    return merged


def _merge_payload(previous: dict | None, event: dict) -> dict:
    """Merge values only; identity and replay ordering belong to LogCompaction."""
    kind = event["k"]
    if kind == K_SET_CONNECTABLE_CONNECTION:
        return _merge_connections(previous, event)
    if previous is None:
        return event
    if kind in (K_SET_XFORM_TRS, K_SET_POINT_INSTANCER):
        if previous["fields"] == event["fields"]:
            # Every previously authored field is replaced, so no merge is needed.
            return event
        for name in event.get("fields", ()):
            previous[name] = event[name]
            if name not in previous["fields"]:
                previous["fields"].append(name)
    elif kind == K_SET_GPRIM_ATTRS:
        for name in ("attrs", "primvar_meta", "attr_interp"):
            if event.get(name):
                previous.setdefault(name, {}).update(event[name])
    elif kind == K_SET_CONNECTABLE_INPUT:
        for name in ("inputs", "input_types"):
            previous.setdefault(name, {}).update(event.get(name, {}))
        if event.get("info_id"):
            previous["info_id"] = event["info_id"]
    elif kind == K_SET_VARIANT_SELECTIONS:
        previous.setdefault("selections", {}).update(event.get("selections", {}))
    elif kind == K_SET_STAGE_METADATA:
        previous.update({name: event[name] for name in STAGE_METADATA_KEYS if name in event})
    elif kind == K_SET_SDF_SPEC_FIELDS:
        if previous["spec_kind"] == event["spec_kind"]:
            return merge_spec_events(previous, event)
        return event
    elif kind == K_ENSURE_PRIM:
        if "typeName" in event:
            previous["typeName"] = event["typeName"]
        schemas = set(previous.get("api_schemas", ())) | set(event.get("api_schemas", ()))
        if schemas:
            previous["api_schemas"] = sorted(schemas)
    else:
        return event
    return previous
