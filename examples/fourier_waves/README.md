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

From the repository root, install the bundled OpenUSD runtime:

```text
uv sync --group bundled-usd
```

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

Open three terminals in the repository root and run one command in each:

```text
uv run openusdconnect-server --port 7301 --base examples/fourier_waves/empty.usda --event-log fourier-demo.db --compact-interval 60 --reclaim-interval 120

uv run python examples/fourier_waves/wave_client.py --port 7301

uv run python examples/fourier_waves/author.py --port 7301 --animate 0
```

The evaluator reports generated mesh updates and the author reports submitted
parameter changes. Stop the author and evaluator with `Ctrl+C`, then stop the
server with `Ctrl+C`. The event log remains at `fourier-demo.db`; periodic
compaction and reclamation limit growth during long runs.

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
