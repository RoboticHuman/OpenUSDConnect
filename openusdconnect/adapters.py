"""DCCAdapter ABC and implementations.

DCCAdapter defines the contract any DCC integration must implement.
UsdStageAdapter applies events to a Usd.Stage (for headless/server consumers).
MockAdapter is a pure-Python dict-based mock for testing without pxr.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from .protocol import (
    K_DEACTIVATE_PRIM,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_SET_GPRIM_ATTRS,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_VARIANT_SELECTIONS,
    K_SET_VISIBILITY,
    K_UNLOAD_PAYLOAD,
)

LOG = logging.getLogger(__name__)


class DCCAdapter(ABC):
    """Abstract interface a DCC integration must implement."""

    @abstractmethod
    def ensure_prim(self, prim_path: str, type_name: str = "Xform") -> bool:
        raise NotImplementedError

    @abstractmethod
    def ensure_xform_ops(self, prim_path: str) -> bool:
        """Prepare the object/prim for TRS application.

        DCC implementations should normalize any parent-child transform
        offset so that setting local TRS values produces the expected
        local-to-parent transform.  **The reset must be world-preserving**:
        the object's world-space position/orientation must not change as a
        result of this call.  Implementations should compensate the local
        transform (e.g. matrix_basis) when clearing the offset.

        This ensures consistent behavior when objects switch between
        emitter and receiver roles and prevents axis-flip artefacts in
        Y-up ↔ Z-up coordinate conversion hierarchies.

        Examples:
        - Blender: new_basis = old_MPI @ old_basis, then MPI = Identity
        - Maya: bake offsetParentMatrix into local xform, then clear it
        - Houdini: fold pre-transform into main transform, then zero it
        """
        raise NotImplementedError

    @abstractmethod
    def set_xform_trs(self, prim_path: str, payload: dict) -> bool:
        raise NotImplementedError

    @abstractmethod
    def set_xform_matrices(self, prim_path: str, payload: dict) -> bool:
        raise NotImplementedError

    @abstractmethod
    def delete_prim(self, prim_path: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def deactivate_prim(self, prim_path: str, active: bool = False) -> bool:
        raise NotImplementedError

    @abstractmethod
    def rename_prim(self, prim_path: str, new_name: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def set_visibility(self, prim_path: str, visible: bool) -> bool:
        raise NotImplementedError

    @abstractmethod
    def set_gprim_attrs(self, prim_path: str, attrs: dict) -> bool:
        raise NotImplementedError

    @abstractmethod
    def set_reference(self, prim_path: str, refs: list) -> bool:
        raise NotImplementedError

    @abstractmethod
    def set_payload(self, prim_path: str, payloads: list) -> bool:
        raise NotImplementedError

    @abstractmethod
    def load_payload(self, prim_path: str) -> bool:
        """Load (import) a previously set payload arc."""
        ...

    @abstractmethod
    def unload_payload(self, prim_path: str) -> bool:
        """Unload a payload, removing its composed children."""
        ...

    @abstractmethod
    def set_variant_selections(self, prim_path: str, selections: dict[str, str]) -> bool:
        """Set variant selections on a prim."""
        raise NotImplementedError


class UsdStageAdapter(DCCAdapter):
    """Applies events to a Usd.Stage via event_apply functions.

    Suitable for headless receivers and server-side USD consumers.
    """

    def __init__(self, stage):
        from pxr import Usd

        if not isinstance(stage, Usd.Stage):
            raise TypeError("UsdStageAdapter requires a Usd.Stage")
        self.stage = stage

    def ensure_prim(self, prim_path: str, type_name: str = "Xform") -> bool:
        from .event_apply import get_or_define_prim

        get_or_define_prim(self.stage, prim_path, type_name)
        return True

    def ensure_xform_ops(self, prim_path: str) -> bool:
        from .event_apply import ensure_canonical_ops

        ensure_canonical_ops(self.stage, prim_path)
        return True

    def set_xform_trs(self, prim_path: str, payload: dict) -> bool:
        from .event_apply import apply_event

        apply_event(self.stage, payload)
        return True

    def set_xform_matrices(self, prim_path: str, payload: dict) -> bool:
        # Diagnostic only — no action needed on USD stage
        return True

    def delete_prim(self, prim_path: str) -> bool:
        self.stage.RemovePrim(prim_path)
        return True

    def deactivate_prim(self, prim_path: str, active: bool = False) -> bool:
        from .event_apply import apply_event

        apply_event(self.stage, {"k": K_DEACTIVATE_PRIM, "prim": prim_path, "active": active})
        return True

    def rename_prim(self, prim_path: str, new_name: str) -> bool:
        from .event_apply import apply_event

        apply_event(self.stage, {"k": K_RENAME_PRIM, "prim": prim_path, "new_name": new_name})
        return True

    def set_visibility(self, prim_path: str, visible: bool) -> bool:
        from .event_apply import apply_event

        apply_event(self.stage, {"k": K_SET_VISIBILITY, "prim": prim_path, "visible": visible})
        return True

    def set_gprim_attrs(self, prim_path: str, attrs: dict) -> bool:
        from .event_apply import apply_event

        apply_event(self.stage, {"k": K_SET_GPRIM_ATTRS, "prim": prim_path, "attrs": attrs})
        return True

    def set_reference(self, prim_path: str, refs: list) -> bool:
        from .event_apply import apply_event

        apply_event(self.stage, {"k": K_SET_REFERENCE, "prim": prim_path, "refs": refs})
        return True

    def set_payload(self, prim_path: str, payloads: list) -> bool:
        from .event_apply import apply_event

        apply_event(self.stage, {"k": K_SET_PAYLOAD, "prim": prim_path, "payloads": payloads})
        # Payloads are unloaded by default — users opt-in to load.
        if payloads:
            self.stage.Unload(prim_path)
        return True

    def load_payload(self, prim_path: str) -> bool:
        from .event_apply import apply_event

        apply_event(self.stage, {"k": K_LOAD_PAYLOAD, "prim": prim_path})
        return True

    def unload_payload(self, prim_path: str) -> bool:
        from .event_apply import apply_event

        apply_event(self.stage, {"k": K_UNLOAD_PAYLOAD, "prim": prim_path})
        return True

    def set_variant_selections(self, prim_path: str, selections: dict[str, str]) -> bool:
        from .event_apply import apply_event

        apply_event(
            self.stage,
            {"k": K_SET_VARIANT_SELECTIONS, "prim": prim_path, "selections": selections},
        )
        return True


class MockAdapter(DCCAdapter):
    """Pure-Python mock adapter for testing without pxr.

    Stores prim state in a dict. No external dependencies.
    """

    def __init__(self):
        self._prims: dict[str, dict] = {}
        self.calls: list[tuple] = []

    def ensure_prim(self, prim_path: str, type_name: str = "Xform") -> bool:
        if prim_path in self._prims:
            return True
        self._prims[prim_path] = {"typeName": type_name, "ops": set(), "trs": {}}
        LOG.info("MockAdapter: defined prim %s (%s)", prim_path, type_name)
        return True

    def ensure_xform_ops(self, prim_path: str) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            LOG.warning("MockAdapter: ensure_xform_ops prim missing %s", prim_path)
            return False
        p["ops"].update({"translate", "orient", "scale"})
        return True

    def set_xform_trs(self, prim_path: str, payload: dict) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            LOG.warning("MockAdapter: set_xform_trs prim missing %s", prim_path)
            return False
        fields = payload.get("fields", [])
        for f in fields:
            if f in payload:
                p["trs"][f] = payload[f]
        LOG.info("MockAdapter: applied TRS to %s fields=%s", prim_path, fields)
        return True

    def set_xform_matrices(self, prim_path: str, payload: dict) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            return False
        p["matrices"] = {"local": payload.get("local_m"), "world": payload.get("world_m")}
        return True

    def delete_prim(self, prim_path: str) -> bool:
        if prim_path in self._prims:
            del self._prims[prim_path]
            LOG.info("MockAdapter: deleted prim %s", prim_path)
            return True
        return False

    def deactivate_prim(self, prim_path: str, active: bool = False) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            return False
        p["active"] = active
        LOG.info("MockAdapter: set active=%s on prim %s", active, prim_path)
        return True

    def rename_prim(self, prim_path: str, new_name: str) -> bool:
        p = self._prims.pop(prim_path, None)
        if p is None:
            return False
        parent = prim_path.rsplit("/", 1)[0]
        new_path = f"{parent}/{new_name}"
        self._prims[new_path] = p
        LOG.info("MockAdapter: renamed %s -> %s", prim_path, new_path)
        return True

    def set_visibility(self, prim_path: str, visible: bool) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            return False
        p["visible"] = visible
        LOG.info("MockAdapter: set visible=%s on prim %s", visible, prim_path)
        return True

    def set_gprim_attrs(self, prim_path: str, attrs: dict) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            return False
        p.setdefault("gprim_attrs", {}).update(attrs)
        LOG.info("MockAdapter: set gprim attrs %s on prim %s", attrs, prim_path)
        return True

    def set_reference(self, prim_path: str, refs: list) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            self._prims[prim_path] = {"typeName": "Xform", "ops": set(), "trs": {}}
            p = self._prims[prim_path]
        p["references"] = list(refs)
        LOG.info("MockAdapter: set reference on prim %s", prim_path)
        return True

    def set_payload(self, prim_path: str, payloads: list) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            self._prims[prim_path] = {"typeName": "Xform", "ops": set(), "trs": {}}
            p = self._prims[prim_path]
        p["payloads"] = list(payloads)
        LOG.info("MockAdapter: set payload on prim %s", prim_path)
        return True

    def load_payload(self, prim_path: str) -> bool:
        self.calls.append(("load_payload", prim_path))
        return True

    def unload_payload(self, prim_path: str) -> bool:
        self.calls.append(("unload_payload", prim_path))
        return True

    def set_variant_selections(self, prim_path: str, selections: dict[str, str]) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            self._prims[prim_path] = {"typeName": "Xform", "ops": set(), "trs": {}}
            p = self._prims[prim_path]
        p["variant_selections"] = dict(selections)
        LOG.info("MockAdapter: set variant selections on prim %s", prim_path)
        return True

    def get_prim(self, prim_path: str) -> dict:
        """Test helper: get stored prim state."""
        return self._prims.get(prim_path, {})

    def get_trs(self, prim_path: str) -> dict:
        """Test helper: get stored TRS values for a prim."""
        p = self._prims.get(prim_path)
        if p is None:
            return {}
        return dict(p.get("trs", {}))
