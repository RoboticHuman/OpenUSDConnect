# Shared-stage client demo

This example edits an existing sublayer through `SharedStageClient`. The same
`Usd.Stage` receives authoritative echoes, so no separate mirror stage or
protocol event dictionaries are needed.

From the repository root, install the bundled OpenUSD runtime:

```text
uv sync --group bundled-usd
```

Start a shared-stage server:

```text
uv run openusdconnect-server --base examples/shared_stage_client/scene.usda --layer-mode shared_stage --event-log shared-stage-demo.db
```

Run an observer and an author in separate terminals:

```text
uv run python examples/shared_stage_client/demo.py --app-name observer --seconds 5
uv run python examples/shared_stage_client/demo.py --app-name author --author --seconds 3
```

Start the observer first, then the author. Both print increasing `seq` values
and the same non-`None` sphere `position`. They exit after the configured
duration; stop the server with `Ctrl+C`. `shared-stage-demo.db` remains in the
repository root so the session can be replayed. Use a different event-log name
for an independent run.

To exercise native change tracking, build the bridge with
`openusdconnect-build-sdf-notice-bridge` and pass its printed path through
`--sdf-notice-bridge`.

Both processes open the same example files here. In a production deployment,
each process may use a different filesystem root or a custom `ArResolver`, as
long as the root document and recursive sublayer graph resolve to equivalent
authored contents. OpenUSDConnect does not compare the complete untouched
baseline, so production integrations must establish that equivalence through
versioned assets or resolver policy. The demo does not save the changed layer;
persistence remains an application decision.

The author only changes the stage while `client.status.phase` is `READY`.
During connection or replay it continues pumping `update()` without authoring;
on a recoverable rejection it stops authoring and reports the server reason.
