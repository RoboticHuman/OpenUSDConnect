"""Server-owned collaboration layer stack.

The stack is expressed in portable OpenUSDConnect layer keys. Concrete
``Sdf.Layer`` identifiers remain local to the server process.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from pxr import Sdf, Usd

from .._managed_sublayers import replace_managed_sublayers


class CollaborationLayerStack:
    """Own the server's ordered collaboration layers.

    Callers serialize mutations with the server's stage lock. The default
    layer is fixed as the weakest managed layer; all other keys may be
    reordered ahead of it. Layers outside this manager retain their relative
    order and offsets after managed-stack changes.
    """

    def __init__(
        self,
        stage: Usd.Stage,
        default_layer: Sdf.Layer,
        *,
        default_key: str = "default",
        default_label: str = "Default",
    ):
        if not isinstance(stage, Usd.Stage):
            raise TypeError("CollaborationLayerStack requires a Usd.Stage")
        if not default_key:
            raise ValueError("default layer key must be non-empty")

        self._stage = stage
        self._default_key = default_key
        self._layers: dict[str, Sdf.Layer] = {default_key: default_layer}
        self._keys_by_identifier: dict[str, str] = {
            default_layer.identifier: default_key,
        }
        self._labels: dict[str, str] = {default_key: default_label}
        self._order: list[str] = [default_key]
        self._order_view: tuple[str, ...] = (default_key,)
        self._ordered_layers: tuple[Sdf.Layer, ...] = (default_layer,)
        self._generation = uuid.uuid4().hex
        self._revision = 1
        self._install()

    @property
    def default_key(self) -> str:
        return self._default_key

    @property
    def generation(self) -> str:
        return self._generation

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def layer_keys(self) -> tuple[str, ...]:
        return self._order_view

    @property
    def ordered_layers(self) -> tuple[Sdf.Layer, ...]:
        return self._ordered_layers

    @property
    def managed_layers(self) -> tuple[Sdf.Layer, ...]:
        return tuple(self._layers.values())

    def has_layer(self, layer_key: str) -> bool:
        return layer_key in self._layers

    def label_for(self, layer_key: str) -> str:
        try:
            return self._labels[layer_key]
        except KeyError:
            raise KeyError(f"unknown collaboration layer {layer_key!r}") from None

    def layer_for(self, layer_key: str) -> Sdf.Layer:
        try:
            return self._layers[layer_key]
        except KeyError:
            raise KeyError(f"unknown collaboration layer {layer_key!r}") from None

    def key_for_layer(self, layer: Sdf.Layer) -> str | None:
        return self._keys_by_identifier.get(layer.identifier)

    def ensure_layer(
        self,
        layer_key: str,
        *,
        label: str | None = None,
    ) -> tuple[Sdf.Layer, bool]:
        """Return the layer for *layer_key*, creating it before the default."""
        if not layer_key:
            raise ValueError("collaboration layer key must be non-empty")
        existing = self._layers.get(layer_key)
        if existing is not None:
            return existing, False

        display_label = label or layer_key
        layer = Sdf.Layer.CreateAnonymous(f"collaboration-{display_label}")
        self._layers[layer_key] = layer
        self._keys_by_identifier[layer.identifier] = layer_key
        self._labels[layer_key] = display_label
        self._order.insert(len(self._order) - 1, layer_key)
        self._refresh_order_cache()
        self._revision += 1
        self._install()
        return layer, True

    def set_order(self, ordered_keys: Iterable[str]) -> bool:
        """Replace the complete managed order.

        Every managed key must appear exactly once and the default key must be
        last.  Policy code is responsible for preserving unlisted keys.
        """
        order = list(ordered_keys)
        order_set = set(order)
        managed_keys = set(self._layers)
        if len(order) != len(order_set):
            raise ValueError("collaboration layer order contains duplicates")
        if order_set != managed_keys:
            missing = sorted(managed_keys - order_set)
            unknown = sorted(order_set - managed_keys)
            raise ValueError(
                "collaboration layer order must contain every managed key "
                f"(missing={missing}, unknown={unknown})"
            )
        if not order or order[-1] != self._default_key:
            raise ValueError("default collaboration layer must remain weakest")
        if order == self._order:
            return False

        self._order = order
        self._refresh_order_cache()
        self._revision += 1
        self._install()
        return True

    def set_muted(self, layer_key: str, muted: bool) -> bool:
        """Set one managed layer's stage-local mute state."""
        layer = self.layer_for(layer_key)
        currently_muted = self._stage.IsLayerMuted(layer.identifier)
        muted = bool(muted)
        if currently_muted == muted:
            return False
        if muted:
            self._stage.MuteLayer(layer.identifier)
        else:
            self._stage.UnmuteLayer(layer.identifier)
        self._revision += 1
        return True

    def remove_layer(self, layer_key: str) -> Sdf.Layer:
        """Detach and forget a non-default managed layer."""
        if layer_key == self._default_key:
            raise ValueError("cannot remove the default collaboration layer")
        layer = self.layer_for(layer_key)
        if self._stage.IsLayerMuted(layer.identifier):
            self._stage.UnmuteLayer(layer.identifier)
        removed_identifier = layer.identifier
        del self._layers[layer_key]
        del self._keys_by_identifier[removed_identifier]
        del self._labels[layer_key]
        self._order.remove(layer_key)
        self._refresh_order_cache()
        self._revision += 1
        self._install(detach_identifiers={removed_identifier})
        return layer

    def clear(self) -> None:
        """Clear authored opinions without changing topology or mute state."""
        for layer in self._layers.values():
            layer.Clear()

    def state(self) -> dict:
        """Return the portable strongest-to-weakest stack state."""
        return {
            "generation": self._generation,
            "revision": self._revision,
            "layers": [
                {
                    "layer_key": key,
                    "label": self._labels[key],
                    "muted": self._stage.IsLayerMuted(
                        self._layers[key].identifier,
                    ),
                }
                for key in self._order
            ],
        }

    def _managed_identifiers(self) -> set[str]:
        return {layer.identifier for layer in self._layers.values()}

    def _refresh_order_cache(self) -> None:
        self._order_view = tuple(self._order)
        self._ordered_layers = tuple(
            self._layers[layer_key] for layer_key in self._order
        )

    def _install(
        self,
        *,
        detach_identifiers: set[str] | None = None,
    ) -> None:
        session = self._stage.GetSessionLayer()
        managed_identifiers = self._managed_identifiers()
        if detach_identifiers:
            managed_identifiers.update(detach_identifiers)
        managed_paths = [
            self._layers[layer_key].identifier
            for layer_key in self._order
        ]
        replace_managed_sublayers(
            session,
            managed_paths,
            managed_identifiers,
        )


__all__ = ["CollaborationLayerStack"]
