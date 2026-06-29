"""Value-level OpenPBR -> standard_surface parameter translation.

A faithful Python port of MaterialX's official ``ND_open_pbr_surface_to_standard_surface``
translation nodegraph (``libraries/bxdf/translation/open_pbr_to_standard_surface.mtlx``).
Where :mod:`integrations.openpbr_translate` rewires a USD graph so a *renderer*
evaluates that nodegraph (hdPrman), this computes the resulting standard_surface
parameter values directly -- for DCC shader mappers that build native nodes and
need the resolved values, not a graph to evaluate.

Keep this in lockstep with the MaterialX library version shipped in the build;
``tests/unit/test_openpbr_to_standard_surface.py`` pins the math against the spec.
"""

from __future__ import annotations

# OpenPBR input defaults, verbatim from the translation nodedef.
OPENPBR_DEFAULTS: dict = {
    "base_weight": 1.0,
    "base_color": (0.8, 0.8, 0.8),
    "base_diffuse_roughness": 0.0,
    "base_metalness": 0.0,
    "specular_weight": 1.0,
    "specular_color": (1.0, 1.0, 1.0),
    "specular_roughness": 0.3,
    "specular_ior": 1.5,
    "specular_roughness_anisotropy": 0.0,
    "transmission_weight": 0.0,
    "transmission_color": (1.0, 1.0, 1.0),
    "transmission_depth": 0.0,
    "transmission_scatter": (0.0, 0.0, 0.0),
    "transmission_scatter_anisotropy": 0.0,
    "transmission_dispersion_scale": 0.0,
    "subsurface_weight": 0.0,
    "subsurface_color": (0.8, 0.8, 0.8),
    "subsurface_radius": 1.0,
    "subsurface_radius_scale": (1.0, 0.5, 0.25),
    "subsurface_scatter_anisotropy": 0.0,
    "fuzz_weight": 0.0,
    "fuzz_color": (1.0, 1.0, 1.0),
    "fuzz_roughness": 0.5,
    "coat_weight": 0.0,
    "coat_color": (1.0, 1.0, 1.0),
    "coat_roughness": 0.0,
    "coat_roughness_anisotropy": 0.0,
    "coat_ior": 1.6,
    "coat_darkening": 1.0,
    "thin_film_weight": 0.0,
    "thin_film_thickness": 0.5,
    "thin_film_ior": 1.4,
    "emission_luminance": 0.0,
    "emission_color": (1.0, 1.0, 1.0),
    "geometry_opacity": 1.0,
    "geometry_thin_walled": False,
}


def _c(v):
    """Coerce a scalar or sequence to a 3-tuple of floats (color3)."""
    if isinstance(v, (int, float)):
        return (float(v), float(v), float(v))
    return (float(v[0]), float(v[1]), float(v[2]))


def _mul_cc(a, b):
    return (a[0] * b[0], a[1] * b[1], a[2] * b[2])


def _mul_cf(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def _mix_c(fg, bg, t):
    """MaterialX <mix>: bg at t=0, fg at t=1."""
    return (bg[0] + (fg[0] - bg[0]) * t,
            bg[1] + (fg[1] - bg[1]) * t,
            bg[2] + (fg[2] - bg[2]) * t)


def _mix_f(fg, bg, t):
    return bg + (fg - bg) * t


def _coat_base_darkening(p) -> tuple:
    """The ``modulated_base_darkening`` color the nodegraph multiplies base /
    subsurface color by, modelling how a coat darkens what sits under it."""
    coat_ior = p["coat_ior"]
    f0_sqrt = (coat_ior - 1.0) / (coat_ior + 1.0)
    coat_F0 = f0_sqrt * f0_sqrt
    Kcoat = 1.0 - (1.0 - coat_F0) / (coat_ior * coat_ior)

    base_color = _c(p["base_color"])
    Emetal = _mul_cf(base_color, p["specular_weight"])
    Edielectric = _mix_c(_c(p["subsurface_color"]), base_color, p["subsurface_weight"])
    Ebase = _mix_c(Emetal, Edielectric, p["base_metalness"])

    one_minus_Kcoat = 1.0 - Kcoat
    base_darkening = tuple(
        one_minus_Kcoat / (1.0 - e * Kcoat) for e in Ebase
    )
    t = p["coat_weight"] * p["coat_darkening"]
    return _mix_c(base_darkening, (1.0, 1.0, 1.0), t)


def open_pbr_to_standard_surface(inputs: dict) -> dict:
    """Translate OpenPBR parameter values to standard_surface values.

    ``inputs`` holds any subset of OpenPBR input names; absent ones take the
    schema default. Returns standard_surface input names -> values (colors as
    3-tuples), ready to drive a standard_surface shader mapper.
    """
    p = {**OPENPBR_DEFAULTS, **inputs}
    darkening = _coat_base_darkening(p)
    return {
        "base": float(p["base_weight"]),
        "base_color": _mul_cc(_c(p["base_color"]), darkening),
        "diffuse_roughness": float(p["base_diffuse_roughness"]),
        "metalness": float(p["base_metalness"]),
        "specular": float(p["specular_weight"]),
        "specular_color": _c(p["specular_color"]),
        "specular_roughness": _mix_f(
            float(p["coat_roughness"]), float(p["specular_roughness"]),
            float(p["coat_weight"]),
        ),
        "specular_IOR": float(p["specular_ior"]),
        "specular_anisotropy": float(p["specular_roughness_anisotropy"]),
        "transmission": float(p["transmission_weight"]),
        "transmission_color": _c(p["transmission_color"]),
        "transmission_depth": float(p["transmission_depth"]),
        "transmission_scatter": _c(p["transmission_scatter"]),
        "transmission_scatter_anisotropy": float(p["transmission_scatter_anisotropy"]),
        "transmission_dispersion": float(p["transmission_dispersion_scale"]),
        "subsurface": float(p["subsurface_weight"]),
        "subsurface_color": _mul_cc(_c(p["subsurface_color"]), darkening),
        "subsurface_radius": _c(p["subsurface_radius_scale"]),
        "subsurface_scale": float(p["subsurface_radius"]),
        "subsurface_anisotropy": float(p["subsurface_scatter_anisotropy"]),
        "sheen": float(p["fuzz_weight"]),
        "sheen_color": _c(p["fuzz_color"]),
        "sheen_roughness": float(p["fuzz_roughness"]) ** 2.5,
        "coat": float(p["coat_weight"]),
        "coat_color": _c(p["coat_color"]),
        "coat_roughness": float(p["coat_roughness"]),
        "coat_anisotropy": float(p["coat_roughness_anisotropy"]),
        "coat_IOR": float(p["coat_ior"]),
        "coat_affect_roughness": 1.0,
        "thin_film_thickness": (
            float(p["thin_film_thickness"]) * 1000.0
            if float(p["thin_film_weight"]) > 0.0 else 0.0
        ),
        "thin_film_IOR": float(p["thin_film_ior"]),
        "emission": float(p["emission_luminance"]),
        "emission_color": _c(p["emission_color"]),
        "opacity": _c(p["geometry_opacity"]),
        "thin_walled": bool(p["geometry_thin_walled"]),
    }
