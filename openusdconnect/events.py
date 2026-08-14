"""Event payload schemas and dispatch registry.

TypedDicts describe the dict-shape of every event kind on the wire; the
FlatBuffers schema (``schema/events.fbs``) is the canonical source for
wire-format compatibility. The dispatch registry holds an :class:`EventSpec`
per kind — populated by decorators at function definition sites in ``codec``,
``event_apply``, and ``protocol_validation``. The accessors (``get``,
``by_tag``, ``all_specs``) lazily import those modules on first call, so the
registry is always
populated by the time it's observed.

The ``k`` field discriminates the :data:`Event` union and matches a ``K_*``
constant from ``protocol_constants``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

import numpy as np

# ---------------------------------------------------------------------------
# Shared sub-types
# ---------------------------------------------------------------------------


ArcListPosition = Literal[
    "explicit",
    "added",
    "prepended",
    "appended",
    "deleted",
    "ordered",
]


class ArcEntry(TypedDict):
    """One item in an authored reference or payload Sdf list operation."""

    asset_path: NotRequired[str]  # omit for an internal arc
    prim_path: NotRequired[str]  # omit to use the referenced layer's default prim
    # Defaults to ``explicit`` for explicit ops, otherwise ``prepended``.
    list_position: NotRequired[ArcListPosition]
    layer_offset: NotRequired[float]  # defaults to 0
    layer_scale: NotRequired[float]  # defaults to 1
    custom_data_fragment: NotRequired[str]  # reference-only typed USDA dictionary


class SublayerEntry(TypedDict):
    """One authored edge in an ordered ``Sdf.Layer.subLayerPaths`` list."""

    authored_path: str
    offset: NotRequired[float]
    scale: NotRequired[float]
    layer_key: NotRequired[str]


class PrimvarMeta(TypedDict):
    """USD type + interpolation for a primvar attribute."""

    typeName: str
    interpolation: NotRequired[str]


class ConnSource(TypedDict):
    """Upstream end of a UsdShade connection edge.

    Both ``source_prim`` and ``source_attr`` are required; ``source_attr``
    is namespace-qualified (``"inputs:..."`` or ``"outputs:..."``).
    """

    source_prim: str
    source_attr: str


# ---------------------------------------------------------------------------
# Attribute value type aliases
# ---------------------------------------------------------------------------

# Values acceptable in ``SetGprimAttrs.attrs``. The codec inspects each value
# at encode time and dispatches to scalar / typed-array / JSON-fallback paths.
AttrValue = bool | int | float | str | list | np.ndarray

# Values acceptable in ``SetConnectableInput.inputs``. Narrower than ``AttrValue``
# because UsdShade inputs are scalars or fixed-stride float vectors.
ConnectableInputValue = bool | int | float | str | list[float]


# ---------------------------------------------------------------------------
# Event TypedDicts — one per K_* kind in protocol_constants
# ---------------------------------------------------------------------------


class EnsurePrim(TypedDict):
    """Idempotent ``DefinePrim``. ``typeName`` may be ``""`` for untyped scopes.

    ``api_schemas`` carries applied API schema names — bare ``"Name"`` for
    single-apply, ``"Name:instance"`` for multi-apply. Additive only on the
    receive side; never removes schemas.
    """

    k: Literal["ensure_prim"]
    prim: str
    typeName: str
    api_schemas: NotRequired[list[str]]


class EnsureXformOps(TypedDict):
    """Establish canonical translate / orient / scale ops on an Xformable."""

    k: Literal["ensure_xform_ops"]
    prim: str


class SetXformTRS(TypedDict):
    """Partial transform update.

    ``fields`` lists which of ``"t"``, ``"r"``, ``"s"`` are present in this
    payload. Rotation ``r`` is a quaternion ``[w, x, y, z]`` — not euler.
    ``time`` is an optional USD time sample; absent = ``Usd.TimeCode.Default()``.
    """

    k: Literal["set_xform_trs"]
    prim: str
    fields: list[str]
    t: NotRequired[list[float]]  # length 3
    r: NotRequired[list[float]]  # length 4, quaternion [w, x, y, z]
    s: NotRequired[list[float]]  # length 3
    time: NotRequired[float]


class DeletePrim(TypedDict):
    """Remove a prim from the stage."""

    k: Literal["delete_prim"]
    prim: str


class DeactivatePrim(TypedDict):
    """Toggle prim activation. ``active=False`` deactivates."""

    k: Literal["deactivate_prim"]
    prim: str
    active: bool


class RenamePrim(TypedDict):
    """Rename a prim in place. ``new_name`` is the leaf name only."""

    k: Literal["rename_prim"]
    prim: str
    new_name: str


class SetVisibility(TypedDict):
    """``UsdGeom.Imageable`` visibility (``False`` → ``"invisible"``).

    ``time`` is an optional USD time sample; absent = ``Usd.TimeCode.Default()``.
    """

    k: Literal["set_visibility"]
    prim: str
    visible: bool
    time: NotRequired[float]


class SetGprimAttrs(TypedDict):
    """Geometry attribute + primvar update.

    ``attrs`` is a name → value map. Primvar attributes (keys starting with
    ``"primvars:"``) carry their USD type + interpolation in
    ``primvar_meta``; non-primvar attrs with authored interpolation use
    ``attr_interp``.
    """

    k: Literal["set_gprim_attrs"]
    prim: str
    attrs: dict[str, AttrValue]
    primvar_meta: NotRequired[dict[str, PrimvarMeta]]
    attr_interp: NotRequired[dict[str, str]]
    time: NotRequired[float]


class SetReference(TypedDict):
    """Replace the authored reference list operation on a prim."""

    k: Literal["set_reference"]
    prim: str
    refs: list[ArcEntry]
    list_op_authored: NotRequired[bool]
    list_op_explicit: NotRequired[bool]


class SetPayload(TypedDict):
    """Replace the authored payload list operation on a prim."""

    k: Literal["set_payload"]
    prim: str
    payloads: list[ArcEntry]
    list_op_authored: NotRequired[bool]
    list_op_explicit: NotRequired[bool]


class LoadPayload(TypedDict):
    """Load a prim's payload children."""

    k: Literal["load_payload"]
    prim: str


