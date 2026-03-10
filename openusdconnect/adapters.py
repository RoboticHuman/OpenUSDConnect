"""DCCAdapter ABC and implementations.

DCCAdapter defines the contract any DCC integration must implement.
UsdStageAdapter applies events to a Usd.Stage (for headless/server consumers).
MockAdapter is a pure-Python dict-based mock for testing without pxr.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Dict

LOG = logging.getLogger(__name__)


class DCCAdapter(ABC):
    """Abstract interface a DCC integration must implement."""

    @abstractmethod
    def ensure_prim(self, prim_path: str, type_name: str = "Xform") -> bool:
        raise NotImplementedError

    @abstractmethod
    def ensure_xform_ops(self, prim_path: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def set_xform_trs(self, prim_path: str, payload: Dict) -> bool:
        raise NotImplementedError

    @abstractmethod
    def set_xform_matrices(self, prim_path: str, payload: Dict) -> bool:
        raise NotImplementedError

    @abstractmethod
    def delete_prim(self, prim_path: str) -> bool:
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

    def set_xform_trs(self, prim_path: str, payload: Dict) -> bool:
        from .event_apply import apply_event
        apply_event(self.stage, payload)
        return True

    def set_xform_matrices(self, prim_path: str, payload: Dict) -> bool:
        # Diagnostic only — no action needed on USD stage
        return True

    def delete_prim(self, prim_path: str) -> bool:
        self.stage.RemovePrim(prim_path)
        return True


class MockAdapter(DCCAdapter):
    """Pure-Python mock adapter for testing without pxr.

    Stores prim state in a dict. No external dependencies.
    """

    def __init__(self):
        self._prims: Dict[str, dict] = {}

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

    def set_xform_trs(self, prim_path: str, payload: Dict) -> bool:
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

    def set_xform_matrices(self, prim_path: str, payload: Dict) -> bool:
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

    def get_prim(self, prim_path: str) -> dict:
        """Test helper: get stored prim state."""
        return self._prims.get(prim_path, {})

    def get_trs(self, prim_path: str) -> dict:
        """Test helper: get stored TRS values for a prim."""
        p = self._prims.get(prim_path)
        if p is None:
            return {}
        return dict(p.get("trs", {}))
