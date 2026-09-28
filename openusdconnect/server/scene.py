"""Live USD scene, edit targets, and caches owned by the authoritative server.

The scene lock protects stage access and its derived indexes. Event admission,
durable persistence, and network publication belong to the caller.
"""

from __future__ import annotations

import threading
from bisect import bisect_left
from collections.abc import Iterable
from contextlib import ExitStack, contextmanager
from itertools import groupby
from typing import TYPE_CHECKING

from pxr import Sdf, Usd

from ..protocol_constants import (
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_RENAME_PRIM,
    K_SET_INSTANCEABLE,
    K_SET_SDF_SPEC_FIELDS,
    NON_COLLABORATION_KINDS,
    STRUCTURAL_EVENT_KINDS,
    LayerMode,
)
from ..shared_layer_graph import SharedLayerGraph
from . import inspection
from .layer_stack import CollaborationLayerStack

if TYPE_CHECKING:
    from .snapshots import PreparedSnapshot


_PRIM_COUNT_CHANGE_KINDS = STRUCTURAL_EVENT_KINDS | {
    K_DELETE_PRIM, K_DEACTIVATE_PRIM, K_RENAME_PRIM,
}


def apply_with_deletion_fields(
    stage: Usd.Stage, layer: Sdf.Layer, events: list[dict], op_cache=None,
) -> None:
    """Complete deletion fields after preceding edits, then remove the spec.

    Removals are already application barriers. Split only there, preserving
    dependency ordering and the original event list for ordinary transactions.
    """
    from ..event_apply import apply_events

    if op_cache is None:
        op_cache = {}
    start = 0
    for index, event in enumerate(events):
        if event["k"] != K_SET_SDF_SPEC_FIELDS or not event["removed"]:
            continue
        if index > start:
            apply_events(stage, events[start:index], op_cache=op_cache, prevalidated=True)
        spec = layer.GetObjectAtPath(Sdf.Path(event["spec_path"]))
        if spec:
            event["fields"] = sorted(
                set(event["fields"]) | {str(key) for key in spec.ListInfoKeys()}
            )
        apply_events(stage, [event], op_cache=op_cache, prevalidated=True)
        start = index + 1
    if start < len(events):
        apply_events(
            stage, events if start == 0 else events[start:],
            op_cache=op_cache, prevalidated=True,
        )


