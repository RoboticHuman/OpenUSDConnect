"""Helpers for UsdShade input/output attribute names on the protocol wire."""

from __future__ import annotations

from dataclasses import dataclass

SHADER_INPUT_PREFIX = "inputs:"
SHADER_OUTPUT_PREFIX = "outputs:"
SHADER_ATTR_SIDE_INPUT = "input"
SHADER_ATTR_SIDE_OUTPUT = "output"


@dataclass(frozen=True, slots=True)
class ShaderAttr:
    """A UsdShade input or output attribute without a prim path.

    USD scene description uses namespace-qualified property names such as
    ``inputs:diffuseColor`` and ``outputs:surface``.  Internally, keeping the
    side and base name split makes it clearer when code is branching on input
    vs output behavior.
    """

    side: str
    base_name: str

    @classmethod
    def from_qualified_name(cls, attr_name) -> ShaderAttr | None:
        """Parse ``inputs:<name>`` / ``outputs:<name>`` into a ShaderAttr."""
        return _parse_qualified_attr(attr_name)

    @property
    def qualified_name(self) -> str:
        """Return the USD namespace-qualified attribute name."""
        if self.side == SHADER_ATTR_SIDE_INPUT:
            return SHADER_INPUT_PREFIX + self.base_name
        if self.side == SHADER_ATTR_SIDE_OUTPUT:
            return SHADER_OUTPUT_PREFIX + self.base_name
        raise ValueError(f"Unknown shader attribute side: {self.side!r}")

    @property
    def is_input(self) -> bool:
        return self.side == SHADER_ATTR_SIDE_INPUT

    @property
    def is_output(self) -> bool:
        return self.side == SHADER_ATTR_SIDE_OUTPUT


def shader_input_attr(base_name: str) -> ShaderAttr:
    """Build a ShaderAttr for an ``inputs:`` property."""
    return ShaderAttr(SHADER_ATTR_SIDE_INPUT, base_name)


def shader_output_attr(base_name: str) -> ShaderAttr:
    """Build a ShaderAttr for an ``outputs:`` property."""
    return ShaderAttr(SHADER_ATTR_SIDE_OUTPUT, base_name)


def split_qualified_attr(attr_name) -> tuple[str, str]:
    """Split a USD-style namespaced shader attribute name.

    Returns ("input", base) for "inputs:<base>",
            ("output", base) for "outputs:<base>",
    and ("", "") for anything else (non-string, missing prefix, or empty
    base name).
    """
    attr = _parse_qualified_attr(attr_name)
    if attr is None:
        return "", ""
    return attr.side, attr.base_name


def _parse_qualified_attr(attr_name) -> ShaderAttr | None:
    if not isinstance(attr_name, str):
        return None
    if attr_name.startswith(SHADER_INPUT_PREFIX) and len(attr_name) > len(SHADER_INPUT_PREFIX):
        return ShaderAttr(
            SHADER_ATTR_SIDE_INPUT,
            attr_name[len(SHADER_INPUT_PREFIX) :],
        )
    if attr_name.startswith(SHADER_OUTPUT_PREFIX) and len(attr_name) > len(SHADER_OUTPUT_PREFIX):
        return ShaderAttr(
            SHADER_ATTR_SIDE_OUTPUT,
            attr_name[len(SHADER_OUTPUT_PREFIX) :],
        )
    return None
