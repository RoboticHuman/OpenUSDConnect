# OpenUSDConnect MCP server

Exposes the OpenUSDConnect event protocol as MCP tools so Claude can author USD
scenes (meshes, references/payloads, transforms, instancing, and
UsdPreviewSurface / MaterialX shader networks) and stream them to the sync
server. Network client only; scene authoring uses the core event protocol, and
the local USD mirror negotiates the optional logical-layer replay capability.

## Quick start

```bash
uv sync --group mcp
uv run python -m openusdconnect.server --port 7200     # in one terminal
```

Register with Claude Code:

```bash
claude mcp add openusdconnect -- uv --directory <repo> run python -m integrations.mcp
```

or Claude Desktop (`claude_desktop_config.json`):

```json
{ "mcpServers": { "openusdconnect": {
  "command": "uv",
  "args": ["--directory", "<repo>", "run", "python", "-m", "integrations.mcp"],
  "env": { "OPENUSDCONNECT_PORT": "7200" }
}}}
```

Configure via `OPENUSDCONNECT_HOST` / `_PORT` / `_CLIENT_ID` / `_DEPARTMENT`.

See [`docs/mcp-server-usage.md`](../../docs/mcp-server-usage.md) for the full
tool reference, authoring recipes, and architecture.

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
