"""Exact authored Sdf changes for shared-stage synchronization.

This Python tracker is the fallback implementation. The native C++ bridge
(``NativeSdfLayerChangeTracker`` in ``sdf_delegate_bridge.py``) is preferred for
production use; build it with
``uv run python -m openusdconnect.build_sdf_notice_bridge``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from pxr import Sdf, Tf, Usd

from .protocol_constants import (
    K_REPLACE_SDF_LAYER_CONTENT,
    K_SET_SDF_SPEC_FIELDS,
    K_SET_SUBLAYERS,
    SDF_LAYER_TOPOLOGY_FIELDS,
    SDF_SPEC_KIND_ATTRIBUTE,
    SDF_SPEC_KIND_LAYER,
    SDF_SPEC_KIND_PRIM,
    SDF_SPEC_KIND_PROPERTY,
    SDF_SPEC_KIND_RELATIONSHIP,
    SDF_SPEC_KIND_VARIANT,
    SDF_SPEC_KIND_VARIANT_SET,
)
from .sdf_spec_delta import event_prim_path, serialize_spec_fields, spec_kind_for_object
from .shared_layer_graph import SharedLayerGraph, read_sublayer_entries

_CREATE_KIND_ORDER = {
    SDF_SPEC_KIND_LAYER: 0,
    SDF_SPEC_KIND_PRIM: 1,
    SDF_SPEC_KIND_VARIANT_SET: 2,
    SDF_SPEC_KIND_VARIANT: 3,
    SDF_SPEC_KIND_ATTRIBUTE: 4,
    SDF_SPEC_KIND_RELATIONSHIP: 4,
    SDF_SPEC_KIND_PROPERTY: 4,
}
_REMOVE_KIND_ORDER = {kind: -order for kind, order in _CREATE_KIND_ORDER.items()}


@dataclass(frozen=True, slots=True)
class _SpecSnapshot:
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _LayerSnapshot:
    layer: Sdf.Layer
    sublayers: tuple[tuple[str, float, float], ...]
    specs: dict[tuple[str, str], _SpecSnapshot]


@dataclass(frozen=True, slots=True)
class PreparedLayerBatch:
    """One retryable transaction containing one layer's local edits."""

    events: tuple[dict, ...]
    layer: Sdf.Layer
    layer_identifier: str
    change_serial: int


def _bind_topology_base(
    batch: PreparedLayerBatch,
    graph: SharedLayerGraph,
) -> PreparedLayerBatch:
    """Freeze topology events against the graph revision they were prepared on."""
    if not graph.ready:
        return batch
    layer_key = graph.key_for(batch.layer)
    if not layer_key:
        return batch
    generation = graph.generation
    revision = graph.parent_revision(layer_key)
    bound = []
    for index, event in enumerate(batch.events):
        if event.get("k") != K_SET_SUBLAYERS or event.get("generation"):
            bound.append(event)
            continue
        routed = dict(event)
        routed["generation"] = generation
        routed["revision"] = revision
        routed["sublayers"] = [dict(entry) for entry in event["sublayers"]]
        bound.append(routed)
        bound.extend(batch.events[index + 1 :])
        return PreparedLayerBatch(
            events=tuple(bound),
            layer=batch.layer,
            layer_identifier=batch.layer_identifier,
            change_serial=batch.change_serial,
        )
    return batch


def _copy_prepared_events(batch: PreparedLayerBatch) -> list[dict]:
    """Copy a batch without sharing mutable topology entries with apply code."""
    events = []
    for event in batch.events:
        copied = dict(event)
        if copied.get("k") == K_SET_SUBLAYERS:
            copied["sublayers"] = [dict(entry) for entry in event["sublayers"]]
        events.append(copied)
    return events


def _topology_snapshot(layer: Sdf.Layer) -> tuple[tuple[str, float, float], ...]:
    return tuple(
        (entry["authored_path"], entry["offset"], entry["scale"])
        for entry in read_sublayer_entries(layer)
    )


def _index_snapshot(snapshot: Sdf.Layer) -> _LayerSnapshot:
    specs = {}

    def _capture(path: Sdf.Path) -> None:
        spec = (
            snapshot.pseudoRoot
            if path == Sdf.Path.absoluteRootPath
            else snapshot.GetObjectAtPath(path)
        )
        if spec is None:
            return
        try:
            kind = spec_kind_for_object(spec)
        except TypeError:
            return
        fields = {str(field) for field in spec.ListInfoKeys()}
        if kind == SDF_SPEC_KIND_LAYER:
            fields.difference_update(SDF_LAYER_TOPOLOGY_FIELDS)
        specs[(kind, str(path))] = _SpecSnapshot(tuple(sorted(fields)))

    snapshot.Traverse(Sdf.Path.absoluteRootPath, _capture)
    return _LayerSnapshot(snapshot, _topology_snapshot(snapshot), specs)


