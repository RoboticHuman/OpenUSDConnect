"""Receiver-local reconstruction of a logical collaboration layer stack.

Layer keys are portable protocol identities, not ``Sdf.Layer`` identifiers.
This module maps those opaque keys to anonymous layers owned by one
receiver and composes them in the advertised strong-to-weak order.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager

from pxr import Sdf, Usd

from ._managed_sublayers import replace_managed_sublayers


class LogicalLayerRouter:
    """Own and route one receiver's logical collaboration layers."""

    def __init__(self, stage: Usd.Stage | None = None):
        self._stage: Usd.Stage | None = None
        self._layers: dict[str, Sdf.Layer] = {}
        self._layer_keys: tuple[str, ...] = ()
        self._muted: dict[str, bool] = {}
        self._generation = ""
        self._revision = -1
        if stage is not None:
            self.bind(stage)

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def layer_keys(self) -> tuple[str, ...]:
        return self._layer_keys

    @property
    def stage(self) -> Usd.Stage | None:
        return self._stage

    def bind(self, stage: Usd.Stage) -> None:
        """Attach the managed layers to *stage*, preserving their contents."""
        if not isinstance(stage, Usd.Stage):
            raise TypeError("LogicalLayerRouter requires a Usd.Stage")
        if stage is self._stage:
            return
        self._detach()
        self._stage = stage
        if self._layer_keys:
            self._install_stack()

    def close(self) -> None:
        """Detach only layers owned by this router."""
        self._detach()
        self._stage = None

    def apply_state(self, state: dict) -> bool:
        """Apply an authoritative layer-stack state.

        Returns ``False`` for an older or duplicate state in the same server
        generation. Revisions may restart when the server process changes;
        ``generation`` scopes the comparison without acting as persistent
        layer identity.
        """
        generation = str(state.get("generation") or "")
        revision = int(state.get("revision", 0))
        if revision < 0:
            raise ValueError("layer stack revision must be non-negative")
        if generation == self._generation and revision <= self._revision:
            return False

        layer_keys: list[str] = []
        labels: dict[str, str] = {}
        muted: dict[str, bool] = {}
        for item in state.get("layers", ()):
            layer_key = str(item.get("layer_key") or "")
            if not layer_key:
                raise ValueError("logical layer state requires a layer_key")
            if layer_key in muted:
                raise ValueError(f"duplicate logical layer {layer_key!r}")
            layer_keys.append(layer_key)
            labels[layer_key] = str(item.get("label") or layer_key)
            muted[layer_key] = bool(item.get("muted", False))

        for layer_key in layer_keys:
            label = labels[layer_key]
            if layer_key not in self._layers:
                self._layers[layer_key] = self._create_layer(
                    layer_key,
                    label,
                )

        self._generation = generation
        self._revision = revision
        self._layer_keys = tuple(layer_keys)
        self._muted = muted
        removed_keys = set(self._layers) - set(layer_keys)
        if self._stage is not None:
            self._install_stack()
        for layer_key in removed_keys:
            del self._layers[layer_key]
        return True

    def layer_for(self, layer_key: str) -> Sdf.Layer:
        """Return the receiver-local layer for one routed event."""
        if self._revision < 0:
            raise RuntimeError("layer stack state has not been received")
        if layer_key not in self._layer_keys:
            raise ValueError(f"event targets unknown logical layer {layer_key!r}")
        return self._layers[layer_key]

    def edit_target_for(self, layer_key: str) -> Usd.EditTarget:
        return Usd.EditTarget(self.layer_for(layer_key))

    def clear(self) -> None:
        """Clear authored receiver state while retaining the advertised stack."""
        for layer in self._layers.values():
            layer.Clear()

    @contextmanager
    def writable(self, layers: Iterable[Sdf.Layer]):
        """Temporarily unmute routed layers so ``UsdEditTarget`` can author."""
        if self._stage is None:
            raise RuntimeError("logical layer router is not bound to a stage")
        identifiers = {
            layer.identifier for layer in layers if self._stage.IsLayerMuted(layer.identifier)
        }
        if identifiers:
            self._stage.MuteAndUnmuteLayers([], sorted(identifiers))
        try:
            yield
        finally:
            if identifiers:
                self._stage.MuteAndUnmuteLayers(sorted(identifiers), [])

    def _create_layer(self, layer_key: str, label: str) -> Sdf.Layer:
        return Sdf.Layer.CreateAnonymous(f"receiver-layer-{label}")

    def _managed_identifiers(self) -> set[str]:
        return {layer.identifier for layer in self._layers.values()}

    def _install_stack(self) -> None:
        stage = self._stage
        if stage is None:
            return

        active_layers = [
            self._layers[layer_key]
            for layer_key in self._layer_keys
        ]
        active_ids = [layer.identifier for layer in active_layers]
        all_ids = self._managed_identifiers()

        # A detached managed layer must not remain in the stage's muted set.
        desired_muted = {
            self._layers[layer_key].identifier
            for layer_key in self._layer_keys
            if self._muted.get(layer_key, False)
        }
        currently_muted = {identifier for identifier in all_ids if stage.IsLayerMuted(identifier)}
        to_unmute = currently_muted - desired_muted
        if to_unmute:
            stage.MuteAndUnmuteLayers([], sorted(to_unmute))

        replace_managed_sublayers(
            stage.GetSessionLayer(),
            active_ids,
            all_ids,
        )

        currently_muted = {
            identifier for identifier in active_ids if stage.IsLayerMuted(identifier)
        }
        to_mute = desired_muted - currently_muted
        to_unmute = currently_muted - desired_muted
        if to_mute or to_unmute:
            stage.MuteAndUnmuteLayers(sorted(to_mute), sorted(to_unmute))

    def _detach(self) -> None:
        stage = self._stage
        if stage is None:
            return
        managed = self._managed_identifiers()
        muted = {identifier for identifier in managed if stage.IsLayerMuted(identifier)}
        if muted:
            stage.MuteAndUnmuteLayers([], sorted(muted))
        replace_managed_sublayers(stage.GetSessionLayer(), [], managed)


__all__ = ["LogicalLayerRouter"]
