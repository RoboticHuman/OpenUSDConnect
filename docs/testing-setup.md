# Testing Setup

OpenUSDConnect has four tiers of tests:

- **Unit tests** (`tests/unit/`) — protocol, event application, roundtrip, emitter notices, stage parity including shader inputs (no Blender needed)
- **Integration tests** (`tests/integration/`) — headless adapter tests and full two-Blender integration tests
- **Asset E2E tests** (`tests/integration/asset_tests/`) — full pipeline with real USD assets, material enrichment, texture connections, variant switching (requires Blender GUI, skipped by default)
- **Visual regression tests** (`tests/visual/`) — render reference scenes with RenderMan and FLIP-compare against committed goldens; catches rendered-output regressions across materials, shaders, cameras, lights, and geometry (requires RenderMan + `uv sync --group visual`, skipped by default)

## Running Tests

```bash
uv sync

# All tests
uv run pytest tests/ -v

# Unit tests only (fast, no Blender needed; timing-dependent "slow" tests
# are skipped by default)
uv run pytest tests/unit/ -v

# Include the timing-dependent tests (e.g. periodic compaction, which
# waits out real tick intervals)
uv run pytest tests/unit/ --slow-tests -v

# Integration tests only (requires Blender)
uv run pytest tests/integration/ -v
```

Unit tests cover protocol validation, USD stage event application, and emitter/adapter roundtrips using the `pxr` (OpenUSD) Python bindings.

## Coverage Report

```bash
# Terminal summary
uv run pytest tests/unit/ --cov

# HTML report (opens htmlcov/index.html)
uv run pytest tests/unit/ --cov --cov-report=html
```

Coverage is configured in `pyproject.toml` under `[tool.coverage.run]` to measure the `openusdconnect` package. The `htmlcov/` directory is gitignored.

## Blender Test Configuration

Blender integration tests run headless (`blender --background`) and require Blender 4.4+. The test framework checks for a Blender executable in this order:

| Priority | Method | Example |
|----------|--------|---------|
| 1 | `--blender` CLI flag | `uv run pytest --blender /path/to/blender` |
| 2 | `BLENDER_EXE` env var | `BLENDER_EXE=/path/to/blender uv run pytest` |
| 3 | `blender.test.cfg` file | Single line with the exe path (gitignored) |
| 4 | None set | Blender tests are skipped |

### Option A: Download portable Blender (recommended)

The bundled setup script downloads the official portable Blender build, extracts it to `.blender/` in the repo root, and writes `blender.test.cfg` automatically:

```bash
# Download latest stable
uv run python scripts/setup_blender_test.py

# Or specify a version
uv run python scripts/setup_blender_test.py --version 5.0.1
```

After this, all Blender tests work immediately:

```bash
uv run pytest tests/ -v
```

To clean up the downloaded Blender (~400 MB extracted):

```bash
uv run python scripts/setup_blender_test.py --cleanup
```

### Option B: Point to a local Blender build

If you build Blender from source or have it installed, write the path to `blender.test.cfg`:

```bash
echo "/path/to/build/bin/blender" > blender.test.cfg
```

Or pass it per-run without a config file:

```bash
# Via environment variable
BLENDER_EXE=/path/to/blender uv run pytest tests/ -v

# Via CLI flag
uv run pytest tests/ -v --blender /path/to/blender
```

### Option C: Skip Blender tests

If none of the above are configured, Blender tests are automatically skipped. No action needed.

## Server Commands

### Starting the server

```bash
# Basic — in-memory stage, default event log
uv run python -m openusdconnect.server --port 7200

# With a base USD scene
uv run python -m openusdconnect.server --port 7200 --base test_scene.usda

# Custom event log path
uv run python -m openusdconnect.server --port 7200 --base test_scene.usda --log my_events.db

# Compact the event log on startup (deduplicates, keeps latest state per prim)
uv run python -m openusdconnect.server --port 7200 --base test_scene.usda --log my_events.db --compact

# With the admin dashboard (requires: uv sync --group dashboard)
uv run python -m openusdconnect.server --port 7200 --dashboard 8080

# Export the override diff layer on shutdown
uv run python -m openusdconnect.server --port 7200 --base test_scene.usda --export-diff diff.usda
```

### Inspecting the event log

```bash
# Dump all events in the SQLite log (defaults to usd_events.db)
uv run python scripts/dump_events.py

# Dump a specific log file
uv run python scripts/dump_events.py my_events.db
```

