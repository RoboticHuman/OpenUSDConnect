# OpenUSD Connect Unreal Engine Plugin

Live two-way sync of an OpenUSD stage between Unreal Engine and any other DCC
(Blender today, others tomorrow) via the **OpenUSDConnect** server.

Edit a prim in Blender and it updates in Unreal's editor. Move a prim in
Unreal's USD Stage editor and the change returns to Blender.

---

## Requirements

| Requirement | Why |
|-------------|-----|
| **Unreal Engine 5.8** | The checked-in `.uplugin` targets 5.8 and uses the pxr USD C++ SDK behind Unreal's USD wrappers. Launcher 5.8 includes the required USD headers and libraries, so a source checkout is not required. Porting the source to an older engine may be possible, but the packaged plugin is not advertised as cross-version binary compatible. |
| **`USDImporter` plugin enabled** | Provides `AUsdStageActor` and the pxr stage handle. |
| **`USDCore` plugin enabled** | Provides `UnrealUSDWrapper` (pxr linkage + `USE_USD_SDK`). |
| **Python OpenUSDConnect server** | The hub all clients connect to. See repo root for the server. |
| FlatBuffers headers | Header-only. Run `python setup_flatbuffers.py` once from the plugin folder it downloads the runtime headers matching the committed flatc-generated protocol bindings (the generated code pins the exact version via a `static_assert`, so engine-shipped copies are not used). |

**Development note:** the protocol bindings under `Source/OpenUSDConnectPXR/Public/Schema/`
are generated from the repo's FlatBuffers schemas and committed, so plugin *users*
never need the compiler. Changing the schemas requires `flatc` available from the
official scoop repo (`scoop install main/flatc`) and one run of
`bash scripts/generate_flatbuffers.sh` from the repo root, which regenerates the
Python and Unreal bindings together. Keep `setup_flatbuffers.py`'s `DEFAULT_VERSION`
in lockstep with the `flatc` version used to regenerate.

---

## Installation

The plugin package and project entry are identical on Windows and macOS. It
contains two internal runtime modules, but only `OpenUSDConnect` is enabled in
the project. Linux follows the same layout but has not been validated here.

1. **Copy the plugin folder** into your project:
   ```
   <YourProject>/Plugins/OpenUSDConnect/
   ```

   Fetch the FlatBuffers runtime headers once (header-only download, any
   engine flavor):
   ```
   cd <YourProject>/Plugins/OpenUSDConnect
   python setup_flatbuffers.py
   ```

2. **Enable it in your `.uproject`** (alongside `USDImporter`):
   ```json
   {
     "Plugins": [
       { "Name": "USDImporter",     "Enabled": true },
       { "Name": "OpenUSDConnect",  "Enabled": true }
     ]
   }
   ```

3. **Regenerate project files** using the platform's normal Unreal workflow.
   On Windows, right-click the `.uproject` and choose *Generate Visual Studio
   project files*. On macOS, generate Xcode project files or build the editor
   target directly with UnrealBuildTool.

4. **Build** the editor target in Development configuration:
   ```
   # Windows
   Engine/Build/BatchFiles/Build.bat <ProjectName>Editor Win64 Development -Project=<path-to-.uproject>

   # macOS
   Engine/Build/BatchFiles/Mac/Build.sh <ProjectName>Editor Mac Development -Project=<path-to-.uproject>
   ```

---

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

The native plugin currently receives through flat replay. Use it with one
unmuted collaboration layer and no server department policy. A department
server rejects the receiver with a clear status message; layered Unreal replay
is future work. The Department setting remains available to the emitter but
does not make the receive path layer-aware.

---

## Usage

### 1. Start the OpenUSDConnect server

From the repo root:
```bash
cd <repo>/OpenUSDConnect
.\.venv\Scripts\activate          # or `source .venv/bin/activate`
python -m openusdconnect.server --port 7200 --base my_scene.usda --vfs-port 7280 --dashboard-port 8080
```
The optional `--dashboard-port 8080` enables a web UI at <http://localhost:8080> useful for verifying clients and event traffic.

### 2. Open the USD stage in Unreal

The **live-open** workflow:

1. Mount or bridge the VFS share so `scene.usd` is browseable, for example:
   ```bash
   uv run python scripts/local_vfs_bridge.py --vfs-url http://127.0.0.1:7280/usd/scene.usd --mirror-dir .ouc_live_mount\usd --drive O: --force
   ```
2. Open the USD Stage panel and choose **File -> Open**.
3. Pick `O:\scene.usd` or the UNC path.
4. The plugin reads `customLayerData["openusdconnect"]`, switches to the metadata host/port, and starts the receiver from `snapshot_seq + 1`.
5. Select the spawned `AUsdStageActor` in the World Outliner and set **Stage State -> `OpenedAndLoaded`**.