def _capture_layer(layer: Sdf.Layer) -> _LayerSnapshot:
    snapshot = Sdf.Layer.CreateAnonymous("openusdconnect-layer-snapshot")
    snapshot.TransferContent(layer)
    return _index_snapshot(snapshot)


def _spec_at(layer: Sdf.Layer, kind: str, path: str):
    if kind == SDF_SPEC_KIND_LAYER:
        return layer.pseudoRoot
    return layer.GetObjectAtPath(Sdf.Path(path))


def _changed_fields(
    previous: _LayerSnapshot,
    current: _LayerSnapshot,
    kind: str,
    spec_path: str,
) -> list[str]:
    old = previous.specs.get((kind, spec_path))
    new = current.specs.get((kind, spec_path))
    old_fields = set(old.fields if old else ())
    new_fields = set(new.fields if new else ())
    old_spec = _spec_at(previous.layer, kind, spec_path) if old else None
    new_spec = _spec_at(current.layer, kind, spec_path) if new else None
    changed = []
    for field in sorted(old_fields | new_fields):
        old_authored = field in old_fields
        new_authored = field in new_fields
        if old_authored != new_authored:
            changed.append(field)
        elif old_spec.GetInfo(field) != new_spec.GetInfo(field):
            changed.append(field)
    return changed


def sdf_event_sort_key(event: dict) -> tuple[int, int, int, str]:
    path = Sdf.Path(event["spec_path"])
    depth = len(path.GetPrefixes())
    if event.get("removed", False):
        return (0, -depth, _REMOVE_KIND_ORDER[event["spec_kind"]], event["spec_path"])
    return (1, depth, _CREATE_KIND_ORDER[event["spec_kind"]], event["spec_path"])


def _diff_specs(
    source_layer: Sdf.Layer,
    previous: _LayerSnapshot,
    current: _LayerSnapshot,
) -> list[dict]:
    events = []
    for kind, spec_path in set(previous.specs) | set(current.specs):
        old = previous.specs.get((kind, spec_path))
        new = current.specs.get((kind, spec_path))
        if old is not None and new is not None:
            fields = _changed_fields(previous, current, kind, spec_path)
            if not fields:
                continue
        else:
            fields = sorted(set(old.fields if old else ()) | set(new.fields if new else ()))
        events.append(
            {
                "k": K_SET_SDF_SPEC_FIELDS,
                "prim": event_prim_path(spec_path, kind),
                "spec_path": spec_path,
                "spec_kind": kind,
                "fields": fields,
                "fragment": (
                    serialize_spec_fields(
                        source_layer,
                        spec_path,
                        kind,
                        fields,
                        stabilize_asset_paths=False,
                    )
                    if new
                    else ""
                ),
                "removed": new is None,
            }
        )
    events.sort(key=sdf_event_sort_key)
    return events


def _event_for_fields(
    source_layer: Sdf.Layer,
    kind: str,
    spec_path: str,
    fields: list[str],
) -> dict:
    return {
        "k": K_SET_SDF_SPEC_FIELDS,
        "prim": event_prim_path(spec_path, kind),
        "spec_path": spec_path,
        "spec_kind": kind,
        "fields": fields,
        "fragment": serialize_spec_fields(
            source_layer,
            spec_path,
            kind,
            fields,
            stabilize_asset_paths=False,
        ),
        "removed": False,
    }


def _topology_event(snapshot: _LayerSnapshot) -> dict:
    return {
        "k": K_SET_SUBLAYERS,
        "prim": "/",
        "generation": "",
        "revision": 0,
        "sublayers": [
            {"authored_path": path, "offset": offset, "scale": scale}
            for path, offset, scale in snapshot.sublayers
        ],
    }