class UnloadPayload(TypedDict):
    """Unload a prim's payload children."""

    k: Literal["unload_payload"]
    prim: str


class SetVariantSelections(TypedDict):
    """Set variant selections (e.g. ``{"size": "large"}``) on a prim."""

    k: Literal["set_variant_selections"]
    prim: str
    selections: dict[str, str]


class SetMaterialBinding(TypedDict):
    """Bind a material to a prim via ``UsdShade.MaterialBindingAPI``.

    ``material_purpose`` selects the per-purpose relationship slot.
    Empty or absent (allPurpose) authors ``material:binding``; ``preview``
    and ``full`` author the purpose-suffixed rels that consumers select
    via ``ComputeBoundMaterial(purpose)``.
    """

    k: Literal["set_material_binding"]
    prim: str
    material_path: str
    material_purpose: NotRequired[str]


class SetConnectableInput(TypedDict):
    """Named input values on a UsdShade.ConnectableAPI container.

    Covers shaders, node graphs, materials, and UsdLux lights uniformly.
    ``info_id`` is the UsdShade ``info:id`` (Sdr identifier) for
    ``UsdShade.Shader`` prims; empty for nodegraphs, materials, and lights.
    ``input_types`` carries Sdf type names (e.g. ``"Color3f"``, ``"Float"``)
    keyed by the same input names as ``inputs``.
    """

    k: Literal["set_connectable_input"]
    prim: str
    info_id: str
    inputs: dict[str, ConnectableInputValue]
    input_types: NotRequired[dict[str, str]]
    time: NotRequired[float]


class SetConnectableConnection(TypedDict):
    """Batch of UsdShade.ConnectableAPI connection edges.

    Keys in ``connections`` are namespace-qualified local attrs on the
    target prim (e.g. ``"inputs:diffuseColor"``, ``"outputs:surface"``);
    values point upstream. ``disconnections`` lists local attrs to clear.
    """

    k: Literal["set_connectable_connection"]
    prim: str
    connections: dict[str, ConnSource]
    disconnections: NotRequired[list[str]]


