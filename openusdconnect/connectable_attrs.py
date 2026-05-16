"""Helpers for UsdShade input/output attribute names on the protocol wire."""

from __future__ import annotations

from dataclasses import dataclass

USDSHADE_INPUT_PREFIX = "inputs:"
USDSHADE_OUTPUT_PREFIX = "outputs:"
SIDE_INPUT = "input"
SIDE_OUTPUT = "output"


@dataclass(frozen=True, slots=True)
class ConnectableAttr:
    """A UsdShade input or output attribute without a prim path.

    USD scene description uses namespace-qualified property names such as
    ``inputs:diffuseColor`` and ``outputs:surface``.  Internally, keeping the
    side and base name split makes it clearer when code is branching on input
    vs output behavior.
    """

    side: str
    base_name: str

    @classmethod
    def from_qualified_name(cls, attr_name) -> ConnectableAttr | None:
        """Parse ``inputs:<name>`` / ``outputs:<name>`` into a ConnectableAttr."""
        return _parse_qualified_attr(attr_name)

    @property
    def qualified_name(self) -> str:
        """Return the USD namespace-qualified attribute name."""
        if self.side == SIDE_INPUT:
            return USDSHADE_INPUT_PREFIX + self.base_name
        if self.side == SIDE_OUTPUT:
            return USDSHADE_OUTPUT_PREFIX + self.base_name
        raise ValueError(f"Unknown connectable attribute side: {self.side!r}")

    @property
    def is_input(self) -> bool:
        return self.side == SIDE_INPUT

    @property
    def is_output(self) -> bool:
        return self.side == SIDE_OUTPUT


def input_attr(base_name: str) -> ConnectableAttr:
    """Build a ConnectableAttr for an ``inputs:`` property."""
    return ConnectableAttr(SIDE_INPUT, base_name)


def output_attr(base_name: str) -> ConnectableAttr:
    """Build a ConnectableAttr for an ``outputs:`` property."""
    return ConnectableAttr(SIDE_OUTPUT, base_name)


def split_qualified_attr(attr_name) -> tuple[str, str]:
    """Split a USD-style namespaced UsdShade attribute name.

    Returns ("input", base) for "inputs:<base>",
            ("output", base) for "outputs:<base>",
    and ("", "") for anything else (non-string, missing prefix, or empty
    base name).
    """
    attr = _parse_qualified_attr(attr_name)
    if attr is None:
        return "", ""
    return attr.side, attr.base_name


def _parse_qualified_attr(attr_name) -> ConnectableAttr | None:
    if not isinstance(attr_name, str):
        return None
    if attr_name.startswith(USDSHADE_INPUT_PREFIX) and len(attr_name) > len(USDSHADE_INPUT_PREFIX):
        return ConnectableAttr(
            SIDE_INPUT,
            attr_name[len(USDSHADE_INPUT_PREFIX) :],
        )
    out_prefix = USDSHADE_OUTPUT_PREFIX
    if attr_name.startswith(out_prefix) and len(attr_name) > len(out_prefix):
        return ConnectableAttr(SIDE_OUTPUT, attr_name[len(out_prefix) :])
    return None