For a one-command local session, run this from the repo root:

```bash
uv run python scripts/start_live_open.py --base my_scene.usda --drive O: --open --force
```

The older manual workflow still works:

1. **Window → Virtual Production → USD Stage** (or search the menu for "USD Stage")
2. In the USD Stage panel: **File → Open** → pick the same `.usda` the server is hosting.
3. This spawns an `AUsdStageActor` in the level. Select it in the World Outliner.
4. In its **Details** panel set **Stage State → `OpenedAndLoaded`**.
   > ⚠ This is required. With `Opened`, the prims appear in the panel tree but
   > **no scene components are generated**, so live edits have nothing to apply to.

### 3. Connect Blender (or any other client)

Install the OpenUSDConnect Blender addon, open the same `.usda`, enable Emit + Receive.

### 4. It just works

Without pressing Play the subsystem ticks in the editor:

- Move a cube in Blender → it moves in Unreal.
- Drag a prim in Unreal's USD Stage tree (or the level viewport) → Blender follows.

The **Output Log** will show:
```
LogUSDConnectSubsystem:   Detected OpenUSDConnect live metadata on stage: 127.0.0.1:7200 snapshot_seq=...
LogUSDConnectSubsystem:   Using USD live metadata; receiver will sync from seq=...
LogUSDConnect:            Connected to OpenUSDConnect server at 127.0.0.1:7200 (receiver, sync_from=...)
LogUSDConnect:            HELLO_OK received entering receive loop
LogUSDEmit:               Emitter connected to 127.0.0.1:7200
LogUSDEmit:               Emitter HELLO_OK ready to send
LogUSDConnectSubsystem:   Attached to AUsdStageActor (UsdStageActor_0)
```
Per-event chatter is at `Verbose` level enable it via `LogUSDConnect Verbose` in the console if you need to debug traffic.

Blueprint or editor utility tooling can call `GetStatus()` on the
`USDConnectSubsystem` to display the active endpoint, metadata source,
snapshot sequence, receiver/emitter connection state, and auth state.

Token-required live-open:

- On first connect, the receiver obtains the TOFU token.
- The plugin keeps it in memory for the current session and saves it when
  **Persist Auth Tokens** is enabled.
- If the emitter was waiting for that first token, it starts on the next tick.
- Future reconnects send the saved token on both receiver and emitter sockets.

---

## How it works (high level)

```
                    Server :7200
                   ┌────────────┐
Blender ──emit──→  │ authoritative ──broadcast──→ Unreal (receiver)
Blender ←recv───   │   pxr stage  ←──────────── Unreal (emitter)
                   └────────────┘
```

The plugin opens **two** TCP connections to the server:

- **Receiver** gets `BroadcastEvent` frames and applies them to the pxr stage that `AUsdStageActor` is holding. The stage actor's USD notice listener then refreshes the Unreal scene components.
- **Emitter** subscribes to the stage actor's USD listener; when a prim is edited locally (viewport transforms, USD Stage panel properties), reads the prim's TRS/visibility and for shader prims, the edited `inputs:*` values from the pxr stage and sends a `Txn` frame.

An echo guard (origin matching + an in-flight `bSuppressEmit` flag) prevents the receiver from re-emitting events it just applied.

Full architecture and protocol notes: see [`PLUGIN_DEV.md`](PLUGIN_DEV.md).

---

## Automated integration test

From the repository root, the opt-in harness can discover a Launcher or source
engine, package this plugin, generate an enabled project, and run two-way stage and
material parity checks:

The harness installs the pinned FlatBuffers headers automatically when they are
missing from a clean checkout.

```bash
uv run python scripts/run_unreal_tests.py --list-engines
uv run python scripts/run_unreal_tests.py --engine-root /path/to/UnrealEngine
```

