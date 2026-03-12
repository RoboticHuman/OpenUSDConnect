# Testing Setup

OpenUSDConnect has two categories of tests:

- **Core tests** — protocol, event application, roundtrip (no Blender needed)
- **Blender tests** — headless adapter tests and full two-Blender integration tests

## Running Core Tests

```bash
uv sync
uv run pytest tests/ -v -k "not blender"
```

These test the protocol validation, USD stage event application, and emitter/adapter roundtrips using the `pxr` (OpenUSD) Python bindings.

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
# Download latest stable (currently 4.5.7)
uv run python scripts/setup_blender_test.py

# Or specify a version
uv run python scripts/setup_blender_test.py --version 4.4.3
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

## What the Blender Tests Cover

**`test_blender_adapter.py`** — Runs 19 headless tests inside Blender's Python:
- Prim creation for each geometry type (Sphere, Cube, Cylinder, Cone, Mesh, Xform)
- Transform application (translate, rotate, scale)
- Visibility toggling
- Gprim attribute mapping (radius, size, height)
- Delete, rename, idempotency
- `test_ensure_xform_ops_preserves_world` — MPI reset preserves world-space position
- `test_ensure_xform_ops_identity_noop` — identity MPI skipped (no unnecessary reset)

**`test_blender_integration.py`** — 4 end-to-end tests with real server + Blender processes:
- `test_emitter_server_receiver_integration`: Manual events sent via socket -> server -> receiver Blender verifies objects arrived correctly
- `test_autotrack_emitter_to_receiver`: Real auto-tracking via depsgraph -> server -> receiver Blender verifies objects, types, positions, and visibility
- `test_autotrack_deferred_props`: Auto-tracked objects get `usd_prim_path`/`usd_type_name` via deferred `bpy.app.timers` (not inside depsgraph callback where writes are discarded)
- `test_roleflip_no_axis_flip`: 3-phase role-flip test — import Y-up scene into Z-up Blender, receive events + flip to emitter, verify no axis flip on received + auto-tracked objects

All integration tests start a real sync server, run Blender processes as emitter and receiver, and assert on the results.

## Files

| File | Purpose |
|------|---------|
| `blender.test.cfg` | Blender exe path (gitignored, created by setup script or manually) |
| `.blender/` | Downloaded portable Blender (gitignored) |
| `conftest.py` | Root pytest conftest with `blender_exe` fixture |
| `scripts/setup_blender_test.py` | Portable Blender download/setup script |
