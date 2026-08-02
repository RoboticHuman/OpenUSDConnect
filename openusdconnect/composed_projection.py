"""Project a layered USD mirror's composed state into a native DCC adapter."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import cast

from pxr import Sdf, Usd, UsdGeom, UsdShade

from .protocol_constants import (
    EVENT_KEYS,
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_SET_CONNECTABLE_CONNECTION,
    K_SET_CONNECTABLE_INPUT,
    K_SET_GPRIM_ATTRS,
    K_SET_INSTANCEABLE,
    K_SET_MATERIAL_BINDING,
    K_SET_PAYLOAD,
    K_SET_POINT_INSTANCER,
    K_SET_REFERENCE,
    K_SET_SDF_SPEC_FIELDS,
    K_SET_VARIANT_SELECTIONS,
    K_SET_VISIBILITY,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
    NATIVE_DIRECT_KINDS,
    NATIVE_FIELD_ROUTED_KINDS,
    NATIVE_PROJECTED_KINDS,
)
from .usd_state import (
    POINT_INSTANCER_USD_TO_WIRE,
    attribute_event_metadata,
    connectable_kind,
    connected_source_attr,
    point_instancer_value_to_wire,
    read_material_binding,
    read_point_instancer,
    read_usdshade_connectable,
    read_variant_selections,
    usd_value_to_python,
    values_equal,
)
from .xform_decompose import as_matrix, decompose_trs_from_matrix

_COMPOSITION_DIRECT_KINDS = frozenset({K_LOAD_PAYLOAD, K_UNLOAD_PAYLOAD})
_TRAILING_DIRECT_KINDS = NATIVE_DIRECT_KINDS - _COMPOSITION_DIRECT_KINDS


_PrimTime = tuple[str, float | None]


@dataclass
class _ProjectionCandidates:
    """Adapter-representable state that one transaction may affect."""

    prims: set[str] = dataclass_field(default_factory=set)
    types: set[str] = dataclass_field(default_factory=set)
    xforms: set[_PrimTime] = dataclass_field(default_factory=set)
    gprim: dict[_PrimTime, set[str]] = dataclass_field(default_factory=lambda: defaultdict(set))
    variants: set[str] = dataclass_field(default_factory=set)
    materials: set[tuple[str, str]] = dataclass_field(default_factory=set)
    connectables: dict[_PrimTime, set[str]] = dataclass_field(
        default_factory=lambda: defaultdict(set)
    )
    active: set[str] = dataclass_field(default_factory=set)
    visibility: set[_PrimTime] = dataclass_field(default_factory=set)
    instanceable: set[str] = dataclass_field(default_factory=set)
    point_instancers: dict[_PrimTime, set[str]] = dataclass_field(
        default_factory=lambda: defaultdict(set)
    )
    arcs: set[tuple[str, str]] = dataclass_field(default_factory=set)


@dataclass(frozen=True)
class _ProjectionBaseline:
    """Selected composed state captured immediately before a transaction."""

    valid: dict[str, bool]
    types: dict[str, tuple[str, tuple[str, ...]] | None]
    xforms: dict[_PrimTime, object | None]
    gprim: dict[_PrimTime, dict[str, object]]
    variants: dict[str, dict[str, str]]
    materials: dict[tuple[str, str], str]
    connectables: dict[_PrimTime, dict]
    active: dict[str, bool | None]
    visibility: dict[_PrimTime, bool | None]
    instanceable: dict[str, bool | None]
    point_instancers: dict[_PrimTime, dict[str, object]]
    arcs: dict[tuple[str, str], list[dict]]


@dataclass(frozen=True)
class _ProjectionStep:
    """One ordered adapter projection phase and the event kinds it owns."""

    name: str
    method_name: str
    event_kinds: frozenset[str] = frozenset()
    candidate_name: str | None = None
    collector_name: str | None = None


# Order follows adapter dependencies: namespace state precedes authored values,
# and direct events run after projected composed state. A new event kind must be
# classified in EVENT_KIND_INFO and owned by exactly one projection step.
_PROJECTION_STEPS = (
    _ProjectionStep(
        "namespace and lifecycle",
        "_project_namespace_and_lifecycle",
        event_kinds=frozenset({K_ENSURE_PRIM, K_DELETE_PRIM, K_RENAME_PRIM}),
        candidate_name="types",
        collector_name="_collect_type_candidates",
    ),
    _ProjectionStep(
        "composition arcs",
        "_project_arcs",
        event_kinds=frozenset({K_SET_REFERENCE, K_SET_PAYLOAD}),
        candidate_name="arcs",
        collector_name="_collect_arc_candidates",
    ),
    _ProjectionStep("payload load state", "_project_payload_load_state"),
    _ProjectionStep(
        "variant selections",
        "_project_variants",
        event_kinds=frozenset({K_SET_VARIANT_SELECTIONS}),
        candidate_name="variants",
        collector_name="_collect_variant_candidates",
    ),
    _ProjectionStep(
        "transforms",
        "_project_xforms",
        event_kinds=frozenset({K_ENSURE_XFORM_OPS, K_SET_XFORM_TRS}),
        candidate_name="xforms",
        collector_name="_collect_xform_candidates",
    ),
    _ProjectionStep(
        "geometry attributes",
        "_project_gprim_attrs",
        event_kinds=frozenset({K_SET_GPRIM_ATTRS}),
        candidate_name="gprim",
        collector_name="_collect_gprim_candidates",
    ),
    _ProjectionStep(
        "point instancers",
        "_project_point_instancers",
        event_kinds=frozenset({K_SET_POINT_INSTANCER}),
        candidate_name="point_instancers",
        collector_name="_collect_point_instancer_candidates",
    ),
    _ProjectionStep(
        "material bindings",
        "_project_materials",
        event_kinds=frozenset({K_SET_MATERIAL_BINDING}),
        candidate_name="materials",
        collector_name="_collect_material_candidates",
    ),
    _ProjectionStep(
        "connectable values and edges",
        "_project_connectables",
        event_kinds=frozenset({K_SET_CONNECTABLE_INPUT, K_SET_CONNECTABLE_CONNECTION}),
        candidate_name="connectables",
        collector_name="_collect_connectable_candidates",
    ),
    _ProjectionStep(
        "active state",
        "_project_active",
        event_kinds=frozenset({K_DEACTIVATE_PRIM}),
        candidate_name="active",
        collector_name="_collect_active_candidates",
    ),
    _ProjectionStep(
        "visibility",
        "_project_visibility",
        event_kinds=frozenset({K_SET_VISIBILITY}),
        candidate_name="visibility",
        collector_name="_collect_visibility_candidates",
    ),
    _ProjectionStep(
        "instancing state",
        "_project_instanceable",
        event_kinds=frozenset({K_SET_INSTANCEABLE}),
        candidate_name="instanceable",
        collector_name="_collect_instanceable_candidates",
    ),
    _ProjectionStep("direct events", "_direct_events"),
)

_PROJECTOR_EVENT_KINDS = frozenset().union(*(step.event_kinds for step in _PROJECTION_STEPS))
_PROJECTOR_EVENT_OWNERS = Counter(
    kind for step in _PROJECTION_STEPS for kind in step.event_kinds
)

if any(
    (step.candidate_name is None) != (step.collector_name is None) for step in _PROJECTION_STEPS
):
    raise RuntimeError("native projection steps must pair candidate and collector names")
if duplicate_kinds := sorted(
    kind for kind, owner_count in _PROJECTOR_EVENT_OWNERS.items() if owner_count != 1
):
    raise RuntimeError(f"native projection event kinds have multiple owners: {duplicate_kinds}")
if _PROJECTOR_EVENT_KINDS != NATIVE_PROJECTED_KINDS:
    missing = sorted(NATIVE_PROJECTED_KINDS - _PROJECTOR_EVENT_KINDS)
    stale = sorted(_PROJECTOR_EVENT_KINDS - NATIVE_PROJECTED_KINDS)
    raise RuntimeError(
        f"native projection event registry is out of sync (missing={missing}, stale={stale})"
    )
if NATIVE_FIELD_ROUTED_KINDS != {K_SET_SDF_SPEC_FIELDS}:
    raise RuntimeError("native field-routed projection requires an explicit Sdf event router")
if _COMPOSITION_DIRECT_KINDS | _TRAILING_DIRECT_KINDS != NATIVE_DIRECT_KINDS:
    raise RuntimeError("native direct event registry is incomplete")


def _sdf_property_path(event: dict) -> Sdf.Path | None:
    if event.get("k") != K_SET_SDF_SPEC_FIELDS:
        return None
    value = event.get("spec_path")
    if not value:
        return None
    path = Sdf.Path(value)
    return path if path.IsPropertyPath() else None


def _is_xform_spec_event(event: dict) -> bool:
    path = _sdf_property_path(event)
    if path is None:
        return False
    name = str(path.name)
    return name == "xformOpOrder" or name.startswith("xformOp:")


def _is_gprim_property_name(name: str) -> bool:
    return (
        name != "xformOpOrder"
        and not name.startswith("xformOp:")
        and name != "visibility"
        and name != "info:id"
        and not name.startswith(("inputs:", "outputs:"))
    )


def _is_gprim_spec_event(event: dict) -> bool:
    path = _sdf_property_path(event)
    if path is None or event.get("spec_kind") != "attribute":
        return False
    name = str(path.name)
    return _is_gprim_property_name(name)


def _is_connectable_spec_event(event: dict) -> bool:
    path = _sdf_property_path(event)
    if path is None or event.get("spec_kind") != "attribute":
        return False
    name = str(path.name)
    return name == "info:id" or name.startswith(("inputs:", "outputs:"))


def _is_variant_spec_event(event: dict) -> bool:
    return (
        event.get("k") == K_SET_SDF_SPEC_FIELDS
        and event.get("spec_kind") == "prim"
        and "variantSelection" in event.get("fields", ())
    )


def _is_active_spec_event(event: dict) -> bool:
    return (
        event.get("k") == K_SET_SDF_SPEC_FIELDS
        and event.get("spec_kind") == "prim"
        and "active" in event.get("fields", ())
    )


def _is_instanceable_spec_event(event: dict) -> bool:
    return (
        event.get("k") == K_SET_SDF_SPEC_FIELDS
        and event.get("spec_kind") == "prim"
        and "instanceable" in event.get("fields", ())
    )


def _material_purpose_from_name(name: str) -> str | None:
    if name == "material:binding":
        return ""
    prefix = "material:binding:"
    return name[len(prefix) :] if name.startswith(prefix) else None


def _material_purpose(event: dict) -> str | None:
    if event.get("k") == K_SET_MATERIAL_BINDING:
        return str(event.get("material_purpose") or "")
    path = _sdf_property_path(event)
    if path is None or event.get("spec_kind") != "relationship":
        return None
    return _material_purpose_from_name(str(path.name))


def _is_material_event(event: dict) -> bool:
    return _material_purpose(event) is not None


def _time_code(value: float | None) -> Usd.TimeCode:
    return Usd.TimeCode.Default() if value is None else Usd.TimeCode(value)


def _rename_target(event: dict) -> str:
    parent = str(event["prim"]).rsplit("/", 1)[0]
    return f"{parent}/{event['new_name']}" if parent else f"/{event['new_name']}"


def _local_transforms(
    stage: Usd.Stage,
    candidates: Iterable[tuple[str, float | None]],
) -> dict[tuple[str, float | None], object | None]:
    result: dict[tuple[str, float | None], object | None] = {}
    for prim_path, time in candidates:
        prim = stage.GetPrimAtPath(prim_path)
        xformable = UsdGeom.Xformable(prim) if prim and prim.IsValid() else None
        result[(prim_path, time)] = (
            as_matrix(xformable.GetLocalTransformation(_time_code(time))) if xformable else None
        )
    return result


def _gprim_values(
    stage: Usd.Stage,
    candidates: dict[tuple[str, float | None], set[str]],
) -> dict[tuple[str, float | None], dict[str, object]]:
    result = {}
    for (prim_path, time), names in candidates.items():
        prim = stage.GetPrimAtPath(prim_path)
        values = {}
        if prim and prim.IsValid():
            time_code = _time_code(time)
            for name in names:
                attr = prim.GetAttribute(name)
                if not attr or not attr.IsValid():
                    continue
                value = attr.Get(time_code)
                if value is not None:
                    values[name] = value
        result[(prim_path, time)] = values
    return result


def _composed_arc_entries(
    stage: Usd.Stage,
    prim_path: str,
    *,
    payload: bool,
) -> list[dict]:
    from .sdf_arc_state import serialize_reference_custom_data

    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return []
    query = Usd.PrimCompositionQuery.GetDirectRootLayerArcs(prim)
    query_filter = query.filter
    query_filter.arcTypeFilter = (
        Usd.PrimCompositionQuery.ArcTypeFilter.Payload
        if payload
        else Usd.PrimCompositionQuery.ArcTypeFilter.Reference
    )
    query.filter = query_filter

    result = []
    for arc in query.GetCompositionArcs():
        _editor, item = arc.GetIntroducingListEditor()
        if item is None:
            continue
        entry = {}
        target_layer = arc.GetTargetLayer()
        asset_path = str(target_layer.resolvedPath or target_layer.realPath or item.assetPath)
        if asset_path:
            entry["asset_path"] = asset_path.replace("\\", "/")
        if not item.primPath.isEmpty:
            entry["prim_path"] = str(item.primPath)
        if item.layerOffset.offset != 0.0:
            entry["layer_offset"] = item.layerOffset.offset
        if item.layerOffset.scale != 1.0:
            entry["layer_scale"] = item.layerOffset.scale
        if not payload and item.customData:
            entry["custom_data_fragment"] = serialize_reference_custom_data(
                item.customData,
            )
        result.append(entry)
    return result


class ComposedChangeProjection:
    """Build native adapter events from a layered mirror transaction.

    Construction captures only values touched by the transaction. After the
    mirror commits, ``build_events`` emits specialized events whose composed
    result changed. ``reapply_composed_paths`` restores native state after a
    masked edit from the same bidirectional client.
    """

    def __init__(
        self,
        stage: Usd.Stage,
        events: list[dict],
        *,
        extra_scene_paths: Iterable[str | Sdf.Path] = (),
        extra_arc_candidates: Iterable[tuple[str, str]] = (),
        reapply_all_composed: bool = False,
        reapply_composed_paths: Iterable[str] = (),
        reset: bool = False,
    ):
        self._stage = stage
        self._events = events
        self._validate_events(events)
        self._reset = reset
        self._prepare_reapplication_scope(
            reapply_all_composed=reapply_all_composed,
            reapply_composed_paths=reapply_composed_paths,
        )
        initial_prim_paths, initial_scene_paths = self._prepare_scene_scope(
            extra_scene_paths,
        )
        self._candidates = _ProjectionCandidates()
        self._collect_initial_candidates(
            initial_prim_paths,
            initial_scene_paths,
            extra_arc_candidates,
        )
        self._before = self._capture_baseline()

    @staticmethod
    def _validate_events(events: list[dict]) -> None:
        unknown_kinds = {event.get("k") for event in events} - EVENT_KEYS
        if unknown_kinds:
            raise ValueError(
                "unknown event kinds in native projection: "
                + ", ".join(sorted(repr(kind) for kind in unknown_kinds))
            )

    def _prepare_reapplication_scope(
        self,
        *,
        reapply_all_composed: bool,
        reapply_composed_paths: Iterable[str],
    ) -> None:
        events = self._events
        self._reapply_all_composed = reapply_all_composed or self._reset
        self._rename_pairs = [
            (str(event["prim"]), _rename_target(event), event)
            for event in events
            if event.get("k") == K_RENAME_PRIM and event.get("prim") and event.get("new_name")
        ]
        self._reapply_composed_paths = {str(path) for path in reapply_composed_paths if path}
        self._reapply_composed_paths.update(
            new_path
            for old_path, new_path, _event in self._rename_pairs
            if old_path in self._reapply_composed_paths
        )
        self._subtree_roots = {
            str(event["prim"])
            for event in events
            if event.get("k") == K_DELETE_PRIM and event.get("prim")
        }
        self._subtree_roots.update(path for pair in self._rename_pairs for path in pair[:2])
        self._local_lifecycle_paths = (
            {
                str(event["prim"])
                for event in events
                if event.get("prim")
                and (
                    self._reapply_all_composed or str(event["prim"]) in self._reapply_composed_paths
                )
                and (
                    event.get("k") in {K_DELETE_PRIM, K_RENAME_PRIM}
                    or (
                        event.get("k") == K_ENSURE_PRIM
                        and (event.get("typeName") or not event.get("api_schemas"))
                    )
                )
            }
            if self._reapply_all_composed or self._reapply_composed_paths
            else set()
        )
        self._local_lifecycle_paths.update(
            new_path
            for old_path, new_path, _event in self._rename_pairs
            if old_path in self._local_lifecycle_paths
        )

    def _prepare_scene_scope(
        self,
        extra_scene_paths: Iterable[str | Sdf.Path],
    ) -> tuple[set[str], set[Sdf.Path]]:
        stack_scene_paths = {Sdf.Path(path) for path in extra_scene_paths}
        self._event_scene_paths = {
            path for event in self._events if (path := _sdf_property_path(event)) is not None
        }
        stack_prim_paths = {
            str(path.GetPrimPath())
            for path in stack_scene_paths
            if path.GetPrimPath() != Sdf.Path.absoluteRootPath
        }
        event_paths = {
            str(event.get("prim") or "")
            for event in self._events
            if event.get("prim") and event.get("k") != K_RENAME_PRIM
        }
        return event_paths | stack_prim_paths, stack_scene_paths | self._event_scene_paths

    def _collect_initial_candidates(
        self,
        prim_paths: Iterable[str],
        scene_paths: Iterable[Sdf.Path],
        arc_candidates: Iterable[tuple[str, str]],
    ) -> None:
        candidates = self._candidates
        candidates.prims.update(prim_paths)
        for step in _PROJECTION_STEPS:
            if step.collector_name is None:
                continue
            collected = getattr(self, step.collector_name)(self._events)
            getattr(candidates, cast(str, step.candidate_name)).update(collected)
        candidates.arcs.update(arc_candidates)
        self._add_scene_path_candidates(scene_paths)
        self._add_subtree_candidates(self._subtree_roots)

    def _capture_baseline(self) -> _ProjectionBaseline:
        stage = self._stage
        candidates = self._candidates
        return _ProjectionBaseline(
            valid={path: bool(stage.GetPrimAtPath(path)) for path in candidates.prims},
            types=self._read_types(candidates.types),
            xforms=_local_transforms(stage, candidates.xforms),
            gprim=_gprim_values(stage, candidates.gprim),
            variants=self._read_variants(candidates.variants),
            materials=self._read_materials(candidates.materials),
            connectables=self._read_connectables(candidates.connectables),
            active=self._read_active(candidates.active),
            visibility=self._read_visibility(candidates.visibility),
            instanceable=self._read_instanceable(candidates.instanceable),
            point_instancers=self._read_point_instancers(candidates.point_instancers),
            arcs=self._read_arcs(candidates.arcs),
        )

    def _should_reapply_composed(self, prim_path: str) -> bool:
        return self._reapply_all_composed or prim_path in self._reapply_composed_paths

    @staticmethod
    def _collect_type_candidates(events: list[dict]) -> set[str]:
        return {
            str(event["prim"])
            for event in events
            if event.get("prim")
            and (
                event.get("k") == K_ENSURE_PRIM
                or (
                    event.get("k") == K_SET_SDF_SPEC_FIELDS
                    and event.get("spec_kind") == "prim"
                    and {"apiSchemas", "specifier", "typeName"} & set(event.get("fields", ()))
                )
            )
        }

    @staticmethod
    def _collect_variant_candidates(events: list[dict]) -> set[str]:
        return {
            str(event["prim"])
            for event in events
            if event.get("prim")
            and (event.get("k") == K_SET_VARIANT_SELECTIONS or _is_variant_spec_event(event))
        }

    @staticmethod
    def _collect_material_candidates(events: list[dict]) -> set[tuple[str, str]]:
        return {
            (str(event["prim"]), purpose)
            for event in events
            if event.get("prim")
            if (purpose := _material_purpose(event)) is not None
        }

    @staticmethod
    def _collect_active_candidates(events: list[dict]) -> set[str]:
        return {
            str(event["prim"])
            for event in events
            if event.get("prim")
            and (event.get("k") == K_DEACTIVATE_PRIM or _is_active_spec_event(event))
        }

    @staticmethod
    def _collect_visibility_candidates(events: list[dict]) -> set[_PrimTime]:
        return {
            (str(event["prim"]), event.get("time"))
            for event in events
            if event.get("prim")
            and (
                event.get("k") == K_SET_VISIBILITY
                or (
                    (path := _sdf_property_path(event)) is not None
                    and str(path.name) == "visibility"
                )
            )
        }

    @staticmethod
    def _collect_instanceable_candidates(events: list[dict]) -> set[str]:
        return {
            str(event["prim"])
            for event in events
            if event.get("prim")
            and (event.get("k") == K_SET_INSTANCEABLE or _is_instanceable_spec_event(event))
        }

    @staticmethod
    def _collect_xform_candidates(events: list[dict]) -> set[tuple[str, float | None]]:
        result = set()
        for event in events:
            if event.get("k") in {K_ENSURE_XFORM_OPS, K_SET_XFORM_TRS} or (
                _is_xform_spec_event(event)
            ):
                prim_path = str(event.get("prim") or "")
                if prim_path:
                    result.add((prim_path, event.get("time")))
        return result

    def _collect_gprim_candidates(
        self,
        events: list[dict],
    ) -> dict[tuple[str, float | None], set[str]]:
        result: dict[tuple[str, float | None], set[str]] = defaultdict(set)
        for event in events:
            prim_path = str(event.get("prim") or "")
            if not prim_path:
                continue
            if event.get("k") == K_SET_GPRIM_ATTRS:
                result[(prim_path, event.get("time"))].update(
                    event.get("attrs", ()),
                )
            elif _is_gprim_spec_event(event):
                path = _sdf_property_path(event)
                if path is not None:
                    name = str(path.name)
                    prim = self._stage.GetPrimAtPath(prim_path)
                    if (
                        prim
                        and prim.IsValid()
                        and prim.IsA(UsdGeom.PointInstancer)
                        and name in POINT_INSTANCER_USD_TO_WIRE
                    ):
                        continue
                    result[(prim_path, None)].add(name)
        return result

    def _collect_point_instancer_candidates(
        self,
        events: list[dict],
    ) -> dict[tuple[str, float | None], set[str]]:
        result: dict[tuple[str, float | None], set[str]] = defaultdict(set)
        for event in events:
            prim_path = str(event.get("prim") or "")
            if not prim_path:
                continue
            if event.get("k") == K_SET_POINT_INSTANCER:
                result[(prim_path, event.get("time"))].update(event.get("fields", ()))
                continue
            path = _sdf_property_path(event)
            prim = self._stage.GetPrimAtPath(prim_path)
            is_point_instancer = bool(prim and prim.IsValid() and prim.IsA(UsdGeom.PointInstancer))
            if path is not None and is_point_instancer:
                field = POINT_INSTANCER_USD_TO_WIRE.get(str(path.name))
                if field:
                    result[(prim_path, None)].add(field)
            if (
                is_point_instancer
                and event.get("k") == K_SET_SDF_SPEC_FIELDS
                and event.get("spec_kind") == "prim"
                and "inactiveIds" in event.get("fields", ())
            ):
                result[(prim_path, None)].add("inactive_ids")
        return result

    @staticmethod
    def _collect_arc_candidates(events: list[dict]) -> set[tuple[str, str]]:
        result = {
            (str(event["prim"]), str(event["k"]))
            for event in events
            if event.get("prim") and event.get("k") in {K_SET_REFERENCE, K_SET_PAYLOAD}
        }
        for event in events:
            if (
                event.get("k") != K_SET_SDF_SPEC_FIELDS
                or event.get("spec_kind") != "prim"
                or not event.get("prim")
            ):
                continue
            fields = set(event.get("fields", ()))
            if "references" in fields:
                result.add((str(event["prim"]), K_SET_REFERENCE))
            if "payload" in fields:
                result.add((str(event["prim"]), K_SET_PAYLOAD))
        return result

    def _add_scene_path_candidates(self, paths: Iterable[Sdf.Path]) -> None:
        xform_prims = set()
        seen_prims = set()
        for path in paths:
            prim_path = str(path.GetPrimPath())
            if not prim_path or prim_path == "/":
                continue
            prim = None
            if prim_path not in seen_prims:
                seen_prims.add(prim_path)
                self._candidates.prims.add(prim_path)
                self._candidates.types.add(prim_path)
                self._candidates.variants.add(prim_path)
                self._candidates.active.add(prim_path)
                self._candidates.instanceable.add(prim_path)
                prim = self._stage.GetPrimAtPath(prim_path)
                if prim and prim.IsValid() and prim.IsA(UsdGeom.PointInstancer):
                    self._candidates.point_instancers[(prim_path, None)].add("inactive_ids")
            if not path.IsPropertyPath():
                continue

            name = str(path.name)
            if name == "xformOpOrder" or name.startswith("xformOp:"):
                self._candidates.xforms.add((prim_path, None))
                xform_prims.add(prim_path)
                continue

            prop = self._stage.GetPropertyAtPath(path)
            sample_times = tuple(prop.GetTimeSamples()) if isinstance(prop, Usd.Attribute) else ()
            if name == "visibility":
                self._candidates.visibility.add((prim_path, None))
                self._candidates.visibility.update((prim_path, time) for time in sample_times)
            elif name == "info:id" or name.startswith(("inputs:", "outputs:")):
                self._candidates.connectables[(prim_path, None)].add(
                    "*" if name == "info:id" else name
                )
                if name.startswith("inputs:"):
                    for time in sample_times:
                        self._candidates.connectables[(prim_path, time)].add(name)
            elif (purpose := _material_purpose_from_name(name)) is not None:
                self._candidates.materials.add((prim_path, purpose))
            elif (
                (prim := prim or self._stage.GetPrimAtPath(prim_path))
                and prim.IsValid()
                and prim.IsA(UsdGeom.PointInstancer)
                and (field := POINT_INSTANCER_USD_TO_WIRE.get(name))
            ):
                self._candidates.point_instancers[(prim_path, None)].add(field)
                for time in sample_times:
                    self._candidates.point_instancers[(prim_path, time)].add(field)
            elif _is_gprim_property_name(name):
                self._candidates.gprim[(prim_path, None)].add(name)
                for time in sample_times:
                    self._candidates.gprim[(prim_path, time)].add(name)

        for prim_path in xform_prims:
            prim = self._stage.GetPrimAtPath(prim_path)
            xformable = UsdGeom.Xformable(prim) if prim and prim.IsValid() else None
            if not xformable:
                continue
            sample_times = {
                time
                for op in xformable.GetOrderedXformOps()
                for time in op.GetAttr().GetTimeSamples()
            }
            self._candidates.xforms.update((prim_path, time) for time in sample_times)

    def _add_subtree_candidates(self, roots: Iterable[str]) -> None:
        paths = []
        for root in roots:
            prim = self._stage.GetPrimAtPath(root)
            if not prim or not prim.IsValid():
                continue
            for descendant in Usd.PrimRange.AllPrims(prim):
                paths.append(descendant.GetPath())
                self._candidates.arcs.update(
                    (str(descendant.GetPath()), kind) for kind in (K_SET_REFERENCE, K_SET_PAYLOAD)
                )
                paths.extend(prop.GetPath() for prop in descendant.GetAuthoredProperties())
        self._add_scene_path_candidates(paths)

    def build_events(self) -> list[dict]:
        """Return adapter events representing the post-transaction composition."""
        if self._event_scene_paths:
            self._add_scene_path_candidates(self._event_scene_paths)
        if self._subtree_roots:
            self._add_subtree_candidates(self._subtree_roots)

        projected: list[dict] = []
        for step in _PROJECTION_STEPS:
            projected.extend(getattr(self, step.method_name)())
        return projected

    def _project_namespace_and_lifecycle(self) -> list[dict]:
        renames = self._project_renames()
        result = [event for _old, _new, event in renames]
        result.extend(
            self._project_prim_lifecycle(
                {path for old, new, _event in renames for path in (old, new)},
            )
        )
        return result

    def _project_payload_load_state(self) -> list[dict]:
        return [event for event in self._events if event.get("k") in _COMPOSITION_DIRECT_KINDS]

    def _read_arcs(
        self,
        candidates: Iterable[tuple[str, str]],
    ) -> dict[tuple[str, str], list[dict]]:
        return {
            key: _composed_arc_entries(
                self._stage,
                key[0],
                payload=key[1] == K_SET_PAYLOAD,
            )
            for key in candidates
        }

    def _project_arcs(self) -> list[dict]:
        after = self._read_arcs(self._candidates.arcs)
        result = []
        for (prim_path, kind), entries in after.items():
            before = self._before.arcs.get((prim_path, kind), [])
            reapply_composed = self._should_reapply_composed(prim_path)
            if entries == before and not reapply_composed:
                continue
            if reapply_composed and not entries and not before:
                continue
            entry_key = "payloads" if kind == K_SET_PAYLOAD else "refs"
            result.append(
                {
                    "k": kind,
                    "prim": prim_path,
                    entry_key: entries,
                    "list_op_authored": True,
                    "list_op_explicit": True,
                }
            )
        return result

    def _read_types(
        self,
        prim_paths: Iterable[str],
    ) -> dict[str, tuple[str, tuple[str, ...]] | None]:
        result = {}
        for prim_path in prim_paths:
            prim = self._stage.GetPrimAtPath(prim_path)
            result[prim_path] = (
                (str(prim.GetTypeName()), tuple(prim.GetAppliedSchemas()))
                if prim and prim.IsValid()
                else None
            )
        return result

    def _project_renames(self) -> list[tuple[str, str, dict]]:
        result = []
        for old_path, new_path, event in self._rename_pairs:
            old_after = bool(self._stage.GetPrimAtPath(old_path))
            new_after = bool(self._stage.GetPrimAtPath(new_path))
            if (
                self._before.valid.get(old_path, False)
                and not self._before.valid.get(new_path, False)
                and not old_after
                and new_after
            ):
                result.append((old_path, new_path, event))
        return result

    def _project_prim_lifecycle(
        self,
        renamed_paths: set[str],
    ) -> list[dict]:
        types_after = self._read_types(self._candidates.types)
        rebuilds = []
        ensures = []
        deletes = []
        for prim_path in sorted(
            self._candidates.prims,
            key=lambda path: (path.count("/"), path),
        ):
            if prim_path in renamed_paths:
                continue
            prim = self._stage.GetPrimAtPath(prim_path)
            valid_after = bool(prim)
            valid_before = self._before.valid.get(prim_path, False)
            before_type = self._before.types.get(prim_path)
            after_type = types_after.get(prim_path)
            type_changed = bool(
                valid_before
                and before_type is not None
                and after_type is not None
                and before_type[0] != after_type[0]
            )
            local_rebuild = (
                valid_before
                and self._should_reapply_composed(prim_path)
                and not self._reset
                and prim_path in self._local_lifecycle_paths
            )
            schema_changed = before_type != after_type
            ensure = {
                "k": K_ENSURE_PRIM,
                "prim": prim_path,
                "typeName": str(prim.GetTypeName()) if valid_after else "",
                "api_schemas": list(prim.GetAppliedSchemas()) if valid_after else [],
            }
            if valid_after and (type_changed or local_rebuild):
                rebuilds.extend(
                    [
                        {"k": K_DELETE_PRIM, "prim": prim_path},
                        ensure,
                    ]
                )
                continue
            if valid_after and (self._reset or not valid_before or schema_changed):
                ensures.append(ensure)
            elif valid_before and not valid_after:
                deletes.append({"k": K_DELETE_PRIM, "prim": prim_path})
        deletes.sort(key=lambda event: (-event["prim"].count("/"), event["prim"]))
        return rebuilds + ensures + deletes

    def _project_gprim_attrs(self) -> list[dict]:
        after = _gprim_values(self._stage, self._candidates.gprim)
        result = []
        for (prim_path, time), values in after.items():
            prim = self._stage.GetPrimAtPath(prim_path)
            if not prim or not prim.IsValid():
                continue
            before = self._before.gprim.get((prim_path, time), {})
            selected = {
                name: value
                for name, value in values.items()
                if self._should_reapply_composed(prim_path)
                or name not in before
                or not values_equal(value, before[name])
            }
            attrs = {
                name: converted
                for name, value in selected.items()
                if (converted := usd_value_to_python(value)) is not None
            }
            if not attrs:
                continue
            event = {
                "k": K_SET_GPRIM_ATTRS,
                "prim": prim_path,
                "attrs": attrs,
            }
            primvar_meta = {}
            attr_interp = {}
            for name in event["attrs"]:
                attr = prim.GetAttribute(name)
                if not attr or not attr.IsValid():
                    continue
                primvar_entry, interp_entry = attribute_event_metadata(prim, name, attr)
                primvar_meta.update(primvar_entry)
                attr_interp.update(interp_entry)
            if primvar_meta:
                event["primvar_meta"] = primvar_meta
            if attr_interp:
                event["attr_interp"] = attr_interp
            if time is not None:
                event["time"] = time
            result.append(event)
        return result

    def _read_point_instancers(
        self,
        candidates: dict[tuple[str, float | None], set[str]],
    ) -> dict[tuple[str, float | None], dict[str, object]]:
        names_by_field: dict[str, set[str]] = defaultdict(set)
        for usd_name, wire_name in POINT_INSTANCER_USD_TO_WIRE.items():
            names_by_field[wire_name].add(usd_name)
        names_by_field["prototypes"].add("prototypes")
        names_by_field["inactive_ids"].add("inactiveIds")

        result = {}
        for key, fields in candidates.items():
            only = set().union(*(names_by_field[field] for field in fields)) if fields else set()
            values = read_point_instancer(
                self._stage,
                key[0],
                only=only,
                time=key[1],
                transport=False,
            )
            result[key] = {
                field: value for field, value in (values or {}).items() if field in fields
            }
        return result

    def _project_point_instancers(self) -> list[dict]:
        after = self._read_point_instancers(self._candidates.point_instancers)
        result = []
        for key, values in after.items():
            prim = self._stage.GetPrimAtPath(key[0])
            if not prim or not prim.IsValid() or not prim.IsA(UsdGeom.PointInstancer):
                continue
            before = self._before.point_instancers.get(key, {})
            fields = sorted(
                field
                for field in self._candidates.point_instancers[key]
                if self._should_reapply_composed(key[0])
                or field not in before
                or field not in values
                or not values_equal(values[field], before[field])
            )
            if not fields:
                continue
            event = {
                "k": K_SET_POINT_INSTANCER,
                "prim": key[0],
                "fields": fields,
            }
            for field in fields:
                value = values.get(field)
                event[field] = (
                    point_instancer_value_to_wire(field, value) if value is not None else []
                )
            if key[1] is not None:
                event["time"] = key[1]
            result.append(event)
        return result

    def _read_variants(self, prim_paths: Iterable[str]) -> dict[str, dict[str, str]]:
        return {
            prim_path: read_variant_selections(self._stage, prim_path) for prim_path in prim_paths
        }

    def _project_variants(self) -> list[dict]:
        after = self._read_variants(self._candidates.variants)
        result = []
        for prim_path, selections in after.items():
            if not self._stage.GetPrimAtPath(prim_path):
                continue
            before = self._before.variants.get(prim_path, {})
            if not self._should_reapply_composed(prim_path) and selections == before:
                continue
            projected = dict(selections)
            projected.update({name: "" for name in before.keys() - selections.keys()})
            result.append(
                {
                    "k": K_SET_VARIANT_SELECTIONS,
                    "prim": prim_path,
                    "selections": projected,
                }
            )
        return result

    def _read_materials(
        self,
        candidates: Iterable[tuple[str, str]],
    ) -> dict[tuple[str, str], str]:
        candidate_list = tuple(candidates)
        by_prim = {
            prim_path: read_material_binding(self._stage, prim_path)
            for prim_path in {prim_path for prim_path, _purpose in candidate_list}
        }
        return {
            (prim_path, purpose): by_prim[prim_path].get(purpose, "")
            for prim_path, purpose in candidate_list
        }

    def _project_materials(self) -> list[dict]:
        after = self._read_materials(self._candidates.materials)
        result = []
        for key, material_path in after.items():
            if not self._stage.GetPrimAtPath(key[0]):
                continue
            if (
                not self._should_reapply_composed(key[0])
                and self._before.materials.get(key, "") == material_path
            ):
                continue
            prim_path, purpose = key
            event = {
                "k": K_SET_MATERIAL_BINDING,
                "prim": prim_path,
                "material_path": material_path,
            }
            if purpose:
                event["material_purpose"] = purpose
            result.append(event)
        return result

    @staticmethod
    def _collect_connectable_candidates(
        events: list[dict],
    ) -> dict[tuple[str, float | None], set[str]]:
        result: dict[tuple[str, float | None], set[str]] = defaultdict(set)
        for event in events:
            prim_path = str(event.get("prim") or "")
            if not prim_path:
                continue
            kind = event.get("k")
            if kind == K_SET_CONNECTABLE_INPUT:
                result[(prim_path, event.get("time"))].update(
                    f"inputs:{name}" for name in event.get("inputs", ())
                )
            elif kind == K_SET_CONNECTABLE_CONNECTION:
                result[(prim_path, None)].update(event.get("connections", ()))
                result[(prim_path, None)].update(event.get("disconnections", ()))
            elif _is_connectable_spec_event(event):
                path = _sdf_property_path(event)
                if path is not None:
                    name = str(path.name)
                    result[(prim_path, None)].add("*" if name == "info:id" else name)
        return result

    def _read_connectables(
        self,
        candidates: dict[tuple[str, float | None], set[str]],
    ) -> dict[tuple[str, float | None], dict]:
        result = {}
        for key, local_attrs in candidates.items():
            prim_path, time = key
            prim = self._stage.GetPrimAtPath(prim_path)
            container_kind = connectable_kind(prim)
            state = {
                "info_id": "",
                "inputs": {},
                "types": {},
                "connections": {},
            }
            if not container_kind:
                result[key] = state
                continue
            connectable = UsdShade.ConnectableAPI(prim)
            if container_kind == "shader":
                state["info_id"] = str(UsdShade.Shader(prim).GetIdAttr().Get() or "")
            include_all = "*" in local_attrs
            for local_attr in local_attrs:
                if local_attr == "*":
                    continue
                namespace, _separator, base_name = local_attr.partition(":")
                port = (
                    connectable.GetInput(base_name)
                    if namespace == "inputs"
                    else connectable.GetOutput(base_name)
                )
                if not port:
                    continue
                sources, _invalid = port.GetConnectedSources()
                if sources:
                    state["connections"][local_attr] = {
                        "source_prim": str(sources[0].source.GetPath()),
                        "source_attr": connected_source_attr(sources[0]).qualified_name,
                    }
                if namespace != "inputs":
                    continue
                value = usd_value_to_python(port.GetAttr().Get(_time_code(time)))
                if value is not None:
                    state["inputs"][base_name] = value
                    state["types"][base_name] = str(port.GetAttr().GetTypeName())
            if include_all:
                _kind, info_id, inputs, types, connections = read_usdshade_connectable(
                    self._stage,
                    prim_path,
                )
                state = {
                    "info_id": info_id,
                    "inputs": inputs,
                    "types": types,
                    "connections": connections,
                }
                for input_port in connectable.GetInputs():
                    value = usd_value_to_python(input_port.GetAttr().Get(_time_code(time)))
                    if value is None:
                        continue
                    name = input_port.GetBaseName()
                    state["inputs"][name] = value
                    state["types"][name] = str(input_port.GetAttr().GetTypeName())
            result[key] = state
        return result

    def _project_connectables(self) -> list[dict]:
        after = self._read_connectables(self._candidates.connectables)
        input_events = []
        connection_events = []
        for key, state in after.items():
            if not self._stage.GetPrimAtPath(key[0]):
                continue
            before = self._before.connectables.get(key, {})
            previous_inputs = before.get("inputs", {})
            changed_inputs = {
                name: value
                for name, value in state["inputs"].items()
                if name not in previous_inputs or not values_equal(value, previous_inputs[name])
            }
            info_changed = state["info_id"] != before.get("info_id", "")
            reapply_composed = self._should_reapply_composed(key[0])
            if reapply_composed or changed_inputs or info_changed:
                inputs = state["inputs"] if reapply_composed or info_changed else changed_inputs
                input_event = {
                    "k": K_SET_CONNECTABLE_INPUT,
                    "prim": key[0],
                    "info_id": state["info_id"],
                    "inputs": inputs,
                    "input_types": {name: state["types"][name] for name in inputs},
                }
                if key[1] is not None:
                    input_event["time"] = key[1]
                input_events.append(input_event)

            previous_connections = before.get("connections", {})
            connections = {
                name: connection
                for name, connection in state["connections"].items()
                if previous_connections.get(name) != connection
            }
            disconnections = [
                name for name in previous_connections if name not in state["connections"]
            ]
            if reapply_composed:
                connections = dict(state["connections"])
            if connections or disconnections:
                connection_events.append(
                    {
                        "k": K_SET_CONNECTABLE_CONNECTION,
                        "prim": key[0],
                        "connections": connections,
                        "disconnections": disconnections,
                    }
                )
        return input_events + connection_events

    def _read_active(self, prim_paths: Iterable[str]) -> dict[str, bool | None]:
        result = {}
        for prim_path in prim_paths:
            prim = self._stage.GetPrimAtPath(prim_path)
            result[prim_path] = prim.IsActive() if prim and prim.IsValid() else None
        return result

    def _project_active(self) -> list[dict]:
        after = self._read_active(self._candidates.active)
        return [
            {
                "k": K_DEACTIVATE_PRIM,
                "prim": prim_path,
                "active": active,
            }
            for prim_path, active in after.items()
            if active is not None
            and (
                self._should_reapply_composed(prim_path)
                or self._before.active.get(prim_path) != active
            )
        ]

    def _read_visibility(
        self,
        candidates: Iterable[tuple[str, float | None]],
    ) -> dict[tuple[str, float | None], bool | None]:
        result = {}
        for key in candidates:
            prim = self._stage.GetPrimAtPath(key[0])
            imageable = UsdGeom.Imageable(prim) if prim and prim.IsValid() else None
            if not imageable:
                result[key] = None
                continue
            visibility = imageable.GetVisibilityAttr().Get(_time_code(key[1]))
            result[key] = visibility != UsdGeom.Tokens.invisible
        return result

    def _project_visibility(self) -> list[dict]:
        after = self._read_visibility(self._candidates.visibility)
        result = []
        for key, visible in after.items():
            if visible is None or (
                not self._should_reapply_composed(key[0])
                and self._before.visibility.get(key) == visible
            ):
                continue
            event = {
                "k": K_SET_VISIBILITY,
                "prim": key[0],
                "visible": visible,
            }
            if key[1] is not None:
                event["time"] = key[1]
            result.append(event)
        return result

    def _read_instanceable(self, prim_paths: Iterable[str]) -> dict[str, bool | None]:
        result = {}
        for prim_path in prim_paths:
            prim = self._stage.GetPrimAtPath(prim_path)
            result[prim_path] = prim.IsInstanceable() if prim and prim.IsValid() else None
        return result

    def _project_instanceable(self) -> list[dict]:
        after = self._read_instanceable(self._candidates.instanceable)
        return [
            {
                "k": K_SET_INSTANCEABLE,
                "prim": prim_path,
                "instanceable": instanceable,
            }
            for prim_path, instanceable in after.items()
            if instanceable is not None
            and (
                self._should_reapply_composed(prim_path)
                or self._before.instanceable.get(prim_path) != instanceable
            )
        ]

    def _project_xforms(self) -> list[dict]:
        after = _local_transforms(self._stage, self._candidates.xforms)
        ensure_ops = {
            event["prim"]
            for event in self._events
            if event.get("k") == K_ENSURE_XFORM_OPS and event.get("prim")
        }
        result = []
        ensured = set()
        for key in sorted(
            self._candidates.xforms,
            key=lambda item: (item[0].count("/"), item[0], -1.0 if item[1] is None else item[1]),
        ):
            prim_path, time = key
            prim = self._stage.GetPrimAtPath(prim_path)
            if not prim or not prim.IsValid():
                continue
            xformable = UsdGeom.Xformable(prim)
            if not xformable:
                continue
            changed = self._before.xforms.get(key) != after.get(key)
            reapply_composed = self._should_reapply_composed(prim_path)
            if not (reapply_composed or changed or prim_path in ensure_ops):
                continue
            if prim_path not in ensured:
                result.append({"k": K_ENSURE_XFORM_OPS, "prim": prim_path})
                ensured.add(prim_path)
            if not (reapply_composed or changed):
                continue
            local = as_matrix(xformable.GetLocalTransformation(_time_code(time)))
            t, r, s = decompose_trs_from_matrix(local)
            event = {
                "k": K_SET_XFORM_TRS,
                "prim": prim_path,
                "fields": ["t", "r", "s"],
                "t": t,
                "r": r,
                "s": s,
            }
            if time is not None:
                event["time"] = time
            result.append(event)
        return result

    def _direct_events(self) -> list[dict]:
        """Return events whose semantics do not depend on layer strength."""
        result: list[dict] = []
        for event in self._events:
            kind = event.get("k")
            if kind not in EVENT_KEYS:
                raise ValueError(f"unknown event kind in native projection: {kind!r}")
            if kind in _TRAILING_DIRECT_KINDS:
                result.append(event)
                continue
            if kind in _COMPOSITION_DIRECT_KINDS:
                continue
            if kind in NATIVE_PROJECTED_KINDS | NATIVE_FIELD_ROUTED_KINDS:
                continue
            raise RuntimeError(f"event kind has no native projection policy: {kind!r}")
        return result

def _validate_projection_steps() -> None:
    candidate_names = _ProjectionCandidates.__dataclass_fields__
    for step in _PROJECTION_STEPS:
        if not hasattr(ComposedChangeProjection, step.method_name):
            raise RuntimeError(
                f"native projection step {step.name!r} has no projector {step.method_name!r}"
            )
        if step.collector_name is None:
            continue
        if step.candidate_name not in candidate_names:
            raise RuntimeError(
                f"native projection step {step.name!r} has unknown candidate "
                f"{step.candidate_name!r}"
            )
        if not hasattr(ComposedChangeProjection, step.collector_name):
            raise RuntimeError(
                f"native projection step {step.name!r} has no collector "
                f"{step.collector_name!r}"
            )


_validate_projection_steps()


__all__ = ["ComposedChangeProjection"]
