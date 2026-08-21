# OpenUSDConnect MCP server

This package is the stdio entry point for the OpenUSDConnect MCP integration.
It publishes USD authoring, mirror inspection, shader discovery, and playback
tools while connecting to the sync server as a normal network client.

See the [MCP server guide](../../docs/mcp-server-usage.md) for installation,
stdio configuration, the tool reference, and authoring examples.

## Layout

| Module | Responsibility |
| --- | --- |
| `cli.py` / `__main__.py` | Entry point (`python -m integrations.mcp`, stdio) |
| `config.py` | `McpConfig` (env + flags) |
| `session.py` | `ConnectionSession`: sender + mirror + read-after-write |
| `registry.py` | `TOOL_TABLE`: one builder per event kind (extend here) |
| `validation.py` | pxr-backed semantic validation + ancestor auto-create |
| `introspection.py` | Mirror-backed reads |
| `discovery.py` | Sdr shader-node discovery |
| `tools.py` | FastMCP assembly (`build_server`) |
