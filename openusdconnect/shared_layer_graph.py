"""Portable routing for a stage's authored root-layer graph."""

from __future__ import annotations

import logging
import math
import ntpath
import posixpath
import uuid
from collections import deque
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from pxr import Ar, Sdf, Usd

from .layer_key_router import LayerKeyRouter
from .protocol_constants import K_SET_SUBLAYERS, MSG_LAYER_GRAPH_STATE

LOG = logging.getLogger(__name__)


def _new_layer_key() -> str:
    return f"layer:{uuid.uuid4().hex}"


def _is_local_asset_path(identifier: str) -> bool:
    outer = Ar.SplitPackageRelativePathOuter(identifier)[0]
    return posixpath.isabs(outer) or ntpath.isabs(outer) or urlsplit(outer).scheme.lower() == "file"


def normalize_sublayer_entries(entries: Iterable[dict]) -> tuple[dict, ...]:
    """Validate and normalize a complete authored sublayer list."""
    normalized = []
    authored_paths = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("sublayer entries must be dictionaries")
        authored_path = entry.get("authored_path")
        if not isinstance(authored_path, str) or not authored_path:
            raise ValueError("sublayer authored paths must be non-empty strings")
        if Sdf.Layer.IsAnonymousLayerIdentifier(authored_path) or _is_local_asset_path(
            authored_path
        ):
            raise ValueError("shared-stage sublayers must have portable asset identifiers")
        if authored_path in authored_paths:
            raise ValueError(f"duplicate authored sublayer path {authored_path!r}")
        authored_paths.add(authored_path)

        offset = entry.get("offset", 0.0)
        scale = entry.get("scale", 1.0)
        if (
            isinstance(offset, bool)
            or not isinstance(offset, (int, float))
            or isinstance(scale, bool)
            or not isinstance(scale, (int, float))
            or not math.isfinite(offset)
            or not math.isfinite(scale)
        ):
            raise ValueError("sublayer offsets and scales must be finite numbers")
        layer_key = entry.get("layer_key", "")
        if not isinstance(layer_key, str):
            raise ValueError("sublayer layer keys must be strings")
        item = {
            "authored_path": authored_path,
            "offset": float(offset),
            "scale": float(scale),
        }
        if layer_key:
            item["layer_key"] = layer_key
        normalized.append(item)
    return tuple(normalized)


def read_sublayer_entries(layer: Sdf.Layer) -> tuple[dict, ...]:
    """Read one layer's ordered authored paths and offsets."""
    paths = list(layer.subLayerPaths)
    offsets = list(layer.subLayerOffsets)
    return tuple(
        {
            "authored_path": str(path),
            "offset": float(offsets[index].offset),
            "scale": float(offsets[index].scale),
        }
        for index, path in enumerate(paths)
    )


def apply_sublayer_entries(layer: Sdf.Layer, entries: Iterable[dict]) -> None:
    """Replace one layer's authored sublayer list without resolving its assets."""
    normalized = normalize_sublayer_entries(entries)
    with Sdf.ChangeBlock():
        layer.subLayerPaths.clear()
        for entry in normalized:
            layer.subLayerPaths.append(entry["authored_path"])
        for index, entry in enumerate(normalized):
            layer.subLayerOffsets[index] = Sdf.LayerOffset(
                entry["offset"],
                entry["scale"],
            )


@dataclass(frozen=True, slots=True)
class PreparedSublayers:
    """Canonical topology event and the child mappings it establishes."""

    parent_key: str
    event: dict
    mappings: tuple[tuple[Sdf.Layer, str], ...]


