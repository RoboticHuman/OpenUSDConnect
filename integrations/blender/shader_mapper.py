"""Blender shader mapper implementations.

Subclasses of the core ShaderMapper ABC for translating USD shader
types to Blender node types. Register new shader support by adding
a mapper to create_default_registry().
"""

from __future__ import annotations

import logging

from openusdconnect.adapters import MultiNodeShaderMapper, ShaderMapper, ShaderMapperRegistry

LOG = logging.getLogger(__name__)

try:
    import bpy

    BPY_AVAILABLE = True
except Exception:
    BPY_AVAILABLE = False


class BlenderShaderMapper(ShaderMapper):
    """Base Blender mapper — applies values to Blender node sockets."""

    def apply_value(self, node, usd_name: str, value, **kwargs) -> None:
        blender_name = self.get_native_input(usd_name)
        if not blender_name or blender_name not in node.inputs:
            return
        inp = node.inputs[blender_name]
        if isinstance(value, list) and len(value) == 3:
            inp.default_value = (*value, 1.0)  # RGB → RGBA
        else:
            inp.default_value = value


class PBRShaderMapper(BlenderShaderMapper):
    """UsdPreviewSurface and compatible surface shaders → Principled BSDF."""

    _EMISSIVE_NAMES = {"emissiveColor", "emission_color"}

    def post_apply(self, node, inputs: dict) -> None:
        # USD's emissiveColor is the final emission value (no separate
        # strength). Blender defaults Emission Strength to 0, so set it
        # to 1.0 (neutral passthrough) when emission color is non-zero.
        for em_name in self._EMISSIVE_NAMES & inputs.keys():
            ec = inputs[em_name]
            if isinstance(ec, list) and any(v > 0 for v in ec):
                if "Emission Strength" in node.inputs:
                    node.inputs["Emission Strength"].default_value = 1.0


class TextureShaderMapper(ShaderMapper):
    """UsdUVTexture and MaterialX image nodes → Image Texture."""

    def apply_value(self, node, usd_name: str, value, **kwargs) -> None:
        blender_name = self.get_native_input(usd_name)
        if blender_name == "_image":
            self._load_image(node, value, kwargs.get("resolve_asset"))
            return
        if not blender_name or blender_name not in node.inputs:
            return
        node.inputs[blender_name].default_value = value

    def _load_image(self, node, value, resolve_asset):
        if not BPY_AVAILABLE or not isinstance(value, str) or not value:
            return
        img = bpy.data.images.get(value)
        if not img:
            resolved = resolve_asset(value) if resolve_asset else None
            if resolved:
                img = bpy.data.images.load(resolved)
            else:
                img = bpy.data.images.new(value, 1, 1)
        node.image = img


class UVReaderMapper(ShaderMapper):
    """UsdPrimvarReader_float2 → UV Map node."""

    def apply_value(self, node, usd_name: str, value, **kwargs) -> None:
        blender_name = self.get_native_input(usd_name)
        if blender_name == "_uv_map":
            if hasattr(node, "uv_map"):
                node.uv_map = str(value)
            return
        if not blender_name or blender_name not in node.inputs:
            return
        node.inputs[blender_name].default_value = value


class ActivisionMtlxMapper(MultiNodeShaderMapper):
    """Delegates to io_blender_mtlx's registered handler at runtime.

    Requires MaterialX Python — available in Blender 5.0+.
    The handler is looked up by shader_id from the vendored
    node_registry.materialx_nodes dict.
    """

    # Cached MaterialX document with standard library loaded.
    # Loading libraries is expensive, so we do it once across all instances.
    _mx_doc = None

    def create_network(self, tree, inputs, **kwargs):
        import MaterialX as mx
        from io_data_mtlx.lib.node_registry import materialx_nodes

        handler = materialx_nodes.get(self.shader_id)
        if handler is None:
            raise ValueError(
                f"No io_blender_mtlx handler for {self.shader_id}",
            )

        mx_node = self._create_mx_node(mx)
        return handler(tree, mx_node)

    def _create_mx_node(self, mx):
        """Create a minimal mx.Node for the vendored handler."""
        doc = self._get_mx_doc(mx)
        node_def = doc.getNodeDef(self.shader_id)
        if node_def is None:
            raise ValueError(
                f"NodeDef {self.shader_id} not found in MaterialX library",
            )
        category = node_def.getNodeString()
        output_type = node_def.getType()
        # Reuse a single scratch NodeGraph to avoid leaking
        ng = doc.getNodeGraph("_vendored_scratch")
        if ng is None:
            ng = doc.addNodeGraph("_vendored_scratch")
        for child in ng.getNodes():
            ng.removeNode(child.getName())
        return ng.addNode(category, self.shader_id, output_type)

    @classmethod
    def _get_mx_doc(cls, mx):
        """Load and cache the MaterialX standard library document."""
        if cls._mx_doc is None:
            doc = mx.createDocument()
            mx.loadLibraries(
                mx.getDefaultDataLibraryFolders(),
                mx.getDefaultDataSearchPath(),
                doc,
            )
            cls._mx_doc = doc
        return cls._mx_doc


