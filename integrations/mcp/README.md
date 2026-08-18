# OpenUSDConnect MCP server

Exposes the OpenUSDConnect event protocol as MCP tools so any MCP-compatible
host can author USD scenes (meshes, references/payloads, transforms,
instancing, and UsdPreviewSurface / MaterialX shader networks) and stream them
to the sync server. Network client only; scene authoring uses the core event
protocol, and the local USD mirror negotiates the optional logical-layer
replay capability.

## Quick start

```bash
uv sync --group bundled-usd --group mcp
uv run python -m openusdconnect.server --port 7200     # in one terminal
```

Register this stdio command with your MCP host:

```bash
uv --directory <repo> run python -m integrations.mcp
```

Many MCP hosts accept a configuration shaped like this:

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
