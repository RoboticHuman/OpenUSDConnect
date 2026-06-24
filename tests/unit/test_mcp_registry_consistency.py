"""Drift guard: the MCP tool table must cover exactly the protocol event kinds.

Mirrors ``test_event_registry_consistency``: adding a core event kind makes
``set(TOOL_TABLE) == EVENT_KEYS`` fail until an MCP ``ToolRow`` is added.
"""

from __future__ import annotations

from integrations.mcp.registry import TOOL_TABLE
from openusdconnect.protocol_constants import EVENT_KEYS


def test_tool_table_covers_every_event_kind():
    assert set(TOOL_TABLE) == EVENT_KEYS, (
        f"missing rows: {EVENT_KEYS - set(TOOL_TABLE)}; orphan rows: {set(TOOL_TABLE) - EVENT_KEYS}"
    )


def test_each_row_kind_matches_its_key():
    for key, row in TOOL_TABLE.items():
        assert row.kind == key, f"{key}: row.kind is {row.kind!r}"
        assert row.summary, f"{key}: empty summary"
        assert callable(row.build), f"{key}: build is not callable"


def test_no_orphan_rows():
    for key in TOOL_TABLE:
        assert key in EVENT_KEYS, f"{key}: in TOOL_TABLE but not EVENT_KEYS"
