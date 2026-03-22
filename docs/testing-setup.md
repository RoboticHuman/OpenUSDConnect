# Testing Setup

OpenUSDConnect has two categories of tests:

- **Unit tests** (`tests/unit/`) — protocol, event application, roundtrip, emitter notices (no Blender needed)
- **Integration tests** (`tests/integration/`) — headless adapter tests and full two-Blender integration tests

## Running Tests

```bash
uv sync

# All tests
uv run pytest tests/ -v

# Unit tests only (fast, no Blender needed)
uv run pytest tests/unit/ -v

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

You can send events to the server using Python one-liners over TCP. These connect as an emitter, send a transaction, then disconnect.

**Create a prim:**

```bash
uv run python -c "
import socket, json
s = socket.create_connection(('127.0.0.1', 7200))
s.sendall((json.dumps({'type':'hello','role':'emitter','protocol_version':1}) + '\n').encode())
s.sendall((json.dumps({'type':'txn','client_id':'cli','events':[
  {'k':'ensure_prim','prim':'/World/MySphere','typeName':'Sphere'},
  {'k':'ensure_xform_ops','prim':'/World/MySphere'}
]}) + '\n').encode())
s.close()
"
```

**Move a prim:**

```bash
uv run python -c "
import socket, json
s = socket.create_connection(('127.0.0.1', 7200))
s.sendall((json.dumps({'type':'hello','role':'emitter','protocol_version':1}) + '\n').encode())
s.sendall((json.dumps({'type':'txn','client_id':'cli','events':[
  {'k':'set_xform_trs','prim':'/World/MySphere','fields':['t'],'t':[3.0, 1.0, 0.0]}
]}) + '\n').encode())
s.close()
"
```

**Set gprim attributes (e.g., radius):**

```bash
uv run python -c "
import socket, json
s = socket.create_connection(('127.0.0.1', 7200))
s.sendall((json.dumps({'type':'hello','role':'emitter','protocol_version':1}) + '\n').encode())
s.sendall((json.dumps({'type':'txn','client_id':'cli','events':[
  {'k':'set_gprim_attrs','prim':'/World/MySphere','attrs':{'radius':2.5}}
]}) + '\n').encode())
s.close()
"
```

**Set a reference on a prim:**

```bash
uv run python -c "
import socket, json
s = socket.create_connection(('127.0.0.1', 7200))
s.sendall((json.dumps({'type':'hello','role':'emitter','protocol_version':1}) + '\n').encode())
s.sendall((json.dumps({'type':'txn','client_id':'cli','events':[
  {'k':'ensure_prim','prim':'/World/Chair','typeName':'Xform'},
  {'k':'set_reference','prim':'/World/Chair','refs':[{'asset_path':'./chair.usd','prim_path':'/Model'}]}
]}) + '\n').encode())
s.close()
"
```

**Clear references from a prim:**

```bash
uv run python -c "
import socket, json
s = socket.create_connection(('127.0.0.1', 7200))
s.sendall((json.dumps({'type':'hello','role':'emitter','protocol_version':1}) + '\n').encode())
s.sendall((json.dumps({'type':'txn','client_id':'cli','events':[
  {'k':'set_reference','prim':'/World/Chair','refs':[]}
]}) + '\n').encode())
s.close()
"
```

**Set a payload on a prim:**

```bash
uv run python -c "
import socket, json
s = socket.create_connection(('127.0.0.1', 7200))
s.sendall((json.dumps({'type':'hello','role':'emitter','protocol_version':1}) + '\n').encode())
s.sendall((json.dumps({'type':'txn','client_id':'cli','events':[
  {'k':'ensure_prim','prim':'/World/Asset','typeName':'Xform'},
  {'k':'set_payload','prim':'/World/Asset','payloads':[{'asset_path':'./heavy_geo.usd','prim_path':'/Model'}]}
]}) + '\n').encode())
s.close()
"
```

**Load a payload:**

```bash
uv run python -c "
import socket, json
s = socket.create_connection(('127.0.0.1', 7200))
s.sendall((json.dumps({'type':'hello','role':'emitter','protocol_version':1}) + '\n').encode())
s.sendall((json.dumps({'type':'txn','client_id':'cli','events':[
  {'k':'load_payload','prim':'/World/Asset'}
]}) + '\n').encode())
s.close()
"
```

**Unload a payload:**

```bash
uv run python -c "
import socket, json
s = socket.create_connection(('127.0.0.1', 7200))
s.sendall((json.dumps({'type':'hello','role':'emitter','protocol_version':1}) + '\n').encode())
s.sendall((json.dumps({'type':'txn','client_id':'cli','events':[
  {'k':'unload_payload','prim':'/World/Asset'}
]}) + '\n').encode())
s.close()
"
```

**Request log compaction:**

```bash
uv run python -c "
import socket, json
s = socket.create_connection(('127.0.0.1', 7200))
s.sendall((json.dumps({'type':'hello','role':'emitter','protocol_version':1}) + '\n').encode())
s.sendall((json.dumps({'type':'compact'}) + '\n').encode())
s.close()
"
```

**Delete the event log and start fresh:**

```bash
rm usd_events.db
```

### Listening for events (receiver)

```bash
uv run python -c "
import socket, json
s = socket.create_connection(('127.0.0.1', 7200))
s.sendall((json.dumps({'type':'hello','role':'receiver','protocol_version':1}) + '\n').encode())
f = s.makefile()
try:
    while True:
        line = f.readline()
        if not line: break
        print(json.dumps(json.loads(line), indent=2))
