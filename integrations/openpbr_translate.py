"""Receiver-side OpenPBR -> standard_surface translation for RenderMan (hdPrman).

RenderMan 27.x's hdPrman MaterialX adapter has no entry for the OpenPBR surface
node, so an OpenPBR material renders unshaded and errors. MaterialX ships a
translation nodegraph (``ND_open_pbr_surface_to_standard_surface``) that maps
OpenPBR parameters onto ``standard_surface``, which hdPrman *does* render. This
module rewrites OpenPBR materials on a stage to standard_surface driven by that
nodegraph, entirely in UsdShade: no ``.mtlx`` round-trip and no MaterialX
Python dependency (the translate nodegraph and both surface defs are already in
USD's Sdr, loaded from the MaterialX data libraries shipped with the build).

This is renderer-specific *receiver-side* conditioning: the core keeps
replicating OpenPBR faithfully; only a hdPrman-targeting consumer (the usdview
RenderMan path) calls this. It is idempotent: after translation a material's
terminal no longer points at an OpenPBR shader, so re-scans skip it.

Connected OpenPBR inputs (e.g. textures) are mirrored as connections and update
on their own. Plain-value inputs are copied into the translate node, and every
pass re-syncs them from the OpenPBR shader (which stays in the graph after
translation), so a live edit to an OpenPBR parameter reaches the standard_surface
on the next receive tick.

Each transferred input also carries its source colorspace so color conversion is
preserved. The translation graph exposes only value and color ports (no normal or
tangent), so there is no world-space stream to translate.
"""

from __future__ import annotations

from pxr import Sdf, Sdr, Usd, UsdShade

OPENPBR_ID = "ND_open_pbr_surface_surfaceshader"
STANDARD_SURFACE_ID = "ND_standard_surface_surfaceshader"
TRANSLATE_ID = "ND_open_pbr_surface_to_standard_surface"


def _shader_id(connectable: UsdShade.ConnectableAPI) -> str:
    shader = UsdShade.Shader(connectable.GetPrim())
    if not shader:
        return ""
    attr = shader.GetIdAttr()
    return attr.Get() if attr else ""


def _find_openpbr_terminal(material: UsdShade.Material):
    """Return ``(terminal_output, openpbr_shader)`` if the material's surface
    terminal is driven by an OpenPBR shader, else ``(None, None)``."""
    for out in material.GetOutputs():
        sources, _ = out.GetConnectedSources()
        for src in sources:
            if _shader_id(src.source) == OPENPBR_ID:
                return out, UsdShade.Shader(src.source.GetPrim())
    return None, None


def _translate_one(stage, material, terminal, openpbr, reg) -> bool:
    xnode = reg.GetShaderNodeByIdentifier(TRANSLATE_ID)
    stdnode = reg.GetShaderNodeByIdentifier(STANDARD_SURFACE_ID)
    if xnode is None or stdnode is None:
        return False  # MaterialX translation libraries not available in this build
    std_inputs = set(stdnode.GetShaderInputNames())
    mat_path = material.GetPath()

    xlate = UsdShade.Shader.Define(stage, mat_path.AppendChild("OpenPBRtoStd"))
    xlate.CreateIdAttr(TRANSLATE_ID)
    for name in xnode.GetShaderInputNames():
        src_input = openpbr.GetInput(name)
        if not src_input or not src_input.GetAttr().IsAuthored():
            continue
        xi = xlate.CreateInput(name, xnode.GetShaderInput(name).GetTypeAsSdfType().GetSdfType())
        sources, _ = src_input.GetConnectedSources()
        if sources:
            xi.ConnectToSource(sources[0])  # mirror the upstream connection
        else:
            value = src_input.Get()
            if value is not None:
                xi.Set(value)
        # Carry the source input's colorspace (e.g. srgb_texture) so the renderer
        # applies the same color conversion the source declared.
        if src_input.GetAttr().HasColorSpace():
            xi.GetAttr().SetColorSpace(src_input.GetAttr().GetColorSpace())

    std = UsdShade.Shader.Define(stage, mat_path.AppendChild("StandardSurface"))
    std.CreateIdAttr(STANDARD_SURFACE_ID)
    for out_name in xnode.GetShaderOutputNames():
        std_name = out_name[:-4] if out_name.endswith("_out") else out_name
        if std_name not in std_inputs:
            continue
        xo = xlate.CreateOutput(
            out_name, xnode.GetShaderOutput(out_name).GetTypeAsSdfType().GetSdfType()
        )
        sti = std.CreateInput(
            std_name, stdnode.GetShaderInput(std_name).GetTypeAsSdfType().GetSdfType()
        )
        sti.ConnectToSource(xo)
    std_out = std.CreateOutput("out", Sdf.ValueTypeNames.Token)

    terminal.ConnectToSource(std_out)  # repoint the material terminal to standard_surface
    return True


