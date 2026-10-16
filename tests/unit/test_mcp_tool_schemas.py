"""Optional MCP tool arguments that default to None must also accept null.

``host: str = None`` makes FastMCP advertise ``"default": null`` on a string-only
field, so a client that sends the default back explicitly fails validation before
the tool runs. Schema/argument checks only: nothing connects to a server.
"""

from __future__ import annotations

import asyncio
import sys

import pytest


def _accepts_null(schema: dict) -> bool:
    if "anyOf" in schema:
        return any(_accepts_null(s) for s in schema["anyOf"])
    t = schema.get("type")
    return t is None or t == "null" or (isinstance(t, list) and "null" in t)


@pytest.fixture(scope="module")
def server():
    # Import here and unload afterwards, so 'mcp' left in sys.modules doesn't defeat
    # test_mcp_optin's "mcp group missing" simulation.
    loaded = set(sys.modules)
    pytest.importorskip("mcp")
    from integrations.mcp.config import McpConfig
    from integrations.mcp.tools import build_server

    yield build_server(McpConfig())
    for name in set(sys.modules) - loaded:
        if name == "mcp" or name.startswith("mcp."):
            del sys.modules[name]


def test_null_default_params_accept_null(server):
    bad = [
        f"{tool.name}.{name}"
        for tool in asyncio.run(server.list_tools())
        for name, prop in tool.inputSchema.get("properties", {}).items()
        if "default" in prop and prop["default"] is None and not _accepts_null(prop)
    ]
    assert bad == [], f"default null but null rejected: {bad}"


def _validate(server, tool: str, arguments: dict):
    meta = server._tool_manager.get_tool(tool).fn_metadata
    return meta.arg_model.model_validate(meta.pre_parse_json(arguments))


def test_explicit_null_passes_argument_validation(server):
    args = _validate(server, "usd_connect", {"host": None, "port": None})
    assert args.host is None and args.port is None
    assert _validate(server, "usd_get_prim", {"path": "/World", "fields": None}).fields is None
    assert _validate(server, "usd_playback_control", {"action": "play", "rate": None}).rate is None


def test_values_still_parse_as_before(server):
    assert _validate(server, "usd_connect", {"host": "127.0.0.1"}).host == "127.0.0.1"
    args = _validate(server, "usd_get_prim", {"path": "/World", "fields": '["xform"]'})
    assert args.fields == ["xform"]
