"""Shared layer-key routing state and lifecycle for mode-specific routers.

Portable protocol layer keys map to local ``Sdf.Layer`` objects. The
generation/revision pair scopes authoritative baselines across explicit
revision-domain changes such as log compaction.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pxr import Sdf, Usd


class LayerKeyRouter(ABC):
    """Map portable layer keys onto local layers with authoritative state sync.

    Subclasses add a topology model on top of the shared bidirectional maps
    and stage lifecycle. The base validates the generation/revision pair;
    subclasses reconcile mode-specific state in ``_apply_state_inner``.
    """

    def __init__(self, stage: Usd.Stage | None = None):
        self._layers: dict[str, Sdf.Layer] = {}
        self._keys_by_identifier: dict[str, str] = {}
        self._generation = ""
        self._revision = -1
        self._ready = False
        self._stage: Usd.Stage | None = None
        if stage is not None:
            self.bind(stage)

    @property
    def generation(self) -> str:
        return self._generation

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def ready(self) -> bool:
        """True once an authoritative state baseline has been applied."""
        return self._ready

    @property
    def stage(self) -> Usd.Stage | None:
        return self._stage

    @property
    def layer_keys(self) -> tuple[str, ...]:
        """Layer keys currently mapped in this router."""
        return tuple(self._layers)

    def layer_for(self, layer_key: str) -> Sdf.Layer | None:
        return self._layers.get(layer_key)

    def key_for(self, layer: Sdf.Layer) -> str | None:
        return self._keys_by_identifier.get(layer.identifier)

    def bind(self, stage: Usd.Stage) -> None:
        """Attach the router's layers to *stage*, preserving their contents."""
        if not isinstance(stage, Usd.Stage):
            raise TypeError("LayerKeyRouter requires a Usd.Stage")
        if stage is self._stage:
            return
        if self._stage is not None:
            self._detach_layers(self._stage)
        self._stage = stage
        self._install_layers(stage)

    def close(self) -> None:
        """Detach only layers owned by this router."""
        if self._stage is not None:
            self._detach_layers(self._stage)
        self._stage = None

    def apply_state(self, state: dict) -> bool:
        """Apply one authoritative state, guarded by generation/revision.

        Returns ``False`` for an older or duplicate state in the same
        generation. An authoritative generation change may restart revisions;
        ``generation`` scopes the comparison without acting as persistent
        layer identity. The generation/revision pair and ``ready`` flag commit
        only when the subclass reconciliation succeeds.
        """
        generation = str(state.get("generation") or "")
        revision = int(state.get("revision", 0))
        if revision < 0:
            raise ValueError("layer state revision must be non-negative")
        if generation == self._generation and revision <= self._revision:
            return False
        self._apply_state_inner(state)
        self._generation = generation
        self._revision = revision
        self._ready = True
        return True

    def _bind_key(self, layer_key: str, layer: Sdf.Layer) -> None:
        """Map one layer key to one local layer, rejecting conflicting maps."""
        existing_layer = self._layers.get(layer_key)
        if existing_layer is not None and existing_layer.identifier != layer.identifier:
            raise ValueError(f"layer key {layer_key!r} maps to more than one local layer")
        existing_key = self._keys_by_identifier.get(layer.identifier)
        if existing_key is not None and existing_key != layer_key:
            raise ValueError(
                f"local layer {layer.identifier!r} maps to both {existing_key!r} and {layer_key!r}"
            )
        self._layers[layer_key] = layer
        self._keys_by_identifier[layer.identifier] = layer_key

    @abstractmethod
    def _apply_state_inner(self, state: dict) -> None:
        """Reconcile mode-specific state after the generation/revision guard.

        Generation/revision commit only when reconciliation returns without
        raising.
        """

    def _install_layers(self, stage: Usd.Stage) -> None:  # noqa: B027
        """Called after bind() to make layers visible in the stage composition."""

    def _detach_layers(self, stage: Usd.Stage) -> None:  # noqa: B027
        """Called before unbinding to remove layers from composition."""


__all__ = ["LayerKeyRouter"]
