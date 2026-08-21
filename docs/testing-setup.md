# Testing

The default suite is headless. Blender, Unreal Engine, RenderMan, native mount,
asset, and visual tests are opt-in.

## Test tiers

| Tier | Location | Default behavior |
| --- | --- | --- |
| Core and headless integration | `tests/unit/`, most of `tests/integration/` | runs normally |
| Timing-dependent | tests marked `slow` | requires `--slow-tests` |
| Blender integration | selected `tests/integration/` | skips when Blender is not configured |
| Blender asset E2E | `tests/integration/asset_tests/` | requires `--asset-tests` and Blender |
| Unreal E2E | `tests/integration/test_unreal_integration.py` | requires `--unreal-tests` |
| Visual regression | `tests/visual/` | requires `--visual-tests`, RenderMan, and FLIP |

The runtime created by `scripts/build_openusd.py` is selected automatically.
For an OpenUSD installation built outside that workflow, configure it first.
On PowerShell:

```powershell
. .\scripts\openusd_env.ps1 "D:\OpenUSDInstall"
```

Current Windows and Unix OpenUSD Python layouts and the legacy `lib/python`
layout are detected automatically. If the bindings were installed into a
virtual environment, activate it first; its site-packages is also searched.
Use `-PythonPath` only for bindings outside both locations. An existing valid
`RMANTREE` is also configured automatically.

Then install the normal development environment without the bundled runtime:

```bash
uv sync --group vfs --group dev
```

Use `uv sync --group bundled-usd --group vfs --group dev` only for a
renderer-neutral test environment that needs neither MaterialX nor custom
plugins.

Run the common suites:

```bash
uv run pytest tests/unit/ -v
uv run pytest tests/ -v
uv run pytest tests/unit/ --slow-tests -v
```

For automation or other shells using an external build, configure the test
command through the wrapper:

```bash
uv run python scripts/run_with_openusd.py --usd-root /path/to/OpenUSD -- \
  pytest tests/ -v
```

The wrapper accepts the equivalent `--python-path` option.

Add `--renderman-root /path/to/RenderManProServer` when running the visual tier.

`tests/` includes headless integration tests. Tests that need a missing external
runtime skip with a reason rather than making the core suite unusable.

## Blender

Blender tests require Blender 4.4 or newer. The executable is resolved in this
order:

1. `--blender /path/to/blender`
2. `BLENDER_EXE`
3. the first non-comment line of `blender.test.cfg`
4. skip if none is configured

To use a repository-local Blender runtime:

```bash
uv run python scripts/setup_blender_test.py --version 5.0.1
```

The script downloads Blender under `.blender/` and writes
`blender.test.cfg`. Omit `--version` for the script's current default. Remove
the portable installation with:

```bash
uv run python scripts/setup_blender_test.py --cleanup
```

Examples with an existing installation:

```bash
BLENDER_EXE=/path/to/blender uv run pytest tests/integration/ -v
uv run pytest tests/integration/ --blender /path/to/blender -v
```

The harness uses repo-local Blender user resources, so tests and addon builds do
not modify a normal user installation.

## Asset E2E tests

Initialize the USD Working Group asset submodule first:

```bash
git submodule update --init --recursive
```

Then run:

```bash
uv run pytest tests/integration/asset_tests/ --asset-tests -v
```

These tests launch Blender and exercise the real server, add-on, native USD
import, references and payloads, variants, texture loading, MaterialX
NodeGraphs, material identity, bindings, cameras, and backlog replay. Scope a
failure to one test while iterating:

```bash
uv run pytest \
  tests/integration/asset_tests/test_assets.py::test_bishop_materialx \
  --asset-tests -v
```

Harness usage and extension guidance live beside the tests in
[`tests/integration/asset_tests/README.md`](../tests/integration/asset_tests/README.md).

### Inspect the Material Zoo interactively

```bash
uv run python scripts/run_material_zoo.py --show --renderman
uv run python scripts/run_material_zoo.py --viewers blender usdview unreal --renderman
```

