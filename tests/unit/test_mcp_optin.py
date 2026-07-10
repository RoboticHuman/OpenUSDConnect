"""The MCP server is opt-in like the dashboard: the package imports without the
optional 'mcp' dependency group, and building the server without it fails with an
actionable message instead of a raw ModuleNotFoundError."""

import importlib
import sys

import pytest


def test_tools_imports_without_the_mcp_group(monkeypatch):
    # Reload tools with 'mcp' unavailable: it must import cleanly (no top-level
    # FastMCP), so unit tests / introspection work without the optional group.
    monkeypatch.setitem(sys.modules, "mcp", None)  # `import mcp...` now raises ImportError
    import integrations.mcp.tools as tools_mod

    importlib.reload(tools_mod)
    assert hasattr(tools_mod, "build_server")


def test_build_server_without_mcp_group_is_actionable(monkeypatch):
    monkeypatch.setitem(sys.modules, "mcp", None)
    from integrations.mcp.tools import build_server

    with pytest.raises(ImportError, match="uv sync --group mcp"):
        build_server()
