# Unreal Engine plugin

The plugin connects an Unreal `AUsdStageActor` to OpenUSDConnect. It receives a
flat event stream and publishes transform, visibility, and connectable-input
edits made in Unreal. Other event types are receive-only; check
[Supported events](#supported-events) before relying on Unreal as an author.

## Requirements

| Requirement | Why |
|-------------|-----|
| **Unreal Engine 5.8** | The checked-in `.uplugin` targets 5.8 and uses the pxr USD C++ SDK behind Unreal's wrappers. Launcher 5.8 contains the required headers and libraries. Build separately for other engine versions. |
| **`USDImporter` plugin enabled** | Provides `AUsdStageActor` and the pxr stage handle. |
| **`USDCore` plugin available** | Provides `UnrealUSDWrapper` (pxr linkage + `USE_USD_SDK`). Enabling `USDImporter` enables this dependency transitively. |
| **OpenUSDConnect server** | Run the Python server from the repository root. |
| FlatBuffers headers | Included in release packages. For source builds, run `python setup_flatbuffers.py` or use the Unreal harness, which stages the pinned headers automatically. |

Generated protocol bindings are committed under
`native/client_core/include/openusdconnect/client/schema/`; users do not need
`flatc`. Schema
changes require the compiler (`scoop install main/flatc` on Windows) and
`bash scripts/generate_flatbuffers.sh` from the repository root. That script
regenerates both Python and native C++ bindings. Keep
`setup_flatbuffers.py::DEFAULT_VERSION` aligned with the compiler version.

## Installation

Windows and macOS use the same plugin layout. The plugin contains two runtime
modules, but the project only enables `OpenUSDConnect`. The Linux layout is the
same but has not been validated.

1. **Install a packaged plugin** into your project:
   ```
   <YourProject>/Plugins/OpenUSDConnect/
   ```

   Release packages are self-contained. From a repository checkout, build and
   install the package with the Unreal harness; it stages the canonical native
   client core and FlatBuffers headers before invoking `BuildPlugin`:

   ```powershell
   uv run python scripts/run_unreal_tests.py --project <path-to-project.uproject> --install-plugin
   ```

   For the repository's Unreal test harness, also initialize the asset
   submodule from the repository root:

   ```bash
   git submodule update --init --recursive
   ```

2. **Enable it in your `.uproject`** (alongside `USDImporter`; `USDCore` is a
   transitive dependency of `USDImporter`):
   ```json
   {
     "Plugins": [
       { "Name": "USDImporter",     "Enabled": true },
       { "Name": "OpenUSDConnect",  "Enabled": true }
     ]
   }
   ```

3. **Regenerate project files.** On Windows, right-click the `.uproject` and
   choose **Generate Visual Studio project files**. On macOS, generate Xcode
   files or build the editor target directly with UnrealBuildTool.

4. **Build** the editor target in Development configuration:
   ```
   # Windows
   Engine/Build/BatchFiles/Build.bat <ProjectName>Editor Win64 Development -Project=<path-to-.uproject>

   # macOS
   Engine/Build/BatchFiles/Mac/Build.sh <ProjectName>Editor Mac Development -Project=<path-to-.uproject>
   ```

## Configuration

**Edit → Project Settings → Plugins → OpenUSD Connect**

| Setting | Default | Description |
|---------|---------|-------------|
| Server Host | `127.0.0.1` | Hostname/IP of the OpenUSDConnect server |
| Server Port | `7200` | TCP port |
| Department | *(empty)* | Optional emitter department. The server enables department policy with `--departments`; the current Unreal receiver remains flat and cannot join such a server. |
| Auto Connect on World Start | `true` | Connect on first tick after world initialization |
| Use USD Live Metadata | `true` | If the opened USD root layer has `customLayerData["openusdconnect"]`, use its host, port, and snapshot sequence. |
| Auto-start Receiver from Metadata | `true` | Start the receiver automatically when live metadata is detected. |
| Auto-start Emitter from Metadata | `true` | Start the emitter automatically when live metadata is detected. |
| Persist Auth Tokens | `true` | Save server-issued TOFU tokens in the user's Unreal config and reuse them on reconnect. |
| Reconnect Delay (s) | `3.0` | Wait time between reconnect attempts |

The plugin receives through flat replay. Use it with one
unmuted collaboration layer and no server department policy. A department
server rejects the receiver with a clear status message; layered Unreal replay
is future work. The Department setting remains available to the emitter but
does not make the receive path layer-aware.

## Usage

### 1. Start the server

The base-file workflow is the default. Configure the same compatible OpenUSD
build and plugin environment used by the project, then install the dashboard
dependency and start the server with the same scene that Unreal will open:

```bash
uv sync --group dashboard
uv run openusdconnect-server --port 7200 --base my_scene.usda --event-log unreal-session.db --dashboard-port 8080
```

See [OpenUSD runtime and custom
plugins](../../../docs/cli-reference.md#openusd-runtime-and-custom-plugins).
Use `bundled-usd` only for a renderer-neutral session that needs neither
MaterialX nor custom plugins.

The dashboard is available at <http://127.0.0.1:8080>. Stop the server with
Ctrl+C; `unreal-session.db` retains its event history.

### 2. Open the USD stage in Unreal

1. Open **Window > Virtual Production > USD Stage**.
2. Choose **File > Open** and select `my_scene.usda`.
3. Select the spawned `AUsdStageActor` in the World Outliner.
4. In **Details**, set **Stage State** to `OpenedAndLoaded`.

With `Opened`, prims appear in the USD Stage tree but Unreal does not create
scene components for live viewport updates.

### 3. Connect another client

Open the same base scene in Blender or a compatible client and connect it to
`127.0.0.1:7200`. In Blender, start the receiver before the emitter.

### 4. Verify synchronization

The subsystem runs in the editor without pressing Play:

- Move a transformable prim in Blender and confirm it moves in Unreal.
- Move the corresponding prim in Unreal's viewport and confirm Blender follows.
- Open the dashboard and confirm the Unreal receiver and emitter connections.

### Optional: open a server-provided USD file

Live-open provides a generated `scene.usd` through a normal file picker. It
requires one unmuted collaboration layer and no department policy.

From the repository root:

```bash
uv sync --group vfs --group dashboard
uv run python scripts/start_live_open.py --base my_scene.usda --dashboard-port 8080 --open
```

Bundled fallback users must also include `--group bundled-usd` in the `uv sync`
command.

The launcher prints the local file path and session state file. On Windows the
default path is `O:\scene.usd`; on macOS and Linux use the printed mirror
directory. Then:

1. Open the generated `scene.usd` in the USD Stage panel.
2. Set its `AUsdStageActor` to `OpenedAndLoaded`.
3. The plugin reads `customLayerData["openusdconnect"]`, selects the metadata
   endpoint, and starts after the embedded snapshot sequence.

Stop the launcher with:

```bash
uv run python scripts/start_live_open.py stop
```

See [Server-Provided USD Files](../../../docs/live-open.md) for custom paths,
write fallback, authentication, and diagnostics.

The **Output Log** should contain:
```
LogUSDConnectSubsystem:   Detected OpenUSDConnect live metadata on stage: 127.0.0.1:7200 snapshot_seq=...
LogUSDConnectSubsystem:   Using USD live metadata; receiver will sync from seq=...
LogUSDConnect:            Connected to OpenUSDConnect server at 127.0.0.1:7200 (receiver, sync_from=...)
LogUSDConnect:            HELLO_OK received entering receive loop
LogUSDEmit:               Emitter connected to 127.0.0.1:7200
LogUSDEmit:               Emitter HELLO_OK ready to send
LogUSDConnectSubsystem:   Attached to AUsdStageActor (UsdStageActor_0)
```
Per-event messages use the `Verbose` level. Enable them with
`LogUSDConnect Verbose` in the editor console.

Blueprint or editor utility tooling can call `GetStatus()` on the
`USDConnectSubsystem` to display the active endpoint, metadata source,
snapshot sequence, receiver/emitter connection state, and auth state.

Token-required live-open:

- On first connect, the receiver obtains the TOFU token.
- The plugin keeps it in memory for the current session and saves it when
  **Persist Auth Tokens** is enabled.
- If the emitter was waiting for that first token, it starts on the next tick.
- Future reconnects send the saved token on both receiver and emitter sockets.

## Architecture

```
                    Server :7200
                   ┌────────────┐
Blender ──emit──→  │ authoritative ──broadcast──→ Unreal (receiver)
Blender ←recv───   │   pxr stage  ←──────────── Unreal (emitter)
                   └────────────┘
```

The plugin opens two TCP connections:

- The receiver applies `BroadcastEvent` frames to the pxr stage owned by
  `AUsdStageActor`. The actor's USD notice listener refreshes the corresponding
  Unreal components.
- The emitter watches the stage actor's USD notices. For supported local edits,
  it reads TRS, visibility, or shader `inputs:*` values and sends a `Txn` frame.

Origin matching and `bSuppressEmit` prevent received events from being
published again.

See [Plugin developer notes](PLUGIN_DEV.md) for threading and protocol details.

## Automated integration test

The opt-in harness finds a Launcher or source engine, packages the plugin,
generates a test project, and checks bidirectional stage and material parity.

It installs the pinned FlatBuffers headers when they are missing.

```bash
uv run python scripts/run_unreal_tests.py --list-engines
uv run python scripts/run_unreal_tests.py --engine-root /path/to/UnrealEngine
```

See [Unreal Engine testing](../../../docs/testing-setup.md#unreal-engine)
for pytest, existing-project, cache, and interactive options.

## Troubleshooting

Live-open-specific checks:

- If the plugin connects to the wrong port after opening `scene.usd`, confirm
  **Use USD Live Metadata** is enabled and the opened root layer contains
  `customLayerData["openusdconnect"]`.
- If the receiver replays old events after live-open, confirm the log says
  `receiver, sync_from=<snapshot_seq + 1>`.
- If auth fails, call `GetStatus()` or check the Output Log for
  `auth_rejected`; revoke the server token and delete the saved local token to
  force a new TOFU first-connect.

| Symptom | Cause / Fix |
|---------|-------------|
| `Could not connect to 127.0.0.1:7200 retrying` | The server is not running or the endpoint differs. Start the server and check the configured port. |
| `LogPluginManager: ... incompatible with engine version` | The `.uplugin` declares 5.8. Rebuild and deliberately source-port the plugin for another engine version rather than editing only the descriptor. |
| Connection works but edits don't reflect in Unreal viewport | The stage actor's `Stage State` is `Opened` instead of `OpenedAndLoaded`. Change it in the Details panel. |
| Geometry imports but renders default gray | The asset's materials only author a MaterialX context (`outputs:mtlx:surface`, e.g. the usd-wg OpenChessSet), and the stage actor defaults to the `universal` (UsdPreviewSurface) context. Set **Render Context → `mtlx`** on the `AUsdStageActor` Details panel. |
| `LogUsd: ... failed to find a valid referenced MaterialX file. Reverting to parsing the generated Material prims` | The fallback prim parse **does not translate MaterialX values**. The plugin's auto-materializer normally prevents this state for supported synced inline networks by generating a local `.mtlx` document (see *MaterialX rendering* below). Persistent warnings can mean the material was deliberately skipped, such as a renderer-specific node (`PxrSurface`, Lama) or an unmapped input type. |
| Inline MaterialX materials render gray or black | UE 5.8 translates referenced `.mtlx` documents but not values from inline `ND_*` networks. The plugin materializes supported networks automatically; see [MaterialX rendering](#materialx-rendering-auto-materializer). Enable **Substrate Adaptive GBuffer** for fuller `standard_surface` support. UsdPreviewSurface uses the universal context and is unaffected. |
| Generated meshes are named after a **container** prim instead of the individual objects (e.g. `SM_World1`, `SM_World2`, … for a root prim called `World`), every synced edit rebuilds them, and materials jump between objects on visibility changes | That container prim is being **collapsed**: it has no `kind`, and UE collapses kind-less subtrees by default (`USD.CollapsePrimsWithoutKind` is true), folding the whole subtree into **one** static mesh whose sections and material slots re-index on every rebuild. Author `kind = "group"` on scene-root Xforms (correct USD model hierarchy), or set `USD.CollapsePrimsWithoutKind 0`, or uncheck **Use Prim Kinds For Collapsing** on the stage actor. |
| Generated assets churn constantly (new transient packages per edit); appearance drifts until a full stage reload | No persistent asset cache: each stage actor defaults to a throwaway transient cache. Create a **USD Asset Cache** asset and assign it on the stage actor (or Project Settings → USDCore → Default Asset Cache). Consider also disabling **Share Assets for Identical Prims**, so prims with identical geometry but different materials don't share one mesh asset. |
| Edits in Unreal don't reach Blender | Confirm the **Emitter HELLO_OK** line appears in the log; if not, the emitter socket failed. Check the dashboard's *Clients* tab. |
| Plugin engine-version warning | The checked-in descriptor targets Unreal 5.8. Rebuild and deliberately port the plugin for another engine version rather than editing only the descriptor. |

For deeper diagnostics, enable verbose logging in the editor console:
```
Log LogUSDConnect Verbose
Log LogUSDConnectSubsystem Verbose
Log LogUSDEmit Verbose
Log LogUSDEventApplier Verbose
```

## Supported events

| Event | Receiver | Emitter |
|---|---|---|
| EnsurePrim (create prim) | ✅ | |
| EnsureXformOps | ✅ | |
| SetXformTrs (T/R/S) | ✅ | ✅ |
| DeletePrim | ✅ | |
| DeactivatePrim | ✅ | |
| RenamePrim | ✅ | |
| SetVisibility | ✅ | ✅ |
| SetStageMetadata (FPS, timecodes, metersPerUnit) | ✅ | |
| SetReference / SetPayload | ✅ | |
| LoadPayload / UnloadPayload | ✅ | |
| SetVariantSelections | ✅ | |
| SetMaterialBinding | ✅ | |
| SetGprimAttrs (mesh points/normals/primvars, camera params) | ✅ | |
| SetInstanceable / SetPointInstancer | ✅ | |
| SetSdfSpecFields (generic authored fields) | ✅ | - |
| SetConnectableInput (shader/material/light parameters) | ✅ | ✅ |
| SetConnectableConnection (connection edges) | ✅ | |

`SetConnectableInput` and `SetConnectableConnection` author typed values,
`info:id`, and connection edges on the pxr stage. Rendering depends on Unreal's
USD material translation and the stage actor's **Render Context**.

## MaterialX rendering (auto-materializer)

Unreal renders MaterialX from referenced `.mtlx` documents. It recognizes
inline `ND_*` networks but does not translate their values. For supported
networks, the plugin serializes the composed graph to
`<Project>/Saved/OpenUSDConnect/MaterialX/<Prim_Path>.mtlx` and references it
from the material prim's session-layer opinion. This local reference is never
sent to the server; other clients continue to consume the inline graph.

Documents are content-addressed as `<Prim_Path>.<hash>.mtlx`. Regeneration
writes a new file and updates the session reference, forcing the stage actor to
re-import the material. Superseded files are removed when possible.

usdMtlx projects document values as interface inputs on the Material prim, so
the USD Stage panel shows local material-level inputs. The plugin redirects
edits on those inputs to the same-named inputs on the inline surface shader.
The shader inputs remain the shared authored state.

UsdPreviewSurface materials are not materialized. Unreal translates them to
material instances and can update their parameters without recompiling a
shader. Under the `mtlx` render context, the plugin explicitly refreshes those
instances because Unreal's normal update path misses the value changes.

The plugin leaves renderer-specific shaders (`PxrSurface`, Lama), unsupported
input types, and materials with an existing external `.mtlx` reference
unchanged.

## Unreal behavior

The following engine behavior affects synchronized stages:

- **Viewport moves collapse xform op stacks.** The engine's component
  write-back (`UnrealToUsd::ConvertXformable`) replaces a prim's entire op
  stack with one `xformOp:transform` matrix after a gizmo move. That would
  disconnect the canonical `translate`/`orient`/`scale` attributes from the
  composed transform. The plugin sends the decomposed TRS and restores the
  canonical stack without changing the composed transform. Prims that already
  use matrix ops keep their structure.
- **Component write-back can re-author shape attributes.** The engine bakes
  gprim attributes (sphere radius, cube size, …) together with the xform into
  one component transform, and resets those attributes when writing a moved
  component back to USD. These are engine-local opinions the plugin does not
  emit; a stage reload replays the shared-scene values.
- **The color picker authors HDR values.** Unreal can write components above
  1.0 to USD color attributes. Other renderers preserve that intensity, so a
  `(10, 1, 1)` color will not behave like normalized red. Keep components in
  the 0–1 range unless HDR output is intentional.
- **Material edits belong on the shader prim.** The material-level inputs the
  USD Stage panel shows on document-backed materials are usdMtlx projections
  local to this Unreal instance. The plugin reroutes edits made there onto the
  inline shader, but the shader's `inputs:*` are the real authored state (see
  *MaterialX rendering*).
- **Don't save the root layer over your source file.** Live sync authors its
  opinions into the stage's root layer in memory, so the editor's
  "Save USD Layers" prompt appears on level changes. Saving would bake the
  entire synchronized session into the `.usda` on disk. Discard the prompt
  unless that is intentional. Plugin-owned documents and references live in
  the session layer and `Saved/`.

See [Plugin developer notes](PLUGIN_DEV.md) for protocol details and known
gaps.
