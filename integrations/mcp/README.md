# OpenUSDConnect MCP server

This package exposes the OpenUSDConnect event protocol as a local stdio MCP
server. It provides live USD authoring, mirror-backed introspection, shader
discovery, and playback tools while using the core sync protocol as a network
client.

For installation, client-neutral stdio configuration, a verified first run,
process and persistence behavior, the tool reference, and authoring recipes,
see the canonical
[`MCP server usage guide`](../../docs/mcp-server-usage.md).

## Layout

| Module | Responsibility |
|---|---|
| `cli.py` / `__main__.py` | Entry point (`python -m integrations.mcp`, stdio) |
| `config.py` | `McpConfig` (env + flags) |
| `session.py` | `ConnectionSession`: sender + mirror + read-after-write |
| `registry.py` | `TOOL_TABLE`: one builder per event kind (extend here) |
| `validation.py` | pxr-backed semantic validation + ancestor auto-create |
| `introspection.py` | Mirror-backed reads |
| `discovery.py` | Sdr shader-node discovery |
| `tools.py` | FastMCP assembly (`build_server`) |
