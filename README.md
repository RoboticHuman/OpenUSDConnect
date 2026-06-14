# OpenUSDConnect

Real-time USD scene synchronization across DCC applications. OpenUSDConnect provides an authoritative sync server and client libraries that keep OpenUSD stages in sync across Blender, Unreal Engine, and other tools over a local network.

## Features

**Sync Server**
- Authoritative sequencer with atomic transaction ordering
- SQLite event log with late-join replay and log compaction
- WebDAV/UNC live-open endpoint that serves a normal-looking USD snapshot
- Per-department shared layers with configurable priority
- TOFU (Trust On First Use) client authentication
- Cross-department edit proposals (propose/approve/reject workflow)
- Rate limiting and connection management for production use
- Web admin dashboard with live event inspector

**Scene Support**
- Transforms (translate, rotate, scale) with quaternion rotation
- Geometry, visibility, materials, and shaders
- References, payloads, and variant selections
- Prim lifecycle: create, rename, delete, deactivate

**DCC Integrations**
- **Blender** — Live emitter/receiver addon with material sync (UsdPreviewSurface, MaterialX), axis conversion, and auto-tracking
- **Unreal Engine** — Bidirectional transform sync via USDStageActor

**Live Open**
- WebDAV/UNC directory: `\\127.0.0.1@7280\usd\`
- Flattened snapshot: `scene.usd`
- Composition root: `scene.live.usda`
- Blender imports the snapshot and auto-connects receiver/emitter when metadata is present.

## Getting Started

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) for local development
- OpenUSD Python bindings (`pxr`) — provided automatically in Docker or by DCC applications

### Local Development

```bash
# Install dependencies
uv sync --group server --group vfs

# Start the sync server
uv run openusdconnect-server --port 7200 --base scene.usda --vfs-port 7280

# Start with admin dashboard
uv run openusdconnect-server --port 7200 --base scene.usda --dashboard 8080

# Start with per-department layers and authentication
uv run openusdconnect-server --port 7200 --base scene.usda \
  --department-priority animation,lighting,fx --require-token
```

### Docker

The server image uses [`usd-core`](https://pypi.org/project/usd-core/) from PyPI for headless OpenUSD support.

```bash
# Build the server image
docker build -t openusdconnect-server .

# Run the server
docker run -p 7200:7200 -p 7280:7280 -v ./scenes:/scenes \
  openusdconnect-server --port 7200 --base /scenes/scene.usda --vfs-port 7280

# Build with dashboard support
docker build --build-arg DASHBOARD=1 -t openusdconnect-server:dashboard .

# Run with dashboard
docker run -p 7200:7200 -p 7280:7280 -p 8080:8080 -v ./scenes:/scenes \
  openusdconnect-server:dashboard \
  --port 7200 --base /scenes/scene.usda --vfs-port 7280 --dashboard 8080
```

Or using Docker Compose:

```bash
# Server only
docker compose --profile default up

# Server with dashboard
docker compose --profile dashboard up
```

### Blender Addon

```bash
uv run python scripts/build_blender_addon.py
```

Install the output zip (`dist/usd_connect_blender.zip`) in Blender via **Edit > Preferences > Add-ons > Install from Disk**.

For seamless live-open, start the server with `--vfs-port`, then import
`\\127.0.0.1@7280\usd\scene.usd` in Blender or paste
`http://127.0.0.1:7280/usd/scene.usd` into the addon's live URL field.
The addon imports the snapshot and auto-connects receiver/emitter from
embedded metadata.

### Unreal Engine

Run the integration script from the Unreal Python console:

```python
py "path/to/OpenUSDConnect/integrations/unreal/connect.py"
```

## Documentation

- [Blender Addon Usage](docs/blender-addon-usage.md) — Installation, UI overview, live sync walkthrough
- [Live Material Editing](docs/live-material-editing.md) — Material and shader synchronization
- [Testing Setup](docs/testing-setup.md) — Test tiers, Blender configuration, adding new tests
- [Profiling](docs/profiling.md) — Performance profiling with py-spy

- [Live-Open Quickstart](docs/live-open-quickstart.md) - WebDAV/UNC live-open and Blender auto-connect
- [Live-Open Production Guide](docs/live-open-production-guide.md) - Production architecture, limitations, and checklist

## Testing

```bash
# Unit tests (fast, no DCC required)
uv run pytest tests/unit/ -v

# All headless tests
uv run pytest tests/ -v

# Asset end-to-end tests (requires Blender)
uv run pytest tests/integration/asset_tests/ --asset-tests -v
```

## Acknowledgments

- [io_blender_mtlx](https://github.com/Activision/io_blender_mtlx) by Activision — MaterialX node handlers for Blender shader networks
- [USD Working Group Assets](https://github.com/usd-wg/assets) — Standardized test assets for the integration test suite