class MaterialXStandardSurfaceMapper(MultiNodeShaderMapper):
    """Standard Surface → 5-node Blender network (fallback).

    Replicates io_blender_mtlx's standard_surface handler topology
    without requiring MaterialX Python. Used as a fallback when the
    vendored io_blender_mtlx import is unavailable.
    """

    _NODE_PREFIX = "MtlxStdSurf"

    def create_network(self, tree, inputs, **kwargs):
        bsdf = self._get_or_create_bsdf(tree)
        mix_base = self._ensure_node(
            tree, "ShaderNodeMix", f"{self._NODE_PREFIX}_MixBase",
        )
        huesat_base = self._ensure_node(
            tree, "ShaderNodeHueSaturation", f"{self._NODE_PREFIX}_HueSatBase",
        )
        mix_spec = self._ensure_node(
            tree, "ShaderNodeMix", f"{self._NODE_PREFIX}_MixSpec",
        )
        huesat_spec = self._ensure_node(
            tree, "ShaderNodeHueSaturation", f"{self._NODE_PREFIX}_HueSatSpec",
        )

        # Configure Mix nodes: RGBA multiply mode with factor=1
        for mix in (mix_base, mix_spec):
            mix.data_type = "RGBA"
            mix.blend_type = "MULTIPLY"
            mix.inputs[0].default_value = 1.0
            # Preprocessing gray from io_blender_mtlx Standard Surface handler
            mix.inputs[6].default_value = (0.604, 0.604, 0.604, 1.0)
            mix.inputs[7].default_value = (0.5, 0.5, 0.5, 1.0)

        # Configure HueSat nodes
        for hs in (huesat_base, huesat_spec):
            hs.inputs[0].default_value = 0.0   # Hue
            hs.inputs[1].default_value = 1.0   # Saturation
            hs.inputs[2].default_value = 1.0   # Value
            hs.inputs[3].default_value = 1.0   # Fac
            hs.inputs[4].default_value = (0.997, 1.0, 1.0, 1.0)

        # Base color path: HueSat → Mix.A, Mix.Result → BSDF.Base Color
        tree.links.new(huesat_base.outputs[0], mix_base.inputs[6])
        tree.links.new(mix_base.outputs[2], bsdf.inputs["Base Color"])

        # Specular path: HueSat → Mix.A, Mix.Result → BSDF.Specular Tint
        tree.links.new(huesat_spec.outputs[0], mix_spec.inputs[6])
        tree.links.new(mix_spec.outputs[2], bsdf.inputs["Specular Tint"])

        nodes = (bsdf, mix_base, huesat_base, mix_spec, huesat_spec)

        input_map = {
            # Preprocessed through HueSat → Mix chains
            "base": huesat_base.inputs[2],
            "base_color": mix_base.inputs[7],
            "specular": huesat_spec.inputs[2],
            "specular_color": mix_spec.inputs[7],
            # Direct to Principled BSDF (Blender 4.0+ socket names)
            "metalness": bsdf.inputs["Metallic"],
            "diffuse_roughness": bsdf.inputs["Diffuse Roughness"],
            "specular_IOR": bsdf.inputs["Specular IOR Level"],
            "specular_anisotropy": bsdf.inputs["Anisotropic"],
            "specular_rotation": bsdf.inputs["Anisotropic Rotation"],
            "specular_roughness": bsdf.inputs["Roughness"],
            "transmission": bsdf.inputs["Transmission Weight"],
            "subsurface": bsdf.inputs["Subsurface Weight"],
            "subsurface_radius": bsdf.inputs["Subsurface Radius"],
            "subsurface_scale": bsdf.inputs["Subsurface Scale"],
            "subsurface_anisotropy": bsdf.inputs["Subsurface Anisotropy"],
            "sheen": bsdf.inputs["Sheen Weight"],
            "sheen_color": bsdf.inputs["Sheen Tint"],
            "sheen_roughness": bsdf.inputs["Sheen Roughness"],
            "coat": bsdf.inputs["Coat Weight"],
            "coat_color": bsdf.inputs["Coat Tint"],
            "coat_roughness": bsdf.inputs["Coat Roughness"],
            "coat_ior": bsdf.inputs["Coat IOR"],
            "coat_normal": bsdf.inputs["Coat Normal"],
            "thin_film_thickness": bsdf.inputs["Thin Film Thickness"],
            "thin_film_IOR": bsdf.inputs["Thin Film IOR"],
            "emission": bsdf.inputs["Emission Strength"],
            "emission_color": bsdf.inputs["Emission Color"],
            "normal": bsdf.inputs["Normal"],
            "tangent": bsdf.inputs["Tangent"],
        }

        output_map = {"out": bsdf.outputs[0]}

        return nodes, input_map, output_map

    def _get_or_create_bsdf(self, tree):
        """Find existing Principled BSDF or create one."""
        for node in tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                return node
        return tree.nodes.new("ShaderNodeBsdfPrincipled")

    def _ensure_node(self, tree, node_type, name):
        """Get existing node by name, or create a new one."""
        existing = tree.nodes.get(name)
        if existing:
            return existing
        node = tree.nodes.new(node_type)
        node.name = name
        return node


