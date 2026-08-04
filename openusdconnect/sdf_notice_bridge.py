"""Native ``SdfNotice::LayersDidChange`` tracking for shared stages."""

from __future__ import annotations

import ctypes
import json
import platform
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import IntFlag
from pathlib import Path

from pxr import Sdf, Usd

from .protocol_constants import (
    K_REPLACE_SDF_LAYER_CONTENT,
    K_SET_SDF_SPEC_FIELDS,
    K_SET_SUBLAYERS,
    SDF_LAYER_TOPOLOGY_FIELDS,
    SDF_SPEC_KIND_PRIM,
    SDF_SPEC_KIND_PROPERTY,
    SDF_SPEC_KIND_VARIANT,
    SDF_SPEC_KIND_VARIANT_SET,
)
from .sdf_layer_tracker import PreparedLayerBatch, sdf_event_sort_key
from .sdf_spec_delta import (
    event_prim_path,
    serialize_layer_content,
    serialize_spec_fields,
    spec_kind_for_object,
)
from .shared_layer_graph import SharedLayerGraph, read_sublayer_entries

_BRIDGE_ABI_VERSION = 1
_DEFAULT_MAX_QUEUED_BYTES = 8 * 1024 * 1024


class _ChangeFlag(IntFlag):
    RENAMED = 1 << 0
    LAYER_IDENTIFIER = 1 << 1
    LAYER_RESOLVED_PATH = 1 << 2
    REPLACED_CONTENT = 1 << 3
    RELOADED_CONTENT = 1 << 4
    REORDERED_CHILDREN = 1 << 5
    REORDERED_PROPERTIES = 1 << 6
    PRIM_VARIANT_SETS = 1 << 7
    PRIM_INHERITS = 1 << 8
    PRIM_SPECIALIZES = 1 << 9
    PRIM_REFERENCES = 1 << 10
    TIME_SAMPLES = 1 << 11
    CONNECTIONS = 1 << 12
    RELATIONSHIP_TARGETS = 1 << 13
    ADDED_TARGET = 1 << 14
    REMOVED_TARGET = 1 << 15
    ADDED_INERT_PRIM = 1 << 16
    ADDED_PRIM = 1 << 17
    REMOVED_INERT_PRIM = 1 << 18
    REMOVED_PRIM = 1 << 19
    ADDED_DECLARATION = 1 << 20
    ADDED_PROPERTY = 1 << 21
    REMOVED_DECLARATION = 1 << 22
    REMOVED_PROPERTY = 1 << 23
    SUBLAYERS = 1 << 24


_ADDED_SPEC_FLAGS = (
    _ChangeFlag.ADDED_INERT_PRIM
    | _ChangeFlag.ADDED_PRIM
    | _ChangeFlag.ADDED_DECLARATION
    | _ChangeFlag.ADDED_PROPERTY
)
_REMOVED_SPEC_FLAGS = (
    _ChangeFlag.REMOVED_INERT_PRIM
    | _ChangeFlag.REMOVED_PRIM
    | _ChangeFlag.REMOVED_DECLARATION
    | _ChangeFlag.REMOVED_PROPERTY
)


class _Record(ctypes.Structure):
    _fields_ = [
        ("serial", ctypes.c_uint64),
        ("flags", ctypes.c_uint64),
        ("layer_identifier", ctypes.c_void_p),
        ("layer_identifier_size", ctypes.c_size_t),
        ("path", ctypes.c_void_p),
        ("path_size", ctypes.c_size_t),
        ("old_path", ctypes.c_void_p),
        ("old_path_size", ctypes.c_size_t),
        ("old_identifier", ctypes.c_void_p),
        ("old_identifier_size", ctypes.c_size_t),
        ("fields", ctypes.POINTER(ctypes.c_char_p)),
        ("field_count", ctypes.c_size_t),
    ]


class _Batch(ctypes.Structure):
    _fields_ = [
        ("serial", ctypes.c_uint64),
        ("records", ctypes.POINTER(_Record)),
        ("record_count", ctypes.c_size_t),
    ]


@dataclass(frozen=True, slots=True)
class SdfNoticeRecord:
    """One owned change-list record copied from the native bridge."""

    serial: int
    flags: _ChangeFlag
    layer_identifier: str
    path: str
    old_path: str
    old_identifier: str
    fields: tuple[str, ...]


def _decode_text(pointer: int | None, size: int) -> str:
    return ctypes.string_at(pointer, size).decode() if pointer and size else ""


def _pxr_version() -> int:
    major, minor, patch = Usd.GetVersion()
    return major * 10_000 + minor * 100 + patch