def _apply_snapshot_events(
    snapshot: _LayerSnapshot,
    events: list[dict],
    *,
    copy_layer: bool = True,
) -> _LayerSnapshot:
    from .sdf_spec_delta import (
        apply_layer_content_replacement,
        apply_spec_delta_to_layer,
    )
    from .shared_layer_graph import apply_sublayer_entries

    if copy_layer:
        working = Sdf.Layer.CreateAnonymous("openusdconnect-layer-snapshot")
        working.TransferContent(snapshot.layer)
    else:
        working = snapshot.layer
    specs = dict(snapshot.specs)
    sublayers = snapshot.sublayers
    requires_reindex = False
    for event in events:
        if event["k"] == K_SET_SUBLAYERS:
            apply_sublayer_entries(working, event.get("sublayers", ()))
            sublayers = _topology_snapshot(working)
        elif event["k"] == K_REPLACE_SDF_LAYER_CONTENT:
            apply_layer_content_replacement(working, event)
            requires_reindex = True
        elif event["k"] == K_SET_SDF_SPEC_FIELDS:
            key = (event["spec_kind"], event["spec_path"])
            existed = key in specs
            apply_spec_delta_to_layer(working, event)
            spec = _spec_at(working, *key)
            if event.get("removed", False) or not existed or spec is None:
                requires_reindex = True
            else:
                fields = {str(field) for field in spec.ListInfoKeys()}
                if event["spec_kind"] == SDF_SPEC_KIND_LAYER:
                    fields.difference_update(SDF_LAYER_TOPOLOGY_FIELDS)
                specs[key] = _SpecSnapshot(tuple(sorted(fields)))
        else:
            raise ValueError(f"unsupported snapshot event {event['k']!r}")
    if requires_reindex:
        return _index_snapshot(working)
    return _LayerSnapshot(working, sublayers, specs)


def _changed_source_fields(
    source_layer: Sdf.Layer,
    previous: _LayerSnapshot,
    path: str,
    fields: set[str],
) -> tuple[str, list[str]] | None:
    sdf_path = Sdf.Path(path)
    source_spec = (
        source_layer.pseudoRoot
        if sdf_path == Sdf.Path.absoluteRootPath
        else source_layer.GetObjectAtPath(sdf_path)
    )
    if source_spec is None:
        return None
    try:
        kind = spec_kind_for_object(source_spec)
    except TypeError:
        return None
    old = previous.specs.get((kind, path))
    if old is None:
        return None
    old_spec = _spec_at(previous.layer, kind, path)
    changed = []
    for field in sorted(fields):
        if kind == SDF_SPEC_KIND_LAYER and field in SDF_LAYER_TOPOLOGY_FIELDS:
            continue
        old_authored = old_spec.HasInfo(field)
        new_authored = source_spec.HasInfo(field)
        if old_authored != new_authored:
            changed.append(field)
        elif old_authored and old_spec.GetInfo(field) != source_spec.GetInfo(field):
            changed.append(field)
    return kind, changed


def _candidate_events(
    source_layer: Sdf.Layer,
    previous: _LayerSnapshot,
    changed_info: dict[str, set[str]],
) -> list[dict]:
    events = []
    current_sublayers = _topology_snapshot(source_layer)
    if current_sublayers != previous.sublayers:
        events.append(
            _topology_event(_LayerSnapshot(previous.layer, current_sublayers, previous.specs))
        )
    spec_events = []
    for path, fields in changed_info.items():
        changed = _changed_source_fields(source_layer, previous, path, fields)
        if changed is None or not changed[1]:
            continue
        kind, changed_fields = changed
        spec_events.append(_event_for_fields(source_layer, kind, path, changed_fields))
    spec_events.sort(key=sdf_event_sort_key)
    events.extend(spec_events)
    return events


def _validated_candidate_events(
    source_layer: Sdf.Layer,
    previous: _LayerSnapshot,
    changed_info: dict[str, set[str]],
) -> list[dict] | None:
    events = _candidate_events(source_layer, previous, changed_info)
    candidate = _apply_snapshot_events(previous, events)
    candidate_text = candidate.layer.ExportToString()
    source_text = source_layer.ExportToString()
    if not source_text or candidate_text != source_text:
        return None
    return events


