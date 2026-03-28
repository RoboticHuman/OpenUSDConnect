"""PrimRegistry — DCC-agnostic prim path to native object mapping.

Tracks the correspondence between USD prim paths and DCC-native objects
(Blender Objects, Maya DAG nodes, etc.).  Handles:

- One-to-one mapping for standalone prims
- Alias paths for merged reference roots (one object, multiple paths)
- Composition-aware tracking (reference children vs standalone prims)
- Separate shader metadata cache (avoids polluting object lookups)
"""

from __future__ import annotations

import logging
from typing import Any

LOG = logging.getLogger(__name__)


class PrimRegistry:
    """Map USD prim paths to DCC-native objects with composition awareness.

    Parameters
    ----------
    scan_fn : callable, optional
        Called on cache miss to scan the DCC scene for an object matching
        a prim path.  Signature: ``scan_fn(prim_path) -> object | None``.
        If not provided, cache misses return None without scanning.
    """

    def __init__(self, *, scan_fn=None):
        self._objects: dict[str, Any] = {}          # prim_path -> DCC object
        self._shaders: dict[str, dict] = {}          # prim_path -> {shader_id, input_map, output_map}
        self._ref_children: set[str] = set()         # paths owned by reference imports
        self._imported_refs: dict[str, tuple] = {}   # prim_path -> (asset_path, prim_path_ref)
        self._scan_fn = scan_fn

    # ------------------------------------------------------------------
    # Object mapping
    # ------------------------------------------------------------------

    def find(self, prim_path: str) -> Any | None:
        """Look up the DCC object for a prim path.

        Returns the cached object if alive, falls back to ``scan_fn``
        on cache miss, or returns None.
        """
        obj = self._objects.get(prim_path)
        if obj is not None:
            if self._is_alive(obj):
                return obj
            del self._objects[prim_path]
        if self._scan_fn is not None:
            obj = self._scan_fn(prim_path)
            if obj is not None:
                self._objects[prim_path] = obj
            return obj
        return None

    def register(self, prim_path: str, obj) -> None:
        """Register a DCC object at a prim path."""
        self._objects[prim_path] = obj

    def register_alias(self, alias_path: str, obj) -> None:
        """Register an additional path that resolves to the same object.

        Used after reference merge: the imported root's original composed
        path (e.g. ``/World/Teapot/teapot_MeshShape``) aliases the merged
        container path (e.g. ``/World/Teapot``).
        """
        self._objects[alias_path] = obj

    def unregister(self, prim_path: str) -> None:
        """Remove a prim path from the registry."""
        self._objects.pop(prim_path, None)

    def rename(self, old_path: str, new_path: str) -> None:
        """Move a registration from one path to another."""
        obj = self._objects.pop(old_path, None)
        if obj is not None:
            self._objects[new_path] = obj

    def contains(self, prim_path: str) -> bool:
        """Check if a path is registered (without liveness probe)."""
        return prim_path in self._objects

    def children_exist(self, prim_path: str) -> bool:
        """Check if any child paths are registered under a prefix.

        Only checks the object registry (not shader cache), so shader
        metadata keys don't cause false positives.
        """
        prefix = prim_path + "/"
        return any(pp.startswith(prefix) for pp in self._objects)

    def pop_children(self, prim_path: str) -> set[str]:
        """Remove and return all child paths under a prefix."""
        prefix = prim_path + "/"
        children = {pp for pp in self._objects if pp.startswith(prefix)}
        for pp in children:
            self._objects.pop(pp)
            self._ref_children.discard(pp)
        return children

    # ------------------------------------------------------------------
    # Composition tracking
    # ------------------------------------------------------------------

    def mark_reference_children(self, child_paths: set[str]) -> None:
        """Record paths that are composed children of a reference import."""
        self._ref_children.update(child_paths)

    def is_reference_child(self, prim_path: str) -> bool:
        """Check if a path is a composed child of a reference import."""
        return prim_path in self._ref_children

    # ------------------------------------------------------------------
    # Reference import tracking
    # ------------------------------------------------------------------

    def set_imported_ref(self, prim_path: str, asset: str, prim_ref: str) -> None:
        """Record which asset was imported at a reference prim path."""
        self._imported_refs[prim_path] = (asset, prim_ref)

    def get_imported_ref(self, prim_path: str) -> tuple | None:
        """Get the (asset_path, prim_path_ref) for a reference import."""
        return self._imported_refs.get(prim_path)

    def clear_imported_ref(self, prim_path: str) -> None:
        """Remove the imported ref record for a prim path."""
        self._imported_refs.pop(prim_path, None)

    # ------------------------------------------------------------------
    # Shader metadata (separate from object cache)
    # ------------------------------------------------------------------

    def set_shader(self, prim_path: str, **kwargs) -> None:
        """Store shader metadata (shader_id, input_map, output_map)."""
        self._shaders.setdefault(prim_path, {}).update(kwargs)

    def get_shader(self, prim_path: str) -> dict:
        """Get shader metadata dict (empty dict if not found)."""
        return self._shaders.get(prim_path, {})

    def iter_shaders(self):
        """Iterate over (prim_path, metadata_dict) for all cached shaders."""
        return self._shaders.items()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _is_alive(obj) -> bool:
        """Check if a DCC object reference is still valid.

        Override in subclass if liveness check differs per DCC.
        Default: probe by accessing .name (works for Blender).
        """
        try:
            obj.name  # noqa: B018
            return True
        except (ReferenceError, AttributeError):
            return False