def _architecture(value: str) -> str:
    return {
        "amd64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86_64": "x86_64",
    }.get(value.lower(), value.lower())


def _load_manifest(library_path: Path) -> dict:
    manifest_path = Path(f"{library_path}.json")
    if not manifest_path.is_file():
        raise RuntimeError(f"Sdf notice bridge manifest is missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read Sdf notice bridge manifest: {exc}") from exc
    expected = {
        "abi_version": _BRIDGE_ABI_VERSION,
        "pxr_version": _pxr_version(),
        "system": platform.system(),
        "architecture": _architecture(platform.machine()),
    }
    actual = dict(manifest)
    actual["architecture"] = _architecture(str(actual.get("architecture", "")))
    mismatches = [
        f"{name}={actual.get(name)!r} (expected {value!r})"
        for name, value in expected.items()
        if actual.get(name) != value
    ]
    if mismatches:
        raise RuntimeError("incompatible Sdf notice bridge: " + ", ".join(mismatches))
    return manifest


class SdfNoticeBridge:
    """Owned C-ABI access to one exact-build OpenUSD notice bridge."""

    def __init__(
        self,
        library_path: str | Path,
        layer_identifiers: list[str],
        *,
        max_queued_bytes: int = _DEFAULT_MAX_QUEUED_BYTES,
    ):
        path = Path(library_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        if max_queued_bytes < 0:
            raise ValueError("max_queued_bytes must not be negative")
        _load_manifest(path)
        self._library = ctypes.CDLL(str(path))
        self._configure_library()
        if self._library.ouc_sdf_notice_abi_version() != _BRIDGE_ABI_VERSION:
            raise RuntimeError("loaded Sdf notice bridge reports an incompatible ABI")
        if self._library.ouc_sdf_notice_pxr_version() != _pxr_version():
            raise RuntimeError("loaded Sdf notice bridge reports an incompatible OpenUSD build")
        identifiers, count = self._encoded_identifiers(layer_identifiers)
        self._handle = self._library.ouc_sdf_notice_tracker_create(
            identifiers,
            count,
            max_queued_bytes,
        )
        if not self._handle:
            raise RuntimeError(self._last_error())

    def _configure_library(self) -> None:
        library = self._library
        library.ouc_sdf_notice_tracker_create.argtypes = [
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.c_size_t,
            ctypes.c_size_t,
        ]
        library.ouc_sdf_notice_tracker_create.restype = ctypes.c_void_p
        library.ouc_sdf_notice_tracker_destroy.argtypes = [ctypes.c_void_p]
        library.ouc_sdf_notice_tracker_destroy.restype = None
        library.ouc_sdf_notice_tracker_set_layers.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.c_size_t,
        ]
        library.ouc_sdf_notice_tracker_set_layers.restype = ctypes.c_int
        library.ouc_sdf_notice_tracker_set_suppressed.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        library.ouc_sdf_notice_tracker_set_suppressed.restype = ctypes.c_int
        library.ouc_sdf_notice_tracker_acquire.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_Batch),
        ]
        library.ouc_sdf_notice_tracker_acquire.restype = ctypes.c_int
        library.ouc_sdf_notice_tracker_release.argtypes = [ctypes.c_void_p]
        library.ouc_sdf_notice_tracker_release.restype = ctypes.c_int
        library.ouc_sdf_notice_tracker_pending_batches.argtypes = [ctypes.c_void_p]
        library.ouc_sdf_notice_tracker_pending_batches.restype = ctypes.c_size_t
        library.ouc_sdf_notice_tracker_queued_bytes.argtypes = [ctypes.c_void_p]
        library.ouc_sdf_notice_tracker_queued_bytes.restype = ctypes.c_size_t
        library.ouc_sdf_notice_tracker_coalesced_batch_count.argtypes = [ctypes.c_void_p]
        library.ouc_sdf_notice_tracker_coalesced_batch_count.restype = ctypes.c_uint64
        library.ouc_sdf_notice_last_error.argtypes = []
        library.ouc_sdf_notice_last_error.restype = ctypes.c_char_p
        library.ouc_sdf_notice_pxr_version.argtypes = []
        library.ouc_sdf_notice_pxr_version.restype = ctypes.c_uint32
        library.ouc_sdf_notice_abi_version.argtypes = []
        library.ouc_sdf_notice_abi_version.restype = ctypes.c_uint32

    @staticmethod
    def _encoded_identifiers(
        layer_identifiers: list[str],
    ) -> tuple[ctypes.Array[ctypes.c_char_p] | None, int]:
        encoded = [identifier.encode() for identifier in layer_identifiers]
        if not encoded:
            return None, 0
        return (ctypes.c_char_p * len(encoded))(*encoded), len(encoded)

    def _last_error(self) -> str:
        value = self._library.ouc_sdf_notice_last_error()
        return value.decode() if value else "native Sdf notice bridge failed"

    @property
    def pending_batch_count(self) -> int:
        return self._library.ouc_sdf_notice_tracker_pending_batches(self._handle)

    @property
    def queued_bytes(self) -> int:
        return self._library.ouc_sdf_notice_tracker_queued_bytes(self._handle)

    @property
    def coalesced_batch_count(self) -> int:
        return self._library.ouc_sdf_notice_tracker_coalesced_batch_count(self._handle)

    def set_layers(self, layer_identifiers: list[str]) -> None:
        identifiers, count = self._encoded_identifiers(layer_identifiers)
        if self._library.ouc_sdf_notice_tracker_set_layers(
            self._handle,
            identifiers,
            count,
        ):
            raise RuntimeError(self._last_error())

    def set_suppressed(self, suppressed: bool) -> None:
        if self._library.ouc_sdf_notice_tracker_set_suppressed(
            self._handle,
            int(suppressed),
        ):
            raise RuntimeError(self._last_error())

    def drain(self) -> tuple[SdfNoticeRecord, ...]:
        records = []
        while True:
            batch = _Batch()
            status = self._library.ouc_sdf_notice_tracker_acquire(
                self._handle,
                ctypes.byref(batch),
            )
            if status < 0:
                raise RuntimeError(self._last_error())
            if status == 0:
                return tuple(records)
            try:
                for index in range(batch.record_count):
                    record = batch.records[index]
                    records.append(
                        SdfNoticeRecord(
                            serial=int(record.serial),
                            flags=_ChangeFlag(record.flags),
                            layer_identifier=_decode_text(
                                record.layer_identifier,
                                record.layer_identifier_size,
                            ),
                            path=_decode_text(record.path, record.path_size),
                            old_path=_decode_text(record.old_path, record.old_path_size),
                            old_identifier=_decode_text(
                                record.old_identifier,
                                record.old_identifier_size,
                            ),
                            fields=tuple(
                                record.fields[field_index].decode()
                                for field_index in range(record.field_count)
                            ),
                        )
                    )
            finally:
                if self._library.ouc_sdf_notice_tracker_release(self._handle):
                    raise RuntimeError(self._last_error())

    def close(self) -> None:
        if self._handle:
            self._library.ouc_sdf_notice_tracker_destroy(self._handle)
            self._handle = None

    def __enter__(self) -> SdfNoticeBridge:
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> bool:
        self.close()
        return False


