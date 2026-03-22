"""Blender shader mapper implementations.

Subclasses of the core ShaderMapper ABC for translating USD shader
types to Blender node types. Register new shader support by adding
a mapper to create_default_registry().
"""

from __future__ import annotations

import logging

from openusdconnect.adapters import ShaderMapper, ShaderMapperRegistry

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


# ---------------------------------------------------------------------------
# Default registry
# ---------------------------------------------------------------------------


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

    reg.register(PBRShaderMapper(
        "ND_standard_surface_surfaceshader", "ShaderNodeBsdfPrincipled",
        {
            "base_color": "Base Color",
            "metalness": "Metallic",
            "specular_roughness": "Roughness",
            "emission_color": "Emission Color",
            "coat": "Coat Weight",
            "coat_roughness": "Coat Roughness",
            "opacity": "Alpha",
            "specular_IOR": "IOR",
        },
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

    return reg