class SdfLayerChangeTracker:
    """Observe and diff only root-stack layers dirtied by Sdf notices.

    Python does not expose OpenUSD's internal ``SdfChangeList`` entries. USD
    object paths seed a candidate delta that is accepted only when it exactly
    reproduces the authored layer. Complete Sdf snapshots cover changes that
    composition notices omit, including inactive variants and muted layers.
    """

    def __init__(self, stage: Usd.Stage, graph: SharedLayerGraph):
        if not isinstance(stage, Usd.Stage):
            raise TypeError("SdfLayerChangeTracker requires a Usd.Stage")
        self.stage = stage
        self.graph = graph
        self._snapshots: dict[str, _LayerSnapshot] = {}
        self._tracked: dict[str, Sdf.Layer] = {}
        self._local_layers: dict[str, Sdf.Layer] = {}
        self._graph_identity = ("", -1)
        self._dirty: set[str] = set()
        self._change_serials: dict[str, int] = {}
        self._suppression_depth = 0
        self._suppressed_dirty: set[str] = set()
        self._prepared: list[PreparedLayerBatch] = []
        self._changed_info: dict[str, set[str]] = {}
        self._has_resync = False
        self._notice_key = Tf.Notice.RegisterGlobally(
            Sdf.Notice.LayersDidChange,
            self._on_layers_changed,
        )
        self._objects_notice_key = Tf.Notice.Register(
            Usd.Notice.ObjectsChanged,
            self._on_objects_changed,
            stage,
        )
        self.sync_graph()

    @property
    def prepared_event_count(self) -> int:
        return sum(len(batch.events) for batch in self._prepared)

    @property
    def has_local_changes(self) -> bool:
        return bool(self._dirty or self._prepared)

    def _on_layers_changed(self, notice, _sender) -> None:
        for layer in notice.GetLayers():
            identifier = layer.identifier
            if identifier not in self._tracked:
                continue
            self._change_serials[identifier] = self._change_serials.get(identifier, 0) + 1
            if self._suppression_depth:
                self._suppressed_dirty.add(identifier)
            else:
                self._dirty.add(identifier)

    def _on_objects_changed(self, notice, _sender) -> None:
        if self._suppression_depth:
            return
        if notice.GetResyncedPaths():
            self._has_resync = True
        for path in notice.GetChangedInfoOnlyPaths():
            fields = self._changed_info.setdefault(str(path), set())
            fields.update(str(field) for field in notice.GetChangedFields(path))

    def sync_graph(self, *, force: bool = False) -> None:
        """Track mapped graph layers and newly authored local sublayers."""
        topology_overrides = {}
        for batch in self._prepared:
            for event in batch.events:
                if event["k"] == K_SET_SUBLAYERS:
                    topology_overrides[batch.layer_identifier] = tuple(event["sublayers"])
        graph_identity = (self.graph.generation, self.graph.revision)
        topology_changed = force or not self._local_layers or bool(topology_overrides)
        if not topology_changed and graph_identity != self._graph_identity:
            topology_changed = True
        if not topology_changed:
            for identifier in self._dirty | self._suppressed_dirty:
                layer = self._tracked.get(identifier)
                snapshot = self._snapshots.get(identifier)
                if layer is not None and snapshot is not None:
                    if _topology_snapshot(layer) != snapshot.sublayers:
                        topology_changed = True
                        break
        if topology_changed:
            self._local_layers = {
                layer.identifier: layer
                for layer in self.graph.local_reachable_layers(topology_overrides)
            }
            self._graph_identity = graph_identity
        current = dict(self._local_layers)
        self._prepared = [batch for batch in self._prepared if batch.layer_identifier in current]
        self._tracked = current
        tracked_identifiers = set(current)
        self._dirty.intersection_update(tracked_identifiers)
        self._suppressed_dirty.intersection_update(tracked_identifiers)
        self._snapshots = {
            identifier: snapshot
            for identifier, snapshot in self._snapshots.items()
            if identifier in tracked_identifiers
        }
        self._change_serials = {
            identifier: serial
            for identifier, serial in self._change_serials.items()
            if identifier in tracked_identifiers
        }
        for identifier, layer in current.items():
            if identifier not in self._snapshots:
                self._snapshots[identifier] = _capture_layer(layer)
            self._change_serials.setdefault(identifier, 0)

    @contextmanager
    def suppressed(self) -> Iterator[None]:
        """Refresh clean baselines for authoritative changes without re-emitting."""
        self._suppression_depth += 1
        try:
            yield
        finally:
            self._suppression_depth -= 1
            if self._suppression_depth == 0:
                self.sync_graph()
                prepared = {batch.layer_identifier for batch in self._prepared}
                for identifier in self._suppressed_dirty:
                    if identifier in self._dirty or identifier in prepared:
                        continue
                    layer = self._tracked.get(identifier)
                    if layer is not None:
                        self._snapshots[identifier] = _capture_layer(layer)
                self._suppressed_dirty.clear()

    def prepare_local_changes(self) -> tuple[PreparedLayerBatch, ...]:
        """Freeze all current local edits before authoritative replay is applied."""
        self.sync_graph()
        prepared_indices = {
            batch.layer_identifier: index for index, batch in enumerate(self._prepared)
        }
        for identifier, layer in self._tracked.items():
            if identifier not in self._dirty:
                continue
            prepared_index = prepared_indices.get(identifier)
            if (
                prepared_index is not None
                and self._prepared[prepared_index].change_serial == self._change_serials[identifier]
            ):
                continue
            previous = self._snapshots[identifier]
            events = None
            if prepared_index is None and not self._has_resync:
                events = _validated_candidate_events(
                    layer,
                    previous,
                    self._changed_info,
                )
            if events is None:
                current = _capture_layer(layer)
                events = []
                if current.sublayers != previous.sublayers:
                    events.append(_topology_event(current))
                events.extend(_diff_specs(current.layer, previous, current))
            if not events:
                self._dirty.discard(identifier)
                if prepared_index is not None:
                    self._prepared.pop(prepared_index)
                    prepared_indices = {
                        batch.layer_identifier: index for index, batch in enumerate(self._prepared)
                    }
                continue
            batch = _bind_topology_base(
                PreparedLayerBatch(
                    events=tuple(events),
                    layer=layer,
                    layer_identifier=identifier,
                    change_serial=self._change_serials[identifier],
                ),
                self.graph,
            )
            if prepared_index is None:
                self._prepared.append(batch)
                prepared_indices[identifier] = len(self._prepared) - 1
            else:
                self._prepared[prepared_index] = batch
        self._changed_info.clear()
        self._has_resync = False
        return tuple(self._prepared)

    def _events_for(self, batch: PreparedLayerBatch) -> list[dict]:
        return _copy_prepared_events(batch)

    def next_routed_batch(self) -> tuple[PreparedLayerBatch, str, list[dict]] | None:
        """Return the next prepared batch whose layer has an authoritative key."""
        if not self.graph.ready:
            return None
        reachable = set(self.graph.reachable_layer_keys())
        for index, batch in enumerate(self._prepared):
            layer_key = self.graph.key_for(batch.layer)
            if layer_key and layer_key in reachable:
                bound = _bind_topology_base(batch, self.graph)
                if bound is not batch:
                    batch = bound
                    self._prepared[index] = batch
                return batch, layer_key, self._events_for(batch)
        return None

    def restore_prepared(self) -> None:
        """Restore frozen local edits after older authoritative records apply."""
        if not self.graph.ready or not self._prepared:
            return
        from .event_apply import apply_events, atomic_apply

        with self.suppressed():
            for batch in self._prepared:
                reachable = {
                    layer.identifier
                    for layer in self.stage.GetLayerStack(includeSessionLayers=False)
                }
                if batch.layer_identifier not in reachable:
                    continue
                with Usd.EditContext(self.stage, Usd.EditTarget(batch.layer)):
                    with atomic_apply(self.stage):
                        apply_events(self.stage, self._events_for(batch))
            self.sync_graph()

    def accept_authoritative_event(self, layer: Sdf.Layer, event: dict) -> None:
        """Advance a dirty layer's comparison baseline by one remote event."""
        identifier = layer.identifier
        prepared = any(batch.layer_identifier == identifier for batch in self._prepared)
        if identifier not in self._dirty and not prepared:
            return
        snapshot = self._snapshots.get(identifier)
        if snapshot is not None:
            self._snapshots[identifier] = _apply_snapshot_events(snapshot, [event])

    def accept_authoritative_sublayers(
        self,
        layer: Sdf.Layer,
        entries: list[dict],
    ) -> None:
        """Advance a dirty layer's baseline by authoritative graph topology."""
        identifier = layer.identifier
        prepared = any(batch.layer_identifier == identifier for batch in self._prepared)
        if identifier not in self._dirty and not prepared:
            return
        snapshot = self._snapshots.get(identifier)
        if snapshot is None:
            return
        from .shared_layer_graph import apply_sublayer_entries

        working = Sdf.Layer.CreateAnonymous("openusdconnect-layer-snapshot")
        working.TransferContent(snapshot.layer)
        apply_sublayer_entries(working, entries)
        self._snapshots[identifier] = _index_snapshot(working)

    def mark_prepared_sent(self, batch: PreparedLayerBatch) -> None:
        """Commit the baseline represented by a successfully sent batch."""
        try:
            self._prepared.remove(batch)
        except ValueError:
            raise ValueError("batch is not currently prepared") from None
        identifier = batch.layer_identifier
        self._snapshots[identifier] = _apply_snapshot_events(
            self._snapshots[identifier],
            self._events_for(batch),
            copy_layer=False,
        )
        if self._change_serials.get(identifier, 0) == batch.change_serial:
            self._dirty.discard(identifier)

    def close(self) -> None:
        if self._notice_key is not None:
            self._notice_key.Revoke()
            self._notice_key = None
        if self._objects_notice_key is not None:
            self._objects_notice_key.Revoke()
            self._objects_notice_key = None
        self._prepared.clear()
        self._snapshots.clear()


__all__ = ["PreparedLayerBatch", "SdfLayerChangeTracker", "sdf_event_sort_key"]