@dataclass(slots=True)
class _PathChanges:
    flags: _ChangeFlag = _ChangeFlag(0)
    fields: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _LayerChanges:
    layer: Sdf.Layer
    serial: int = 0
    replacement: bool = False
    topology: bool = False
    paths: dict[str, _PathChanges] = field(default_factory=dict)

    def add(self, path: str, flags: _ChangeFlag, fields: tuple[str, ...]) -> None:
        changes = self.paths.setdefault(path, _PathChanges())
        changes.flags |= flags
        changes.fields.update(fields)
        self.serial += 1


def _removed_spec_kind(path: Sdf.Path) -> str:
    if path.IsPropertyPath():
        return SDF_SPEC_KIND_PROPERTY
    if path.IsPrimVariantSelectionPath():
        return SDF_SPEC_KIND_VARIANT if path.GetVariantSelection()[1] else SDF_SPEC_KIND_VARIANT_SET
    if path.IsPrimPath():
        return SDF_SPEC_KIND_PRIM
    raise ValueError(f"cannot classify removed Sdf spec {path}")


def _topology_event(layer: Sdf.Layer) -> dict:
    return {
        "k": K_SET_SUBLAYERS,
        "prim": "/",
        "generation": "",
        "revision": 0,
        "sublayers": [dict(entry) for entry in read_sublayer_entries(layer)],
    }


def _removal_event(path: Sdf.Path) -> dict:
    kind = _removed_spec_kind(path)
    return {
        "k": K_SET_SDF_SPEC_FIELDS,
        "prim": event_prim_path(path, kind),
        "spec_path": str(path),
        "spec_kind": kind,
        "fields": [],
        "fragment": "",
        "removed": True,
    }


