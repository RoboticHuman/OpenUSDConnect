"""Blender shader mapper implementations.

Subclasses of the core ShaderMapper ABC for translating USD shader
types to Blender node types. Register new shader support by adding
a mapper to create_default_registry().
"""

from __future__ import annotations

import logging
import os

from openusdconnect.adapters import MultiNodeShaderMapper, ShaderMapper, ShaderMapperRegistry

LOG = logging.getLogger(__name__)

try:
    import bpy

    BPY_AVAILABLE = True
except Exception:
    BPY_AVAILABLE = False


class BlenderShaderMapper(ShaderMapper):
    """Base Blender mapper applies values to Blender node sockets."""

    def apply_value(self, node, usd_name: str, value, **kwargs) -> None:
        blender_name = self.get_native_input(usd_name)
        if not blender_name or blender_name not in node.inputs:
            return
        inp = node.inputs[blender_name]
        if isinstance(value, list) and len(value) == 3:
            inp.default_value = (*value, 1.0)  # RGB → RGBA
        else:
            inp.default_value = value

    def read_value(self, node, usd_name: str):
        """Read a single input value from a Blender node back to USD form."""
        blender_name = self.get_native_input(usd_name)
        if not blender_name or blender_name.startswith("_"):
            return None
        if blender_name not in node.inputs:
            return None
        inp = node.inputs[blender_name]
        if inp.is_linked:
            return None
        val = inp.default_value
        if hasattr(val, "__len__") and len(val) == 4:
            return [float(val[0]), float(val[1]), float(val[2])]
        return float(val)

    def read_all_inputs(self, node) -> dict:
        """Read all mapped input values from a Blender node."""
        result = {}
        for usd_name in self._input_map:
            val = self.read_value(node, usd_name)
            if val is not None:
                result[usd_name] = val
        return result


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
    """UsdUVTexture and MaterialX image nodes → Image Texture.

    The optional *colorspace* selects the Blender image color space for
    loaded textures: "sRGB" for albedo/color textures, "Non-Color" for
    data textures (normal maps, roughness, metallic) so Blender's
    sampling pipeline doesn't apply gamma correction to vector data.
    """

    def __init__(self, shader_id: str, node_type: str, input_map: dict,
                 colorspace: str = "sRGB"):
        super().__init__(shader_id, node_type, input_map)
        self._colorspace = colorspace

    def apply_value(self, node, usd_name: str, value, **kwargs) -> None:
        blender_name = self.get_native_input(usd_name)
        if blender_name == "_image":
            self._load_image(node, value, kwargs.get("resolve_asset"))
            return
        if not blender_name or blender_name not in node.inputs:
            return
        node.inputs[blender_name].default_value = value

    def read_all_inputs(self, node) -> dict:
        """Read the assigned image's filepath back as the ``file`` input.

        Returns the absolute filesystem path (matching the wire convention
        for asset values); empty when the node has no image or the image
        has no filepath (generated/packed-only datablocks).
        """
        img = getattr(node, "image", None)
        filepath = getattr(img, "filepath", "") if img is not None else ""
        if not filepath:
            return {}
        if BPY_AVAILABLE:
            filepath = bpy.path.abspath(filepath)
        return {"file": os.path.normpath(filepath)}

    def _load_image(self, node, value, resolve_asset):
        if not BPY_AVAILABLE or not isinstance(value, str) or not value:
            return
        img = bpy.data.images.get(value)
        if not img:
            resolved = resolve_asset(value) if resolve_asset else None
            if resolved:
                # check_existing reuses a datablock already loaded from this
                # absolute path (e.g. by Blender's USD importer) instead of
                # creating a duplicate.
                img = bpy.data.images.load(resolved, check_existing=True)
        # Only assign when we resolved a real image.  If resolution failed
        # and the node already has an image (commonly loaded by Blender's
        # USD importer using pxr's asset resolver), keep it clobbering
        # with a placeholder loses the working texture.
        if img is not None:
            node.image = img
            if hasattr(img, "colorspace_settings"):
                img.colorspace_settings.name = self._colorspace


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

    def read_all_inputs(self, node) -> dict:
        """Read the UV map name back as the ``varname`` input."""
        uv_map = getattr(node, "uv_map", "")
        return {"varname": str(uv_map)} if uv_map else {}


