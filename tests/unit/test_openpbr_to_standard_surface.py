"""Pin the OpenPBR -> standard_surface value port against the MaterialX spec graph."""

import pytest

from integrations.openpbr_to_standard_surface import open_pbr_to_standard_surface as xlate


def test_defaults_match_schema():
    out = xlate({})
    assert out["base"] == 1.0
    assert out["base_color"] == pytest.approx((0.8, 0.8, 0.8))  # no coat -> no darkening
    assert out["specular"] == 1.0
    assert out["specular_color"] == pytest.approx((1.0, 1.0, 1.0))
    assert out["specular_roughness"] == pytest.approx(0.3)
    assert out["specular_IOR"] == pytest.approx(1.5)
    assert out["coat"] == 0.0
    assert out["coat_IOR"] == pytest.approx(1.6)
    assert out["coat_affect_roughness"] == 1.0
    assert out["thin_film_thickness"] == 0.0


def test_renames_passthrough():
    out = xlate({"base_metalness": 1.0, "fuzz_weight": 0.7, "fuzz_color": (0.2, 0.3, 0.4),
                 "emission_luminance": 5.0, "specular_ior": 1.7})
    assert out["metalness"] == 1.0
    assert out["sheen"] == 0.7                       # fuzz_weight -> sheen
    assert out["sheen_color"] == pytest.approx((0.2, 0.3, 0.4))
    assert out["emission"] == 5.0                    # emission_luminance -> emission
    assert out["specular_IOR"] == pytest.approx(1.7)


def test_no_coat_base_color_passthrough():
    bc = (0.85, 0.35, 0.12)
    out = xlate({"base_color": bc, "coat_weight": 0.0})
    assert out["base_color"] == pytest.approx(bc)    # darkening collapses to identity


def test_full_coat_darkens_base_color():
    # coat_weight=1, coat_ior=1.6, dielectric base 0.8: hand-derived from the graph
    # (Kcoat=0.6301775, base_darkening=0.745824 -> 0.8*0.745824).
    out = xlate({"base_color": (0.8, 0.8, 0.8), "coat_weight": 1.0})
    assert out["base_color"] == pytest.approx((0.596659, 0.596659, 0.596659), abs=1e-5)


def test_specular_roughness_mixes_to_coat_under_coat():
    out = xlate({"specular_roughness": 0.3, "coat_roughness": 0.1, "coat_weight": 0.5})
    assert out["specular_roughness"] == pytest.approx(0.2)   # mix(0.1, 0.3, 0.5)


def test_sheen_roughness_power_2_5():
    out = xlate({"fuzz_roughness": 0.5})
    assert out["sheen_roughness"] == pytest.approx(0.5 ** 2.5)


def test_thin_film_thickness_gated_and_scaled():
    off = xlate({"thin_film_weight": 0.0, "thin_film_thickness": 0.5})
    on = xlate({"thin_film_weight": 1.0, "thin_film_thickness": 0.5})
    assert off["thin_film_thickness"] == 0.0
    assert on["thin_film_thickness"] == pytest.approx(500.0)  # 0.5 * 1000


def test_subsurface_radius_remap():
    out = xlate({"subsurface_radius": 2.0, "subsurface_radius_scale": (1.0, 0.5, 0.25)})
    assert out["subsurface_scale"] == 2.0                         # radius (float) -> scale
    assert out["subsurface_radius"] == pytest.approx((1.0, 0.5, 0.25))  # radius_scale -> radius
