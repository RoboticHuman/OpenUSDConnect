"""Shader mapping contracts and legacy adapter imports."""

import pytest

from openusdconnect import adapters, shader_mapping
from openusdconnect.shader_mapping import ShaderMapper, ShaderMapperRegistry


class _Mapper(ShaderMapper):
    def apply_value(self, node, usd_name, value, **kwargs):
        node[self.get_native_input(usd_name)] = value


@pytest.mark.parametrize("name", ["ShaderMapper", "MultiNodeShaderMapper", "ShaderMapperRegistry"])
def test_adapter_reexports_preserve_identity(name):
    canonical = getattr(shader_mapping, name)
    assert getattr(adapters, name) is canonical
    assert canonical.__module__ == "openusdconnect.shader_mapping"


def test_input_mapping_and_private_native_inputs():
    mapper = _Mapper("TestShader", "TestNode", {"color": "Color", "file": "_image"})
    assert mapper.get_native_input("file") == "_image"
    assert mapper.get_native_input("unknown") is None
    assert mapper.get_usd_input("Color") == "color"
    assert mapper.get_usd_input("_image") is None
    assert mapper.get_usd_input("unknown") is None
    assert not mapper.is_multi_node
    node = {}
    mapper.apply_value(node, "color", [1, 0, 0])
    assert node == {"Color": [1, 0, 0]}


def test_registry_lookup_and_replacement_accept_legacy_subclasses():
    registry = ShaderMapperRegistry()
    assert registry.get("TestShader") is None
    assert registry.get_node_type("TestShader") is None
    first = _Mapper("TestShader", "FirstNode", {})
    assert isinstance(first, adapters.ShaderMapper)
    registry.register(first)
    assert registry.get("TestShader") is first
    assert registry.get_node_type("TestShader") == "FirstNode"
    replacement = _Mapper("TestShader", "SecondNode", {})
    registry.register(replacement)
    assert registry.get("TestShader") is replacement
    assert registry.get_node_type("TestShader") == "SecondNode"
