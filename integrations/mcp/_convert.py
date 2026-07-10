"""Convert pxr/Gf/Vt values into JSON-serializable Python for tool results."""

from __future__ import annotations

from typing import Any


def to_jsonable(value: Any, *, max_items: int | None = None) -> Any:
    """Recursively convert a USD value to JSON-friendly Python.

    Gf vectors / Vt arrays become lists; unknown objects fall back to ``str``.
    When ``max_items`` is set, sequences longer than it are summarized as
    ``{"_array": True, "len": N, "sample": [...]}`` so large mesh arrays don't
    flood a tool result.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        items = list(value)
    except TypeError:
        return str(value)
    if max_items is not None and len(items) > max_items:
        return {
            "_array": True,
            "len": len(items),
            "sample": [to_jsonable(x, max_items=max_items) for x in items[:max_items]],
        }
    return [to_jsonable(x, max_items=max_items) for x in items]
