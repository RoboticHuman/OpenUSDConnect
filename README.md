# OpenUSDConnect

Real-time OpenUSD scene synchronization across DCC applications. A pure-Python
core and an authoritative sync server keep OpenUSD stages consistent across
Blender, usdview, Unreal Engine, and headless clients over a local network.
Transforms, geometry, materials, composition arcs, instancing, cameras, lights,
animation, and playback all replicate live through a compact event protocol.

## Overview

OpenUSDConnect is a **general-purpose USD livelink framework**, not a
single-DCC plugin. The core library (`openusdconnect/`) is pure Python on top of
the OpenUSD bindings (`pxr`), has no DCC dependencies, and is fully testable
headless. The bundled integrations (Blender, usdview, Unreal, an MCP authoring
server) are **reference clients**: thin emit/receive layers on the core, meant
to be used as-is and to serve as templates for building integrations for other
hosts (Maya, Houdini, and so on). The protocol itself is DCC-agnostic.

Scene changes are expressed as a fixed vocabulary of **events** (`ensure_prim`,
`set_xform_trs`, `set_material_binding`, and so on) that map directly to USD spec
operations. Clients emit events when their stage changes; the server sequences
them into an authoritative, append-only log and fans them out to every connected
client, which applies them to its own stage. The wire format is length-prefixed
FlatBuffers over TCP.

```
              +-------------------------------------------+
              |                Sync Server                |
              |   authoritative sequencer, SQLite event   |
              |   log, per-department layers, dashboard    |
              +-------------------------------------------+
                  ^           ^           ^           ^
                  |     TCP (length-prefixed FlatBuffers)
                  v           v           v           v
               Blender     usdview      Unreal     MCP / LLM
             (emit+recv)  (hdPrman)   (transforms) (authoring)
```

## Features

### Sync server
- Authoritative sequencer with atomic, ordered transactions
- SQLite event log with late-join replay (sync from any sequence) and log compaction
- Per-department shared layers with configurable strength ordering
- Cross-department edit proposals (propose, approve, reject)
- TOFU (trust-on-first-use) token authentication and per-client rate limiting
- Single-leader playback synchronization (a shared playhead)
- Optional web admin dashboard with a live prim tree and event inspector

### USD coverage
- **Transforms**: translate / orient / scale with quaternion rotation; partial TRS updates
- **Geometry**: meshes, curves, points; typed attributes, primvars and interpolation; visibility
- **Materials and shaders**: UsdPreviewSurface and MaterialX (`ND_*`) networks, nested NodeGraphs, per-purpose bindings
- **Composition**: references, payloads (load/unload), variant selections
- **Instancing**: native scenegraph instancing and `UsdGeomPointInstancer`
- **Cameras and lights**: `UsdGeomCamera`; `UsdLux` lights with applied API schemas (Shaping, Shadow, and others)
- **Stage metadata**: units (`metersPerUnit`, `upAxis`) and timeline (fps, time codes, range)
- **Animation**: time-sampled values on transforms, visibility, attributes, shader inputs, and point instancers
- **Prim lifecycle**: create, rename, delete, deactivate

### Integrations
These ship as **reference implementations**: working clients you can run today,
and worked examples for writing your own. Each is a thin layer on the core
library, so a new host integration is mostly mapping its scene to the same
emit/receive event flow.

| Integration | Direction | Notes |
|---|---|---|
| **Blender** | bidirectional | Emitter/receiver addon: live transform and material sync (UsdPreviewSurface, MaterialX), Y-up/Z-up conversion, depsgraph auto-tracking |
| **usdview** | receive | Live viewer plugin and launcher; RenderMan-safe (Sdr plugin bootstrap) with OpenPBR to standard_surface translation for hdPrman |
| **Unreal Engine** | bidirectional | Transform sync via `USDStageActor` |
| **MCP server** | author + introspect | Exposes the event protocol as Model Context Protocol tools so an LLM (e.g. Claude) can author USD and inspect scenes |
| **Dashboard** | admin | Web admin UI: status, client table, prim tree, event log |

