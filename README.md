# OpenUSDConnect

General-purpose real-time USD sync framework for DCC livelink. The core library (`openusdconnect/`) is DCC-agnostic — Blender, Maya, Houdini, Unreal, etc. are plugins that sit on top.

## Quick Start

```bash
# Install dependencies
uv sync

# Run core tests (no Blender needed)
uv run pytest tests/ -v -k "not blender"

# Start sync server
uv run python -m openusdconnect.server --port 7200 --base scene.usda --log events.db
```

## Documentation

- **[Testing Setup](docs/testing-setup.md)** — How to configure Blender for tests: download portable Blender, point to a local build, or skip Blender tests entirely.
- **[Blender Addon Usage](docs/blender-addon-usage.md)** — Installing the addon, UI overview, two-Blender live sync walkthrough, auto-tracking, and synced event types.

## Building the Blender Addon

```bash
uv run python scripts/build_blender_addon.py
# Output: dist/usd_connect_blender.zip
```

Install in Blender via **Edit > Preferences > Add-ons > Install from Disk**.

## Features

### Core Framework (`openusdconnect/`)
- JSON Lines over TCP protocol with 10 event types (transform, visibility, delete, rename, etc.)
- Authoritative sequencer server with SQLite event log and late-join replay
- Background receiver thread with sequence-based reconnect resume
- Stage change detection via `Usd.Notice.ObjectsChanged` with partial TRS diffing
- Quaternion rotation using USD-native `xformOp:orient`
- `DCCAdapter` ABC — plug in any DCC (Blender, Maya, Houdini, Unreal)
- `UsdStageAdapter` for headless/server-side USD consumers
- `MockAdapter` for testing without pxr

### Blender Integration (`integrations/blender/`)
- USDHook for automatic prim-path tagging on import
- Live network emitter via depsgraph handler
- Auto-track mode — assigns prim paths to new objects on first manipulation
- Network receiver with `bpy.app.timers` queue drain
- World-preserving `matrix_parent_inverse` normalization (Y-up <> Z-up safe)
- Batch-scoped feedback loop guard (emitter + receiver on same instance)
- Ancestor event emission for correct parent-child ordering
- Sidebar UI: Import, Local Capture, Network Emitter, Network Receiver
- Geometry types: Sphere, Cube, Cylinder, Cone, Mesh, Xform
- Parametric attribute sync (radius, size, height)
- Visibility, delete, rename, deactivate sync

## Architecture

| Module | Purpose |
|--------|---------|
| `openusdconnect/protocol.py` | Event schema and validation |
| `openusdconnect/transport.py` | TCP send/recv (JSON Lines) |
| `openusdconnect/event_apply.py` | Apply events to a `Usd.Stage` |
| `openusdconnect/emitter.py` | Stage change detection and partial diffing |
| `openusdconnect/server.py` | Authoritative TCP sequencer with replay |
| `openusdconnect/receiver.py` | Background TCP client with event queue |
| `openusdconnect/adapters.py` | `DCCAdapter` ABC + USD/Mock implementations |
| `integrations/blender/` | Blender addon (capture, receiver, UI) |

## Tests

Run all tests: `uv run pytest tests/ -v`
Run core only (no Blender): `uv run pytest tests/ -v -k "not blender"`

See [Testing Setup](docs/testing-setup.md) for Blender configuration and detailed coverage.

### Core tests (no Blender needed)
- `test_protocol.py` — event schema validation, message construction
- `test_event_apply.py` — prim creation, canonical xform ops, TRS application
- `test_roundtrip.py` — emitter → adapter full pipeline, partial diffs, visibility
- `test_blender_stage_author.py` — BlenderStageAuthor + NoticeEmitter integration with mock bpy: auto-track, partial diff, deletion detection, feedback guard

### Blender tests (headless, requires Blender 4.4+)
- `test_blender_adapter.py` — 19 headless tests: prim types, TRS, visibility, gprim attrs, delete, rename, MPI world-preservation
- `test_blender_integration.py` — 4 end-to-end tests with real server + Blender processes:
  - emitter → server → receiver pipeline
  - auto-track with type inference
  - deferred custom property persistence
  - role-flip axis-flip verification (Y-up USD scene, auto-tracked objects, 3-phase emitter/receiver/verifier)
