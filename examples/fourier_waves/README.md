# Fourier waves

This example evaluates a `UsdProcGenerativeProcedural` prim in an
OpenUSDConnect client instead of a C++ Hydra plugin.

The authored prim contains a `proceduralSystem` token and wave parameters as
primvars: `frequencies`, `amplitudes`, `phases`, `resolution`, and `size`. When
a parameter changes, the compute client regenerates a displaced grid from a
sum of golden-angle-directed sinusoids and publishes it below `Result`.
Receivers consume ordinary mesh events and do not need Hydra support.

Configure OpenUSD using the shared [example runtime
setup](../README.md#runtime-setup) before running this demo.

## Quickstart

One command starts the server, usdview, compute client, and animated author:

```bash
uv run python examples/fourier_waves/run.py
```

In usdview, frame `/World/FourierWave/Result` and watch the wave roll.

Run with `--help` for all options. For example:

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

Any receiver can connect to the same server: usdview through
`integrations.usdview.launcher`, Blender, or a `UsdStageAdapter` client. Editing
a parameter primvar from any author, including `primvars:frequencies` through
MCP, triggers a new mesh.

## Protocol features

- `ensure_prim` with typeName `GenerativeProcedural` and the
  `HydraGenerativeProceduralAPI` applied schema.
- Typed primvars (`float[]`, `int`, `float`) with interpolation riding
  `set_gprim_attrs`, live-updated at 20 Hz.
- The dispatcher's `on_applied` hook as a change feed driving downstream
  computation.
- Mesh geometry (points, quad topology, extent) emitted by a client and
  replicated to all receivers.

The authored prim can also be consumed by a Hydra
`HdGpGenerativeProcedural` plugin; only the evaluator changes.
