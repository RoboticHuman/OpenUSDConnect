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
from .event_apply import (
    ensure_canonical_ops as _ensure_canonical_ops,
)
from .protocol_constants import (
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_SET_CONNECTABLE_CONNECTION,
    K_SET_CONNECTABLE_INPUT,
    K_SET_GPRIM_ATTRS,
    K_SET_MATERIAL_BINDING,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_VARIANT_SELECTIONS,
    K_SET_VISIBILITY,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
)

LOG = logging.getLogger(__name__)


# Per-kind kwargs extractor.  The dispatch key (a K_* constant) is also the
# adapter method name, so apply_event uses ``getattr(self, event["k"])`` —
# no method-name strings duplicated in this table.  Each lambda returns the
# kwargs to splat into the adapter method, so signatures stay semantic
# (no raw event dicts leaking into adapter implementations).
def _trs_kwargs(ev: dict) -> dict:
    """Extract t/r/s kwargs from a SetXformTRS event (only fields present)."""
    fields = ev.get("fields", [])
    return {f: ev[f] for f in ("t", "r", "s") if f in fields}


_DISPATCH: dict[str, Callable[[dict], dict]] = {
    K_ENSURE_PRIM: lambda ev: {
        "prim_path": ev["prim"],
        "type_name": ev["typeName"],
        "api_schemas": ev.get("api_schemas", []),
    },
    K_ENSURE_XFORM_OPS: lambda ev: {"prim_path": ev["prim"]},
    K_SET_XFORM_TRS: lambda ev: {"prim_path": ev["prim"], **_trs_kwargs(ev)},
    K_DELETE_PRIM: lambda ev: {"prim_path": ev["prim"]},
    K_DEACTIVATE_PRIM: lambda ev: {"prim_path": ev["prim"], "active": ev["active"]},
    K_RENAME_PRIM: lambda ev: {"prim_path": ev["prim"], "new_name": ev["new_name"]},
    K_SET_VISIBILITY: lambda ev: {"prim_path": ev["prim"], "visible": ev["visible"]},
    K_SET_GPRIM_ATTRS: lambda ev: {"prim_path": ev["prim"], "attrs": ev["attrs"]},
    K_SET_REFERENCE: lambda ev: {"prim_path": ev["prim"], "refs": ev["refs"]},
    K_SET_VARIANT_SELECTIONS: lambda ev: {
        "prim_path": ev["prim"],
        "selections": ev["selections"],
    },
    K_SET_PAYLOAD: lambda ev: {"prim_path": ev["prim"], "payloads": ev["payloads"]},
    K_LOAD_PAYLOAD: lambda ev: {"prim_path": ev["prim"]},
    K_UNLOAD_PAYLOAD: lambda ev: {"prim_path": ev["prim"]},
    K_SET_MATERIAL_BINDING: lambda ev: {
        "prim_path": ev["prim"],
        "material_path": ev["material_path"],
    },
    K_SET_CONNECTABLE_INPUT: lambda ev: {
        "prim_path": ev["prim"],
        "info_id": ev["info_id"],
        "inputs": ev["inputs"],
        "input_types": ev.get("input_types", {}),
    },
    K_SET_CONNECTABLE_CONNECTION: lambda ev: {
        "prim_path": ev["prim"],
        "connections": ev["connections"],
        "disconnections": ev.get("disconnections", []),
    },
}


