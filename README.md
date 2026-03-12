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
