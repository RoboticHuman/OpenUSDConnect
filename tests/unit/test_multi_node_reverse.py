"""Tests for MultiNodeShaderMapper.read_all_inputs (reverse reading)."""

from openusdconnect.adapters import MultiNodeShaderMapper


class _MockSocket:
    """Minimal socket mock with default_value, is_linked, and node."""

    def __init__(self, value, linked=False):
        self.default_value = value
        self.is_linked = linked
        self.node = object()  # non-None sentinel


class _ConcreteMapper(MultiNodeShaderMapper):
    """Concrete subclass for testing (create_network is a no-op)."""

    def create_network(self, tree, inputs, **kwargs):
        return (), {}, {}


def _make_mapper():
    return _ConcreteMapper("TestShader", "TestNode", {})


class TestMultiNodeReadAllInputs:
    def test_reads_float_values(self):
        mapper = _make_mapper()
        sockets = {
            "roughness": _MockSocket(0.5),
            "metallic": _MockSocket(0.8),
        }
        result = mapper.read_all_inputs(input_map=sockets)
        assert result == {"roughness": 0.5, "metallic": 0.8}

    def test_reads_color3_values(self):
        mapper = _make_mapper()
        sockets = {"base_color": _MockSocket([0.1, 0.2, 0.3])}
        result = mapper.read_all_inputs(input_map=sockets)
        assert result == {"base_color": [0.1, 0.2, 0.3]}

    def test_rgba_converted_to_rgb(self):
        mapper = _make_mapper()
        sockets = {"base_color": _MockSocket([0.1, 0.2, 0.3, 1.0])}
        result = mapper.read_all_inputs(input_map=sockets)
        assert result == {"base_color": [0.1, 0.2, 0.3]}

    def test_skips_linked_sockets(self):
        mapper = _make_mapper()
        sockets = {
            "roughness": _MockSocket(0.5),
            "base_color": _MockSocket([1.0, 0.0, 0.0, 1.0], linked=True),
        }
        result = mapper.read_all_inputs(input_map=sockets)
        assert result == {"roughness": 0.5}
        assert "base_color" not in result

    def test_empty_map_returns_empty(self):
        mapper = _make_mapper()
        assert mapper.read_all_inputs(input_map={}) == {}

    def test_none_map_returns_empty(self):
        mapper = _make_mapper()
        assert mapper.read_all_inputs(input_map=None) == {}
        assert mapper.read_all_inputs() == {}

    def test_mixed_types(self):
        mapper = _make_mapper()
        sockets = {
            "roughness": _MockSocket(0.3),
            "coat_weight": _MockSocket(0.0),
            "base_color": _MockSocket([0.8, 0.2, 0.1, 1.0]),
            "emission_color": _MockSocket([1.0, 1.0, 1.0]),
            "normal": _MockSocket([0.0, 0.0, 1.0], linked=True),
        }
        result = mapper.read_all_inputs(input_map=sockets)
        assert result["roughness"] == 0.3
        assert result["coat_weight"] == 0.0
        assert result["base_color"] == [0.8, 0.2, 0.1]
        assert result["emission_color"] == [1.0, 1.0, 1.0]
        assert "normal" not in result