class DCCAdapter(ABC):
    """Abstract interface a DCC integration must implement."""

    def apply_event(self, event: dict):
        """Route one protocol event dict to the matching adapter method."""
        k = event["k"]
        extract = _DISPATCH.get(k)
        if extract is None:
            return None
        return getattr(self, k)(**extract(event))

    def apply_events(self, events: list[dict]) -> int:
        """Apply a batch of protocol events to this adapter."""
        for event in events:
            self.apply_event(event)
        return len(events)

    @abstractmethod
    def ensure_prim(
        self,
        prim_path: str,
        type_name: str = "Xform",
        api_schemas: list[str] | None = None,
    ) -> bool:
        """Idempotent prim definition.

        ``api_schemas`` carries applied API schema names — bare ``"Name"``
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

        Examples:
        - Blender: new_basis = old_MPI @ old_basis, then MPI = Identity
        - Maya: bake offsetParentMatrix into local xform, then clear it
        - Houdini: fold pre-transform into main transform, then zero it
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
    ) -> bool:
        """Apply local translation, quaternion rotation, and/or scale.

        Only the components passed (non-``None``) are written; absent
        components are unchanged.  Rotation ``r`` is a quaternion
        ``[w, x, y, z]``.
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

    @abstractmethod
    def set_material_binding(self, prim_path: str, material_path: str) -> bool:
        """Bind or unbind a material to a prim."""
        raise NotImplementedError

    @abstractmethod
    def set_connectable_input(
        self, prim_path: str, info_id: str, inputs: dict, input_types: dict
    ) -> bool:
        """Set input values on a UsdShade connectable (Shader / NodeGraph / Material / Light).

        ``info_id`` is the UsdShade ``info:id`` (Sdr identifier) for
        ``UsdShade.Shader`` prims; empty string for non-shader connectables.
        """
        raise NotImplementedError

    @abstractmethod
    def set_connectable_connection(
        self, prim_path: str, connections: dict, disconnections: list | None = None
    ) -> bool:
        """Apply UsdShade input/output connection and disconnection edges."""
        raise NotImplementedError