See [Unreal Engine testing](../../../docs/testing-setup.md#unreal-engine)
for pytest, existing-project, cache, and interactive options.

---

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
| `Could not connect to 127.0.0.1:7200 retrying` | Server isn't running, or port mismatch. Start the server. |
| `LogPluginManager: ... incompatible with engine version` | The `.uplugin` declares 5.8. Rebuild and deliberately source-port the plugin for another engine version rather than editing only the descriptor. |
| Connection works but edits don't reflect in Unreal viewport | The stage actor's `Stage State` is `Opened` instead of `OpenedAndLoaded`. Change it in the Details panel. |
| Geometry imports but renders default gray | The asset's materials only author a MaterialX context (`outputs:mtlx:surface`, e.g. the usd-wg OpenChessSet), and the stage actor defaults to the `universal` (UsdPreviewSurface) context. Set **Render Context → `mtlx`** on the `AUsdStageActor` Details panel. |
| `LogUsd: ... failed to find a valid referenced MaterialX file. Reverting to parsing the generated Material prims` | The fallback prim parse **does not translate MaterialX values**. The plugin's auto-materializer normally prevents this state for supported synced inline networks by generating a local `.mtlx` document (see *MaterialX rendering* below). Persistent warnings can mean the material was deliberately skipped, such as a renderer-specific node (`PxrSurface`, Lama) or an unmapped input type. |
| Inline MaterialX materials render default gray or black | UE 5.8 translates MaterialX only from **referenced `.mtlx` documents** (via Interchange); inline `ND_*` prim networks are recognized structurally but their values are not translated. The plugin works around this automatically see *MaterialX rendering* below. Consider **Substrate Adaptive GBuffer** (Project Settings → Rendering) for full standard_surface fidelity. UsdPreviewSurface inline networks are unaffected they translate through the universal context. |
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

---

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

`SetConnectableInput` / `SetConnectableConnection` author the shader network onto
the pxr stage (typed values, `info:id`, connection edges); what renders from it
is up to UE's USD material translation and the stage actor's **Render Context**
(see Troubleshooting).

## MaterialX rendering (auto-materializer)

The engine renders MaterialX only from referenced `.mtlx` documents inline
`ND_*` prim networks are recognized but their values are not translated. The
plugin bridges supported networks automatically: whenever a synced or
locally-edited material with an `mtlx` surface output changes, it serializes
the composed network to a document under
`<Project>/Saved/OpenUSDConnect/MaterialX/<Prim_Path>.mtlx` and references it
on the material prim in the stage's **session layer**. Session-layer state is
local to this Unreal instance it never reaches the sync server, so every
other client keeps consuming the inline network untouched.

Documents are content-addressed (`<Prim_Path>.<hash>.mtlx`): every
regeneration produces a new file and swaps the session reference to it a
real composition change the stage actor must re-import, so live material
edits from any client (or Unreal's own USD Stage panel) update the Unreal
render within a tick. Superseded revisions are deleted best-effort.

One side effect of the document reference: usdMtlx projects the document's
values as **interface inputs on the Material prim**, so the USD Stage panel
shows editable-looking inputs at the material level that exist only in this
Unreal instance. Edits there are rerouted onto the inline surface shader's
same-named inputs automatically (the generated document guarantees the
mapping), so editing either level reaches the shared scene but the shader's
inputs are the real authored state.

**UsdPreviewSurface** materials never get documents the engine translates
them to material instances whose parameters update in place (no shader
compile, unlike a document re-import). The engine's own update chain misses
those value edits under the `mtlx` render context, so the plugin re-pulls the
linked instances' parameters with the engine's converter instead.

Skipped (left exactly as authored): renderer-specific shaders (`PxrSurface`,
Lama), networks with input types that have no MaterialX equivalent, and
materials already backed by a foreign `.mtlx` document reference.

## Unreal-specific quirks

Engine behaviors worth knowing when Unreal is one of the synced clients. The
plugin compensates for each; the notes explain what you'll observe and why.

- **Viewport moves collapse xform op stacks.** The engine's component
  write-back (`UnrealToUsd::ConvertXformable`) replaces a prim's entire op
  stack with a single `xformOp:transform` matrix on every gizmo move. Left
  alone, this orphans the canonical `translate`/`orient`/`scale` attributes
  (per-op edits in the USD Stage panel stop doing anything) and breaks
  TRS-based sync. The plugin decomposes the matrix for the wire and restores
  the canonical op stack right after each move same composed transform, so
  nothing jumps. Prims that natively author matrix ops (foreign assets) keep
  their structure; their moves sync through decomposition.
- **Component write-back can re-author shape attributes.** The engine bakes
  gprim attributes (sphere radius, cube size, …) together with the xform into
  one component transform, and resets those attributes when writing a moved
  component back to USD. These are engine-local opinions the plugin does not
  emit; a stage reload replays the shared-scene values.
- **The color picker authors HDR.** Unreal's picker happily writes component
  values above 1.0 into USD color attributes. Other renderers will show
  blow-outs (a `(10, 1, 1)` "red" renders white-hot). Keep values in 0–1
  unless HDR is intended.
- **Material edits belong on the shader prim.** The material-level inputs the
  USD Stage panel shows on document-backed materials are usdMtlx projections
  local to this Unreal instance. The plugin reroutes edits made there onto the
  inline shader, but the shader's `inputs:*` are the real authored state (see
  *MaterialX rendering*).
- **Don't save the root layer over your source file.** Live sync authors its
  opinions into the stage's root layer in memory, so the editor's
  "Save USD Layers" prompt appears on level changes. Saving would bake the
  entire synced session into the `.usda` on disk discard unless that's what
  you want. Everything the plugin adds for itself (documents, references)
  lives in the session layer and `Saved/`, and never prompts.

See [`PLUGIN_DEV.md`](PLUGIN_DEV.md) for the protocol details and a list of remaining work.