class AttributeReaderMapper(ShaderMapper):
    """Non-UV UsdPrimvarReader variants → ShaderNodeAttribute.

    Routes USD's varname input to Blender's attribute_name so the node
    reads the named primvar (vertex color, custom normal, etc.) at
    render time.
    """

    def apply_value(self, node, usd_name: str, value, **kwargs) -> None:
        blender_name = self.get_native_input(usd_name)
        if blender_name == "_attribute_name":
            if hasattr(node, "attribute_name"):
                node.attribute_name = str(value)
            return
        if not blender_name or blender_name not in node.inputs:
            return
        node.inputs[blender_name].default_value = value

    def read_all_inputs(self, node) -> dict:
        """Read the attribute name back as the ``varname`` input."""
        attribute_name = getattr(node, "attribute_name", "")
        return {"varname": str(attribute_name)} if attribute_name else {}


class NormalMapShaderMapper(ShaderMapper):
    """ND_normalmap_* → ShaderNodeNormalMap.

    Both MaterialX and Blender's Normal Map node interpret tangent-space
    normal map textures using the OpenGL convention (R=X, G=+Y, B=Z), so
    no coordinate flip is needed at the output the decoded world-space
    normal feeds straight into BSDF.Normal.
    """

    def apply_value(self, node, usd_name: str, value, **kwargs) -> None:
        blender_name = self.get_native_input(usd_name)
        if not blender_name or blender_name not in node.inputs:
            return
        node.inputs[blender_name].default_value = value


def _apply_glass_fixups(tree, bsdf, input_map, inputs) -> None:
    """Rewire a standard_surface Principled BSDF for transmissive (glass) looks.

    Blender's Principled BSDF has one IOR socket (both specular reflection and
    refraction) and no transmission-color socket: it tints transmitted light by
    Base Color. MaterialX standard_surface keeps specular_IOR, base_color, and
    transmission_color separate, and glass sets base=0 (no diffuse). Mapped
    literally that drives Base Color to black, so the glass renders opaque, and
    specular_IOR lands on the specular weight rather than the refraction index.
    When transmission is active, send specular_IOR to the IOR socket and the
    transmission tint to Base Color, bypassing the diffuse base chain.
    """
    transmission = inputs.get("transmission")
    if not (isinstance(transmission, (int, float)) and transmission > 0.0):
        return
    base_color = bsdf.inputs["Base Color"]
    for link in list(base_color.links):
        tree.links.remove(link)
    base_color.default_value = (1.0, 1.0, 1.0, 1.0)
    input_map["specular_IOR"] = bsdf.inputs["IOR"]
    input_map["transmission_color"] = base_color
    input_map.pop("base", None)
    input_map.pop("base_color", None)