def _expanded_path_changes(changes: _LayerChanges) -> dict[str, _PathChanges]:
    expanded = {
        path: _PathChanges(path_changes.flags, set(path_changes.fields))
        for path, path_changes in changes.paths.items()
    }
    for path_string, path_changes in tuple(expanded.items()):
        if not path_changes.flags & _ChangeFlag.RENAMED:
            continue
        path = Sdf.Path(path_string)
        spec = changes.layer.GetObjectAtPath(path)
        if spec is None or isinstance(spec, Sdf.PropertySpec):
            continue

        def _include(descendant: Sdf.Path, root: Sdf.Path = path) -> None:
            if descendant == root:
                return
            descendant_changes = expanded.setdefault(str(descendant), _PathChanges())
            descendant_changes.flags |= _ADDED_SPEC_FLAGS

        changes.layer.Traverse(path, _include)
    return expanded


def _materialize_changes(changes: _LayerChanges) -> tuple[dict, ...]:
    layer = changes.layer
    if changes.replacement:
        events = [
            {
                "k": K_REPLACE_SDF_LAYER_CONTENT,
                "prim": "/",
                "fragment": serialize_layer_content(layer),
            }
        ]
        if changes.topology:
            events.append(_topology_event(layer))
        return tuple(events)

    spec_events = []
    for path_string, path_changes in _expanded_path_changes(changes).items():
        path = Sdf.Path(path_string)
        if path.IsTargetPath():
            continue
        spec = (
            layer.pseudoRoot if path == Sdf.Path.absoluteRootPath else layer.GetObjectAtPath(path)
        )
        if spec is None:
            if path_changes.flags & _REMOVED_SPEC_FLAGS:
                spec_events.append(_removal_event(path))
            continue

        kind = spec_kind_for_object(spec)
        fields = set(path_changes.fields)
        if path_changes.flags & (_ADDED_SPEC_FLAGS | _ChangeFlag.RENAMED):
            fields.update(str(field_name) for field_name in spec.ListInfoKeys())
        if kind == "layer":
            fields.difference_update(SDF_LAYER_TOPOLOGY_FIELDS)
        if not fields and not path_changes.flags & (_ADDED_SPEC_FLAGS | _ChangeFlag.RENAMED):
            continue
        ordered_fields = sorted(fields)
        spec_events.append(
            {
                "k": K_SET_SDF_SPEC_FIELDS,
                "prim": event_prim_path(path, kind),
                "spec_path": str(path),
                "spec_kind": kind,
                "fields": ordered_fields,
                "fragment": serialize_spec_fields(
                    layer,
                    path,
                    kind,
                    ordered_fields,
                    stabilize_asset_paths=False,
                ),
                "removed": False,
            }
        )

    spec_events.sort(key=sdf_event_sort_key)
    events = [_topology_event(layer)] if changes.topology else []
    events.extend(spec_events)
    return tuple(events)