class ShaderMapper(ABC):
    """Maps a USD shader type to a DCC-native node.

    Subclass per DCC integration (Blender, Maya, etc.) and per shader
    behavior (PBR surface, texture, UV reader).  The ``node`` parameter
    in apply_value/post_apply is DCC-specific (untyped) — each
    implementation knows its own node object type.
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
    pattern — it receives all inputs at once and builds the full graph.
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
        Surface input — and don't clear an already-authored surface BSDF
        when the receiver adapter prepares the node tree.
        """
        return True

    def apply_value(self, node, usd_name: str, value, **kwargs) -> None:
        pass  # Not used — create_network handles everything

    def read_all_inputs(self, node=None, *, input_map=None) -> dict:
        """Read all mapped input values from a multi-node network.

        Unlike single-node mappers which read from one node, multi-node
        mappers read from the socket map returned by ``create_network``.
        Each socket is a generic object with ``.default_value`` and
        ``.is_linked`` attributes — no DCC-specific imports needed.
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

    Suitable for headless receivers and server-side USD consumers.
    """

    def __init__(self, stage):
        if not isinstance(stage, Usd.Stage):
            raise TypeError("UsdStageAdapter requires a Usd.Stage")
        self.stage = stage

    def apply_event(self, event: dict):
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
        _apply_event_to_stage(
            self.stage,
            {
                "k": K_ENSURE_PRIM,
                "prim": prim_path,
                "typeName": type_name,
                "api_schemas": list(api_schemas or []),
            },
        )
        return True

    def ensure_xform_ops(self, prim_path: str) -> bool:
        _ensure_canonical_ops(self.stage, prim_path)
        return True

    def set_xform_trs(
        self,
        prim_path: str,
        *,
        t: list[float] | None = None,
        r: list[float] | None = None,
        s: list[float] | None = None,
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
        _apply_event_to_stage(self.stage, payload)
        return True

    def delete_prim(self, prim_path: str) -> bool:
        self.stage.RemovePrim(prim_path)
        return True

    def deactivate_prim(self, prim_path: str, active: bool = False) -> bool:
        _apply_event_to_stage(
            self.stage,
            {"k": K_DEACTIVATE_PRIM, "prim": prim_path, "active": active},
        )
        return True

    def rename_prim(self, prim_path: str, new_name: str) -> bool:
        _apply_event_to_stage(
            self.stage,
            {"k": K_RENAME_PRIM, "prim": prim_path, "new_name": new_name},
        )
        return True

    def set_visibility(self, prim_path: str, visible: bool) -> bool:
        _apply_event_to_stage(
            self.stage,
            {"k": K_SET_VISIBILITY, "prim": prim_path, "visible": visible},
        )
        return True

    def set_gprim_attrs(self, prim_path: str, attrs: dict) -> bool:
        _apply_event_to_stage(
            self.stage,
            {"k": K_SET_GPRIM_ATTRS, "prim": prim_path, "attrs": attrs},
        )
        return True

    def set_reference(self, prim_path: str, refs: list) -> bool:
        _apply_event_to_stage(
            self.stage,
            {"k": K_SET_REFERENCE, "prim": prim_path, "refs": refs},
        )
        return True

    def set_payload(self, prim_path: str, payloads: list) -> bool:
        _apply_event_to_stage(
            self.stage,
            {"k": K_SET_PAYLOAD, "prim": prim_path, "payloads": payloads},
        )
        # Payloads are unloaded by default — users opt-in to load.
        if payloads:
            self.stage.Unload(prim_path)
        return True

    def load_payload(self, prim_path: str) -> bool:
        _apply_event_to_stage(self.stage, {"k": K_LOAD_PAYLOAD, "prim": prim_path})
        return True

    def unload_payload(self, prim_path: str) -> bool:
        _apply_event_to_stage(self.stage, {"k": K_UNLOAD_PAYLOAD, "prim": prim_path})
        return True

    def set_variant_selections(self, prim_path: str, selections: dict[str, str]) -> bool:
        _apply_event_to_stage(
            self.stage,
            {"k": K_SET_VARIANT_SELECTIONS, "prim": prim_path, "selections": selections},
        )
        return True

    def set_material_binding(self, prim_path: str, material_path: str) -> bool:
        _apply_event_to_stage(
            self.stage,
            {"k": K_SET_MATERIAL_BINDING, "prim": prim_path, "material_path": material_path},
        )
        return True

    def set_connectable_input(
        self, prim_path: str, info_id: str, inputs: dict, input_types: dict
    ) -> bool:
        _apply_event_to_stage(
            self.stage,
            {
                "k": K_SET_CONNECTABLE_INPUT,
                "prim": prim_path,
                "info_id": info_id,
                "inputs": inputs,
                "input_types": input_types,
            },
        )
        return True

    def set_connectable_connection(
        self, prim_path: str, connections: dict, disconnections: list | None = None
    ) -> bool:
        _apply_event_to_stage(
            self.stage,
            {
                "k": K_SET_CONNECTABLE_CONNECTION,
                "prim": prim_path,
                "connections": connections,
                "disconnections": disconnections or [],
            },
        )
        return True


class MockAdapter(DCCAdapter):
    """Pure-Python mock adapter for testing without pxr.

    Stores prim state in a dict. No external dependencies.
    """

    def __init__(self):
        self._prims: dict[str, dict] = {}
        self.calls: list[tuple] = []

    def ensure_prim(
        self,
        prim_path: str,
        type_name: str = "Xform",
        api_schemas: list[str] | None = None,
    ) -> bool:
        existing = self._prims.get(prim_path)
        if existing is not None:
            # Additive: union api_schemas onto existing prim.
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
    ) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            LOG.warning("MockAdapter: set_xform_trs prim missing %s", prim_path)
            return False
        fields_set = []
        if t is not None:
            p["trs"]["t"] = t
            fields_set.append("t")
        if r is not None:
            p["trs"]["r"] = r
            fields_set.append("r")
        if s is not None:
            p["trs"]["s"] = s
            fields_set.append("s")
        LOG.info("MockAdapter: applied TRS to %s fields=%s", prim_path, fields_set)
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

    def set_material_binding(self, prim_path: str, material_path: str) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            self._prims[prim_path] = {"typeName": "Xform", "ops": set(), "trs": {}}
            p = self._prims[prim_path]
        p["material_binding"] = material_path
        LOG.info("MockAdapter: set material binding %s -> %s", prim_path, material_path)
        return True

    def set_connectable_input(
        self, prim_path: str, info_id: str, inputs: dict, input_types: dict
    ) -> bool:
        p = self._prims.get(prim_path)
        if p is None:
            self._prims[prim_path] = {"typeName": "Shader", "ops": set(), "trs": {}}
            p = self._prims[prim_path]
        p["info_id"] = info_id
        p.setdefault("connectable_inputs", {}).update(inputs)
        p.setdefault("connectable_input_types", {}).update(input_types)
        LOG.info("MockAdapter: set connectable input on %s", prim_path)
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

    def get_prim(self, prim_path: str) -> dict:
        """Test helper: get stored prim state."""
        return self._prims.get(prim_path, {})

    def get_trs(self, prim_path: str) -> dict:
        """Test helper: get stored TRS values for a prim."""
        p = self._prims.get(prim_path)
        if p is None:
            return {}
        return dict(p.get("trs", {}))
