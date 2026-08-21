# Instancing dance demo

This example replicates a ring of scenegraph instances that share one pyramid
prototype. Per-instance sine-wave translations produce the vertical motion;
the prototype's matrix animation supplies the rotation.

Configure OpenUSD using the shared [example runtime
setup](../README.md#runtime-setup), then initialize the Pyramid asset used by
the default demo:

```text
git submodule update --init --recursive
```

## Quickstart

One command starts the server, usdview, and the sender:

```bash
uv run python examples/instancing_dance/run.py
```

Run with `--help` for all options. Common combinations are listed under
[Customization](#customization).

## Protocol features

- `set_instanceable` marks each tower as a USD scenegraph instance,
  so every connected receiver composes one shared prototype rather
  than re-translating geometry per copy.
- `set_reference` wires each tower to `/Protos/Pyramid`, which in
  turn references the on-disk Pyramid USD asset.
- `set_xform_trs` carries the per-frame sine-wave translation. The
  prototype's authored animated `xformOp:transform` composes
  underneath, so the receivers see both motions at once.

These are ordinary protocol events; receivers need no demo-specific code.

## Three-terminal recipe

Run the processes separately to inspect each log or connect additional
receivers such as Blender, Unreal, or another usdview process.

Open three terminals in the repository root and run one command in each:

```text
uv run openusdconnect-server --host 127.0.0.1 --port 7300 --base examples/instancing_dance/empty.usda --event-log instancing-demo.db

uv run python -m integrations.usdview.launcher examples/instancing_dance/empty.usda --host 127.0.0.1 --port 7300

uv run python examples/instancing_dance/dance.py
```

Stop the sender with `Ctrl+C`, close usdview, and then stop the server with
`Ctrl+C`. The server keeps `instancing-demo.db` in the repository root for
replay; choose another event-log name for an independent run.

## Customization

`run.py --help` and `dance.py --help` list the same knobs:

- `--instances 16 --radius 12 --amplitude 5` creates a wider, deeper ring.
- `--period 1.5 --rate 60` increases the motion and update rates.
- `--asset path/to/your-asset.usd` to swap the prototype for any USD
  file with a default prim.
- `--no-usdview` (launcher only) skips the viewer if you only want to
  drive the server from another receiver.
- `--host`/`--port` if your server is elsewhere; defaults are
  `127.0.0.1:7300`.

## Files

- `run.py`: starts the server, usdview, and sender, then handles cleanup.
- `dance.py`: sender only; use it with an existing server.
- `empty.usda`: minimal base stage containing `/World`.