### Sending events manually (one-shot client)

Use the built-in send tool to fire events at a running server. It handles FlatBuffers encoding, the hello/hello_ok handshake, and graceful disconnect automatically. Events are passed as JSON strings.

```bash
# Full help
uv run python -m openusdconnect.send --help
```

**Create a prim:**

```bash
uv run python -m openusdconnect.send \
  '{"k":"ensure_prim","prim":"/World/MySphere","typeName":"Sphere"}' \
  '{"k":"ensure_xform_ops","prim":"/World/MySphere"}'
```

**Move a prim:**

```bash
uv run python -m openusdconnect.send \
  '{"k":"set_xform_trs","prim":"/World/MySphere","fields":["t"],"t":[3.0,1.0,0.0]}'
```

**Set gprim attributes (e.g., radius):**

```bash
uv run python -m openusdconnect.send \
  '{"k":"set_gprim_attrs","prim":"/World/MySphere","attrs":{"radius":2.5}}'
```

**Set a reference on a prim:**

```bash
uv run python -m openusdconnect.send \
  '{"k":"ensure_prim","prim":"/World/Chair","typeName":"Xform"}' \
  '{"k":"set_reference","prim":"/World/Chair","refs":[{"asset_path":"./chair.usd","prim_path":"/Model"}]}'
```

**Clear references from a prim:**

```bash
uv run python -m openusdconnect.send \
  '{"k":"set_reference","prim":"/World/Chair","refs":[]}'
```

**Set a payload on a prim:**

```bash
uv run python -m openusdconnect.send \
  '{"k":"ensure_prim","prim":"/World/Asset","typeName":"Xform"}' \
  '{"k":"set_payload","prim":"/World/Asset","payloads":[{"asset_path":"./heavy_geo.usd","prim_path":"/Model"}]}'
```

**Load / unload a payload:**

```bash
uv run python -m openusdconnect.send '{"k":"load_payload","prim":"/World/Asset"}'
uv run python -m openusdconnect.send '{"k":"unload_payload","prim":"/World/Asset"}'
```

**Request log compaction:**

```bash
uv run python -m openusdconnect.send --msg '{"type":"compact"}'
```

**Read events from a file (one JSON per line):**

```bash
cat events.jsonl | uv run python -m openusdconnect.send --stdin
```

**Custom host/port:**

```bash
uv run python -m openusdconnect.send --host 10.0.0.1 --port 7201 \
  '{"k":"ensure_prim","prim":"/World/Foo","typeName":"Xform"}'
```

**Delete the event log and start fresh:**

```bash
rm usd_events.db
```

## Asset Integration Tests (E2E)

Heavy end-to-end tests that launch Blender (GUI mode) per test, send events through a real server, and verify material enrichment, texture connections, variant switching, and material identity on real USD assets.

**Skipped by default.** Enable with `--asset-tests`:

```bash
# Run all asset tests (~90 seconds)
uv run pytest tests/integration/asset_tests/ --asset-tests -v

# Run a single asset test
uv run pytest tests/integration/asset_tests/test_assets.py::test_bishop_materialx --asset-tests -v
```

The addon is automatically rebuilt before running. Each test starts its own server and cleans up after.

### Asset test inventory

| Test | Asset | What it verifies |
|------|-------|-----------------|
| `test_bishop_materialx` | OpenChessSet Bishop | MaterialX multi-node network, texture loading via NodeGraph resolution, diffuse connection chain (Mix←HueSat←Texture), dual materials (Black/White), material binding, shader map seeding |
| `test_teapot_variants` | Teapot | Payload loading, default variant material (Ceramic with primvar Base Color), variant switch Utah↔Fancy with material rebinding, interleaved live editing with value retention across variant round-trips |
| `test_two_teapots_identity` | Teapot ×2 | Path-based material identity — two references get separate Ceramic materials with different `usd_material_path` tags, node tree integrity, parent-context object naming |
| `test_vehicles_multi_binding` | Vehicles 4WD | 6 material bindings across mesh parts from external material file references |
| `test_camera_scene` | intent-vfx teapotScene_camera | UsdGeomCamera replicates as a `bpy.types.Camera` object with correct `lens`/`clip_start`/`clip_end`/`type` after the metersPerUnit-aware unit conversion, then is promoted to the active scene camera |

### Adding new asset tests