The runner starts a temporary server, opens the unchanged `test_scene.usda` in
the selected viewers, and streams the committed Material Zoo fixture plus a
shared camera and IBL. `--show` is the Blender-and-usdview shortcut; add
`--viewers unreal` to launch Unreal alone or select all three as shown above.
If Blender is unavailable, add `--download-blender` to download and configure
the repo-local portable runtime before any viewers or server are started.
Unreal is discovered and its ABI-matched plugin package is built or reused by
the existing test harness. Use `--unreal-engine-root` to select an engine,
`--no-presentation` to omit camera/IBL events, and `--exit-after 0` to keep the
session open until Ctrl+C.

## Visual regression

Visual tests render reference scenes with RenderMan and compare them with
committed goldens using FLIP:

```bash
uv sync --group visual
uv run pytest tests/visual --visual-tests -v
```

Regenerate an intended baseline change, inspect the image, then run the compare
pass again:

```bash
uv run pytest tests/visual --visual-tests --update-baselines -v
uv run pytest tests/visual --visual-tests -v
```

The tier skips when `RMANTREE` or `flip-evaluator` is unavailable. A comparison
failure reports the FLIP error-map path. Goldens depend on the renderer, sample
budget, and USD/RenderMan versions and are stored with Git LFS.

The renderer registry is in `integrations/visualtest/renderers.py`. The default
RenderMan path translates OpenPBR to standard_surface because hdPrman does not
provide an OpenPBR adapter.

## Unreal Engine

Unreal tests package the plugin and launch a real editor. Initialize the asset
submodule first:

```bash
git submodule update --init --recursive
```

The harness uses MaterialX texture fixtures from the asset submodule even
before launching the editor. Then discover available engines with:

```bash
uv run python scripts/run_unreal_tests.py --list-engines
```

Run the generated-project scenario:

```bash
uv run python scripts/run_unreal_tests.py \
  --engine-root /path/to/UnrealEngine \
  --work-dir /tmp/openusdconnect-unreal
```

Equivalent pytest entry point:

```bash
uv run pytest tests/integration/test_unreal_integration.py \
  --unreal-tests \
  --unreal-engine /path/to/UnrealEngine \
  -v
```

The harness can also target an existing project or prebuilt plugin package.
Use `uv run python scripts/run_unreal_tests.py --help` before enabling
`--install-plugin` or `--replace-plugin`, because those options intentionally
modify the selected project.

## Live-open and VFS tests

The focused portable suite covers metadata, ETags, snapshot caching, WebDAV
verbs, write modes, local-bridge conflict handling, and replay continuation:

```bash
uv run pytest \
  tests/unit/test_vfs.py \
  tests/unit/test_vfs_mount.py \
  tests/unit/test_local_vfs_bridge.py \
  tests/unit/test_start_live_open.py \
  tests/integration/test_vfs_webdav.py \
  tests/integration/test_start_live_open.py \
  tests/integration/test_live_discovery.py \
  -q
```

Real OS mounts are opt-in:

```powershell
$env:OUC_RUN_UNC_SMOKE = "1"
uv run pytest tests/integration/test_windows_unc_webdav.py -q
```

```bash
OUC_RUN_MACOS_WEBDAV_SMOKE=1 \
  uv run pytest tests/integration/test_macos_webdav_mount.py -q
```

## Interactive Blender debugging

The debug launcher starts a server and one or two repo-isolated Blender
instances with optional debugpy attachment:

```bash
uv run python scripts/start_usdconnect_debug.py --wait-for-debugger
uv run python scripts/start_usdconnect_debug.py --wait-for-debugger --two-blenders
```

Generate VS Code launch/tasks configuration after the first addon install:

```bash
uv run python scripts/setup_vscode.py
```

Rebuild and hot-reload the addon into running debug instances:

```bash
uv run python scripts/start_usdconnect_debug.py --reload
```

Use `--start-emitter`, `--start-receiver`, `--no-server`, `--base`, and
`--event-log` as needed. Run the launcher with `--help` for the complete option
list.

## Coverage and lint

```bash
uv run pytest tests/unit/ --cov
uv run pytest tests/unit/ --cov --cov-report=html
uv run ruff check
uv run ruff format --check
```

Coverage is configured in `pyproject.toml` for the `openusdconnect` package.

## Useful diagnostics

```bash
uv run openusdconnect-server --base test_scene.usda --event-log events.db
uv run openusdconnect-send --help
uv run python scripts/dump_events.py events.db
```

For performance testing and py-spy commands, see [Profiling](profiling.md).