class SceneState:
    def __init__(self, stage: Usd.Stage, *, layer_mode: LayerMode, op_cache_size: int):
        # Client-only packages import server types without installing its cache dependency.
        from cachetools import LRUCache

        self.stage = stage
        self.lock = threading.RLock()
        self.layer_mode = layer_mode
        self.edit_layer = self._create_edit_layer()
        self.layer_stack = CollaborationLayerStack(stage, self.edit_layer)
        self.shared_layer_graph: SharedLayerGraph | None = None
        self.op_cache = LRUCache(maxsize=op_cache_size)
        self._op_cache_layer: str | None = None
        self._prim_paths: dict[str, str] = {}
        self._instanceable_paths: set[str] = set()
        self._namespace_paths: tuple[str, ...] | None = None
        self._prim_count = 0
        self._prim_count_dirty = True
        self._prim_count_layer_revision = -1

    def clear_op_cache(self) -> None:
        """Discard cached attribute handles while excluding scene mutations."""
        with self.lock:
            self.op_cache.clear()
            self._op_cache_layer = None

    def invalidate_prim_count(self) -> None:
        with self.lock:
            self._prim_count_dirty = True

    def _create_edit_layer(self) -> Sdf.Layer:
        """Create an override sublayer on the session layer and set it as the edit target.

        The session layer is stronger than the entire root layer stack, so
        opinions authored here always compose on top of the base file and
        its sublayers.  The override is inserted as a sublayer of the session
        layer (rather than using the session layer directly) so that
        collaboration can add shared department layers alongside it.
        """
        layer = Sdf.Layer.CreateAnonymous("server-edits")
        session = self.stage.GetSessionLayer()
        session.subLayerPaths.insert(0, layer.identifier)
        self.stage.SetEditTarget(Usd.EditTarget(layer))
        return layer

    def _op_cache_for(self, layer: Sdf.Layer):
        """Reuse op setup only for consecutive edits to the same layer.

        XformOp.Set uses the stage's current edit target; the op is not bound
        to a layer. This cache also skips setup of that layer's xformOpOrder,
        which must run again after switching layers.

        The caller holds the scene lock through use of the returned cache.
        """
        if self._op_cache_layer != layer.identifier:
            self.op_cache.clear()
            self._op_cache_layer = layer.identifier
        return self.op_cache

    def _tracked_subtree(self, prim: str) -> Iterable[str]:
        """Find tracked descendants without scanning the whole tree per deletion."""
        if self._namespace_paths is None:
            self._namespace_paths = tuple(sorted(
                self._prim_paths.keys() | self._instanceable_paths,
            ))
        paths = self._namespace_paths
        yield prim
        prefix = prim + "/"
        index = bisect_left(paths, prefix)
        while index < len(paths) and paths[index].startswith(prefix):
            yield paths[index]
            index += 1

    def _track_prim_event(self, ev: dict):
        """Update incremental prim trackers from a single event.

        This is the specialized-event tree, not a traversal of base or referenced
        content. The dashboard can refresh it without querying pxr.
        The caller holds the scene lock.
        """
        k = ev.get("k")
        prim = ev.get("prim", "")
        if k == K_ENSURE_PRIM:
            self._prim_paths[prim] = ev["typeName"]
            self._namespace_paths = None
        elif k in (K_DELETE_PRIM, K_RENAME_PRIM):
            new_root = (
                f"{prim.rsplit('/', 1)[0]}/{ev['new_name']}" if k == K_RENAME_PRIM else None
            )
            for path in self._tracked_subtree(prim):
                type_name = self._prim_paths.pop(path, None)
                was_instanceable = path in self._instanceable_paths
                self._instanceable_paths.discard(path)
                if new_root is not None:
                    if type_name is not None:
                        self._prim_paths[new_root + path[len(prim):]] = type_name
                    if was_instanceable:
                        self._instanceable_paths.add(new_root + path[len(prim):])
            if new_root is not None:
                self._prim_paths.setdefault(new_root, "Xform")
                self._namespace_paths = None
            # Deleted entries can stay in the index until the next insertion;
            # subsequent deletes skip them without rebuilding a large tree.
        elif k == K_SET_INSTANCEABLE:
            if ev.get("instanceable", True):
                self._instanceable_paths.add(prim)
                self._namespace_paths = None
            else:
                self._instanceable_paths.discard(prim)

    def update_prim_tracking(self, events: list[dict]) -> None:
        """Publish dashboard indexes for successfully applied events."""
        with self.lock:
            for event in events:
                kind = event.get("k")
                if kind in _PRIM_COUNT_CHANGE_KINDS:
                    self._prim_count_dirty = True
                if kind in (K_ENSURE_PRIM, K_DELETE_PRIM, K_RENAME_PRIM, K_SET_INSTANCEABLE):
                    self._track_prim_event(event)

    def rebuild_caches(self, events: Iterable[dict] = ()) -> None:
        """Discard cached USD handles and rebuild indexes from replacement history."""
        with self.lock:
            self.clear_op_cache()
            self._prim_paths.clear()
            self._instanceable_paths.clear()
            self._namespace_paths = None
            for event in events:
                self._track_prim_event(event)
            self._prim_count_dirty = True

    @contextmanager
    def atomic_edit(
        self,
        layer_paths: Iterable[tuple[Sdf.Layer, set[str]]],
        *,
        include_session: bool = False,
    ):
        """Roll back touched opinions if mutation or persistence fails.

        Apply with update_tracking=False until persistence succeeds, then
        publish derived indexes before leaving this scope. Only USD opinions
        roll back. The scene restores the edit target and invalidates cached
        USD handles after rollback.
        """
        from ..event_apply import atomic_apply_layer

        with self.lock:
            original_target = self.stage.GetEditTarget()
            try:
                with ExitStack() as rollback:
                    for layer, paths in layer_paths:
                        rollback.enter_context(atomic_apply_layer(layer, paths))
                    if include_session:
                        rollback.enter_context(atomic_apply_layer(self.stage.GetSessionLayer()))
                    yield
            except Exception:
                self.clear_op_cache()
                raise
            finally:
                self.stage.SetEditTarget(original_target)

    def replay_events(self, routed_events: Iterable[tuple[Sdf.Layer, dict]]) -> None:
        """Restore persisted opinions in order, batching adjacent layer targets."""
        from ..event_apply import apply_events

        with self.lock:
            try:
                for layer, run in groupby(routed_events, key=lambda item: item[0]):
                    events = [event for _layer, event in run]
                    with Usd.EditContext(self.stage, Usd.EditTarget(layer)):
                        apply_events(self.stage, events, op_cache=self._op_cache_for(layer))
                    self.update_prim_tracking(events)
            except Exception:
                self.clear_op_cache()
                raise
            finally:
                self._prim_count_dirty = True

    def snapshot_layer(self, layer: Sdf.Layer) -> Sdf.Layer:
        """Copy authored content under the scene lock for serialization outside it."""
        with self.lock:
            snapshot = Sdf.Layer.CreateAnonymous("server-export.usda")
            snapshot.TransferContent(layer)
            return snapshot

    def clear_collaboration_layers(self) -> None:
        with self.lock:
            self.layer_stack.clear()
            self.rebuild_caches()

    def merge_layer_into_root(self, layer: Sdf.Layer) -> None:
        """Copy leaf opinions while preserving existing root siblings.

        Hold the scene lock for both source traversal and destination writes;
        reading an Sdf layer also needs exclusion from concurrent mutation.
        """
        with self.lock:
            pending = list(reversed(list(layer.rootPrims)))
            root = self.stage.GetRootLayer()
            while pending:
                spec = pending.pop()
                if spec.nameChildren:
                    pending.extend(reversed(list(spec.nameChildren)))
                    continue
                path = spec.path
                parent = path.GetParentPath()
                if parent != Sdf.Path.absoluteRootPath and not root.GetPrimAtPath(parent):
                    Sdf.CreatePrimInLayer(root, parent)
                if root.GetPrimAtPath(path):
                    for prop in spec.properties:
                        Sdf.CopySpec(layer, prop.path, root, prop.path)
                else:
                    Sdf.CopySpec(layer, path, root, path)
            self._prim_count_dirty = True

    def install_snapshot(self, prepared: PreparedSnapshot) -> None:
        """Install a prepared snapshot after its history has been persisted."""
        from ..event_apply import apply_events

        with self.lock:
            self.edit_layer.TransferContent(prepared.edit_layer)
            edit_target = Usd.EditTarget(self.edit_layer)
            self.stage.SetEditTarget(edit_target)
            if prepared.session_events:
                self.stage.SetEditTarget(Usd.EditTarget(self.stage.GetSessionLayer()))
                try:
                    apply_events(self.stage, prepared.session_events, prevalidated=True)
                finally:
                    self.stage.SetEditTarget(edit_target)
            self.rebuild_caches(prepared.events)

    def apply_validated(
        self,
        events: list[dict],
        layer: Sdf.Layer | None = None,
        *,
        update_tracking: bool = True,
    ) -> None:
        """Complete deletion fields and apply admitted events under the scene lock.

        Spec deletions acquire field names from the current scene, including
        preceding edits in this request. Encode those events only after this method returns.
        """
        target = layer or self.edit_layer

        with self.lock:
            edit_target = Usd.EditTarget(target)
            target_was_muted = self.stage.IsLayerMuted(target.identifier)
            needs_unmute = target_was_muted and any(
                ev["k"] not in NON_COLLABORATION_KINDS for ev in events
            )
            post_apply_target = (
                Usd.EditTarget(self.stage.GetSessionLayer()) if target_was_muted else edit_target
            )
            if needs_unmute:
                self.stage.UnmuteLayer(target.identifier)
            try:
                if any(event["k"] in NON_COLLABORATION_KINDS for event in events):
                    self._apply_routed_runs(events, target, edit_target)
                else:
                    # Most transactions author one collaboration layer. Avoid
                    # session targets and copied runs when no routing is needed.
                    self.stage.SetEditTarget(edit_target)
                    apply_with_deletion_fields(
                        self.stage, target, events, op_cache=self._op_cache_for(target),
                    )
            finally:
                self.stage.SetEditTarget(post_apply_target)
                if needs_unmute:
                    self.stage.MuteLayer(target.identifier)

            if update_tracking:
                self.update_prim_tracking(events)

    def _apply_routed_runs(
        self, events: list[dict], target: Sdf.Layer, edit_target: Usd.EditTarget,
    ) -> None:
        """Route consecutive runs without reordering session and layer opinions."""
        session_layer = self.stage.GetSessionLayer()
        session_target = Usd.EditTarget(session_layer)
        for session_events, run in groupby(
            events, key=lambda event: event["k"] in NON_COLLABORATION_KINDS,
        ):
            run_layer = session_layer if session_events else target
            self.stage.SetEditTarget(session_target if session_events else edit_target)
            apply_with_deletion_fields(
                self.stage, run_layer, list(run), op_cache=self._op_cache_for(run_layer),
            )

    def get_prim_count(self) -> int:
        """Return the number of prims on the composed stage (thread-safe, cached)."""
        with self.lock:
            if (
                self._prim_count_dirty
                or self._prim_count_layer_revision != self.layer_stack.revision
            ):
                self._prim_count = sum(1 for _ in self.stage.Traverse())
                self._prim_count_dirty = False
                self._prim_count_layer_revision = self.layer_stack.revision
            return self._prim_count

    def get_tracked_prim_count(self) -> int:
        """Return the number of prims tracked (incremental, no log scan)."""
        with self.lock:
            return len(self._prim_paths)

    def get_prim_tree(self) -> list[dict]:
        """Build inspector tree rows from snapshots of the incremental indexes."""
        with self.lock:
            prims = dict(self._prim_paths)
            instanceable_paths = set(self._instanceable_paths)
        return inspection.build_prim_tree(
            prims,
            instanceable_paths=instanceable_paths,
        )

    def get_instance_count(self) -> int:
        """Count of prims with authored ``instanceable=true``.

        Whether each actually composes into an instance depends on a
        composition arc; this is the upper bound and matches what the
        tree's badge shows.
        """
        with self.lock:
            return len(self._instanceable_paths)

    def get_prototype_count(self) -> int:
        """Number of implicit prototype prims the stage composed."""
        with self.lock:
            return len(self.stage.GetPrototypes())

    def get_prim_detail(self, path: str) -> dict:
        """Return composed prim details while holding the stage lock."""
        with self.lock:
            return inspection.read_prim_detail(self.stage, path)

    def get_transforms_snapshot(self) -> list[dict]:
        """Return composed TRS rows while holding the stage lock."""
        with self.lock:
            return inspection.read_transforms(self.stage)
