"""Sdr-backed discovery returns correct node ids, inputs, and Sdf types."""

from __future__ import annotations

import pytest

from integrations.mcp import discovery
from integrations.mcp.errors import ToolError


def test_describe_usd_preview_surface():
    d = discovery.describe_shader_node("UsdPreviewSurface")
    by_name = {i["name"]: i for i in d["inputs"]}
    assert {"diffuseColor", "roughness", "metallic", "opacity"} <= set(by_name)
    assert by_name["roughness"]["type"] == "float"
    assert by_name["diffuseColor"]["type"] == "color3f"
    assert any(o["name"] == "surface" for o in d["outputs"])


def test_describe_materialx_standard_surface():
    d = discovery.describe_shader_node("ND_standard_surface_surfaceshader")
    assert d["source_type"] == "mtlx"
    by_name = {i["name"]: i for i in d["inputs"]}
    assert by_name["base_color"]["type"] == "color3f"


def test_resolve_input_type():
    assert discovery.resolve_input_type("UsdPreviewSurface", "roughness") == "float"
    assert discovery.resolve_input_type("UsdPreviewSurface", "diffuseColor") == "color3f"
    assert discovery.resolve_input_type("UsdPreviewSurface", "nope") is None
    assert discovery.resolve_input_type("ND_not_real", "x") is None


def test_node_exists():
    assert discovery.node_exists("UsdPreviewSurface")
    assert discovery.node_exists("ND_standard_surface_surfaceshader")
    assert not discovery.node_exists("ND_not_a_real_node")
    assert not discovery.node_exists("")


def test_describe_unknown_raises():
    with pytest.raises(ToolError) as ei:
        discovery.describe_shader_node("ND_not_a_real_node")
    assert ei.value.code == "unknown_node"


def test_list_mtlx_nodes_filtered():
    r = discovery.list_shader_nodes(source_type="mtlx", max=5)
    assert r["count"] > 100
    assert r["returned"] == 5
    assert all(n["source_type"] == "mtlx" for n in r["nodes"])


def test_list_filter_substring():
    r = discovery.list_shader_nodes(filter="standard_surface", source_type="mtlx")
    assert r["count"] >= 1
    assert all("standard_surface" in n["info_id"] for n in r["nodes"])
