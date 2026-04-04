# OpenUSDConnect

General-purpose real-time USD sync framework for DCC livelink. The core library (`openusdconnect/`) is DCC-agnostic — Blender, Maya, Houdini, Unreal, etc. are plugins that sit on top.

## Quick Start

```bash
# Install dependencies
uv sync

# Run tests (no Blender needed)
uv run pytest tests/unit/ -v

# Start sync server
uv run python -m openusdconnect.server --port 7200 --base scene.usda

# Start with admin dashboard
uv run python -m openusdconnect.server --port 7200 --base scene.usda --dashboard 8080

# Start with per-department layers and TOFU auth
uv run python -m openusdconnect.server --port 7200 --base scene.usda \
  --department-priority animation,lighting,fx --require-token

# Build Blender addon
uv run python scripts/build_blender_addon.py
# Output: dist/usd_connect_blender.zip
```

Install the addon in Blender via **Edit > Preferences > Add-ons > Install from Disk**.

## Documentation

- **[Blender Addon Usage](docs/blender-addon-usage.md)** — Installing, UI overview, live sync walkthrough
- **[Testing Setup](docs/testing-setup.md)** — Blender configuration, test tiers
- **[Profiling](docs/profiling.md)** — Performance profiling with py-spy

## Features

### Core Framework
- JSON Lines over TCP protocol: transforms, visibility, references, payloads, variants, materials, shaders
- Authoritative sequencer server with SQLite event log, late-join replay, and compaction
- Per-department shared layers with configurable priority ordering (composition strength)
- TOFU (Trust On First Use) token authentication
- Cross-department edit proposals (propose/approve/reject workflow)
- Stage change detection via `Usd.Notice.ObjectsChanged` with partial diffing
- Quaternion rotation using USD-native `xformOp:orient`
- `DCCAdapter` ABC for plugging in any DCC application

### Blender Integration
- Live network emitter with configurable coalescing
- Network receiver with timer-based queue drain
- Auto-track mode for new objects
- Reference and payload import with load/unload lifecycle
- Material sync: UsdPreviewSurface, MaterialX (Standard Surface, OpenPBR), texture connections
- Geometry, visibility, delete, rename sync
- Y-up / Z-up axis conversion

### Unreal Integration
- Stage-level bridge via USDStageActor (no DCCAdapter needed)
- Bidirectional transform sync

## Architecture

| Module | Purpose |
|--------|---------|
| `openusdconnect/protocol.py` | Event schema and validation |
| `openusdconnect/transport.py` | TCP send/recv (JSON Lines) |
| `openusdconnect/event_apply.py` | Apply events to a `Usd.Stage` |
| `openusdconnect/emitter.py` | Stage change detection and partial diffing |
| `openusdconnect/server.py` | Authoritative TCP sequencer with per-department layers, replay, and compaction |
| `openusdconnect/receiver.py` | Background TCP client with event queue |
| `openusdconnect/adapters.py` | `DCCAdapter` ABC + USD/Mock implementations |
| `openusdconnect/token_store.py` | SQLite-backed TOFU token persistence |
| `openusdconnect/token_client.py` | Client-side token storage |
| `openusdconnect/client_id.py` | Deterministic client ID generation |
| `integrations/blender/` | Blender addon (capture, receiver, shader mappers, UI) |
| `integrations/unreal/` | Unreal Engine integration via USDStageActor |
| `integrations/dashboard/` | NiceGUI web admin dashboard (layers, auth, proposals) |

## Tests

```bash
# Unit tests (fast, no Blender)
uv run pytest tests/unit/ -v

# All tests (unit + headless integration)
uv run pytest tests/ -v

# Asset E2E tests (requires Blender)
uv run pytest tests/integration/asset_tests/ --asset-tests -v
```

See [Testing Setup](docs/testing-setup.md) for details.

## Acknowledgments

- [**io_blender_mtlx**](https://github.com/Activision/io_blender_mtlx) by Activision — MaterialX node handlers for Blender shader networks
- [**USD Working Group Assets**](https://github.com/usd-wg/assets) — standardized test assets for the integration test suite