class MaterialXStandardSurfaceMapper(MultiNodeShaderMapper):
    """MaterialX Standard Surface -> a 5-node Blender Principled network.

    The base/specular weight and color are combined through a HueSat -> Mix
    chain into the Principled Base Color and Specular Tint; the remaining
    standard_surface inputs map directly onto Principled sockets.
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
            # Neutral preprocessing grey on the A input (overwritten by the
            # HueSat link below); only the B factor carries the authored color.
            mix.inputs[6].default_value = (0.604, 0.604, 0.604, 1.0)
        # The B factor is the multiply color, defaulting to the standard_surface
        # schema value used when the event omits it: base_color grey, specular_color
        # white. A provided base_color/specular_color overwrites these via input_map.
        mix_base.inputs[7].default_value = (0.8, 0.8, 0.8, 1.0)
        mix_spec.inputs[7].default_value = (1.0, 1.0, 1.0, 1.0)

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
            "subsurface_color": mix_base.inputs[7],
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

        subsurface_weight = inputs.get("subsurface", 0.0)
        if (
            isinstance(subsurface_weight, (int, float))
            and subsurface_weight > 0.0
            and "subsurface_color" in inputs
        ):
            input_map.pop("base_color", None)

        output_map = {"out": bsdf.outputs[0]}

        _apply_glass_fixups(tree, bsdf, input_map, inputs)
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


class OpenPBRSurfaceMapper(MaterialXStandardSurfaceMapper):
    """OpenPBR surface -> standard_surface (official MaterialX translation) -> Blender.

    Builds the standard_surface network, then routes each OpenPBR input to the
    right standard_surface socket per ``ND_open_pbr_surface_surfaceshader_to_standard_surface``.
    Most inputs are passthrough renames (values and connected textures flow
    straight through). The few computed channels get real Blender nodes only when
    active, so a connected texture is handled correctly: coat-darkening of the
    base color (when ``coat`` > 0), the spec-roughness/coat-roughness mix (coat),
    and the sheen-roughness power (fuzz). See ``integrations.openpbr_to_standard_surface``
    for the value-level port the node graph mirrors.
    """

    _PREFIX = "MtlxOpenPBR"

    # OpenPBR input name -> standard_surface input_map key (pure passthroughs).
    _PASSTHROUGH = {
        "base_weight": "base",
        "base_diffuse_roughness": "diffuse_roughness",
        "base_metalness": "metalness",
        "specular_weight": "specular",
        "specular_color": "specular_color",
        "specular_ior": "specular_IOR",
        "specular_roughness_anisotropy": "specular_anisotropy",
        "transmission_weight": "transmission",
        "subsurface_weight": "subsurface",
        "subsurface_color": "subsurface_color",
        "subsurface_radius": "subsurface_scale",
        "subsurface_radius_scale": "subsurface_radius",
        "subsurface_scatter_anisotropy": "subsurface_anisotropy",
        "fuzz_weight": "sheen",
        "fuzz_color": "sheen_color",
        "coat_weight": "coat",
        "coat_color": "coat_color",
        "coat_roughness": "coat_roughness",
        "coat_roughness_anisotropy": "coat_anisotropy",
        "coat_ior": "coat_ior",
        "thin_film_ior": "thin_film_IOR",
        "emission_luminance": "emission",
        "emission_color": "emission_color",
        "normal": "normal",
        "tangent": "tangent",
    }

    def create_network(self, tree, inputs, **kwargs):
        from integrations.openpbr_to_standard_surface import open_pbr_to_standard_surface

        std_values = open_pbr_to_standard_surface(inputs)
        nodes, std_map, output_map = super().create_network(tree, std_values, **kwargs)

        input_map = {}
        for opbr_name, std_key in self._PASSTHROUGH.items():
            socket = std_map.get(std_key)
            if socket is not None:
                input_map[opbr_name] = socket

        coat_weight = float(inputs.get("coat_weight", 0.0) or 0.0)
        fuzz_weight = float(inputs.get("fuzz_weight", 0.0) or 0.0)

        # base_color: identity when uncoated; coat darkens it (per-pixel, so it
        # must be Blender nodes for a connected texture to darken correctly).
        if coat_weight > 0.0 and "base_color" in std_map:
            input_map["base_color"] = self._build_coat_darkening(
                tree, inputs, std_map["base_color"],
            )
        else:
            input_map["base_color"] = std_map["base_color"]

        # specular_roughness = mix(coat_roughness, specular_roughness, coat_weight).
        if coat_weight > 0.0 and "specular_roughness" in std_map:
            mix = self._ensure_node(tree, "ShaderNodeMix", f"{self._PREFIX}_SpecRough")
            mix.data_type = "FLOAT"
            mix.inputs[0].default_value = coat_weight
            mix.inputs[3].default_value = float(inputs.get("coat_roughness", 0.0) or 0.0)
            tree.links.new(mix.outputs[0], std_map["specular_roughness"])
            input_map["specular_roughness"] = mix.inputs[2]
        elif "specular_roughness" in std_map:
            input_map["specular_roughness"] = std_map["specular_roughness"]

        # sheen_roughness = fuzz_roughness ** 2.5.
        if fuzz_weight > 0.0 and "sheen_roughness" in std_map:
            pw = self._ensure_node(tree, "ShaderNodeMath", f"{self._PREFIX}_SheenRough")
            pw.operation = "POWER"
            pw.inputs[1].default_value = 2.5
            tree.links.new(pw.outputs[0], std_map["sheen_roughness"])
            input_map["fuzz_roughness"] = pw.inputs[0]

        return nodes, input_map, output_map

    def _build_coat_darkening(self, tree, inputs, base_color_socket):
        """Build the coat base-color darkening sub-network; return its color input
        socket. Scalar coat/metalness/subsurface params are plain-value constants
        (textures driving those under coat are out of scope); base_color flows as
        a node so a connected albedo texture darkens per-pixel."""
        coat_ior = float(inputs.get("coat_ior", 1.6) or 1.6)
        f0_sqrt = (coat_ior - 1.0) / (coat_ior + 1.0)
        kcoat = 1.0 - (1.0 - f0_sqrt * f0_sqrt) / (coat_ior * coat_ior)
        metal = float(inputs.get("base_metalness", 0.0) or 0.0)
        spec_w = float(inputs.get("specular_weight", 1.0) or 1.0)
        ssw = float(inputs.get("subsurface_weight", 0.0) or 0.0)
        ss = inputs.get("subsurface_color", (0.8, 0.8, 0.8))
        mod = (float(inputs.get("coat_weight", 0.0) or 0.0)
               * float(inputs.get("coat_darkening", 1.0) or 1.0))
        # Ebase = base_color * a + b  (a float, b color); base_darkening = num / (C - base_color*D)
        a = spec_w * metal + (1.0 - ssw) * (1.0 - metal)
        b = tuple(float(ss[i]) * ssw * (1.0 - metal) for i in range(3))
        num = 1.0 - kcoat
        d = a * kcoat
        c = tuple(1.0 - b[i] * kcoat for i in range(3))

        def mix(name, blend, fac, a_val, b_val):
            n = self._ensure_node(tree, "ShaderNodeMix", f"{self._PREFIX}_{name}")
            n.data_type = "RGBA"
            n.blend_type = blend
            n.inputs[0].default_value = fac
            if a_val is not None:
                n.inputs[6].default_value = a_val
            if b_val is not None:
                n.inputs[7].default_value = b_val
            return n

        carrier = mix("BCCarrier", "MIX", 0.0, None, (1.0, 1.0, 1.0, 1.0))
        s1 = mix("BC1", "MULTIPLY", 1.0, None, (d, d, d, 1.0))
        s2 = mix("BC2", "SUBTRACT", 1.0, (*c, 1.0), None)
        s3 = mix("BC3", "DIVIDE", 1.0, (num, num, num, 1.0), None)
        s4 = mix("BC4", "MULTIPLY", 1.0, None, (mod, mod, mod, 1.0))
        s5 = mix("BC5", "ADD", 1.0, None, (1.0 - mod, 1.0 - mod, 1.0 - mod, 1.0))
        s6 = mix("BC6", "MULTIPLY", 1.0, None, None)

        tree.links.new(carrier.outputs[2], s1.inputs[6])
        tree.links.new(s1.outputs[2], s2.inputs[7])
        tree.links.new(s2.outputs[2], s3.inputs[7])
        tree.links.new(s3.outputs[2], s4.inputs[6])
        tree.links.new(s4.outputs[2], s5.inputs[6])
        tree.links.new(carrier.outputs[2], s6.inputs[6])
        tree.links.new(s5.outputs[2], s6.inputs[7])
        tree.links.new(s6.outputs[2], base_color_socket)
        return carrier.inputs[6]


# ---------------------------------------------------------------------------
# MaterialX utility-node mappers (one Blender node each, in1/in2 -> out),
# implemented per the MaterialX spec. Registered as non-surface multi-node
# mappers so set_connectable_connection routes through their input/output maps.
# ---------------------------------------------------------------------------


class _BinaryMathMapper(MultiNodeShaderMapper):
    """MaterialX binary math/vector op -> one Blender Math or Vector Math node."""

    def __init__(self, shader_id, node_type, operation, *, out_index=0, defaults=None):
        super().__init__(shader_id, node_type, {})
        self._node_type = node_type
        self._operation = operation
        self._out_index = out_index
        self._defaults = defaults or {}

    @property
    def is_surface_shader(self) -> bool:
        return False

    def create_network(self, tree, inputs, **kwargs):
        n = tree.nodes.new(self._node_type)
        n.operation = self._operation
        for idx, val in self._defaults.items():
            n.inputs[idx].default_value = val
        in_map = {"in1": n.inputs[0], "in2": n.inputs[1]}
        return (n,), in_map, {"out": n.outputs[self._out_index]}


class _MixMapper(MultiNodeShaderMapper):
    """MaterialX mix/multiply (color3 or vector3) -> one Blender Mix node.

    MaterialX ``mix`` outputs ``bg`` at mix=0 and ``fg`` at mix=1, so fg maps to
    Blender's B (factor=1) socket and bg to A (factor=0). ``multiply`` uses the
    MULTIPLY blend at factor 1.
    """

    def __init__(self, shader_id, blend_type, data_type, port_names):
        super().__init__(shader_id, "ShaderNodeMix", {})
        self._blend = blend_type
        self._data_type = data_type
        self._port_names = port_names  # {mtlx_name: "a"|"b"|"factor"}

    @property
    def is_surface_shader(self) -> bool:
        return False

    def create_network(self, tree, inputs, **kwargs):
        n = tree.nodes.new("ShaderNodeMix")
        n.data_type = self._data_type
        n.blend_type = self._blend
        if self._data_type == "RGBA":
            slots = {"factor": n.inputs[0], "a": n.inputs[6], "b": n.inputs[7]}
            out = n.outputs[2]
        else:  # VECTOR
            n.factor_mode = "UNIFORM"
            slots = {"factor": n.inputs["Factor"], "a": n.inputs["A"], "b": n.inputs["B"]}
            out = n.outputs["Result"]
        if "factor" not in self._port_names:
            slots["factor"].default_value = 1.0  # multiply: full blend
        in_map = {mtlx: slots[role] for mtlx, role in self._port_names.items()}
        return (n,), in_map, {"out": out}


class _IfEqualMapper(MultiNodeShaderMapper):
    """MaterialX ifequal -> a Compare (Math) node driving a Mix.

    Outputs ``in1`` when ``value1 == value2`` (COMPARE returns 1 -> Mix factor 1
    -> the B socket), else ``in2`` (factor 0 -> A). ``data_type`` is FLOAT, RGBA,
    or VECTOR.
    """

    def __init__(self, shader_id, data_type):
        super().__init__(shader_id, "ShaderNodeMix", {})
        self._data_type = data_type

    @property
    def is_surface_shader(self) -> bool:
        return False

    def create_network(self, tree, inputs, **kwargs):
        cmp = tree.nodes.new("ShaderNodeMath")
        cmp.operation = "COMPARE"
        cmp.inputs[2].default_value = 0.0  # epsilon
        mix = tree.nodes.new("ShaderNodeMix")
        mix.data_type = self._data_type
        if self._data_type == "FLOAT":
            factor, a, b, out = mix.inputs[0], mix.inputs[2], mix.inputs[3], mix.outputs[0]
        elif self._data_type == "RGBA":
            factor, a, b, out = mix.inputs[0], mix.inputs[6], mix.inputs[7], mix.outputs[2]
        else:  # VECTOR
            mix.factor_mode = "UNIFORM"
            factor = mix.inputs["Factor"]
            a, b, out = mix.inputs["A"], mix.inputs["B"], mix.outputs["Result"]
        tree.links.new(cmp.outputs[0], factor)
        in_map = {"value1": cmp.inputs[0], "value2": cmp.inputs[1], "in2": a, "in1": b}
        return (cmp, mix), in_map, {"out": out}


class _ExtractColor4Mapper(MultiNodeShaderMapper):
    """MaterialX extract (a color4 channel by index) -> a Separate Color output.

    ``index`` is resolved at build time (a constant in practice) to pick the
    Separate Color output. Channels 0-2 (RGB) are supported; the alpha channel
    (3) isn't separable from a Blender Color socket and is clamped to blue.
    """

    def __init__(self, shader_id):
        super().__init__(shader_id, "ShaderNodeSeparateColor", {})

    @property
    def is_surface_shader(self) -> bool:
        return False

    def create_network(self, tree, inputs, **kwargs):
        idx = int(inputs.get("index", 0) or 0)
        if not 0 <= idx <= 2:
            LOG.warning("extract_color4 index %d unsupported on a Color socket; using blue", idx)
            idx = 2
        node = tree.nodes.new("ShaderNodeSeparateColor")
        return (node,), {"in": node.inputs[0]}, {"out": node.outputs[idx]}


class _SingleNodeUtilityMapper(MultiNodeShaderMapper):
    """MaterialX utility node -> one configured Blender node with a fixed port map."""

    def __init__(self, shader_id, node_type, *, inputs, output, setup=None):
        super().__init__(shader_id, node_type, {})
        self._node_type = node_type
        self._inputs = inputs
        self._output = output
        self._setup = setup

    @property
    def is_surface_shader(self) -> bool:
        return False

    def create_network(self, tree, inputs, **kwargs):
        n = tree.nodes.new(self._node_type)
        if self._setup:
            self._setup(n)
        in_map = {mtlx: n.inputs[key] for mtlx, key in self._inputs.items()}
        out_map = {"out": n.outputs[self._output]} if self._output is not None else {}
        return (n,), in_map, out_map


# ---------------------------------------------------------------------------
# Default registry
# ---------------------------------------------------------------------------


def _register_native_materialx(reg: ShaderMapperRegistry) -> None:
    """Register the MaterialX surface + utility mappers."""
    reg.register(MaterialXStandardSurfaceMapper(
        "ND_standard_surface_surfaceshader", "ShaderNodeBsdfPrincipled", {},
    ))
    reg.register(OpenPBRSurfaceMapper(
        "ND_open_pbr_surface_surfaceshader", "ShaderNodeBsdfPrincipled", {},
    ))
    _register_native_utility(reg)


def _combine_color_hsv(node) -> None:
    node.mode = "HSV"


def _register_native_utility(reg: ShaderMapperRegistry) -> None:
    """Register native MaterialX utility-node mappers (math, mix, convert, ...)."""
    reg.register(_BinaryMathMapper(
        "ND_multiply_float", "ShaderNodeMath", "MULTIPLY", defaults={1: 1.0}))
    reg.register(_BinaryMathMapper(
        "ND_multiply_vector2", "ShaderNodeVectorMath", "MULTIPLY",
        defaults={1: (1.0, 1.0, 1.0)}))
    reg.register(_BinaryMathMapper(
        "ND_divide_vector2", "ShaderNodeVectorMath", "DIVIDE",
        defaults={1: (1.0, 1.0, 1.0)}))
    reg.register(_BinaryMathMapper(
        "ND_subtract_vector2", "ShaderNodeVectorMath", "SUBTRACT"))
    reg.register(_BinaryMathMapper(
        "ND_distance_vector3", "ShaderNodeVectorMath", "DISTANCE", out_index=1))

    reg.register(_MixMapper("ND_multiply_color3", "MULTIPLY", "RGBA",
                            {"in1": "a", "in2": "b"}))
    reg.register(_MixMapper("ND_mix_color3", "MIX", "RGBA",
                            {"fg": "b", "bg": "a", "mix": "factor"}))
    reg.register(_MixMapper("ND_mix_vector3", "MIX", "VECTOR",
                            {"fg": "b", "bg": "a", "mix": "factor"}))

    reg.register(_SingleNodeUtilityMapper(
        "ND_texcoord_vector2", "ShaderNodeUVMap", inputs={}, output="UV"))
    reg.register(_SingleNodeUtilityMapper(
        "ND_surfacematerial", "ShaderNodeOutputMaterial",
        inputs={"surfaceshader": 0}, output=None))
    reg.register(_SingleNodeUtilityMapper(
        "ND_convert_float_color3", "ShaderNodeCombineColor",
        inputs={"in": 2}, output=0, setup=_combine_color_hsv))

    # 4-component MaterialX types ride Blender Color (RGBA) sockets, so dropping
    # the 4th component to color3/vector3 is a passthrough (RGB/XYZ is read).
    for conv in ("ND_convert_color3_vector3", "ND_convert_color4_color3",
                 "ND_convert_vector4_vector3"):
        reg.register(_SingleNodeUtilityMapper(conv, "NodeReroute", inputs={"in": 0}, output=0))

    reg.register(_IfEqualMapper("ND_ifequal_floatB", "FLOAT"))
    reg.register(_IfEqualMapper("ND_ifequal_color3B", "RGBA"))
    reg.register(_IfEqualMapper("ND_ifequal_vector3B", "VECTOR"))

    reg.register(_ExtractColor4Mapper("ND_extract_color4"))


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
    # Color-typed MaterialX image variants load as sRGB so Blender's
    # color pipeline gamma-corrects them for display correctness.
    for color_tex in (
        "ND_image_color3", "ND_image_color4",
        "ND_tiledimage_color3", "ND_tiledimage_color4",
    ):
        reg.register(TextureShaderMapper(
            color_tex, "ShaderNodeTexImage", {"file": "_image"},
            colorspace="sRGB",
        ))
    # Data-typed image variants (floats, vectors) carry per-pixel data,
    # not color load as Non-Color so Blender skips gamma correction.
    # Critical for normal maps, roughness, metallic, displacement, etc.
    for data_tex in (
        "ND_image_float",
        "ND_image_vector2", "ND_image_vector3", "ND_image_vector4",
        "ND_tiledimage_float",
        "ND_tiledimage_vector2", "ND_tiledimage_vector3",
        "ND_tiledimage_vector4",
    ):
        reg.register(TextureShaderMapper(
            data_tex, "ShaderNodeTexImage", {"file": "_image"},
            colorspace="Non-Color",
        ))

    reg.register(UVReaderMapper(
        "UsdPrimvarReader_float2", "ShaderNodeUVMap",
        {"varname": "_uv_map"},
    ))
    for primvar_id in (
        "UsdPrimvarReader_float", "UsdPrimvarReader_float3",
        "UsdPrimvarReader_float4", "UsdPrimvarReader_int",
        "UsdPrimvarReader_normal", "UsdPrimvarReader_point",
        "UsdPrimvarReader_vector",
    ):
        reg.register(AttributeReaderMapper(
            primvar_id, "ShaderNodeAttribute",
            {"varname": "_attribute_name"},
        ))

    for nm_id in (
        "ND_normalmap_float", "ND_normalmap_vector2",
        "ND_normalmap_vector3", "ND_normalmap_vector4",
    ):
        reg.register(NormalMapShaderMapper(
            nm_id, "ShaderNodeNormalMap",
            {"in": "Color", "scale": "Strength"},
        ))

    # MaterialX surface + utility mappers (Standard Surface, OpenPBR, and the
    # utility nodes), skipping IDs already registered above (textures, UV reader).
    _register_native_materialx(reg)

    return reg
