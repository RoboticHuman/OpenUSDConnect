# OpenUSDConnect MCP Server

An [MCP](https://modelcontextprotocol.io) server that exposes the OpenUSDConnect
event protocol as tools, so Claude (Desktop / Code) can author USD scenes —
procedural meshes, references/payloads, transforms, instancing, and especially
`UsdShade.ConnectableAPI` shader networks (UsdPreviewSurface + MaterialX) — and
stream them to the sync server, which fans them out to every connected DCC
(Blender, Unreal, usdview).

The MCP is a network **client** built on the core library (`EventSender` +
`ReceiverThread` + `EventDispatcher` + `UsdStageAdapter`), the same shape as the
`usdview` integration. It introduces **no** protocol changes: every event kind
it sends already exists in `openusdconnect`.

## Install

```bash
uv sync --group mcp     # installs the official `mcp` SDK alongside pxr
```

## Run

Start a sync server (one is required — the MCP is network-only):

```bash
uv run python -m openusdconnect.server --port 7200
```

Then register the MCP server with your client. **Claude Code:**

```bash
claude mcp add openusdconnect -- uv --directory D:\gamedev\OpenUSDConnect run python -m integrations.mcp
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "openusdconnect": {
      "command": "uv",
      "args": ["--directory", "D:\\gamedev\\OpenUSDConnect", "run", "python", "-m", "integrations.mcp"],
      "env": { "OPENUSDCONNECT_PORT": "7200" }
    }
  }
}
```

`uv --directory <repo>` sets the working dir so `integrations.mcp` resolves. The
MCP talks to the client over **stdio**; all of its own logging goes to stderr.

## Configuration

Flags (CLI) override environment variables override defaults.

| Env var | Flag | Default | Purpose |
|---|---|---|---|
| `OPENUSDCONNECT_HOST` | `--host` | `127.0.0.1` | Sync server host |
| `OPENUSDCONNECT_PORT` | `--port` | `7200` | Sync server port |
| `OPENUSDCONNECT_CLIENT_ID` | `--client-id` | `<user>-<host>-mcp` | Per-client layer id |
| `OPENUSDCONNECT_DEPARTMENT` | `--department` | _(none)_ | Layer-ordering department |
| `OPENUSDCONNECT_MIRROR` | `--no-mirror` | on | In-memory mirror for introspection |
| `OPENUSDCONNECT_AUTO_CONNECT` | — | on | Auto-connect on first authoring tool |
| `OPENUSDCONNECT_AUTO_ANCESTORS` | — | on | Auto-create missing parent prims (as `Xform`) |

## Tools

### Session
- `usd_connect(host?, port?, client_id?, department?)` — connect + start mirror.
- `usd_status()` — connection state, mirror prim count, last sequence, metadata.
- `usd_disconnect()`.

Authoring/introspection tools auto-connect, so `usd_connect` is only needed to
target a non-default server.

### Authoring
- `usd_send_events(events)` — validate + send a list of raw event dicts as **one
  atomic transaction**. The primary tool for composite authoring (a whole mesh
  or shader network at once).
- One thin tool per event kind (`usd_ensure_prim`, `usd_set_xform_trs`,
  `usd_set_gprim_attrs`, `usd_set_reference`, `usd_set_payload`,
  `usd_set_material_binding`, `usd_set_connectable_input`,
  `usd_set_connectable_connection`, `usd_set_point_instancer`,
  `usd_set_instanceable`, `usd_set_variant_selections`,
  `usd_set_stage_metadata`, ...). Each sends a single event.

`usd_set_xform_trs` accepts a quaternion `r=[w,x,y,z]` or, for convenience,
`rotate_euler=[rx,ry,rz]` degrees + `rotate_order` (converted to a quaternion).

### Introspection (mirror-backed)
- `usd_scene_summary(under?)` — total/active/material counts, max depth, and a
  count-by-type histogram. Cheap orientation before listing a large scene.
- `usd_list_prims(under?, type_name?, is_a?, max?, offset?, depth?)` — filter by
  exact `type_name` and/or schema base `is_a` (`"UsdGeomMesh"`, `"UsdGeomGprim"`,
  `"UsdGeomImageable"`, …); `depth` limits descent (`1` = immediate children).
  Page large scenes with `offset`/`max` and the returned `next_offset` cursor.
- `usd_get_prim(path, fields?)` — type, schemas, transform, attributes, variant
  selections, material binding. Pass `fields` (e.g. `["xform","material_binding"]`)
  to return only those sections; the child list is capped (`child_count` exact).
- `usd_get_prims(paths, fields?)` — batch `usd_get_prim` in one call (pairs with
  `usd_changes_since`); an unresolved path yields an error entry, not a failure.
- `usd_changes_since(since_seq?, max?)` — prims changed since a sequence (oldest
  first). Poll with the `last_seq` from `usd_status`/a prior call to find your own
  and other clients' edits without re-listing the scene, then `usd_get_prims`
  those paths. Efficient diffs for thousand-object scenes.
- `usd_describe_shader_network(material_path)` — full ConnectableAPI topology.
- `usd_get_stage_metadata()`

### Shader discovery (no connection needed)
- `usd_list_shader_nodes(filter?, source_type?, max?)` — `source_type` is
  `"mtlx"` (MaterialX, ~785 nodes), `"glslfx"` (UsdPreviewSurface family), or
  `"USD"`.
- `usd_describe_shader_node(info_id)` — exact input/output names + Sdf types +
  defaults. Use this to author correct `info_id`, `input_types`, and connection
  endpoints.

### Playback
- `usd_claim_playback(time?)`, `usd_playback_control(action, time?, rate?)`.
- `usd_playback_status()` — read the shared playhead (playing/time/rate, leader
  client id, whether this client leads) from the latest broadcast PlaybackState.

## Authoring recipes

**Procedural mesh** (one atomic transaction; ancestors auto-created):

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

**UsdPreviewSurface material + bind** — discover input types first with
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

**MaterialX** is identical except the shader's `info_id` is an `ND_*` id (e.g.
`ND_standard_surface_surfaceshader`) and the Material terminal is
`outputs:mtlx:surface` connected to the shader's `outputs:out`. Reference an
external `.mtlx` document instead with `set_reference`
(`asset_path` ending in `.mtlx`).

Verify any network with
`usd_describe_shader_network("/World/Looks/Brass")`.

## How it works

- **Emit + mirror.** The MCP emits via `EventSender` (origin `…-emit`) and keeps
  a read-only `Usd.Stage` mirror via `ReceiverThread` (origin `…-recv`),
  replaying from sequence 1. Differing origins make the server echo the MCP's own
  writes back, so the mirror is the server's authoritative composed result of the
  MCP's edits **and** every other DCC's. Same `client_id` keeps all MCP edits in
  one per-client layer.
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
fails until the row exists. New shader nodes (MaterialX, custom) need no MCP
changes — `usd_describe_shader_node` reads them from the Sdr registry.

## Testing

```bash
uv run pytest tests/unit/test_mcp_*.py -v                 # no server needed
uv run pytest tests/integration/test_mcp_*.py -v          # spins up a real server
```
