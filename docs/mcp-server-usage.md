# OpenUSDConnect MCP server

The [MCP](https://modelcontextprotocol.io) integration exposes OpenUSDConnect
operations as local stdio tools. A client can author USD transactions and
inspect the composed result through an in-memory mirror.

The MCP process is a network client built on the core library (`EventSender` +
`ReceiverThread` + `EventDispatcher` + `UsdStageAdapter`), the same shape as the
`usdview` integration. Every scene event it sends uses the core protocol. Its
USD mirror also negotiates the optional layered-replay capability so authored
logical-layer opinions retain their server strength ordering during live sync
and replay. Departments remain the server policy that assigns clients to those
logical layers.

## Install

The managed runtime from `scripts/build_openusd.py` is selected automatically.
Configure an external OpenUSD build when applicable, then install the MCP
dependency:

```bash
uv sync --group mcp
```

Use `uv sync --group bundled-usd --group mcp` only for a renderer-neutral
session that needs neither MaterialX nor custom plugins.

## Run

Start a sync server (one is required because the MCP is network-only):

```text
uv run openusdconnect-server --port 7200 --event-log mcp-session.db --export-diff mcp-session-changes.usda
```

Keep that terminal open. Register the MCP process with any client that supports
local stdio servers. Use the absolute path to the cloned repository in place of
`<repo>`:

```json
{
  "mcpServers": {
    "openusdconnect": {
      "command": "uv",
      "args": ["--directory", "<repo>", "run", "--no-sync", "python", "scripts/run_with_openusd.py", "--", "python", "-m", "integrations.mcp"],
      "env": { "OPENUSDCONNECT_PORT": "7200" }
    }
  }
}
```

The wrapper applies the registered managed runtime to the MCP process. Add
`--usd-root <OpenUSDInstall>` before `--` for an external build. Add its
repeatable `--plugin-path` or `--dll-dir` options, or `--renderman-root`, before
the `--` separator when required. On Windows JSON paths either use forward
slashes, such as `D:/Workspace/OpenUSDConnect`, or escape each backslash.

For the bundled fallback, replace `args` with:

```json
["--directory", "<repo>", "run", "--no-sync", "python", "-m", "integrations.mcp"]
```

`uv --directory <repo>` sets the working directory so `integrations.mcp`
resolves. `--no-sync` preserves the runtime selected during installation.

The MCP client starts and owns this stdio process. It is separate from the TCP
sync server in the first terminal. MCP protocol messages use stdout; diagnostics
use stderr.

## Verify the connection

Ask the MCP client to call these tools in order:

1. `usd_connect()` should return `connected: true`, host `127.0.0.1`, and port
   `7200`. On an existing session it also drains the initial replay for up to
   the configured read timeout.
2. `usd_status()` should report the same endpoint, `mirror_enabled: true`, and
   `mirror_synchronized: true`. If a large initial replay exceeds the timeout,
   poll `usd_status()` until that field becomes true before reading prims.
3. `usd_ensure_prim(prim="/World/McpSmoke", type_name="Xform")` should return
   `ok: true`, `sent: true`, and normally `mirror_synced: true`.
4. `usd_get_prim(path="/World/McpSmoke")` should return that path with type
   `Xform`.

The dashboard is optional and is not required for this smoke test. To include
it, install `--group dashboard` and start the server with
`--dashboard-port 8080`.

When finished, call `usd_disconnect()`, then let the MCP client stop its stdio
server process. Stop the TCP sync server with `Ctrl+C`. The server keeps
`mcp-session.db` for replay and writes `mcp-session-changes.usda` on shutdown.
The MCP mirror itself is in memory and is not a separate saved USD file. Use new
artifact names for an independent session.

## Configuration

Flags (CLI) override environment variables override defaults.

| Env var | Flag | Default | Purpose |
|---|---|---|---|
| `OPENUSDCONNECT_HOST` | `--host` | `127.0.0.1` | Sync server host |
| `OPENUSDCONNECT_PORT` | `--port` | `7200` | Sync server port |
| `OPENUSDCONNECT_CLIENT_ID` | `--client-id` | `<user>-<host>-mcp` | Client identity |
| `OPENUSDCONNECT_DEPARTMENT` | `--department` | _(none)_ | Department identity; ordering requires server `--departments` policy |
| `OPENUSDCONNECT_MIRROR` | `--mirror` / `--no-mirror` | on | In-memory mirror for introspection |
| `OPENUSDCONNECT_AUTO_CONNECT` | `--auto-connect` / `--no-auto-connect` | on | Auto-connect on first authoring tool |
| `OPENUSDCONNECT_AUTO_ANCESTORS` | `--auto-create-ancestors` / `--no-auto-create-ancestors` | on | Auto-create missing parent prims (as `Xform`) |
| `OPENUSDCONNECT_READ_TIMEOUT` | `--read-after-write-timeout` | `2.0` | Seconds to wait for initial replay or post-write mirror visibility |

## Tools

### Session

- `usd_connect(host?, port?, client_id?, department?)` connect + start mirror.
- `usd_status()` connection state, mirror prim count, last sequence, metadata.
- `usd_disconnect()`.

Authoring tools auto-connect when that option is enabled. Mirror-backed
introspection requires an established connection, so call `usd_connect` before
inspecting a fresh session or when targeting a non-default server.

### Authoring

- `usd_send_events(events)` validates and sends managed authoring event dicts as
  **one atomic transaction**. It is the primary tool for composite authoring,
  such as a whole mesh or shader network.
- One thin tool per publicly exposed managed event kind (`usd_ensure_prim`, `usd_set_xform_trs`,
  `usd_set_gprim_attrs`, `usd_set_reference`, `usd_set_payload`,
  `usd_set_material_binding`, `usd_set_connectable_input`,
  `usd_set_connectable_connection`, `usd_set_point_instancer`,
  `usd_set_instanceable`, `usd_set_variant_selections`,
  `usd_set_stage_metadata`, ...). Each sends a single event. Shared-stage layer
  transport kinds are internal and are not exposed as MCP tools.

`usd_set_xform_trs` accepts a quaternion `r=[w,x,y,z]` or, for convenience,
`rotate_euler=[rx,ry,rz]` degrees + `rotate_order` (converted to a quaternion).

### Introspection

- `usd_scene_summary(under?)`: total/active/material counts, max depth, and a
  count-by-type histogram. Cheap orientation before listing a large scene.
- `usd_list_prims(under?, type_name?, is_a?, max?, offset?, depth?)`: filter by
  exact `type_name` and/or schema base `is_a` (`"UsdGeomMesh"`, `"UsdGeomGprim"`,
  `"UsdGeomImageable"`, ...); `depth` limits descent (`1` = immediate children).
  Page large scenes with `offset`/`max` and the returned `next_offset` cursor.
- `usd_get_prim(path, fields?)`: type, schemas, transform, attributes, variant
  selections, material binding. Pass `fields` (e.g. `["xform","material_binding"]`)
  to return only those sections; the child list is capped (`child_count` exact).
- `usd_get_attributes(path, names?, max_items?)`: read just the attributes you
  need. With `names`, returns `{name: value}` for those only (arrays summarized to
  `max_items`); without `names`, a values-free index (name, type, array length).
- `usd_get_prims(paths, fields?)`: batch `usd_get_prim` in one call (pairs with
  `usd_changes_since`); an unresolved path yields an error entry, not a failure.
- `usd_get_bounds(path)`: world-space bounding box (`min`/`max`/`center`/`size`)
  of a prim and its subtree, transform chain composed. Use for relative placement
  (beside / on top of / aligned with) without fetching geometry or doing the math.
- `usd_changes_since(since_seq?, max?)`: prims changed since a sequence (oldest
  first). Poll with the `last_seq` from `usd_status`/a prior call to find your own
  and other clients' edits without re-listing the scene, then `usd_get_prims`
  those paths. Efficient diffs for thousand-object scenes.
- `usd_describe_shader_network(material_path)`: full ConnectableAPI topology.
- `usd_get_stage_metadata()`

### Shader discovery

These tools read the local Sdr registry and do not require a server connection.

- `usd_list_shader_nodes(filter?, source_type?, max?)` `source_type` is
  `"mtlx"` (MaterialX), `"glslfx"` (UsdPreviewSurface family), or `"USD"`.
- `usd_describe_shader_node(info_id)` exact input/output names + Sdf types +
  defaults. Use this to author correct `info_id`, `input_types`, and connection
  endpoints.

### Playback

- `usd_claim_playback(time?)`, `usd_playback_control(action, time?, rate?)`.
- `usd_playback_status()` read the shared playhead (playing/time/rate, leader
  client id, whether this client leads) from the latest broadcast PlaybackState.

## Authoring recipes

#### Procedural mesh

This is one atomic transaction; missing ancestors are created automatically.

```json
[
  {"k":"ensure_prim","prim":"/World/Quad","typeName":"Mesh"},
  {"k":"set_gprim_attrs","prim":"/World/Quad","attrs":{
     "points":[[-1,-1,0],[1,-1,0],[1,1,0],[-1,1,0]],
     "faceVertexCounts":[4],"faceVertexIndices":[0,1,2,3],
     "primvars:st":[[0,0],[1,0],[1,1],[0,1]]},
   "primvar_meta":{"primvars:st":{"typeName":"texCoord2f[]","interpolation":"vertex"}}},
  {"k":"ensure_xform_ops","prim":"/World/Quad"},
  {"k":"set_xform_trs","prim":"/World/Quad","fields":["t"],"t":[0,1,0]}
]
```

#### UsdPreviewSurface material and binding

Discover input types first with
`usd_describe_shader_node("UsdPreviewSurface")`, then send one transaction:

```json
[
  {"k":"ensure_prim","prim":"/World/Looks/Brass","typeName":"Material"},
  {"k":"ensure_prim","prim":"/World/Looks/Brass/Surface","typeName":"Shader"},
  {"k":"set_connectable_input","prim":"/World/Looks/Brass/Surface",
   "info_id":"UsdPreviewSurface",
   "inputs":{"diffuseColor":[0.71,0.65,0.26],"roughness":0.3,"metallic":1.0},
   "input_types":{"diffuseColor":"color3f","roughness":"float","metallic":"float"}},
  {"k":"set_connectable_connection","prim":"/World/Looks/Brass",
   "connections":{"outputs:surface":
     {"source_prim":"/World/Looks/Brass/Surface","source_attr":"outputs:surface"}}},
  {"k":"set_material_binding","prim":"/World/Quad","material_path":"/World/Looks/Brass"}
]
```

For **MaterialX**, use an `ND_*` shader ID (for example,
`ND_standard_surface_surfaceshader`). The Material terminal is
`outputs:mtlx:surface` connected to the shader's `outputs:out`. Reference an
external `.mtlx` document instead with `set_reference`
(`asset_path` ending in `.mtlx`).

Verify any network with
`usd_describe_shader_network("/World/Looks/Brass")`.

## Implementation notes

- **Emit + mirror.** The MCP emits via `EventSender` and keeps a read-only
  `Usd.Stage` mirror through `ReceiverThread`, replaying from sequence 1. The
  server broadcasts committed records to every receiver, including the
  producer's receiver, so the mirror contains the authoritative result of both
  local and remote edits. The emitter and receiver use distinct diagnostic
  origins but share one `client_id`, giving both roles the same stable client
  identity. The server routes edits to its shared managed layer or to the
  configured department layer.
- **Atomicity.** `usd_send_events` and every per-kind tool send exactly one
  transaction; the server applies it create → structural → value.
- **Read-after-write.** After sending, the session drains the mirror (bounded by
  `read_after_write_timeout_s`) so the next introspection call reflects the write
  (`mirror_synced` in the result reports whether the wait completed).
- **Validation.** Before sending, every event is checked for dict shape, path
  validity, schema `typeName`/`api_schemas`, Sdf type names (`input_types`,
  `primvar_meta`), and connection-source existence. Failures return a structured
  `{ok:false, error:{code,message,field,hint}}` and nothing is sent.

## Extending

To expose a newly added core event kind through the MCP, add one row to
`TOOL_TABLE` in `integrations/mcp/registry.py`. `test_mcp_registry_consistency`
fails until the row exists. New shader nodes, including custom MaterialX nodes,
need no MCP changes; `usd_describe_shader_node` reads them from the Sdr
registry.

## Testing

```bash
uv run pytest tests/unit/test_mcp_*.py -v                 # no server needed
uv run pytest tests/integration/test_mcp_*.py -v          # spins up a real server
```
