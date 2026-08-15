"""DCCAdapter ABC and implementations.

DCCAdapter defines the contract any DCC integration must implement.
UsdStageAdapter applies events to a Usd.Stage (for headless/server consumers).
MockAdapter is a pure-Python dict-based mock for testing without pxr.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable

from pxr import Usd

from .event_apply import (
    apply_event as _apply_event_to_stage,
)
from .event_apply import (
    apply_events as _apply_events_to_stage,
)
from .protocol_constants import (
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_REPLACE_SDF_LAYER_CONTENT,
    K_SET_CONNECTABLE_CONNECTION,
    K_SET_CONNECTABLE_INPUT,
    K_SET_GPRIM_ATTRS,
    K_SET_INSTANCEABLE,
    K_SET_MATERIAL_BINDING,
    K_SET_PAYLOAD,
    K_SET_POINT_INSTANCER,
    K_SET_REFERENCE,
    K_SET_SDF_SPEC_FIELDS,
    K_SET_STAGE_METADATA,
    K_SET_SUBLAYERS,
    K_SET_VARIANT_SELECTIONS,
    K_SET_VISIBILITY,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
    POINT_INSTANCER_FIELDS,
    STAGE_METADATA_KEYS,
)

LOG = logging.getLogger(__name__)


# Per-kind kwargs extractor. The dispatch key (a K_* constant) is also the
# adapter method name, so apply_event uses ``getattr(self, event["k"])``.
# Each lambda returns the kwargs to splat into the adapter method, so
# signatures stay semantic and raw event dicts don't leak into adapters.
def _trs_kwargs(ev: dict) -> dict:
    """Extract t/r/s kwargs from a SetXformTRS event (only fields present)."""
    fields = ev.get("fields", [])
    return {f: ev[f] for f in ("t", "r", "s") if f in fields}


def _time_kwarg(ev: dict) -> dict:
    """``{"time": v}`` when ``ev`` has a non-None ``time``, ``{}`` otherwise.

    Lets adapters that don't model time samples omit the ``time=`` parameter
    entirely they receive the kwarg only when there's an actual sample
    to write, never a spurious ``time=None``.
    """
    t = ev.get("time")
    return {"time": t} if t is not None else {}


def _arc_state_kwargs(ev: dict, entries_key: str) -> dict:
    explicit = bool(ev.get("list_op_explicit", False))
    return {
        "list_op_authored": bool(
            ev.get("list_op_authored", ev[entries_key] or explicit),
        ),
        "list_op_explicit": explicit,
    }


_DISPATCH: dict[str, Callable[[dict], dict]] = {
    K_ENSURE_PRIM: lambda ev: {
        "prim_path": ev["prim"],
        "type_name": ev["typeName"],
        "api_schemas": ev.get("api_schemas", []),
    },
    K_ENSURE_XFORM_OPS: lambda ev: {"prim_path": ev["prim"]},
    K_SET_XFORM_TRS: lambda ev: {
        "prim_path": ev["prim"],
        **_time_kwarg(ev),
        **_trs_kwargs(ev),
    },
    K_DELETE_PRIM: lambda ev: {"prim_path": ev["prim"]},
    K_DEACTIVATE_PRIM: lambda ev: {"prim_path": ev["prim"], "active": ev["active"]},
    K_RENAME_PRIM: lambda ev: {"prim_path": ev["prim"], "new_name": ev["new_name"]},
    K_SET_VISIBILITY: lambda ev: {
        "prim_path": ev["prim"],
        "visible": ev["visible"],
        **_time_kwarg(ev),
    },
    K_SET_GPRIM_ATTRS: lambda ev: {
        "prim_path": ev["prim"],
        "attrs": ev["attrs"],
        **_time_kwarg(ev),
    },
    K_SET_SDF_SPEC_FIELDS: lambda ev: {
        "prim_path": ev["prim"],
        "spec_path": ev["spec_path"],
        "spec_kind": ev["spec_kind"],
        "fields": ev.get("fields", []),
        "fragment": ev.get("fragment", ""),
        "removed": bool(ev.get("removed", False)),
    },
    K_REPLACE_SDF_LAYER_CONTENT: lambda ev: {
        "fragment": ev["fragment"],
    },
    K_SET_SUBLAYERS: lambda ev: {
        "sublayers": ev.get("sublayers", []),
        "generation": ev.get("generation", ""),
        "revision": int(ev.get("revision", 0)),
    },
    K_SET_REFERENCE: lambda ev: {
        "prim_path": ev["prim"],
        "refs": ev["refs"],
        **_arc_state_kwargs(ev, "refs"),
    },
    K_SET_VARIANT_SELECTIONS: lambda ev: {
        "prim_path": ev["prim"],
        "selections": ev["selections"],
    },
    K_SET_PAYLOAD: lambda ev: {
        "prim_path": ev["prim"],
        "payloads": ev["payloads"],
        **_arc_state_kwargs(ev, "payloads"),
    },
    K_LOAD_PAYLOAD: lambda ev: {"prim_path": ev["prim"]},
    K_UNLOAD_PAYLOAD: lambda ev: {"prim_path": ev["prim"]},
    K_SET_MATERIAL_BINDING: lambda ev: {
        "prim_path": ev["prim"],
        "material_path": ev["material_path"],
        "material_purpose": ev.get("material_purpose", ""),
    },
    K_SET_CONNECTABLE_INPUT: lambda ev: {
        "prim_path": ev["prim"],
        "info_id": ev["info_id"],
        "inputs": ev["inputs"],
        "input_types": ev.get("input_types", {}),
        **_time_kwarg(ev),
    },
    K_SET_CONNECTABLE_CONNECTION: lambda ev: {
        "prim_path": ev["prim"],
        "connections": ev["connections"],
        "disconnections": ev.get("disconnections", []),
    },
    K_SET_STAGE_METADATA: lambda ev: {k: ev[k] for k in STAGE_METADATA_KEYS if k in ev},
    K_SET_INSTANCEABLE: lambda ev: {
        "prim_path": ev["prim"],
        "instanceable": ev["instanceable"],
    },
    K_SET_POINT_INSTANCER: lambda ev: {
        "prim_path": ev["prim"],
        **{f: ev[f] for f in ev.get("fields", []) if f in POINT_INSTANCER_FIELDS},
        **_time_kwarg(ev),
    },
}


class DCCAdapter(ABC):
    """Abstract interface a receiving scene integration must implement.

    Semantic methods return ``True`` when they performed or accepted an
    operation and ``False`` for an intentional no-op. These booleans are
    diagnostic; batch delivery fails only when an implementation raises. An
    adapter that cannot leave its destination in the requested state must
    raise an exception instead of returning ``False``.

    Subclasses that write directly into a ``Usd.Stage`` must override
    :meth:`targets_stage`. Adapters for an external scene, such as native DCC
    objects, inherit its default ``None`` result. That distinction controls
    whether layered dispatch applies authored events directly to the mirror
    stage or projects the mirror's composed result into a separate scene.
    """

    def apply_event(self, event: dict) -> bool:
        """Route one known protocol event to the matching adapter method.

        The boolean is the semantic method's diagnostic result. Unknown event
        kinds are programmer/protocol errors and are never silently ignored.
        """
        k = event["k"]
        extract = _DISPATCH.get(k)
        if extract is None:
            raise ValueError(f"unsupported adapter event kind {k!r}")
        return getattr(self, k)(**extract(event))

    def apply_events(self, events: list[dict]) -> int:
        """Dispatch a batch, returning the number of events attempted.

        Per-event ``False`` results remain intentional no-ops. Implementations
        must raise to abort delivery; exceptions propagate to the dispatcher,
        which retains/replays the unapplied sequence suffix.
        """
        for event in events:
            self.apply_event(event)
        return len(events)

    def targets_stage(self) -> Usd.Stage | None:
        """Declare whether this adapter writes directly into a ``Usd.Stage``.

        Return the exact stage instance mutated by :meth:`apply_events`, or
        ``None`` when the adapter writes to an external/native scene. This is
        an architectural capability declaration, not descriptive metadata.

        During layered replay, returning the same object as the dispatcher's
        mirror stage means OpenUSD itself provides composition, so events are
        applied directly and composed projection is skipped. Returning
        ``None`` or a different stage enables before/after composed projection
        into the adapter's separate destination. Stage identity is tested with
        ``is``; an equivalent stage opened from the same layers is still a
        separate destination.

        Stage-backed adapters must override this method. External-scene
        adapters should inherit the default ``None`` result.
        """
        return None

    @abstractmethod
    def ensure_prim(
        self,
        prim_path: str,
        type_name: str = "Xform",
        api_schemas: list[str] | None = None,
    ) -> bool:
        """Idempotent prim definition.

        ``api_schemas`` carries applied API schema names bare ``"Name"``
        for single-apply, ``"Name:instance"`` for multi-apply. Additive
        only on the receive side; never removes schemas.
        """
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

        DCCs that hold a secondary pre-transform alongside the canonical
        local transform (basis matrix, offset parent matrix, pre-xform)
        must fold that pre-transform into the local transform and clear
        the secondary slot, so the composed result is unchanged but the
        canonical TRS stack carries the full opinion.
        """
        raise NotImplementedError

    @abstractmethod
    def set_xform_trs(
        self,
        prim_path: str,
        *,
        t: list[float] | None = None,
        r: list[float] | None = None,
        s: list[float] | None = None,
        time: float | None = None,
    ) -> bool:
        """Apply local translation, quaternion rotation, and/or scale.

        Only the components passed (non-``None``) are written; absent
        components are unchanged.  Rotation ``r`` is a quaternion
        ``[w, x, y, z]``.  ``time`` selects a USD time sample; ``None``
        writes the static (default) opinion.
        """
        raise NotImplementedError

    def has_imported_children(self, prim_path: str) -> bool:
        """Return True when this adapter has already imported the children
        composed under ``prim_path`` (a reference or payload root).

        Used by ``EventDispatcher`` to skip a redundant adapter dispatch
        when the composed stage already matches the incoming arc event
        AND the consumer side has materialised the children.  Adapters
        that import composition arcs (references, payloads) should
        override this; the default returns ``False``, meaning "always
        dispatch".
        """
        return False

    def native_composition_subtree_roots(self, events: list[dict]) -> set[str]:
        """Return composition roots this batch materializes natively.

        Some external-scene adapters implement references, loaded payloads,
        or variants by asking the DCC's USD importer to build the complete
        composed subtree. For those roots, composed projection must not also
        synthesize notice-discovered descendant lifecycle, geometry, material,
        and shader events: replaying them would overwrite the higher-fidelity
        native import.

        The declaration affects only candidates discovered indirectly from
        USD composition notices. Explicit descendant edits in ``events`` are
        still projected and delivered after the root composition operation.
        Adapters that construct descendants solely from projected events must
        keep the default empty result.

        Implementations must return a root only when applying the batch's
        composition event will import, re-import, or remove that subtree. As
        with :meth:`apply_events`, failure to realize a declared operation must
        raise rather than silently returning ``False``.
        """
        return set()

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
    def set_visibility(self, prim_path: str, visible: bool, time: float | None = None) -> bool:
        raise NotImplementedError

    @abstractmethod
    def set_gprim_attrs(
        self,
        prim_path: str,
        attrs: dict,
        time: float | None = None,
    ) -> bool:
        raise NotImplementedError

    def set_sdf_spec_fields(
        self,
        prim_path: str,
        spec_path: str,
        spec_kind: str,
        fields: list[str],
        fragment: str,
        removed: bool = False,
    ) -> bool:
        """Accept an Sdf-only mirror update.

        DCC-backed adapters keep this as a no-op because the dispatcher
        applies the event to their mirror stage. Stage-backed adapters
        override it and apply the spec delta directly.
        """
        return True

    def set_sublayers(
        self,
        sublayers: list[dict],
        *,
        generation: str,
        revision: int,
    ) -> bool:
        """Accept topology already applied to a USD mirror stage."""
        return True

    def replace_sdf_layer_content(self, fragment: str) -> bool:
        """Accept layer content already applied to a USD mirror stage."""
        return True

    @abstractmethod
    def set_reference(
        self,
        prim_path: str,
        refs: list,
        *,
        list_op_authored: bool,
        list_op_explicit: bool,
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    def set_payload(
        self,
        prim_path: str,
        payloads: list,
        *,
        list_op_authored: bool,
        list_op_explicit: bool,
    ) -> bool:
        """Author payload arcs without changing runtime load state.

        Payload list editing and working-set control are separate protocol
        operations. Implementations must leave their current loaded/unloaded
        state unchanged here; :meth:`load_payload` and :meth:`unload_payload`
        apply explicit runtime-state changes.
        """
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

    @abstractmethod
    def set_material_binding(
        self,
        prim_path: str,
        material_path: str,
        material_purpose: str = "",
    ) -> bool:
        """Bind or unbind a material to a prim.

        ``material_purpose`` selects the per-purpose binding slot: empty
        for allPurpose (``material:binding``), ``"preview"`` or ``"full"``
        for the purpose-suffixed rels consumers select via
        ``ComputeBoundMaterial(purpose)``.
        """
        raise NotImplementedError

    @abstractmethod
    def set_connectable_input(
        self,
        prim_path: str,
        info_id: str,
        inputs: dict,
        input_types: dict,
        time: float | None = None,
    ) -> bool:
        """Set input values on a UsdShade connectable (Shader / NodeGraph / Material / Light).

        ``info_id`` is the UsdShade ``info:id`` (Sdr identifier) for
        ``UsdShade.Shader`` prims; empty string for non-shader connectables.
        ``time`` selects a USD time sample; ``None`` writes the static opinion.
        """
        raise NotImplementedError

    @abstractmethod
    def set_connectable_connection(
        self, prim_path: str, connections: dict, disconnections: list | None = None
    ) -> bool:
        """Apply UsdShade input/output connection and disconnection edges."""
        raise NotImplementedError

    @abstractmethod
    def set_stage_metadata(
        self,
        *,
        timeCodesPerSecond: float | None = None,
        framesPerSecond: float | None = None,
        startTimeCode: float | None = None,
        endTimeCode: float | None = None,
        metersPerUnit: float | None = None,
        upAxis: str | None = None,
    ) -> bool:
        """Apply stage-level metadata (units + timeline).

        Only fields the caller passes as non-``None`` are applied the
        emitter ships partial updates to keep wire cost down. DCCs map
        these to their own scene units / fps / timeline settings.
        """
        raise NotImplementedError

    @abstractmethod
    def set_instanceable(self, prim_path: str, instanceable: bool) -> bool:
        """Toggle native scenegraph instancing on a prim.

        Receivers rebuild the instance from this flag plus the prim's
        composition arcs; prototype paths never cross the wire (they are
        implementation-defined and unstable across sessions).
        """
        raise NotImplementedError

    @abstractmethod
    def set_point_instancer(
        self,
        prim_path: str,
        *,
        prototypes: list[str] | None = None,
        proto_indices=None,
        positions=None,
        orientations=None,
        scales=None,
        velocities=None,
        accelerations=None,
        angular_velocities=None,
        ids=None,
        invisible_ids=None,
        inactive_ids=None,
        time: float | None = None,
    ) -> bool:
        """Apply UsdGeomPointInstancer state.

        Only fields present on the event arrive as non-None kwargs. On the
        receive path array values are numpy views: (N, 3) float32 for the
        vec3 arrays, (N, 4) float32 wxyz rows for ``orientations``, int64
        for ``ids`` / ``invisible_ids``. ``prototypes`` is the ordered
        prototype prim-path list and only accompanies default-time events;
        ``time`` selects a USD time sample for the arrays.
        """
        raise NotImplementedError


class ShaderMapper(ABC):
    """Maps a USD shader type to a DCC-native node.

    Subclass per DCC integration and per shader behavior (PBR surface,
    texture, UV reader). The ``node`` parameter in apply_value/post_apply
    is DCC-specific (untyped); each implementation knows its own node
    object type.
    """

    def __init__(self, shader_id: str, node_type: str, input_map: dict):
        self.shader_id = shader_id
        self.node_type = node_type
        self._input_map = input_map

    def get_native_input(self, usd_name: str) -> str | None:
        """Return the DCC-native input name for a USD input, or None."""
        return self._input_map.get(usd_name)

    def get_usd_input(self, native_name: str) -> str | None:
        """Return the USD input name for a DCC-native input (reverse lookup)."""
        if not hasattr(self, "_reverse_map"):
            self._reverse_map = {v: k for k, v in self._input_map.items() if not v.startswith("_")}
        return self._reverse_map.get(native_name)

    @abstractmethod
    def apply_value(self, node, usd_name: str, value, **kwargs) -> None:
        """Apply a USD input value to the DCC node."""
        raise NotImplementedError

    @property
    def is_multi_node(self) -> bool:
        """Whether this mapper creates multiple DCC nodes for one USD shader."""
        return False

    def post_apply(self, node, inputs: dict) -> None:  # noqa: B027
        """Hook called after all inputs are applied. Override as needed."""


class MultiNodeShaderMapper(ShaderMapper):
    """Mapper that creates multiple DCC nodes for one USD shader.

    Used for complex shaders like MaterialX Standard Surface that need
    preprocessing nodes (Hue/Sat, Mix, Math) before the main BSDF.
    The ``create_network`` method replaces the per-input ``apply_value``
    pattern it receives all inputs at once and builds the full graph.
    """

    @property
    def is_multi_node(self) -> bool:
        return True

    @property
    def is_surface_shader(self) -> bool:
        """True when this mapper's `out` socket is a Shader output that
        belongs on Material Output.Surface.  Helper mappers (normal-map,
        displacement pre-processing, etc.) override to False so they
        don't misroute their Vector/Color outputs into the Shader-typed
        Surface input and don't clear an already-authored surface BSDF
        when the receiver adapter prepares the node tree.
        """
        return True

    def apply_value(self, node, usd_name: str, value, **kwargs) -> None:
        pass  # Not used create_network handles everything

    def read_all_inputs(self, node=None, *, input_map=None) -> dict:
        """Read all mapped input values from a multi-node network.

        Unlike single-node mappers which read from one node, multi-node
        mappers read from the socket map returned by ``create_network``.
        Each socket is a generic object with ``.default_value`` and
        ``.is_linked`` attributes no DCC-specific imports needed.
        """
        if not input_map:
            return {}
        result = {}
        for usd_name, socket in input_map.items():
            if socket.is_linked:
                continue
            val = socket.default_value
            if hasattr(val, "__len__") and len(val) >= 3:
                # Truncate RGBA/RGB to [r, g, b]
                result[usd_name] = [float(val[0]), float(val[1]), float(val[2])]
            else:
                result[usd_name] = float(val)
        return result

    @abstractmethod
    def create_network(self, tree, inputs: dict, **kwargs) -> tuple:
        """Create the full node network for this shader.

        Args:
            tree: DCC-specific node tree (e.g., bpy.types.NodeTree)
            inputs: dict of USD input name → Python value
            **kwargs: DCC-specific extras (e.g., resolve_asset callback)

        Returns:
            (nodes, input_map, output_map) where:
            - nodes: tuple of created DCC nodes
            - input_map: dict of usd_input_name → DCC input socket
            - output_map: dict of usd_output_name → DCC output socket
        """
        raise NotImplementedError


class ShaderMapperRegistry:
    """Extensible registry of USD shader ID → ShaderMapper."""

    def __init__(self):
        self._mappers: dict[str, ShaderMapper] = {}

    def register(self, mapper: ShaderMapper):
        """Register a mapper for a shader ID."""
        self._mappers[mapper.shader_id] = mapper

    def get(self, shader_id: str) -> ShaderMapper | None:
        """Look up a mapper by USD shader ID."""
        return self._mappers.get(shader_id)

    def get_node_type(self, shader_id: str) -> str | None:
        """Return the DCC node type for a shader ID, or None."""
        mapper = self._mappers.get(shader_id)
        return mapper.node_type if mapper else None


class UsdStageAdapter(DCCAdapter):
    """Applies events to a Usd.Stage via event_apply functions.

    Suitable for headless receivers and server-side USD consumers. Direct
    semantic methods construct protocol events and use the same registered
    appliers as network delivery; batches retain event_apply's optimized
    ordering path.
    """

    def __init__(self, stage):
        if not isinstance(stage, Usd.Stage):
            raise TypeError("UsdStageAdapter requires a Usd.Stage")
        self.stage = stage

    def targets_stage(self):
        return self.stage

    def apply_event(self, event: dict) -> bool:
        _apply_event_to_stage(self.stage, event)
        return True

    def apply_events(self, events: list[dict]) -> int:
        _apply_events_to_stage(self.stage, events)
        return len(events)

    def ensure_prim(
        self,
        prim_path: str,
        type_name: str = "Xform",
        api_schemas: list[str] | None = None,
    ) -> bool:
        return self.apply_event(
            {
                "k": K_ENSURE_PRIM,
                "prim": prim_path,
                "typeName": type_name,
                "api_schemas": list(api_schemas or []),
            }
        )

    def ensure_xform_ops(self, prim_path: str) -> bool:
        return self.apply_event({"k": K_ENSURE_XFORM_OPS, "prim": prim_path})

    def set_xform_trs(
        self,
        prim_path: str,
        *,
        t: list[float] | None = None,
        r: list[float] | None = None,
        s: list[float] | None = None,
        time: float | None = None,
    ) -> bool:
        fields: list[str] = []
        payload: dict = {"k": K_SET_XFORM_TRS, "prim": prim_path, "fields": fields}
        if t is not None:
            fields.append("t")
            payload["t"] = t
        if r is not None:
            fields.append("r")
            payload["r"] = r
        if s is not None:
            fields.append("s")
            payload["s"] = s
        if time is not None:
            payload["time"] = time
        return self.apply_event(payload)

    def delete_prim(self, prim_path: str) -> bool:
        return self.apply_event({"k": K_DELETE_PRIM, "prim": prim_path})

    def deactivate_prim(self, prim_path: str, active: bool = False) -> bool:
        return self.apply_event(
            {"k": K_DEACTIVATE_PRIM, "prim": prim_path, "active": active},
        )

    def rename_prim(self, prim_path: str, new_name: str) -> bool:
        return self.apply_event(
            {"k": K_RENAME_PRIM, "prim": prim_path, "new_name": new_name},
        )

    def set_visibility(
        self,
        prim_path: str,
        visible: bool,
        time: float | None = None,
    ) -> bool:
        ev: dict = {"k": K_SET_VISIBILITY, "prim": prim_path, "visible": visible}
        if time is not None:
            ev["time"] = time
        return self.apply_event(ev)

    def set_gprim_attrs(
        self,
        prim_path: str,
        attrs: dict,
        time: float | None = None,
    ) -> bool:
        ev: dict = {"k": K_SET_GPRIM_ATTRS, "prim": prim_path, "attrs": attrs}
        if time is not None:
            ev["time"] = time
        return self.apply_event(ev)

    def set_sdf_spec_fields(
        self,
        prim_path: str,
        spec_path: str,
        spec_kind: str,
        fields: list[str],
        fragment: str,
        removed: bool = False,
    ) -> bool:
        return self.apply_event(
            {
                "k": K_SET_SDF_SPEC_FIELDS,
                "prim": prim_path,
                "spec_path": spec_path,
                "spec_kind": spec_kind,
                "fields": fields,
                "fragment": fragment,
                "removed": removed,
            }
        )

    def set_sublayers(
        self,
        sublayers: list[dict],
        *,
        generation: str,
        revision: int,
    ) -> bool:
        return self.apply_event(
            {
                "k": K_SET_SUBLAYERS,
                "prim": "/",
                "sublayers": sublayers,
                "generation": generation,
                "revision": revision,
            }
        )

    def replace_sdf_layer_content(self, fragment: str) -> bool:
        return self.apply_event(
            {
                "k": K_REPLACE_SDF_LAYER_CONTENT,
                "prim": "/",
                "fragment": fragment,
            }
        )

    def set_reference(
        self,
        prim_path: str,
        refs: list,
        *,
        list_op_authored: bool = True,
        list_op_explicit: bool = False,
    ) -> bool:
        return self.apply_event(
            {
                "k": K_SET_REFERENCE,
                "prim": prim_path,
                "refs": refs,
                "list_op_authored": list_op_authored,
                "list_op_explicit": list_op_explicit,
            }
        )

    def set_payload(
        self,
        prim_path: str,
        payloads: list,
        *,
        list_op_authored: bool = True,
        list_op_explicit: bool = False,
    ) -> bool:
        return self.apply_event(
            {
                "k": K_SET_PAYLOAD,
                "prim": prim_path,
                "payloads": payloads,
                "list_op_authored": list_op_authored,
                "list_op_explicit": list_op_explicit,
            }
        )

    def load_payload(self, prim_path: str) -> bool:
        return self.apply_event({"k": K_LOAD_PAYLOAD, "prim": prim_path})

    def unload_payload(self, prim_path: str) -> bool:
        return self.apply_event({"k": K_UNLOAD_PAYLOAD, "prim": prim_path})

    def set_variant_selections(self, prim_path: str, selections: dict[str, str]) -> bool:
        return self.apply_event(
            {"k": K_SET_VARIANT_SELECTIONS, "prim": prim_path, "selections": selections},
        )

    def set_material_binding(
        self,
        prim_path: str,
        material_path: str,
        material_purpose: str = "",
    ) -> bool:
        ev = {
            "k": K_SET_MATERIAL_BINDING,
            "prim": prim_path,
            "material_path": material_path,
        }
        if material_purpose:
            ev["material_purpose"] = material_purpose
        return self.apply_event(ev)

    def set_connectable_input(
        self,
        prim_path: str,
        info_id: str,
        inputs: dict,
        input_types: dict,
        time: float | None = None,
    ) -> bool:
        ev: dict = {
            "k": K_SET_CONNECTABLE_INPUT,
            "prim": prim_path,
            "info_id": info_id,
            "inputs": inputs,
            "input_types": input_types,
        }
        if time is not None:
            ev["time"] = time
        return self.apply_event(ev)

    def set_stage_metadata(
        self,
        *,
        timeCodesPerSecond: float | None = None,
        framesPerSecond: float | None = None,
        startTimeCode: float | None = None,
        endTimeCode: float | None = None,
        metersPerUnit: float | None = None,
        upAxis: str | None = None,
    ) -> bool:
        ev: dict = {"k": K_SET_STAGE_METADATA}
        for key, val in (
            ("timeCodesPerSecond", timeCodesPerSecond),
            ("framesPerSecond", framesPerSecond),
            ("startTimeCode", startTimeCode),
            ("endTimeCode", endTimeCode),
            ("metersPerUnit", metersPerUnit),
            ("upAxis", upAxis),
        ):
            if val is not None:
                ev[key] = val
        return self.apply_event(ev)

    def set_connectable_connection(
        self, prim_path: str, connections: dict, disconnections: list | None = None
    ) -> bool:
        return self.apply_event(
            {
                "k": K_SET_CONNECTABLE_CONNECTION,
                "prim": prim_path,
                "connections": connections,
                "disconnections": disconnections or [],
            }
        )

    def set_instanceable(self, prim_path: str, instanceable: bool) -> bool:
        return self.apply_event(
            {"k": K_SET_INSTANCEABLE, "prim": prim_path, "instanceable": instanceable},
        )

    def set_point_instancer(
        self,
        prim_path: str,
        *,
        prototypes: list[str] | None = None,
        proto_indices=None,
        positions=None,
        orientations=None,
        scales=None,
        velocities=None,
        accelerations=None,
        angular_velocities=None,
        ids=None,
        invisible_ids=None,
        inactive_ids=None,
        time: float | None = None,
    ) -> bool:
        values = {
            "prototypes": prototypes,
            "proto_indices": proto_indices,
            "positions": positions,
            "orientations": orientations,
            "scales": scales,
            "velocities": velocities,
            "accelerations": accelerations,
            "angular_velocities": angular_velocities,
            "ids": ids,
            "invisible_ids": invisible_ids,
            "inactive_ids": inactive_ids,
        }
        present = {k: v for k, v in values.items() if v is not None}
        ev: dict = {
            "k": K_SET_POINT_INSTANCER,
            "prim": prim_path,
            "fields": list(present),
            **present,
        }
        if time is not None:
            ev["time"] = time
        return self.apply_event(ev)


class MockAdapter(DCCAdapter):
    """Pure-Python mock adapter for testing without pxr.

    Stores prim state in a dict. No external dependencies.
    """

    def __init__(self):
        self._prims: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.stage_metadata: dict = {}

    def ensure_prim(
        self,
        prim_path: str,
        type_name: str = "Xform",
        api_schemas: list[str] | None = None,
    ) -> bool:
        existing = self._prims.get(prim_path)
        if existing is not None:
            if api_schemas:
                merged = set(existing.get("api_schemas") or [])
                merged.update(api_schemas)
                existing["api_schemas"] = list(merged)
            return True
        self._prims[prim_path] = {
            "typeName": type_name,
            "ops": set(),
            "trs": {},
            "api_schemas": list(api_schemas or []),
        }
        LOG.info("MockAdapter: defined prim %s (%s)", prim_path, type_name)
        return True

    def ensure_xform_ops(self, prim_path: str) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            LOG.warning("MockAdapter: ensure_xform_ops prim missing %s", prim_path)
            return False
        p["ops"].update({"translate", "orient", "scale"})
        return True

    def set_xform_trs(
        self,
        prim_path: str,
        *,
        t: list[float] | None = None,
        r: list[float] | None = None,
        s: list[float] | None = None,
        time: float | None = None,
    ) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            LOG.warning("MockAdapter: set_xform_trs prim missing %s", prim_path)
            return False
        fields_set = []
        if time is None:
            store = p["trs"]
        else:
            store = p.setdefault("trs_samples", {}).setdefault(time, {})
        if t is not None:
            store["t"] = t
            fields_set.append("t")
        if r is not None:
            store["r"] = r
            fields_set.append("r")
        if s is not None:
            store["s"] = s
            fields_set.append("s")
        LOG.info(
            "MockAdapter: applied TRS to %s fields=%s time=%s",
            prim_path,
            fields_set,
            time,
        )
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

    def set_visibility(
        self,
        prim_path: str,
        visible: bool,
        time: float | None = None,
    ) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            return False
        if time is None:
            p["visible"] = visible
        else:
            p.setdefault("visibility_samples", {})[time] = visible
        LOG.info(
            "MockAdapter: set visible=%s on prim %s time=%s",
            visible,
            prim_path,
            time,
        )
        return True

    def set_gprim_attrs(
        self,
        prim_path: str,
        attrs: dict,
        time: float | None = None,
    ) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            return False
        if time is None:
            p.setdefault("gprim_attrs", {}).update(attrs)
        else:
            p.setdefault("gprim_attr_samples", {}).setdefault(time, {}).update(attrs)
        LOG.info(
            "MockAdapter: set gprim attrs %s on prim %s time=%s",
            attrs,
            prim_path,
            time,
        )
        return True

    def set_reference(
        self,
        prim_path: str,
        refs: list,
        *,
        list_op_authored: bool = True,
        list_op_explicit: bool = False,
    ) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            self._prims[prim_path] = {"typeName": "Xform", "ops": set(), "trs": {}}
            p = self._prims[prim_path]
        p["references"] = list(refs)
        p["reference_list_op_authored"] = list_op_authored
        p["reference_list_op_explicit"] = list_op_explicit
        LOG.info("MockAdapter: set reference on prim %s", prim_path)
        return True

    def set_payload(
        self,
        prim_path: str,
        payloads: list,
        *,
        list_op_authored: bool = True,
        list_op_explicit: bool = False,
    ) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            self._prims[prim_path] = {"typeName": "Xform", "ops": set(), "trs": {}}
            p = self._prims[prim_path]
        p["payloads"] = list(payloads)
        p["payload_list_op_authored"] = list_op_authored
        p["payload_list_op_explicit"] = list_op_explicit
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

    def set_material_binding(
        self,
        prim_path: str,
        material_path: str,
        material_purpose: str = "",
    ) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            self._prims[prim_path] = {"typeName": "Xform", "ops": set(), "trs": {}}
            p = self._prims[prim_path]
        bindings = p.setdefault("material_bindings", {})
        bindings[material_purpose] = material_path
        LOG.info(
            "MockAdapter: set material binding %s [%s] -> %s",
            prim_path,
            material_purpose or "allPurpose",
            material_path,
        )
        return True

    def set_connectable_input(
        self,
        prim_path: str,
        info_id: str,
        inputs: dict,
        input_types: dict,
        time: float | None = None,
    ) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            self._prims[prim_path] = {"typeName": "Shader", "ops": set(), "trs": {}}
            p = self._prims[prim_path]
        p["info_id"] = info_id
        if time is None:
            p.setdefault("connectable_inputs", {}).update(inputs)
            p.setdefault("connectable_input_types", {}).update(input_types)
        else:
            (p.setdefault("connectable_input_samples", {}).setdefault(time, {}).update(inputs))
        LOG.info(
            "MockAdapter: set connectable input on %s time=%s",
            prim_path,
            time,
        )
        return True

    def set_stage_metadata(
        self,
        *,
        timeCodesPerSecond: float | None = None,
        framesPerSecond: float | None = None,
        startTimeCode: float | None = None,
        endTimeCode: float | None = None,
        metersPerUnit: float | None = None,
        upAxis: str | None = None,
    ) -> bool:
        meta = {
            "timeCodesPerSecond": timeCodesPerSecond,
            "framesPerSecond": framesPerSecond,
            "startTimeCode": startTimeCode,
            "endTimeCode": endTimeCode,
            "metersPerUnit": metersPerUnit,
            "upAxis": upAxis,
        }
        for k, v in meta.items():
            if v is not None:
                self.stage_metadata[k] = v
        LOG.info("MockAdapter: stage metadata %s", {k: v for k, v in meta.items() if v is not None})
        return True

    def set_connectable_connection(
        self, prim_path: str, connections: dict, disconnections: list | None = None
    ) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            self._prims[prim_path] = {"typeName": "Shader", "ops": set(), "trs": {}}
            p = self._prims[prim_path]
        conns = p.setdefault("connectable_connections", {})
        conns.update(connections)
        for name in disconnections or []:
            conns.pop(name, None)
        LOG.info("MockAdapter: set connectable connection on %s", prim_path)
        return True

    def set_instanceable(self, prim_path: str, instanceable: bool) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            return False
        p["instanceable"] = instanceable
        LOG.info("MockAdapter: set instanceable=%s on %s", instanceable, prim_path)
        return True

    def set_point_instancer(
        self,
        prim_path: str,
        *,
        prototypes: list[str] | None = None,
        proto_indices=None,
        positions=None,
        orientations=None,
        scales=None,
        velocities=None,
        accelerations=None,
        angular_velocities=None,
        ids=None,
        invisible_ids=None,
        inactive_ids=None,
        time: float | None = None,
    ) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            return False
        values = {
            "prototypes": prototypes,
            "proto_indices": proto_indices,
            "positions": positions,
            "orientations": orientations,
            "scales": scales,
            "velocities": velocities,
            "accelerations": accelerations,
            "angular_velocities": angular_velocities,
            "ids": ids,
            "invisible_ids": invisible_ids,
            "inactive_ids": inactive_ids,
        }
        present = {k: v for k, v in values.items() if v is not None}
        if time is None:
            p.setdefault("point_instancer", {}).update(present)
        else:
            p.setdefault("point_instancer_samples", {}).setdefault(time, {}).update(present)
        LOG.info(
            "MockAdapter: point instancer %s fields=%s time=%s",
            prim_path,
            sorted(present),
            time,
        )
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
