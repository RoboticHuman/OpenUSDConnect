# OpenUSDConnect usdview integration

This receive-only plugin adds **OpenUSDConnect** connect, disconnect, and
status commands to usdview. It applies server events directly to the stage
usdview renders, including transforms, visibility, materials, composition
arcs, time samples, and stage metadata.

Because usdview already owns a `Usd.Stage`, the plugin uses `UsdStageAdapter`
directly. It does not create a separate native scene and does not publish
usdview edits.

## Requirements

- Python 3.13+ and `uv` for the OpenUSDConnect server environment.
- An OpenUSD installation that includes `usdview` and its Qt dependencies.
  The lightweight `bundled-usd` group supplies `pxr` for headless workflows but
  does not supply the usdview application.

Make `usdview` discoverable on `PATH`, set `USDVIEW_PATH` to its executable, or
pass `--usdview /path/to/usdview`. If the OpenUSD Python bindings and native
libraries also come from that installation, configure the project runtime as
described in [OpenUSD runtime and custom
plugins](../../docs/cli-reference.md#openusd-runtime-and-custom-plugins).

## Start a temporary session

```bash
uv run python scripts/start_usdview.py test_scene.usda
```

The launcher selects an unused port, starts a server with a temporary event
log, finds usdview in the active OpenUSD environment, and enables the receiver
plugin. Closing usdview or pressing `Ctrl+C` stops the server. Pass
`--event-log events.db` to keep the session.

To connect usdview to a server that is already running instead:

```bash
uv run python -m integrations.usdview.launcher test_scene.usda --host 127.0.0.1 --port 7200
```

In either case, usdview opens with the **OpenUSDConnect** menu and the plugin
auto-connects after the window is ready.

If discovery fails, run `uv run python scripts/start_usdview.py --help`, confirm
the executable independently with `usdview --help`, then pass its full path
with `--usdview`. Both launchers report each searched source as a concise error.

## Receive behavior

The integration negotiates layered replay. It maps each opaque collaboration
layer key to a receiver-owned anonymous layer, composes those layers in the
advertised strong-to-weak order, and tracks live mute changes. Department
assignment is a server policy that currently selects those logical layers; it
is not part of the receiver's composition logic. Unrelated session sublayers
retain their relative order and offsets. Flat replay is not used by this
integration.

Authored fields from the sender's current edit-target layer are preserved as
exact Sdf spec deltas. This includes custom properties, relationships, prim and
layer metadata, and local variant definitions, including inactive variants.
The protocol does not yet reproduce arbitrary client-authored sublayer graphs
or route transactions to layers outside the managed collaboration block.

## RenderMan

Pass `--renderman` to configure the hdPrman Hydra delegate and select it at
startup:

```bash
uv run python scripts/start_usdview.py test_scene.usda --renderman
```

This requires a RenderManProServer installation with `RMANTREE` set and an
OpenUSD build made with `PXR_BUILD_PRMAN_PLUGIN=ON`. The launcher configures
the `RMAN_*` variables and native library paths, then selects
`HdPrmanLoaderRendererPlugin`.

Without `--renderman`, the launcher leaves renderer configuration unchanged.
Use `--renderman --renderer Storm` to start in Storm while keeping RenderMan
available in usdview's **Renderer** menu. See the
[OpenUSD RenderMan plugin documentation](https://openusd.org/release/plugins_renderman.html).

## How usdview discovery works

Both launchers call `find_usdview()`, which checks these sources in order:

1. `usdview.cmd`, `usdview.exe`, or `usdview` on `PATH`.
2. `bin/usdview*` below the OpenUSD installation inferred from
   `pxr.__file__`.
3. The path in `USDVIEW_PATH`.

If you have a non-standard install, set `USDVIEW_PATH` to the executable
and the launcher will use it directly.

## Manual installation

To load the plugin when starting usdview directly, add the plugin and repository
paths to the environment:

```bash
# Bash / Zsh
export PXR_PLUGINPATH_NAME="/path/to/OpenUSDConnect/integrations/usdview:$PXR_PLUGINPATH_NAME"
export PYTHONPATH="/path/to/OpenUSDConnect:$PYTHONPATH"
usdview some_scene.usda
```

```powershell
# PowerShell
$env:PXR_PLUGINPATH_NAME = "D:\gamedev\OpenUSDConnect\integrations\usdview;$env:PXR_PLUGINPATH_NAME"
$env:PYTHONPATH = "D:\gamedev\OpenUSDConnect;$env:PYTHONPATH"
usdview some_scene.usda
```

Without `OPENUSDCONNECT_HOST`, the menu loads but does not auto-connect. Choose
**Connect to OpenUSDConnect…** and enter the endpoint.

## Refreshing a late asset dependency

Variant definitions supplied by referenced or payloaded assets remain in those
assets; OpenUSDConnect replicates the local selection opinion. Locally authored
variant definitions in the synchronized edit target use exact Sdf spec deltas.
If a shared asset was unavailable when its composition event arrived, make it
available through the receiver's normal asset resolver and retry it from
usdview's Python console:

```python
from integrations.usdview.connection import refresh_asset_dependency

refresh_asset_dependency("asset:Character/{$VERSION}/Character.usda")
# Omit the argument to retry every currently unresolved dependency.
```

The retry refreshes the stage's resolver context and locally reapplies matching
composition events that resolve. Passing an explicit identifier also refreshes
an already-resolved dependency whose custom-resolver mapping changed. It does
not send anything to the server or advance the receiver sequence. Reapplication
uses the edit target that originally received the arc and abandons stale
tracking if that local opinion has since changed. The reapplied event preserves
the complete authored list op, including list position, layer offset and scale,
and reference custom data. Custom resolvers must be installed and configured in
the usdview process. Since a resolver context refresh is context-wide, other
tracked arcs whose resolution changes in the same refresh are updated too.

## Environment variables

| Variable | Purpose |
|---|---|
| `OPENUSDCONNECT_HOST` | Auto-connect target host (skips the dialog) |
| `OPENUSDCONNECT_PORT` | Auto-connect target port (default 7200) |
| `OPENUSDCONNECT_TOKEN` | Explicit TOFU token. When omitted, `UsdReceiver` loads and persists the normal cached token for the host and port. |
| `USDVIEW_PATH` | Explicit path to `usdview` executable for the launcher |
| `PXR_PLUGINPATH_NAME` | Directory containing this `plugInfo.json` |
| `PYTHONPATH` | Must include the OpenUSDConnect repo root so `integrations.usdview` and `openusdconnect` resolve |
| `RMANTREE` | RenderManProServer install root. Read only when `--renderman` is passed; the launcher derives all `RMAN_*` paths from it. |

## Limitations

- Sending edits from usdview back to the server (usdview has no native
  authoring UX)
- Playback leader control
- A custom Qt sidebar; usdview's plugin API exposes menus and new top-level
  windows, not dockable panels
