# Fourier waves: a compute-client procedural

A live demonstration of evaluating a `UsdProcGenerativeProcedural` prim with
an ordinary OpenUSDConnect network client instead of a C++ Hydra plugin.

The authored prim is declaration-only, exactly as the UsdProc schema intends:
a `proceduralSystem` token plus wave parameters as primvars (`frequencies`,
`amplitudes`, `phases`, `resolution`, `size`). The wave client subscribes to
it over the sync protocol and, on every parameter change, regenerates a
wave-displaced grid mesh (a sum of golden-angle-directed sinusoids) and emits
it as the prim's `Result` child. Because the evaluator is just another
client, every connected receiver sees the expanded geometry, including ones
with no Hydra at all.

## Quickstart

One command starts the server, usdview, the wave client, and an animated
author; `Ctrl+C` stops everything cleanly:

```bash
uv run python examples/fourier_waves/run.py
```

In usdview, frame `/World/FourierWave/Result` and watch the wave roll.

`--help` lists every knob. Some combinations to try:

```bash
# calm single swell, coarse grid
uv run python examples/fourier_waves/run.py --frequencies 0.8 --amplitudes 0.9 --resolution 48

# choppy interference, fine grid, faster animation
uv run python examples/fourier_waves/run.py --frequencies 1,3.7,6.1,9.3 --amplitudes .5,.3,.2,.1 --resolution 128 --rate 3
```

## Three-terminal recipe

```bash
# 1. server (periodic compaction: continuous regeneration otherwise grows
#    the event log by roughly 100 KB per update; --reclaim-interval returns
#    the freed disk space to the OS)
uv run python -m openusdconnect.server --port 7301 --base examples/fourier_waves/empty.usda --event-log /tmp/fourier.db --compact-interval 60 --reclaim-interval 120

# 2. the procedural evaluator
uv run python examples/fourier_waves/wave_client.py --port 7301

# 3. author the prim, then keep animating its phases
uv run python examples/fourier_waves/author.py --port 7301 --animate 0
```

Point any receiver at the same server (usdview via
`integrations.usdview.launcher`, Blender, a `UsdStageAdapter` client) to watch
the same result. Editing any parameter primvar from any client (for example
`primvars:frequencies` via the MCP server) regenerates the wave everywhere.

## What this exercises

- `ensure_prim` with typeName `GenerativeProcedural` and the
  `HydraGenerativeProceduralAPI` applied schema.
- Typed primvars (`float[]`, `int`, `float`) with interpolation riding
  `set_gprim_attrs`, live-updated at 20 Hz.
- The dispatcher's `on_applied` hook as a change feed driving downstream
  computation.
- Mesh geometry (points, quad topology, extent) emitted by a client and
  replicated to all receivers.

The same authored prim remains consumable by a real Hydra
`HdGpGenerativeProcedural` plugin later; only the evaluator differs.