# ---------------------------------------------------------------------------
# Default registry
# ---------------------------------------------------------------------------


def _register_vendored_materialx(reg: ShaderMapperRegistry) -> None:
    """Register vendored io_blender_mtlx handlers as multi-node mappers.

    Runs inside Blender where MaterialX Python is available.
    Skips shader IDs that already have a registered mapper (e.g.,
    texture nodes that need our _load_image handling).
    Falls back to MaterialXStandardSurfaceMapper if import fails.
    """
    try:
        import os
        import sys

        _this_dir = os.path.dirname(os.path.abspath(__file__))
        _project_root = os.path.dirname(os.path.dirname(_this_dir))
        vendor_path = os.path.join(
            _project_root, "vendor", "io_blender_mtlx", "bl_env", "addons",
        )
        if vendor_path not in sys.path:
            sys.path.insert(0, vendor_path)

        from io_data_mtlx.lib.node_registry import materialx_nodes

        for shader_id in materialx_nodes:
            if reg.get(shader_id) is not None:
                continue
            reg.register(ActivisionMtlxMapper(shader_id, "", {}))
        LOG.info(
            "Registered %d vendored MaterialX handlers",
            sum(1 for s in materialx_nodes if reg.get(s)
                and isinstance(reg.get(s), ActivisionMtlxMapper)),
        )
    except Exception:
        LOG.debug(
            "io_blender_mtlx not available, using fallback Standard Surface mapper",
            exc_info=True,
        )
        reg.register(MaterialXStandardSurfaceMapper(
            "ND_standard_surface_surfaceshader", "ShaderNodeBsdfPrincipled", {},
        ))


def create_default_registry() -> ShaderMapperRegistry:
    """Build the default registry with all known shader mappings."""
    reg = ShaderMapperRegistry()

    _preview_surface_map = {
        "diffuseColor": "Base Color",
        "metallic": "Metallic",
        "roughness": "Roughness",
        "emissiveColor": "Emission Color",
        "clearcoat": "Coat Weight",
        "clearcoatRoughness": "Coat Roughness",
        "opacity": "Alpha",
        "ior": "IOR",
        "specularColor": "Specular Tint",
    }
    reg.register(PBRShaderMapper(
        "UsdPreviewSurface", "ShaderNodeBsdfPrincipled",
        _preview_surface_map,
    ))
    reg.register(PBRShaderMapper(
        "ND_UsdPreviewSurface_surfaceshader", "ShaderNodeBsdfPrincipled",
        _preview_surface_map,
    ))

    _tex_map = {"file": "_image", "st": "Vector"}
    reg.register(TextureShaderMapper(
        "UsdUVTexture", "ShaderNodeTexImage", _tex_map,
    ))
    for mtlx_tex in ("ND_tiledimage_color3", "ND_tiledimage_float",
                      "ND_image_color3"):
        reg.register(TextureShaderMapper(
            mtlx_tex, "ShaderNodeTexImage", {"file": "_image"},
        ))

    reg.register(UVReaderMapper(
        "UsdPrimvarReader_float2", "ShaderNodeUVMap",
        {"varname": "_uv_map"},
    ))

    # Vendored MaterialX handlers — adds Standard Surface, OpenPBR,
    # and 23 utility node handlers from io_blender_mtlx.
    # Skips IDs already registered above (textures, UV reader).
    _register_vendored_materialx(reg)

    return reg
