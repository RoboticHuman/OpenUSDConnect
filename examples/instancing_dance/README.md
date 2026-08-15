# Instancing dance demo

A live demonstration of OpenUSDConnect's USD instancing replication.
A ring of pyramids, each a scenegraph instance sharing one prototype,
bobs on phase-offset sine waves while the prototype's own animated
matrix transform keeps the pyramids spinning.

## Quickstart

One command, starts the server, launches usdview, and runs the
sender; `Ctrl+C` stops everything cleanly:

```bash
uv run python examples/instancing_dance/run.py
```

`--help` lists every knob (instances, radius, amplitude, period,
host, port, etc.); see "Customization" below for combinations.

## What this exercises

- `set_instanceable` marks each tower as a USD scenegraph instance,
  so every connected receiver composes one shared prototype rather
  than re-translating geometry per copy.
- `set_reference` wires each tower to `/Protos/Pyramid`, which in
  turn references the on-disk Pyramid USD asset.
- `set_xform_trs` carries the per-frame sine-wave translation. The
  prototype's authored animated `xformOp:transform` composes
  underneath, so the receivers see both motions at once.

Same wire format as everything else in the framework, no special
handling needed on the receiver side.

## Three-terminal recipe

The launcher above is shorthand for what you'd otherwise type in
three terminals. Run it this way to see each process's logs in
isolation, or to point arbitrary receivers (Blender, Unreal, a second
usdview) at the same server.

```bash
# 1. Server
uv run python -m openusdconnect.server \
    --host 127.0.0.1 --port 7300 \
    --base examples/instancing_dance/empty.usda

# 2. usdview (or any other connected viewer)
uv run python -m integrations.usdview.launcher \
    examples/instancing_dance/empty.usda \
    --host 127.0.0.1 --port 7300

# 3. The dance sender
uv run python examples/instancing_dance/dance.py
```

`Ctrl+C` the sender to stop the animation, close usdview when done,
and `Ctrl+C` the server.

## Customization

`run.py --help` and `dance.py --help` list the same knobs:

- `--instances 16 --radius 12 --amplitude 5` for a wider, deeper ring.
- `--period 1.5 --rate 60` for snappier motion with smoother updates.
- `--asset path/to/your-asset.usd` to swap the prototype for any USD
  file with a default prim.
- `--no-usdview` (launcher only) skips the viewer if you only want to
  drive the server from another receiver.
- `--host`/`--port` if your server is elsewhere; defaults are
  `127.0.0.1:7300`.

## Files

- `run.py` one-command orchestrator: server + usdview + dance,
  with cleanup.
- `dance.py` sender only. Use this if a server is already running
  or you want to drive it from a different terminal.
- `empty.usda` the base scene the server loads. Just `/World`.