class SharedLayerGraph(LayerKeyRouter):
    """Map portable layer keys onto one process's root-layer stack.

    Keys are protocol identity. ``Sdf.Layer.identifier`` remains local and is
    used only to avoid opening or routing the same local layer twice.
    """

    def __init__(self, stage: Usd.Stage, *, authoritative: bool = False):
        if not isinstance(stage, Usd.Stage):
            raise TypeError("SharedLayerGraph requires a Usd.Stage")
        super().__init__(stage)
        self.authoritative = bool(authoritative)
        self.root_layer_key = ""
        self._states: dict[str, tuple[dict, ...]] = {}

        if self.authoritative:
            self._generation = uuid.uuid4().hex
            self._revision = 1
            self._ready = True
            self.root_layer_key = _new_layer_key()
            self._bind_key(self.root_layer_key, stage.GetRootLayer())
            self._capture_reachable(assign_keys=True)

    def sublayers_for(self, layer_key: str) -> tuple[dict, ...] | None:
        entries = self._states.get(layer_key)
        return tuple(dict(entry) for entry in entries) if entries is not None else None

    def reachable_layer_keys(self) -> tuple[str, ...]:
        if not self.ready:
            return ()
        reachable = []
        queue = deque([self.root_layer_key])
        visited = set()
        while queue:
            layer_key = queue.popleft()
            if layer_key in visited:
                continue
            visited.add(layer_key)
            reachable.append(layer_key)
            queue.extend(
                entry["layer_key"]
                for entry in self._states.get(layer_key, ())
                if entry.get("layer_key")
            )
        return tuple(reachable)

    def reachable_layers(self) -> tuple[Sdf.Layer, ...]:
        return tuple(
            self._layers[key] for key in self.reachable_layer_keys() if key in self._layers
        )

    def local_reachable_layers(
        self,
        sublayer_overrides: dict[str, tuple[dict, ...]] | None = None,
    ) -> tuple[Sdf.Layer, ...]:
        """Resolve the root graph currently authored in this process."""
        overrides = sublayer_overrides or {}
        layers = []
        queue = deque([self.stage.GetRootLayer()])
        visited = set()
        while queue:
            layer = queue.popleft()
            if layer.identifier in visited:
                continue
            visited.add(layer.identifier)
            layers.append(layer)
            entries = overrides.get(layer.identifier)
            if entries is None:
                entries = read_sublayer_entries(layer)
            layer_key = self.key_for(layer)
            known_children = {
                entry["authored_path"]: self.layer_for(entry["layer_key"])
                for entry in self._states.get(layer_key, ())
                if entry.get("layer_key")
            }
            for entry in entries:
                child = known_children.get(entry["authored_path"])
                if child is None:
                    child = self._resolve(layer, entry["authored_path"])
                if child is not None:
                    queue.append(child)
        return tuple(layers)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Roll back routing state when a surrounding stage edit fails."""
        old = (
            self._generation,
            self._revision,
            self.root_layer_key,
            dict(self._layers),
            dict(self._keys_by_identifier),
            dict(self._states),
        )
        try:
            yield
        except Exception:
            (
                self._generation,
                self._revision,
                self.root_layer_key,
                self._layers,
                self._keys_by_identifier,
                self._states,
            ) = old
            raise

    def _resolve(self, parent: Sdf.Layer, authored_path: str) -> Sdf.Layer | None:
        try:
            with Ar.ResolverContextBinder(self.stage.GetPathResolverContext()):
                return Sdf.Layer.FindOrOpenRelativeToLayer(parent, authored_path)
        except Exception as exc:  # resolver plugins can surface Python exceptions
            LOG.warning(
                "Could not resolve sublayer %r relative to %s: %s",
                authored_path,
                parent.identifier,
                exc,
            )
            return None

    def _entries_for_layer(
        self,
        layer: Sdf.Layer,
        *,
        assign_keys: bool,
    ) -> tuple[tuple[dict, ...], tuple[Sdf.Layer, ...]]:
        entries = []
        children = []
        for entry in normalize_sublayer_entries(read_sublayer_entries(layer)):
            child = self._resolve(layer, entry["authored_path"])
            if child is not None:
                layer_key = self.key_for(child)
                if layer_key is None and assign_keys:
                    layer_key = _new_layer_key()
                    self._bind_key(layer_key, child)
                if layer_key:
                    entry["layer_key"] = layer_key
                children.append(child)
            entries.append(entry)
        return tuple(entries), tuple(children)

    def _capture_reachable(self, *, assign_keys: bool) -> None:
        root = self.layer_for(self.root_layer_key)
        if root is None:
            raise RuntimeError("shared layer graph has no local root layer")
        states: dict[str, tuple[dict, ...]] = {}
        queue = deque([root])
        visited = set()
        while queue:
            layer = queue.popleft()
            if layer.identifier in visited:
                continue
            visited.add(layer.identifier)
            layer_key = self.key_for(layer)
            if layer_key is None:
                if not assign_keys:
                    continue
                layer_key = _new_layer_key()
                self._bind_key(layer_key, layer)
            entries, children = self._entries_for_layer(layer, assign_keys=assign_keys)
            states[layer_key] = entries
            queue.extend(children)
        self._states = states

    def state_message(self, *, seq: int) -> dict:
        """Return the complete reachable graph as a sequenced baseline."""
        if not self.ready:
            raise RuntimeError("shared layer graph has not received a baseline")
        layer_keys = self.reachable_layer_keys()
        missing = [layer_key for layer_key in layer_keys if layer_key not in self._states]
        if missing:
            raise RuntimeError(f"shared layer graph has incomplete state for {missing!r}")
        return {
            "type": MSG_LAYER_GRAPH_STATE,
            "seq": int(seq),
            "generation": self.generation,
            "revision": self.revision,
            "root_layer_key": self.root_layer_key,
            "layers": [
                {
                    "layer_key": layer_key,
                    "sublayers": [dict(entry) for entry in entries],
                }
                for layer_key in layer_keys
                for entries in (self._states[layer_key],)
            ],
        }

    @staticmethod
    def _validate_topology(message: dict) -> dict[str, tuple[dict, ...]]:
        generation = message.get("generation")
        root_key = message.get("root_layer_key")
        revision = message.get("revision")
        layers = message.get("layers")
        if not isinstance(generation, str) or not generation:
            raise ValueError("layer graph generation must be a non-empty string")
        if not isinstance(root_key, str) or not root_key:
            raise ValueError("layer graph root key must be a non-empty string")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("layer graph revision must be a positive integer")
        if not isinstance(layers, list):
            raise ValueError("layer graph layers must be a list")

        states = {}
        for state in layers:
            if not isinstance(state, dict):
                raise ValueError("layer graph states must be dictionaries")
            layer_key = state.get("layer_key")
            if not isinstance(layer_key, str) or not layer_key or layer_key in states:
                raise ValueError("layer graph keys must be unique non-empty strings")
            states[layer_key] = normalize_sublayer_entries(state.get("sublayers", ()))
        if root_key not in states:
            raise ValueError("layer graph baseline does not contain its root layer")
        declared = set(states)
        for entries in states.values():
            for entry in entries:
                child_key = entry.get("layer_key")
                if child_key and child_key not in declared:
                    raise ValueError(f"sublayer references undeclared layer key {child_key!r}")
        return states

    def apply_state(self, message: dict) -> None:
        """Apply and route a complete authoritative graph baseline."""
        if not super().apply_state(message):
            return

    def _apply_state_inner(self, state: dict) -> bool:
        states = self._validate_topology(state)
        root_key = state["root_layer_key"]
        old = (
            self._generation,
            self._revision,
            self.root_layer_key,
            self._layers,
            self._keys_by_identifier,
            self._states,
        )
        backups: dict[str, tuple[dict, ...]] = {}
        self._generation = state["generation"]
        self._revision = int(state["revision"])
        self.root_layer_key = root_key
        self._layers = {}
        self._keys_by_identifier = {}
        self._states = states
        self._bind_key(root_key, self.stage.GetRootLayer())
        try:
            self._materialize_states(backups=backups)
        except Exception:
            for identifier, entries in backups.items():
                layer = Sdf.Layer.Find(identifier)
                if layer is not None:
                    apply_sublayer_entries(layer, entries)
            (
                self._generation,
                self._revision,
                self.root_layer_key,
                self._layers,
                self._keys_by_identifier,
                self._states,
            ) = old
            raise
        return True

    def _materialize_states(
        self,
        *,
        backups: dict[str, tuple[dict, ...]] | None = None,
    ) -> tuple[str, ...]:
        newly_mapped = []
        queue = deque([self.root_layer_key])
        visited = set()
        while queue:
            parent_key = queue.popleft()
            if parent_key in visited:
                continue
            visited.add(parent_key)
            parent = self.layer_for(parent_key)
            if parent is None:
                continue
            entries = self._states.get(parent_key)
            if entries is None:
                raise ValueError(f"layer graph has no state for {parent_key!r}")
            if backups is not None and parent.identifier not in backups:
                backups[parent.identifier] = read_sublayer_entries(parent)
            apply_sublayer_entries(parent, entries)
            for entry in entries:
                child_key = entry.get("layer_key")
                if not child_key:
                    continue
                child = self._resolve(parent, entry["authored_path"])
                if child is None:
                    continue
                was_known = self.layer_for(child_key) is not None
                self._bind_key(child_key, child)
                if not was_known:
                    newly_mapped.append(child_key)
                queue.append(child_key)
        return tuple(newly_mapped)

    def describe_sublayers(self, layer: Sdf.Layer) -> dict:
        """Build a client-authored full-parent topology event."""
        layer_key = self.key_for(layer)
        if not layer_key or not self.ready:
            raise ValueError("layer is not mapped by the authoritative graph")
        entries, _children = self._entries_for_layer(layer, assign_keys=False)
        return {
            "k": K_SET_SUBLAYERS,
            "prim": "/",
            "generation": self.generation,
            "revision": 0,
            "sublayers": [dict(entry) for entry in entries],
        }

    def canonicalize_sublayers(self, parent_key: str, event: dict) -> PreparedSublayers:
        """Assign authoritative child keys without mutating the stage."""
        if not self.authoritative:
            raise RuntimeError("only the authoritative graph can assign layer keys")
        if event.get("generation") != self.generation:
            raise ValueError("sublayer event belongs to a different graph generation")
        parent = self.layer_for(parent_key)
        if parent is None or parent_key not in self.reachable_layer_keys():
            raise ValueError(f"unknown shared layer key {parent_key!r}")

        entries = normalize_sublayer_entries(event.get("sublayers", ()))
        pending_by_identifier: dict[str, str] = {}
        mappings = []
        canonical = []
        for entry in entries:
            item = dict(entry)
            child = self._resolve(parent, item["authored_path"])
            if child is not None:
                layer_key = self.key_for(child) or pending_by_identifier.get(child.identifier)
                supplied_key = item.get("layer_key")
                if layer_key and supplied_key and supplied_key != layer_key:
                    raise ValueError(
                        f"sublayer {item['authored_path']!r} has stale layer key {supplied_key!r}"
                    )
                if layer_key is None:
                    layer_key = _new_layer_key()
                    pending_by_identifier[child.identifier] = layer_key
                item["layer_key"] = layer_key
                mappings.append((child, layer_key))
            else:
                item.pop("layer_key", None)
            canonical.append(item)

        canonical_event = {
            "k": K_SET_SUBLAYERS,
            "prim": "/",
            "generation": self.generation,
            "revision": self.revision + 1,
            "sublayers": canonical,
        }
        return PreparedSublayers(parent_key, canonical_event, tuple(mappings))

    def accept_sublayers(self, prepared: PreparedSublayers) -> None:
        """Commit routing state after its topology event applied successfully."""
        event = prepared.event
        expected_revision = self.revision + 1
        if event.get("generation") != self.generation:
            raise ValueError("sublayer event belongs to a different graph generation")
        if int(event.get("revision", 0)) != expected_revision:
            raise ValueError(
                f"expected layer graph revision {expected_revision}, got {event.get('revision', 0)}"
            )
        for layer, layer_key in prepared.mappings:
            self._bind_key(layer_key, layer)
        self._states[prepared.parent_key] = normalize_sublayer_entries(event["sublayers"])
        self._revision = expected_revision

    def discover_sublayer_states(
        self,
        child_mappings: Iterable[tuple[Sdf.Layer, str]],
    ) -> tuple[tuple[str, dict], ...]:
        """Assign routing state for newly reachable descendant layers."""
        if not self.authoritative:
            raise RuntimeError("only the authoritative graph can discover layer state")
        records = []
        queue = deque(child_mappings)
        visited = set()
        while queue:
            layer, layer_key = queue.popleft()
            if layer_key in visited or layer_key in self._states:
                continue
            visited.add(layer_key)
            event = self.describe_sublayers(layer)
            prepared = self.canonicalize_sublayers(layer_key, event)
            self.accept_sublayers(prepared)
            records.append((layer_key, prepared.event))
            queue.extend(prepared.mappings)
        self._prune_unreachable_states()
        return tuple(records)

    def refresh_resolved_sublayers(self) -> tuple[tuple[str, dict], ...]:
        """Assign keys to graph edges that have become resolvable."""
        if not self.authoritative:
            raise RuntimeError("only the authoritative graph can assign layer keys")
        records = []
        for parent_key in self.reachable_layer_keys():
            parent = self.layer_for(parent_key)
            if parent is None:
                continue
            current = self._states.get(parent_key, ())
            if not any(not entry.get("layer_key") for entry in current):
                continue
            event = self.describe_sublayers(parent)
            prepared = self.canonicalize_sublayers(parent_key, event)
            if prepared.event["sublayers"] == list(current):
                continue
            self.accept_sublayers(prepared)
            records.append((parent_key, prepared.event))
            records.extend(self.discover_sublayer_states(prepared.mappings))
        self._prune_unreachable_states()
        return tuple(records)

    def _prune_unreachable_states(self) -> None:
        reachable = set(self.reachable_layer_keys())
        self._states = {
            layer_key: entries
            for layer_key, entries in self._states.items()
            if layer_key in reachable
        }

    def apply_sublayers(self, parent_key: str, event: dict) -> None:
        """Apply one canonical server event and advance local routing state."""
        if event.get("generation") != self.generation:
            raise ValueError("sublayer event belongs to a different graph generation")
        if parent_key not in self.reachable_layer_keys():
            raise ValueError(f"unknown shared layer key {parent_key!r}")
        expected_revision = self.revision + 1
        if int(event.get("revision", 0)) != expected_revision:
            raise ValueError(
                f"expected layer graph revision {expected_revision}, got {event.get('revision', 0)}"
            )
        parent = self.layer_for(parent_key)
        entries = normalize_sublayer_entries(event.get("sublayers", ()))
        mappings = []
        if parent is not None:
            apply_sublayer_entries(parent, entries)
            for entry in entries:
                child_key = entry.get("layer_key")
                if not child_key:
                    continue
                child = self._resolve(parent, entry["authored_path"])
                if child is not None:
                    mappings.append((child, child_key))
        self.accept_sublayers(PreparedSublayers(parent_key, dict(event), tuple(mappings)))
        self._prune_unreachable_states()

    def refresh_dependencies(self) -> tuple[str, ...]:
        """Retry unresolved keyed edges under the stage's resolver context."""
        Ar.GetResolver().RefreshContext(self.stage.GetPathResolverContext())
        return self._materialize_states()


__all__ = [
    "PreparedSublayers",
    "SharedLayerGraph",
    "apply_sublayer_entries",
    "normalize_sublayer_entries",
    "read_sublayer_entries",
]
