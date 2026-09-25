"""Department assignments and replay compatibility for a managed layer stack."""

from __future__ import annotations

import logging
from collections.abc import Callable

from pxr import Sdf

from .scene import SceneState
from .types import ReplayModeConflictError

LOG = logging.getLogger(__name__)
_DEFAULT_LAYER_KEY = "default"
_DEPARTMENT_LAYER_KEY_PREFIX = "department:"


def _layer_key_for_department(department: str | None) -> str:
    if not department:
        return _DEFAULT_LAYER_KEY
    return f"{_DEPARTMENT_LAYER_KEY_PREFIX}{department}"


def department_for_layer_key(layer_key: str) -> str | None:
    """Return department policy metadata for one OpenUSDConnect layer key."""
    if layer_key == _DEFAULT_LAYER_KEY:
        return None
    if layer_key.startswith(_DEPARTMENT_LAYER_KEY_PREFIX):
        department = layer_key[len(_DEPARTMENT_LAYER_KEY_PREFIX) :]
        if department:
            return department
    return None


def label_for_layer_key(layer_key: str) -> str:
    department = department_for_layer_key(layer_key)
    if department:
        return department
    if layer_key == _DEFAULT_LAYER_KEY:
        return "Default"
    return layer_key


class CollaborationPolicy:
    """Own policy state; the scene owns USD layers and their composition.

    The scene lock protects assignments, replay admission, and layer lifecycle
    together. Mutation methods release it before publishing changes.
    """

    def __init__(
        self, scene: SceneState, *, department_priority: list[str] | None,
        bump_snapshot_epoch: Callable[[str], None],
        broadcast_layer_stack_state: Callable[[], None],
    ):
        self._scene = scene
        self._client_layer_keys: dict[str, str] = {}
        self._department_priority = self._validate_priority(department_priority or [])
        self._flat_receiver_count = 0
        self._bump_snapshot_epoch = bump_snapshot_epoch
        self._broadcast_layer_stack_state = broadcast_layer_stack_state

    @staticmethod
    def _validate_priority(departments: list[str]) -> list[str]:
        departments = list(departments)
        if any(not department for department in departments):
            raise ValueError("department names must be non-empty")
        if len(set(departments)) != len(departments):
            raise ValueError("department priority contains duplicates")
        return departments

    def restore_assignment(self, client_id: str, layer_key: str) -> None:
        """Restore routing from the durable log before workers start."""
        with self._scene.lock:
            self._client_layer_keys[client_id] = layer_key

    @property
    def department_priority(self) -> list[str]:
        with self._scene.lock:
            return list(self._department_priority)

    @property
    def flat_receiver_count(self) -> int:
        with self._scene.lock:
            return self._flat_receiver_count

    @property
    def client_layers(self) -> dict[str, Sdf.Layer]:
        """Return a snapshot of client assignments to shared collaboration layers."""
        with self._scene.lock:
            return {
                client_id: self._scene.layer_stack.layer_for(layer_key)
                for client_id, layer_key in self._client_layer_keys.items()
            }

    def get_or_create_client_layer(
        self,
        client_id: str,
        department: str | None = None,
    ) -> Sdf.Layer:
        """Get or create a layer for this client.

        With department: clients share a department layer (last-write-wins
        within the department, department priority controls strength).
        Without department: uses the shared edit_layer (weakest, last-write-wins).
        """
        layer_key = _layer_key_for_department(department)
        added = False
        with self._scene.lock:
            if department:
                if not self._scene.layer_stack.has_layer(layer_key):
                    self._reject_layer_stack_change_for_flat_receivers()
                layer, added = self._scene.layer_stack.ensure_layer(layer_key, label=department)
                if added:
                    self._apply_department_order_locked()
            else:
                layer = self._scene.edit_layer
            self._client_layer_keys[client_id] = layer_key
        if added:
            self._bump_snapshot_epoch(f"create_department_layer:{department}")
            self._broadcast_layer_stack_state()
            LOG.info("Created shared layer for department %s", department)
        return layer

    def _flat_replay_rejection_reason_locked(self) -> str:
        """Return why a new flat receiver cannot mirror this server."""
        if self._department_priority:
            return "department collaboration requires layered replay"
        if len(self._scene.layer_stack.layer_keys) != 1:
            return "multiple collaboration layers require layered replay"
        layer = self._scene.layer_stack.ordered_layers[0]
        if self._scene.stage.IsLayerMuted(layer.identifier):
            return "muted collaboration layers require layered replay"
        return ""

    def reserve_receiver_replay_mode(self, layered: bool) -> tuple[bool, str]:
        """Atomically admit a receiver under the current layer-stack contract."""
        if layered:
            return True, ""
        with self._scene.lock:
            reason = self._flat_replay_rejection_reason_locked()
            if reason:
                return False, reason
            self._flat_receiver_count += 1
        return True, ""

    def release_receiver_replay_mode(self, layered: bool) -> None:
        """Release a replay-mode reservation when a receiver disconnects."""
        if layered:
            return
        with self._scene.lock:
            if self._flat_receiver_count <= 0:
                raise RuntimeError("flat receiver reservation underflow")
            self._flat_receiver_count -= 1

    def _reject_layer_stack_change_for_flat_receivers(self) -> None:
        if self._flat_receiver_count:
            raise ReplayModeConflictError(
                "layer-stack changes require all connected receivers to use layered replay"
            )

    def apply_department_order(self) -> bool:
        """Restore configured ordering after materializing layers during startup."""
        with self._scene.lock:
            return self._apply_department_order_locked()

    def _apply_department_order_locked(self) -> bool:
        priority_keys = []
        for department in self._department_priority:
            layer_key = _layer_key_for_department(department)
            if self._scene.layer_stack.has_layer(layer_key):
                priority_keys.append(layer_key)
        priority_set = set(priority_keys)
        unlisted_keys = [
            layer_key
            for layer_key in self._scene.layer_stack.layer_keys
            if layer_key != _DEFAULT_LAYER_KEY and layer_key not in priority_set
        ]
        return self._scene.layer_stack.set_order(
            [*priority_keys, *unlisted_keys, _DEFAULT_LAYER_KEY]
        )

    def ordered_department_names(self) -> list[str]:
        """Return department policy entries in composed strength order."""
        with self._scene.lock:
            departments = []
            for layer_key in self._scene.layer_stack.layer_keys:
                department = department_for_layer_key(layer_key)
                if department:
                    departments.append(department)
            return departments

    def resolve_layer_key(self, key: str) -> str | None:
        """Resolve a client ID, department name, or layer key."""
        with self._scene.lock:
            client_key = self._client_layer_keys.get(key)
            if client_key:
                return client_key
            department_key = _layer_key_for_department(key)
            if self._scene.layer_stack.has_layer(department_key):
                return department_key
            if self._scene.layer_stack.has_layer(key):
                return key
            return None

    def resolve_layer(self, key: str) -> Sdf.Layer | None:
        """Resolve a layer by client ID, department name, or layer key."""
        with self._scene.lock:
            layer_key = self.resolve_layer_key(key)
            return self._scene.layer_stack.layer_for(layer_key) if layer_key else None

    def department_for_layer(self, layer: Sdf.Layer) -> str | None:
        """Return department policy metadata for a managed layer."""
        with self._scene.lock:
            layer_key = self._scene.layer_stack.key_for_layer(layer)
            return department_for_layer_key(layer_key) if layer_key is not None else None

    def merge_layer(self, client_id: str) -> bool:
        """Merge the client's department opinions into the root layer.

        Releases this client; the department layer remains while other clients
        use it. Existing root opinions on sibling prims are preserved.
        Returns False for clients on the shared edit_layer (no-op).
        """
        with self._scene.lock:
            layer_key = self._client_layer_keys.get(client_id)
            if not layer_key or layer_key == _DEFAULT_LAYER_KEY:
                return False
            layer = self._scene.layer_stack.layer_for(layer_key)
            self._scene.merge_layer_into_root(layer)
            removed = self._release_client_locked(client_id)
        self._bump_snapshot_epoch(f"merge_layer:{client_id}")
        if removed:
            self._broadcast_layer_stack_state()
        LOG.info("Merged department opinions and released client %s", client_id)
        return True

    def delete_layer(self, client_id: str) -> bool:
        """Release a client's department assignment.

        The department layer is discarded only when its last client leaves.

        Returns False for clients on the shared edit_layer (no-op).
        """
        with self._scene.lock:
            layer_key = self._client_layer_keys.get(client_id)
            if not layer_key or layer_key == _DEFAULT_LAYER_KEY:
                return False
            removed = self._release_client_locked(client_id)
        self._bump_snapshot_epoch(f"delete_layer:{client_id}")
        if removed:
            self._broadcast_layer_stack_state()
        LOG.info("Released department assignment for client %s", client_id)
        return True

    def _release_client_locked(self, client_id: str) -> bool:
        """Release a department client; return whether its layer was removed."""
        layer_key = self._client_layer_keys.pop(client_id)
        if layer_key in self._client_layer_keys.values():
            return False
        self._scene.layer_stack.remove_layer(layer_key)
        return True

    def set_department_priority(self, ordered_departments: list[str]) -> None:
        """Set department priority ordering (strongest first)."""
        ordered_departments = self._validate_priority(ordered_departments)
        with self._scene.lock:
            policy_changed = ordered_departments != self._department_priority
            if policy_changed:
                self._reject_layer_stack_change_for_flat_receivers()
            self._department_priority = ordered_departments
            order_changed = self._apply_department_order_locked()
        if not order_changed and not policy_changed:
            return
        self._bump_snapshot_epoch("set_department_priority")
        self._broadcast_layer_stack_state()

    def get_layer_stack_info(self) -> list[dict]:
        """Return ordered layer stack info for the dashboard.

        Department policy metadata is projected over the generic stack.
        Layers without clients or authored content stay out of the dashboard.
        """
        with self._scene.lock:
            muted = set(self._scene.stage.GetMutedLayers())
            layer_keys = self._scene.layer_stack.layer_keys
            layers = {
                layer_key: self._scene.layer_stack.layer_for(layer_key) for layer_key in layer_keys
            }
            labels = {
                layer_key: self._scene.layer_stack.label_for(layer_key) for layer_key in layer_keys
            }
            client_keys = dict(self._client_layer_keys)
            authored = {layer_key: not layers[layer_key].empty for layer_key in layer_keys}

        clients_by_key: dict[str, list[str]] = {}
        for client_id, layer_key in client_keys.items():
            clients_by_key.setdefault(layer_key, []).append(client_id)

        return [
            {
                "layer_key": layer_key,
                "label": labels[layer_key],
                "department": department_for_layer_key(layer_key),
                "clients": clients_by_key.get(layer_key, []),
                "identifier": layers[layer_key].identifier,
                "muted": layers[layer_key].identifier in muted,
                "shared": layer_key == _DEFAULT_LAYER_KEY,
            }
            for layer_key in layer_keys
            if clients_by_key.get(layer_key) or authored[layer_key]
        ]

    def set_muted(self, key: str, muted: bool) -> bool:
        """Change muting only when the admitted receivers can represent it."""
        with self._scene.lock:
            layer_key = self.resolve_layer_key(key)
            if layer_key is None:
                return False
            layer = self._scene.layer_stack.layer_for(layer_key)
            if self._scene.stage.IsLayerMuted(layer.identifier) == muted:
                return True
            self._reject_layer_stack_change_for_flat_receivers()
            self._scene.layer_stack.set_muted(layer_key, muted)
        action = "mute_layer" if muted else "unmute_layer"
        self._bump_snapshot_epoch(f"{action}:{key}")
        self._broadcast_layer_stack_state()
        return True
