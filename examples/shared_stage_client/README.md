# Shared-stage client demo

This example edits an existing sublayer through `SharedStageClient`. The same
`Usd.Stage` receives authoritative echoes, so no separate mirror stage or
protocol event dictionaries are needed.

Start a shared-stage server:

```bash
uv run openusdconnect-server \
  --base examples/shared_stage_client/scene.usda \
  --layer-mode shared_stage \
  --event-log /tmp/openusdconnect-shared-stage.db
```

Run an observer and an author in separate terminals:

```bash
uv run python examples/shared_stage_client/demo.py --app-name observer
uv run python examples/shared_stage_client/demo.py --app-name author --author
```

To exercise native change tracking, build the bridge with
`openusdconnect-build-sdf-notice-bridge` and pass its printed path through
`--sdf-notice-bridge`.

Both processes open the same example files here. In a production deployment,
each process may use a different filesystem root or a custom `ArResolver`, as
long as the root document and recursive sublayer graph resolve to equivalent
assets. The demo does not save the changed layer; persistence remains an
application decision.
