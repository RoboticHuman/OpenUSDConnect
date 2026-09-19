"""DCC-neutral shader mapping interfaces and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod


class ShaderMapper(ABC):
    """Maps a USD shader type to a DCC-native node.

    ``node`` arguments are deliberately untyped because integrations supply
    their own node classes.
    """

    def __init__(self, shader_id: str, node_type: str, input_map: dict):
        self.shader_id = shader_id
        self.node_type = node_type
        self._input_map = input_map
        self._reverse_map = {
            native: usd for usd, native in input_map.items() if not native.startswith("_")
        }

    def get_native_input(self, usd_name: str) -> str | None:
        """Return the DCC-native input name for a USD input, or None."""
        return self._input_map.get(usd_name)

    def get_usd_input(self, native_name: str) -> str | None:
        """Return the USD input name for a DCC-native input (reverse lookup)."""
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

    ``create_network`` receives all inputs and replaces per-input
    ``apply_value`` calls.
    """

    @property
    def is_multi_node(self) -> bool:
        return True

    @property
    def is_surface_shader(self) -> bool:
        """Whether ``out`` belongs on the material's Surface input.

        Helper mappers override this to avoid routing non-shader outputs there
        or clearing an existing surface shader.
        """
        return True

    def apply_value(self, node, usd_name: str, value, **kwargs) -> None:
        pass  # Not used create_network handles everything

    def read_all_inputs(self, node=None, *, input_map=None) -> dict:
        """Read all mapped input values from a multi-node network.

        ``input_map`` is the socket map returned by ``create_network``; sockets
        expose ``default_value`` and ``is_linked``.
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
            inputs: dict of USD input name -> Python value
            **kwargs: DCC-specific extras (e.g., resolve_asset callback)

        Returns:
            (nodes, input_map, output_map) where:
            - nodes: tuple of created DCC nodes
            - input_map: dict of usd_input_name -> DCC input socket
            - output_map: dict of usd_output_name -> DCC output socket
        """
        raise NotImplementedError


class ShaderMapperRegistry:
    """Extensible registry of USD shader ID -> ShaderMapper."""

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
