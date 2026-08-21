# Profiling

[py-spy](https://github.com/benfred/py-spy) samples the server or a DCC without
instrumenting its Python code.

## Install

```bash
uv sync --group profile
```

On Windows, attaching to another process normally requires an Administrator
terminal. The server prints the real Python PID at startup.

## Profile the server manually

Start the server:

```bash
uv run openusdconnect-server --base test_scene.usda --port 7200
```

Attach in another terminal:

```bash
# Flame graph
py-spy record --pid <PID> --rate 50 --output server.svg

# Collapsed stacks for automated analysis
py-spy record --pid <PID> --rate 50 --format raw --output server.raw

# Live view
py-spy top --pid <PID>
```

Stop a recording with Ctrl+C so py-spy can flush the output.

Sampling changes the workload. Run it once without profiling and use that as
the throughput and latency baseline. In one local 40-emitter,
40-receiver, 20-bidirectional saturation run, 50 Hz sampling approximately
halved durable throughput; 200 Hz fell far behind and slowed the workload by an
order of magnitude. The exact overhead is machine and workload dependent.

Use 25 to 50 Hz for routine hotspot discovery. Increase the rate only for a
short focused capture, and discard timing conclusions if py-spy reports that it
is behind in sampling.

`--native` adds C/C++ frames, including OpenUSD internals, but increases capture
cost and platform requirements.

## Department stress workload

The stress harness starts a temporary server and defaults to 40 send-only,
40 receive-only, and 20 bidirectional clients:

```bash
# Performance baseline, no profiler
uv run --isolated python scripts/stress_test_departments.py \
  --emitters 40 --receivers 40 --bidi 20 --iterations 100

# Text hotspot report at a controlled sampling rate
uv run --isolated --group profile python scripts/stress_test_departments.py \
  --emitters 40 --receivers 40 --bidi 20 --iterations 100 \
  --text-profile --profile-rate 50 \
  --profile-output stress-profile.txt

# SVG flame graph
uv run --isolated --group profile python scripts/stress_test_departments.py \
  --profile --profile-rate 50 \
  --profile-output stress-profile.svg
```

The script profiles only the transaction workload, stops py-spy before replay
verification, and then checks final shared and private prim state.
Its server inherits the selected OpenUSD runtime and plugin environment.
RenderMan is discovered automatically when `RMANTREE` is set. The reported
server PID is the real interpreter PID, so no manual process lookup is needed.
The commands assume the project OpenUSD environment is active. For a
renderer-neutral profile that needs neither MaterialX nor custom plugins,
replace `--isolated` with `--group bundled-usd`.

Interpret its metrics separately:

- submission throughput is how quickly writers hand transactions to the server
- durable throughput is how quickly the server commits and acknowledges them
- acknowledgement latency includes queueing when offered load exceeds durable
  capacity
- receiver counts describe live delivery observed before harness shutdown;
  final replay verification is the durable correctness check

An unpaced saturation run is useful for capacity and hotspot discovery, but it
is not an interactive-latency benchmark. Measure a paced load curve before
setting a real-time service target.

## Profile Blender

Attach to the `blender.exe` process while exercising the behavior of interest:

```bash
py-spy record --pid <BLENDER_PID> --rate 50 --output blender.svg
```

Profile the actual integration harness or installed addon build you intend to
ship. A standalone Python microbenchmark does not capture Blender depsgraph,
timer, native import, and node-tree costs.

Useful receive path functions include:

- `_process_queue_timer`: Blender timer and receive drain
- `EventDispatcher.drain_and_apply`: decode, mirror apply, projection, adapter
- adapter methods such as `set_connectable_input` and `set_gprim_attrs`

Useful emit path functions include:

- Blender depsgraph capture and `BlenderStageAuthor`
- `NoticeEmitter.build_events_for_dirty`
- `NoticeEmitter._build_dirty_prim_events`

## Focused benchmarks

Generic Sdf fields, emitter paths, native projection, and collaboration-layer
reorder:

```bash
uv run python scripts/benchmark_sdf_spec_delta.py
```

Shared-stage layer tracking and topology:

```bash
uv run python scripts/benchmark_shared_stage.py
```

Optional native Sdf delegate bridge:

```bash
uv run openusdconnect-build-sdf-notice-bridge --build-dir build/sdf-notice-bridge
uv run python scripts/benchmark_native_sdf_notice.py \
  --bridge /path/to/the/printed/library
```

VFS snapshot generation:

```bash
uv run python scripts/bench_vfs_snapshot.py --base /path/to/scene.usda
uv run python scripts/bench_vfs_snapshot.py --synthetic-prims 10000
```

Run benchmark commands more than once, report medians or percentiles, and keep
the dataset, OpenUSD build, renderer, durability mode, and profiler rate in the
result. Do not compare a profiled run directly with an unprofiled result.