def _find_openpbr_shader(material: UsdShade.Material):
    """Return the OpenPBR shader anywhere under ``material``, or None.

    After translation the OpenPBR shader is no longer the surface terminal but
    stays in the graph, so it is found by id rather than via the terminal.
    """
    for prim in Usd.PrimRange(material.GetPrim()):
        if prim.IsA(UsdShade.Shader) and _shader_id(UsdShade.ConnectableAPI(prim)) == OPENPBR_ID:
            return UsdShade.Shader(prim)
    return None


def _refresh_translation(material: UsdShade.Material) -> None:
    """Re-copy value inputs from the OpenPBR shader into an existing translate
    node so live edits to OpenPBR parameters reach standard_surface. No-op unless
    the material was already translated."""
    stage = material.GetPrim().GetStage()
    xlate_prim = stage.GetPrimAtPath(material.GetPath().AppendChild("OpenPBRtoStd"))
    if not xlate_prim.IsValid():
        return
    openpbr = _find_openpbr_shader(material)
    if openpbr is None:
        return
    for xi in UsdShade.Shader(xlate_prim).GetInputs():
        if xi.HasConnectedSource():
            continue  # connected inputs are already live
        src = openpbr.GetInput(xi.GetBaseName())
        if not src or not src.GetAttr().IsAuthored():
            continue
        value = src.Get()
        if value is not None and xi.Get() != value:
            xi.Set(value)  # write only on a real change to avoid spurious re-renders


def translate_openpbr_materials(stage) -> int:
    """Translate OpenPBR-terminated materials on ``stage`` to standard_surface,
    refreshing the value inputs of any already-translated ones.

    Returns the count of materials newly translated this pass. Cheap to re-run
    each tick: already-translated materials are only refreshed (counted as 0).
    """
    reg = Sdr.Registry()
    count = 0
    for prim in stage.Traverse():
        if not prim.IsA(UsdShade.Material):
            continue
        material = UsdShade.Material(prim)
        terminal, openpbr = _find_openpbr_terminal(material)
        if openpbr is not None:
            if _translate_one(stage, material, terminal, openpbr, reg):
                count += 1
        else:
            _refresh_translation(material)
    return count


def _material_for_path(stage, path):
    """Return the UsdShade.Material owning ``path`` (itself or an ancestor), or
    None when the path is not under a material."""
    prim = stage.GetPrimAtPath(path)
    while prim and prim.IsValid() and not prim.IsPseudoRoot():
        if prim.IsA(UsdShade.Material):
            return UsdShade.Material(prim)
        prim = prim.GetParent()
    return None


def translate_openpbr_for_paths(stage, prim_paths) -> int:
    """Translate or refresh only the materials owning ``prim_paths``.

    The scoped counterpart to :func:`translate_openpbr_materials`, driven from
    the dispatcher's applied-event paths so an edit touches just its own
    material instead of re-scanning the whole stage.
    """
    reg = Sdr.Registry()
    seen = set()
    count = 0
    for path in prim_paths:
        material = _material_for_path(stage, path)
        if material is None or material.GetPath() in seen:
            continue
        seen.add(material.GetPath())
        terminal, openpbr = _find_openpbr_terminal(material)
        if openpbr is not None:
            if _translate_one(stage, material, terminal, openpbr, reg):
                count += 1
        else:
            _refresh_translation(material)
    return count
