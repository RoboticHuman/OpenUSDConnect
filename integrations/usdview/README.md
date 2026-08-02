# OpenUSDConnect — usdview integration

Receive-side integration for Pixar's reference USD viewer. Adds an
**OpenUSDConnect** menu to usdview with Connect / Disconnect / Status
commands. While connected, every event the server broadcasts (transforms,
visibility, material bindings, composition arcs, time samples, stage
metadata) is applied directly to the stage usdview is rendering.

This is the smallest possible DCC integration in the project — usdview
already owns a live `Usd.Stage`, so the core `UsdStageAdapter` plugs in
unchanged. No DCC-native object mapping, no emitter (receive-only).

The integration negotiates layered replay. It maps each opaque collaboration
layer key to a receiver-owned anonymous layer, composes those layers in the
advertised strong-to-weak order, and tracks live mute changes. Department
assignment is a server policy that currently selects those logical layers; it
is not part of the receiver's composition logic. Unrelated session sublayers
retain their relative order and offsets. If the server does not acknowledge
the capability, the receiver falls back to the existing flat replay path.

Authored fields from the sender's current edit-target layer are preserved as
exact Sdf spec deltas. This includes custom properties, relationships, prim and
layer metadata, and local variant definitions, including inactive variants.
The protocol does not yet reproduce arbitrary client-authored sublayer graphs
or route transactions to layers outside the managed collaboration block.

## Quick start

```bash
# Terminal 1 — start the server on a base scene
uv run python -m openusdconnect.server --port 7200 --base test_scene.usda --event-log events.db

# Terminal 2 — launch usdview pre-wired to that server
uv run python -m integrations.usdview.launcher test_scene.usda --host 127.0.0.1 --port 7200
```

usdview opens, the **OpenUSDConnect** menu appears in the menubar, and
the plugin auto-connects ~100ms after the window is up (driven by the
`OPENUSDCONNECT_HOST` env var the launcher sets).

## RenderMan (hdPrman) renderer — optional

Pixar's **hdPrman** Hydra delegate is off by default. Pass `--renderman`
to make it available and start usdview in it:

```bash
uv run python -m integrations.usdview.launcher test_scene.usda --renderman
```

The flag sets the `RMAN_*` search paths from `$RMANTREE` plus the OpenUSD
install that ships hdPrman (discovered the same way as `usdview`), prepends
`$RMANTREE\bin` and `$RMANTREE\lib` to `PATH` so `libprman.dll` loads, and
selects the `HdPrmanLoaderRendererPlugin` delegate ("RenderMan RIS"). It
requires a RenderManProServer install with `RMANTREE` set and an OpenUSD
build made with the RenderMan imaging plugin (`PXR_BUILD_PRMAN_PLUGIN=ON`).

Without `--renderman` nothing changes — usdview runs Storm-only as before.
You can still switch renderers from usdview's **Renderer** menu once
`--renderman` is on, and you can override which delegate it starts in by
forwarding your own `--renderer` (e.g. `--renderman --renderer Storm` opens
in Storm with RenderMan available in the menu). See
https://openusd.org/release/plugins_renderman.html

## How `find_usdview()` works

The launcher's `find_usdview()` tries three strategies in order:

1. `shutil.which("usdview.cmd"/"usdview.exe"/"usdview")` — works when
   the OpenUSD `bin/` directory is on `PATH`.
2. Walk up from `pxr.__file__` to the install root and probe
   `bin/usdview*`. `pxr` imports without PySide6 (only `pxr.Usdviewq`
   needs Qt), so this works in any env where the pxr Python bindings
   are reachable.
3. `USDVIEW_PATH` env var as an explicit override.

If you have a non-standard install, set `USDVIEW_PATH` to the executable
and the launcher will use it directly.

## Manual install (for system-wide usdview, no launcher)

If you want the plugin to load whenever you run usdview directly
(without going through the launcher):

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

Without `OPENUSDCONNECT_HOST` set, the menu is present but won't
auto-connect — pick **Connect to OpenUSDConnect…** from the menu and
enter the host/port.

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
| `OPENUSDCONNECT_TOKEN` | TOFU token to present on connect. Must be set explicitly — the integration does not yet read the cached `~/.openusdconnect/tokens.json`. |
| `USDVIEW_PATH` | Explicit path to `usdview` executable for the launcher |
| `PXR_PLUGINPATH_NAME` | Directory containing this `plugInfo.json` |
| `PYTHONPATH` | Must include the OpenUSDConnect repo root so `integrations.usdview` and `openusdconnect` resolve |
| `RMANTREE` | RenderManProServer install root. Read only when `--renderman` is passed; the launcher derives all `RMAN_*` paths from it. |

## Out of scope (later phases)

- Sending edits from usdview back to the server (usdview has no native
  authoring UX)
- Playback leader control
- Custom Qt sidebar panel (usdview's plugin API only exposes menus and
  new top-level windows)