## Getting started

### Prerequisites
- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- OpenUSD Python bindings (`pxr`): pulled in by the `server` dependency group for
  headless use, provided by the DCC for integrations, or by `usd-core` in Docker

Optional features are opt-in dependency groups: `server` (headless `pxr`),
`dashboard`, `mcp`, `dev` (tests and lint), `profile`.

### Run the sync server
```bash
# Install headless dependencies (pulls pxr via usd-core)
uv sync --group server

# Start the server
uv run openusdconnect-server --port 7200 --base scene.usda

# With the admin dashboard (uv sync --group dashboard)
uv run openusdconnect-server --port 7200 --base scene.usda --dashboard 8080

# With per-department layers and authentication
uv run openusdconnect-server --port 7200 --base scene.usda \
  --department-priority animation,lighting,fx --require-token
```

If a Hydra renderer such as RenderMan is installed into the shared USD build,
launch through the renderer-safe wrapper so the Sdr registry can load its plugins:

```bash
uv run python -m integrations.run_server --port 7200 --base scene.usda
```

### Docker

The server image uses [`usd-core`](https://pypi.org/project/usd-core/) from PyPI
for headless OpenUSD support.

```bash
# Build the server image
docker build -t openusdconnect-server .

# Run the server
docker run -p 7200:7200 -v ./scenes:/scenes \
  openusdconnect-server --port 7200 --base /scenes/scene.usda

# Build and run with dashboard support
docker build --build-arg DASHBOARD=1 -t openusdconnect-server:dashboard .
docker run -p 7200:7200 -p 8080:8080 -v ./scenes:/scenes \
  openusdconnect-server:dashboard \
  --port 7200 --base /scenes/scene.usda --dashboard 8080
```

Or using Docker Compose:

```bash
docker compose --profile default up      # server only
docker compose --profile dashboard up    # server + dashboard
```

### Blender addon
```bash
uv run python scripts/build_blender_addon.py
```
Install the output zip (`dist/usd_connect_blender.zip`) in Blender via
**Edit > Preferences > Add-ons > Install from Disk**.

### usdview
```bash
uv run python -m integrations.usdview.launcher scene.usda --port 7200
# add --renderman to use the hdPrman delegate
```

### Unreal Engine
The Unreal integration is a UE5 plugin (with a Python launcher for live edits).
Requirements, installation, and the two-way sync walkthrough are in the
[Unreal plugin README](integrations/unreal/OpenUSDConnect/README.md).

### MCP server (LLM authoring)
```bash
uv sync --group mcp
uv run python -m integrations.mcp     # stdio MCP server
```
Register it with an MCP client (e.g. Claude Code or Desktop) and point it at a
running sync server. See [MCP Server Usage](docs/mcp-server-usage.md).

## Documentation
- [Blender Addon Usage](docs/blender-addon-usage.md): installation, UI overview, live-sync walkthrough
- [Live Material Editing](docs/live-material-editing.md): material and shader synchronization
- [Unreal Engine Plugin](integrations/unreal/OpenUSDConnect/README.md): UE5 plugin requirements, install, two-way sync
- [MCP Server Usage](docs/mcp-server-usage.md): tools, configuration, Claude client setup
- [Testing Setup](docs/testing-setup.md): test tiers, Blender configuration, adding tests
- [Profiling](docs/profiling.md): performance profiling with py-spy

## Testing
```bash
uv run pytest tests/unit/ -v                                   # unit (fast, no DCC)
uv run pytest tests/ -v                                        # plus headless integration
uv run pytest tests/integration/asset_tests/ --asset-tests -v  # asset E2E (requires Blender)
```

## Acknowledgments
- [io_blender_mtlx](https://github.com/Activision/io_blender_mtlx) by Activision: MaterialX node handlers for Blender shader networks
- [USD Working Group Assets](https://github.com/usd-wg/assets): standardized test assets for the integration test suite