class SetStageMetadata(TypedDict):
    """Stage-level metadata snapshot — only present fields are applied.

    Carries units (``metersPerUnit``, ``upAxis``) and timeline metadata
    (``timeCodesPerSecond``, ``framesPerSecond``, ``startTimeCode``,
    ``endTimeCode``). The applier writes only the keys actually present;
    missing keys leave the stage's current opinion untouched.
    """

    k: Literal["set_stage_metadata"]
    timeCodesPerSecond: NotRequired[float]
    framesPerSecond: NotRequired[float]
    startTimeCode: NotRequired[float]
    endTimeCode: NotRequired[float]
    metersPerUnit: NotRequired[float]
    upAxis: NotRequired[str]


class SetInstanceable(TypedDict):
    """Native scenegraph-instancing flag on a prim.

    Replicates the ``instanceable`` metadata bit only. Prototype paths
    (``/__Prototype_N``) are implementation-defined and unstable across
    sessions, so each receiver's composition engine rebuilds the instance
    from the flag plus the prim's composition arcs.
    """

    k: Literal["set_instanceable"]
    prim: str
    instanceable: bool


class SetPointInstancer(TypedDict):
    """UsdGeomPointInstancer state: prototypes relationship + per-instance arrays.

    ``fields`` lists which of the ``POINT_INSTANCER_FIELDS`` keys are
    present (distinguishes "absent" from "explicitly empty").
    ``orientations`` is float32 quaternion ``[w, x, y, z]`` rows on the
    wire regardless of whether the source authored quath ``orientations``
    or quatf ``orientationsf``. ``prototypes`` is uniform (relationships
    cannot be time-sampled) and only rides default-time events; the
    arrays may ride per-``time`` events. Array fields accept numpy arrays
    or nested lists.
    """

    k: Literal["set_point_instancer"]
    prim: str
    fields: list[str]
    prototypes: NotRequired[list[str]]
    proto_indices: NotRequired[list[int]]
    positions: NotRequired[list[list[float]]]  # (N, 3)
    orientations: NotRequired[list[list[float]]]  # (N, 4) [w, x, y, z]
    scales: NotRequired[list[list[float]]]  # (N, 3)
    velocities: NotRequired[list[list[float]]]  # (N, 3)
    accelerations: NotRequired[list[list[float]]]  # (N, 3)
    angular_velocities: NotRequired[list[list[float]]]  # (N, 3)
    ids: NotRequired[list[int]]  # int64
    invisible_ids: NotRequired[list[int]]  # int64
    inactive_ids: NotRequired[list[int]]  # int64; resolved prim metadata, uniform
    time: NotRequired[float]


class SetSdfSpecFields(TypedDict):
    """Delta for selected fields on one exact authored Sdf spec.

    ``spec_path`` may contain variant selections. ``fragment`` is a USDA layer
    fragment containing declaration data plus the fields listed in ``fields``.
    A listed field absent from the fragment is cleared. ``removed=True``
    removes the spec and ignores the fragment.
    """

    k: Literal["set_sdf_spec_fields"]
    prim: str
    spec_path: str
    spec_kind: Literal[
        "layer",
        "prim",
        "attribute",
        "relationship",
        "variant_set",
        "variant",
        "property",
    ]
    fields: list[str]
    fragment: str
    removed: bool


class ReplaceSdfLayerContent(TypedDict):
    """Replace all non-topology opinions in the targeted authored layer."""

    k: Literal["replace_sdf_layer_content"]
    prim: Literal["/"]
    fragment: str


class SetSublayers(TypedDict):
    """Replace one layer's sublayers at its authoritative parent revision."""

    k: Literal["set_sublayers"]
    prim: Literal["/"]
    generation: str
    revision: int
    sublayers: list[SublayerEntry]


# ---------------------------------------------------------------------------
# Discriminated union — accept any event kind
# ---------------------------------------------------------------------------

Event = (
    EnsurePrim
    | EnsureXformOps
    | SetXformTRS
    | DeletePrim
    | DeactivatePrim
    | RenamePrim
    | SetVisibility
    | SetGprimAttrs
    | SetReference
    | SetPayload
    | LoadPayload
    | UnloadPayload
    | SetVariantSelections
    | SetMaterialBinding
    | SetConnectableInput
    | SetConnectableConnection
    | SetStageMetadata
    | SetInstanceable
    | SetPointInstancer
    | SetSdfSpecFields
    | ReplaceSdfLayerContent
    | SetSublayers
)


