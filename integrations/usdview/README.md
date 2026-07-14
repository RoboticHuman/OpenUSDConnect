# OpenUSDConnect — usdview integration

Receive-side integration for Pixar's reference USD viewer. Adds an
**OpenUSDConnect** menu to usdview with Connect / Disconnect / Status
commands. While connected, every event the server broadcasts (transforms,
visibility, material bindings, composition arcs, time samples, stage
metadata) is applied directly to the stage usdview is rendering.

This is the smallest possible DCC integration in the project — usdview
already owns a live `Usd.Stage`, so the core `UsdStageAdapter` plugs in
unchanged. No DCC-native object mapping, no emitter (receive-only).

## Quick start

```bash
# Terminal 1 — start the server on a base scene
uv run python -m openusdconnect.server --port 7200 --base test_scene.usda --log events.db

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

## Cycles (hdCycles) renderer — optional

When an hdCycles plugin built against the same OpenUSD installation is already
discoverable through `PXR_PLUGINPATH_NAME`, pass `--cycles` to start in Cycles
while keeping scene interpretation entirely under HdCycles' control:

```bash
PXR_PLUGINPATH_NAME="/path/to/cycles/install/hydra:$PXR_PLUGINPATH_NAME" \
uv run python -m integrations.usdview.launcher test_scene.usda --cycles
```

OpenUSDConnect does not author renderer-specific material branches. Portable
materials are presented to HdCycles unchanged, and an explicitly authored
`outputs:cycles:surface` network passes through like any other synchronized USD
data. This keeps unsupported MaterialX features visible as delegate capability
gaps instead of approximating them in the receiver.

The `--cycles` path does not change HdCycles sampling, adaptive sampling,
denoising, lighting, or other viewport-quality settings. Those remain under the
renderer and user's control. On Apple Silicon, the launcher defaults
`CYCLES_DEVICE` to `METAL` before the delegate is created and exposes the
installed OIDN runtime modules. An explicitly set `CYCLES_DEVICE` still wins.

On first use for a new scene feature set, the HdCycles viewport may remain black
while Cycles compiles its Metal render kernels. This build reports that warm-up
as 76 kernels and warns internally that it may take a few minutes; a controlled
M4 run took about 69 seconds. The compiled kernels are cached under
`~/.cache/cycles`, and subsequent matching launches render immediately. Set
`CYCLES_LOGGING=1` and `CYCLES_LOGGING_LEVEL=info` before launch to expose device
and render progress in the terminal, or set `CYCLES_DEVICE=CPU` to opt out of
Metal rendering and GPU denoising.

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