except KeyboardInterrupt:
    pass
s.close()
"
```

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

```powershell
.\scripts\start_usdconnect_debug.ps1
```

3. Once Blender opens and you see `addon installed at: ...` in the terminal, close Blender, then regenerate VS Code configs with the correct debugpy path mappings:

```powershell
uv run python scripts/setup_vscode.py
```

This reads `.blender_addon_path` (written by the bootstrap) and generates `.vscode/launch.json` and `.vscode/tasks.json` with path mappings that bridge your workspace source files to where Blender actually loads the addon from.

### Launching a debug session

```powershell
# One Blender instance, waits for VS Code debugger attach
.\scripts\start_usdconnect_debug.ps1 -WaitForDebugger

# Two Blender instances (A on :5678, B on :5679)
.\scripts\start_usdconnect_debug.ps1 -WaitForDebugger -TwoBlenders
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

```powershell
.\scripts\start_usdconnect_debug.ps1 -Reload
```

This builds a fresh addon zip and drops a `.reload_addon` trigger file. Each running Blender instance has a background timer that watches for this file every 2 seconds, then disables, reinstalls, and re-enables the addon automatically.

### Launcher flags

| Flag | Description |
|------|-------------|
| `-WaitForDebugger` | Blender blocks at startup until VS Code attaches |
| `-TwoBlenders` | Launch a second Blender instance (B on port 5679) |
| `-Reload` | Build addon + signal running instances to reload, then exit |
| `-StartEmitter` | Auto-start the network emitter on launch |
| `-StartReceiver` | Auto-start the network receiver on launch |
| `-DebugPort N` | debugpy port for instance A (default 5678) |
| `-DebugPortB N` | debugpy port for instance B (default 5679) |
| `-ServerPort N` | Sync server port (default 7200) |
| `-BlenderExe path` | Override Blender executable (default: from `blender.test.cfg`) |

## Files

| File | Purpose |
|------|---------|
| `blender.test.cfg` | Blender exe path (gitignored, created by setup script or manually) |
| `.blender/` | Downloaded portable Blender (gitignored) |
| `.blender/user_data/` | Isolated Blender user config and addon install directory (gitignored) |
| `.blender_addon_path` | Installed addon directory (gitignored, written by bootstrap) |
| `tests/conftest.py` | Pytest conftest with `blender_exe` fixture |
| `scripts/setup_blender_test.py` | Portable Blender download/setup script |
| `scripts/start_usdconnect_debug.ps1` | Debug session launcher (server + Blender + debugpy) |
| `scripts/blender_bootstrap_instance.py` | Blender startup script (addon install, debugpy, reload watcher) |
| `scripts/setup_vscode.py` | Generates `.vscode/launch.json` and `tasks.json` with correct path mappings |