# ---------------------------------------------------------------------------
# Dispatch registry
# ---------------------------------------------------------------------------


@dataclass
class EventSpec:
    """Everything dispatch needs to know about one event kind."""

    kind: str
    fb_tag: int | None = None  # FlatBuffers union tag (EventPayloadType.*)
    fb_class: type | None = None  # generated FlatBuffers table class
    encode: Callable | None = None  # (builder, ev_dict) -> int offset
    decode: Callable | None = None  # (fb_obj, kind) -> ev_dict
    apply: Callable | None = None  # (stage, ev_dict) -> None
    validate: Callable | None = None  # (ev_dict) -> None; raises on invalid data


_REGISTRY: dict[str, EventSpec] = {}
_BY_TAG: dict[int, EventSpec] = {}

_HANDLERS_LOADED = False


def _ensure_handlers_loaded() -> None:
    """Import the modules whose decorators populate the registry.

    Called from accessor functions so the registry is always observably
    complete on first read, regardless of which module the caller
    imported first.
    """
    global _HANDLERS_LOADED
    if _HANDLERS_LOADED:
        return
    _HANDLERS_LOADED = True
    from . import codec, event_apply, protocol_validation  # noqa: F401


def _spec_for(kind: str) -> EventSpec:
    spec = _REGISTRY.get(kind)
    if spec is None:
        spec = EventSpec(kind=kind)
        _REGISTRY[kind] = spec
    return spec


def register_encoder(kind: str, *, fb_tag: int, fb_class: type):
    """Register ``fn(builder, ev) -> int`` as the encoder for ``kind``."""

    def deco(fn: Callable) -> Callable:
        spec = _spec_for(kind)
        spec.encode = fn
        spec.fb_tag = fb_tag
        spec.fb_class = fb_class
        _BY_TAG[fb_tag] = spec
        return fn

    return deco


def register_decoder(kind: str):
    """Register ``fn(fb_obj, kind) -> dict`` as the decoder for ``kind``."""

    def deco(fn: Callable) -> Callable:
        _spec_for(kind).decode = fn
        return fn

    return deco


def register_applier(kind: str):
    """Register ``fn(stage, ev)`` as the applier for ``kind``."""

    def deco(fn: Callable) -> Callable:
        _spec_for(kind).apply = fn
        return fn

    return deco


def register_validator(kind: str):
    """Register ``fn(ev)`` as the raising validator for ``kind``."""

    def deco(fn: Callable) -> Callable:
        _spec_for(kind).validate = fn
        return fn

    return deco


def get(kind: str) -> EventSpec | None:
    """Return the spec for ``kind`` or ``None`` if unregistered."""
    _ensure_handlers_loaded()
    return _REGISTRY.get(kind)


def by_tag(tag: int) -> EventSpec | None:
    """Return the spec whose ``fb_tag`` is ``tag``, or ``None``."""
    _ensure_handlers_loaded()
    return _BY_TAG.get(tag)


def all_specs() -> list[EventSpec]:
    """Return every registered spec (insertion order)."""
    _ensure_handlers_loaded()
    return list(_REGISTRY.values())


__all__ = [
    "ArcListPosition",
    "ArcEntry",
    "SublayerEntry",
    "PrimvarMeta",
    "ConnSource",
    "AttrValue",
    "ConnectableInputValue",
    "EnsurePrim",
    "EnsureXformOps",
    "SetXformTRS",
    "DeletePrim",
    "DeactivatePrim",
    "RenamePrim",
    "SetVisibility",
    "SetGprimAttrs",
    "SetReference",
    "SetPayload",
    "LoadPayload",
    "UnloadPayload",
    "SetVariantSelections",
    "SetMaterialBinding",
    "SetConnectableInput",
    "SetConnectableConnection",
    "SetStageMetadata",
    "SetInstanceable",
    "SetPointInstancer",
    "SetSdfSpecFields",
    "ReplaceSdfLayerContent",
    "SetSublayers",
    "Event",
    "EventSpec",
    "register_encoder",
    "register_decoder",
    "register_applier",
    "register_validator",
    "get",
    "by_tag",
    "all_specs",
]