See `tests/integration/asset_tests/README.md` for the `TestHarness` API and step-by-step guide.

## Visual Regression Tests

A headless harness that renders reference scenes and FLIP-compares against committed golden images, catching any change to rendered output (materials, cameras, lights, geometry). **Skipped by default.**

```bash
# Run the visual tier (requires RenderMan installed + the visual deps)
uv sync --group visual
uv run pytest tests/visual --visual-tests -v

# Regenerate the golden images (review the change before committing)
uv run pytest tests/visual --visual-tests --update-baselines -v
```

The tier skips cleanly when RenderMan (`RMANTREE`) or `flip-evaluator` is absent. A render regresses when its mean [FLIP](https://github.com/NVIDIA/flip) error exceeds the per-scene threshold; the error map lands in the pytest temp dir. Goldens are pinned to the renderer, sample budget (`HD_PRMAN_MAX_SAMPLES`), and USD/RenderMan version, so changing any of those needs a deliberate `--update-baselines` regen. Goldens use Git LFS (`git lfs install` once per clone).

**Regenerating goldens.** `--update-baselines` re-renders and overwrites the goldens, so that run *skips* (nothing left to compare); rerun without the flag to confirm they are stable. To review an intended change first, run the compare pass: a failing test writes a FLIP error map (path in its assert message) showing what moved. Scope to one scene with its test path (e.g. `tests/visual/test_material_zoo.py`); a new scene reports `missing` until its golden is captured the same way. Inspect the regenerated PNG before committing (the git diff is an opaque LFS pointer).

**Renderers are pluggable** (`renderers.py`): a renderer name maps to a Hydra delegate plus optional env setup and material conditioning. `renderman` (default) sets the `RMAN_*` paths and translates OpenPBR to standard_surface (hdPrman has no OpenPBR adapter); `embree`/`storm` need neither. Add Cycles or Mitsuba as one `RENDERERS` entry.

**Scenes** are static `.usda` (`tests/visual/scenes/`) or a curated event log replayed through the real `codec` + `apply_events` pipeline. `test_material_zoo.py` replays `material_zoo.jsonl` (UsdPreviewSurface, MaterialX standard_surface, OpenPBR, tiled / triplanar / UV-image texturing, a referenced chess piece) onto `test_scene.usda` under a framed camera + StinsonBeach IBL. Heavy assets come from the `usd-wg/assets` submodule (run `git submodule update --init --recursive`) plus a vendored UV sphere in `tests/visual/assets/`; the log stores them as portable `{REPO}` path tokens expanded at replay time, so the committed fixture carries no machine paths. The fixture is JSONL (semantic events re-encoded through the current codec at replay), so it survives wire/storage changes without a binary db.

| File | Purpose |
|------|---------|
| `integrations/visualtest/renderers.py` | Pluggable Hydra renderer registry; add Cycles/Mitsuba here |
| `visualtest/render.py`, `compare.py`, `harness.py` | Render, FLIP compare (mean + p99), baseline primitive |
| `visualtest/replay.py`, `scene.py` | Event-log replay, camera framing + IBL |
| `tests/visual/{scenes,fixtures,references,assets}/` | Static `.usda`, JSONL logs, LFS goldens, vendored geometry |

> Requires USD >= 0.26.5 (earlier builds hit a RenderMan `ri:projection` camera-adapter bug). HdEmbree and Storm are registered alongside RenderMan in `renderers.py`.

## What the Blender Tests Cover

**`tests/integration/test_blender_adapter.py`** — Runs headless tests inside Blender's Python:
- Prim creation for each geometry type (Sphere, Cube, Cylinder, Cone, Mesh, Xform)
- Transform application (translate, rotate, scale)
- Visibility toggling
- Gprim attribute mapping (radius, size, height)
- Delete, rename, idempotency
- `test_ensure_xform_ops_preserves_world` — MPI reset preserves world-space position
- `test_ensure_xform_ops_identity_noop` — identity MPI skipped (no unnecessary reset)

**`tests/integration/test_blender_integration.py`** — End-to-end tests with real server + Blender processes:
- `test_emitter_server_receiver_integration`: Manual events sent via socket -> server -> receiver Blender verifies objects arrived correctly
- `test_autotrack_emitter_to_receiver`: Real auto-tracking via depsgraph -> server -> receiver Blender verifies objects, types, positions, and visibility
- `test_autotrack_deferred_props`: Auto-tracked objects get `usd_prim_path`/`usd_type_name` via deferred `bpy.app.timers` (not inside depsgraph callback where writes are discarded)
- `test_roleflip_no_axis_flip`: 3-phase role-flip test — import Y-up scene into Z-up Blender, receive events + flip to emitter, verify no axis flip on received + auto-tracked objects

All integration tests start a real sync server, run Blender processes as emitter and receiver, and assert on the results.

Both the debug launcher and integration tests set `BLENDER_USER_RESOURCES` to `.blender/user_data/` so the addon installs to a repo-local directory instead of your system AppData. This means the portable Blender is fully isolated — it won't interfere with any system-installed Blender.

## Interactive Debugging with VS Code

For hands-on debugging with breakpoints, the repo includes a launcher script that starts the sync server and one or two Blender instances with debugpy enabled, plus VS Code configs for attaching.

### First-time setup

1. Make sure `blender.test.cfg` exists (see Blender Test Configuration above).

2. Generate the VS Code configs and launch Blender once so it writes the addon install path:

```bash
uv run python scripts/start_usdconnect_debug.py
```

3. Once Blender opens and you see `addon installed at: ...` in the terminal, close Blender, then regenerate VS Code configs with the correct debugpy path mappings:

```bash
uv run python scripts/setup_vscode.py
```

This reads `.blender_addon_path` (written by the bootstrap) and generates `.vscode/launch.json` and `.vscode/tasks.json` with path mappings that bridge your workspace source files to where Blender actually loads the addon from.

### Launching a debug session

```bash
# One Blender instance, waits for VS Code debugger attach
uv run python scripts/start_usdconnect_debug.py --wait-for-debugger

# Two Blender instances (A on :5678, B on :5679)
uv run python scripts/start_usdconnect_debug.py --wait-for-debugger --two-blenders
```

The launcher starts the sync server and Blender, prints a summary table with PIDs and debug ports, then waits. When you close all Blender windows, the server is stopped automatically and the terminal is released.

In VS Code, use the attach configs from the Run and Debug panel:

| Config | Description |
|--------|-------------|
| `Attach: Blender A (debugpy :5678)` | Attach to instance A |
| `Attach: Blender B (debugpy :5679)` | Attach to instance B |
| `Attach All USD Connect` | Compound — attaches to both |

Breakpoints work in both `integrations/blender/` and `openusdconnect/` source files.

### Hot-reloading the addon

After making code changes, you can rebuild the addon and push it to all running Blender instances without restarting them. From a separate terminal:

```bash
uv run python scripts/start_usdconnect_debug.py --reload
```

This builds a fresh addon zip and drops a `.reload_addon` trigger file. Each running Blender instance has a background timer that watches for this file every 2 seconds, then disables, reinstalls, and re-enables the addon automatically.

### Launcher flags

| Flag | Description |
|------|-------------|
| `--wait-for-debugger` | Blender blocks at startup until VS Code attaches |
| `--two-blenders` | Launch a second Blender instance (B on port 5679) |
| `--reload` | Build addon + signal running instances to reload, then exit |
| `--start-emitter` | Auto-start the network emitter on launch |
| `--start-receiver` | Auto-start the network receiver on launch |
| `--debug-port N` | debugpy port for instance A (default 5678) |
| `--debug-port-b N` | debugpy port for instance B (default 5679) |
| `--server-port N` | Sync server port (default 7200) |
| `--blender-exe path` | Override Blender executable (default: from `blender.test.cfg`) |

## Files

| File | Purpose |
|------|---------|
| `blender.test.cfg` | Blender exe path (gitignored, created by setup script or manually) |
| `.blender/` | Downloaded portable Blender (gitignored) |
| `.blender/user_data/` | Isolated Blender user config and addon install directory (gitignored) |
| `.blender_addon_path` | Installed addon directory (gitignored, written by bootstrap) |
| `tests/conftest.py` | Pytest conftest with `blender_exe` fixture |
| `scripts/setup_blender_test.py` | Portable Blender download/setup script |
| `scripts/start_usdconnect_debug.py` | Debug session launcher (server + Blender + debugpy) |
| `scripts/blender_bootstrap_instance.py` | Blender startup script (addon install, debugpy, reload watcher) |
| `scripts/setup_vscode.py` | Generates `.vscode/launch.json` and `tasks.json` with correct path mappings |