class NativeSdfLayerChangeTracker:
    """Materialize exact authored deltas from native OpenUSD change lists."""

    def __init__(
        self,
        stage: Usd.Stage,
        graph: SharedLayerGraph,
        bridge_path: str | Path,
        *,
        max_queued_bytes: int = _DEFAULT_MAX_QUEUED_BYTES,
    ):
        if not isinstance(stage, Usd.Stage):
            raise TypeError("NativeSdfLayerChangeTracker requires a Usd.Stage")
        self.stage = stage
        self.graph = graph
        self._tracked: dict[str, Sdf.Layer] = {}
        self._pending: dict[int, _LayerChanges] = {}
        self._prepared: list[PreparedLayerBatch] = []
        self._suppression_depth = 0
        layers = graph.local_reachable_layers()
        self._bridge = SdfNoticeBridge(
            bridge_path,
            [layer.identifier for layer in layers],
            max_queued_bytes=max_queued_bytes,
        )
        self.sync_graph(force=True)

    @property
    def prepared_event_count(self) -> int:
        return sum(len(batch.events) for batch in self._prepared)

    @property
    def has_local_changes(self) -> bool:
        return bool(self._pending or self._prepared or self._bridge.pending_batch_count)

    @property
    def coalesced_batch_count(self) -> int:
        return self._bridge.coalesced_batch_count

    def sync_graph(self, *, force: bool = False) -> None:
        topology_overrides = {}
        for batch in self._prepared:
            for event in batch.events:
                if event["k"] == K_SET_SUBLAYERS:
                    topology_overrides[batch.layer_identifier] = tuple(event["sublayers"])
        layers = self.graph.local_reachable_layers(topology_overrides)
        current = {layer.identifier: layer for layer in layers}
        if force or current.keys() != self._tracked.keys():
            self._tracked = current
            self._bridge.set_layers(list(current))
        reachable_ids = {id(layer) for layer in layers}
        self._pending = {
            layer_id: changes
            for layer_id, changes in self._pending.items()
            if layer_id in reachable_ids
        }
        self._prepared = [batch for batch in self._prepared if id(batch.layer) in reachable_ids]

    @contextmanager
    def suppressed(self) -> Iterator[None]:
        self._suppression_depth += 1
        if self._suppression_depth == 1:
            self._bridge.set_suppressed(True)
        try:
            yield
        finally:
            self._suppression_depth -= 1
            if self._suppression_depth == 0:
                self._bridge.set_suppressed(False)
                self.sync_graph()

    def _merge_records(self, records: tuple[SdfNoticeRecord, ...]) -> None:
        for record in records:
            layer = self._tracked.get(record.layer_identifier)
            if layer is None:
                continue
            changes = self._pending.setdefault(id(layer), _LayerChanges(layer))
            changes.replacement |= bool(record.flags & _ChangeFlag.REPLACED_CONTENT)
            changes.topology |= bool(
                record.flags & (_ChangeFlag.REPLACED_CONTENT | _ChangeFlag.SUBLAYERS)
            )
            if record.path:
                changes.add(record.path, record.flags, record.fields)
            if record.old_path:
                changes.add(record.old_path, _ChangeFlag.REMOVED_PROPERTY, ())

    def prepare_local_changes(self) -> tuple[PreparedLayerBatch, ...]:
        self.sync_graph()
        self._merge_records(self._bridge.drain())
        prepared_by_layer = {id(batch.layer): index for index, batch in enumerate(self._prepared)}
        for layer_id, changes in tuple(self._pending.items()):
            events = _materialize_changes(changes)
            prepared_index = prepared_by_layer.get(layer_id)
            if not events:
                if prepared_index is not None:
                    self._prepared.pop(prepared_index)
                    prepared_by_layer = {
                        id(batch.layer): index for index, batch in enumerate(self._prepared)
                    }
                del self._pending[layer_id]
                continue
            batch = PreparedLayerBatch(
                events=events,
                layer=changes.layer,
                layer_identifier=changes.layer.identifier,
                change_serial=changes.serial,
            )
            if prepared_index is None:
                self._prepared.append(batch)
                prepared_by_layer[layer_id] = len(self._prepared) - 1
            else:
                self._prepared[prepared_index] = batch
        return tuple(self._prepared)

    def _events_for(self, batch: PreparedLayerBatch) -> list[dict]:
        events = []
        for event in batch.events:
            routed = dict(event)
            if routed["k"] == K_SET_SUBLAYERS:
                routed["generation"] = self.graph.generation
                routed["sublayers"] = [dict(entry) for entry in event["sublayers"]]
            events.append(routed)
        return events

    def next_routed_batch(self) -> tuple[PreparedLayerBatch, str, list[dict]] | None:
        if not self.graph.ready:
            return None
        reachable = set(self.graph.reachable_layer_keys())
        for batch in self._prepared:
            layer_key = self.graph.key_for(batch.layer)
            if layer_key and layer_key in reachable:
                return batch, layer_key, self._events_for(batch)
        return None

    def restore_prepared(self) -> None:
        if not self.graph.ready or not self._prepared:
            return
        from .event_apply import apply_events, atomic_apply

        with self.suppressed():
            reachable = {
                layer.identifier for layer in self.stage.GetLayerStack(includeSessionLayers=False)
            }
            for batch in self._prepared:
                if batch.layer.identifier not in reachable:
                    continue
                with Usd.EditContext(self.stage, Usd.EditTarget(batch.layer)):
                    with atomic_apply(self.stage):
                        apply_events(self.stage, self._events_for(batch))
            self.sync_graph()

    def accept_authoritative_event(self, _layer: Sdf.Layer, _event: dict) -> None:
        pass

    def accept_authoritative_sublayers(
        self,
        _layer: Sdf.Layer,
        _entries: list[dict],
    ) -> None:
        pass

    def mark_prepared_sent(self, batch: PreparedLayerBatch) -> None:
        try:
            self._prepared.remove(batch)
        except ValueError:
            raise ValueError("batch is not currently prepared") from None
        changes = self._pending.get(id(batch.layer))
        if changes is not None and changes.serial == batch.change_serial:
            del self._pending[id(batch.layer)]

    def close(self) -> None:
        self._bridge.close()
        self._prepared.clear()
        self._pending.clear()


__all__ = [
    "NativeSdfLayerChangeTracker",
    "SdfNoticeBridge",
    "SdfNoticeRecord",
]
