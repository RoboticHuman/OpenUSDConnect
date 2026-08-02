# Profiling OpenUSDConnect

Profile the server and Blender addon using [py-spy](https://github.com/benfred/py-spy), a low-overhead sampling profiler for Python.

## Setup

Install py-spy into the project venv:

```bash
uv pip install py-spy
```

The server prints its PID on startup:

```
Server listening on 127.0.0.1:7200 (PID 12345)
```

**Windows**: py-spy requires an **Administrator** terminal to attach to processes.

## Profiling the Server

Start the server normally in one terminal:

```bash
uv run python -m openusdconnect.server --port 7200 --base test_scene.usda
```

In a second terminal (Administrator on Windows), attach py-spy using the PID from the server output.

### Flame graph (SVG)

```bash
py-spy record --pid <PID> --output profile.svg
```

Interact with the scene (move objects in Blender, run test scripts), then Ctrl+C to stop. Open `profile.svg` in a browser — wide bars are bottlenecks.

### Raw collapsed stacks (text)

```bash
py-spy record --pid <PID> --format raw --output profile.txt
```

One line per unique call stack with sample counts. Useful for automated analysis or diffing before/after an optimization.

### Live top view

```bash
py-spy top --pid <PID>
```

Real-time `htop`-like display of where CPU time is spent. Good for watching the hot path while interacting with the scene.

## Profiling Blender

Same approach — attach to the Blender process by PID.

1. Start Blender with the addon enabled and connect to the server
2. Find the PID:
   - **Windows**: Task Manager > Details tab > `blender.exe` > PID column
   - **Linux/Mac**: `pgrep -f blender`
3. Attach:

```bash
py-spy record --pid <BLENDER_PID> --output blender_profile.svg
```

4. Move objects, adjust materials, etc. during the recording
5. Ctrl+C to stop — py-spy writes the flame graph, the server keeps running

## Options

| Flag | What it does |
|---|---|
| `--output FILE` | Output file (`.svg` for flame graph, `.txt` with `--format raw`) |
| `--format raw` | Collapsed stacks text instead of SVG |
| `--duration N` | Stop recording after N seconds |
| `--rate N` | Sampling rate in Hz (default 100) |
| `--native` | Include C/C++ stack frames (pxr internals). Requires Administrator on Windows |
| `--subprocesses` | Also profile child processes |

## What to Look For

### Server hot path

| Function | What it does |
|---|---|
| `apply_events` | Applies event batch to USD stage |
| `_apply_set_xform_trs` | Sets translate/orient/scale ops via pxr |
| `find_op` | Linear scan of xform ops (should be rare with op cache) |
| `append` / `append_batch` | SQLite event persistence |
| `broadcast` | Sends events to all connected receivers |

### Blender emitter hot path

| Function | What it does |
|---|---|
| `_depsgraph_handler` | Entry point — fires on every scene change |
| `on_depsgraph_update` | Authors Blender changes to the local USD stage |
| `build_events_for_dirty` | Diffs dirty prims and builds protocol events |
| `_build_dirty_prim_events` | Per-prim attribute diffing |
| `snapshot_prim` | Transform decomposition via pxr |

### Blender receiver hot path

| Function | What it does |
|---|---|
| `_process_queue_timer` | Timer callback that drains the receive queue |
| `drain_and_apply` | EventDispatcher cycle: decode, dedup, mirror commit, adapter dispatch |
| `apply_event` | Dispatches one event to the adapter method |

## Platform Notes

| | Windows | Linux | macOS |
|---|---|---|---|
| Attach to process | Administrator required | May need `sudo` | Works |
| `--native` (C++ stacks) | Administrator required | Works | Works |
| Overhead | ~1-5% | ~1-5% | ~1-5% |

## Sdf spec field deltas

Use the focused benchmark for generic Sdf fields, emitter notices, inactive
variant edits, and full authored-layer snapshots:

```bash
uv run python scripts/benchmark_sdf_spec_delta.py
```

Run `stress_test_departments.py` as well. The focused benchmark measures the
generic Sdf path, emitter paths, native composed projection, and collaboration
layer reorder in-process. The stress test guards transport, persistence, and
department composition under load.
