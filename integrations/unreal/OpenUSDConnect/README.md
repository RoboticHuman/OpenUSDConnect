# OpenUSD Connect — Unreal Engine Plugin

Live two-way sync of an OpenUSD stage between Unreal Engine and any other DCC
(Blender today, others tomorrow) via the **OpenUSDConnect** server.

Edit a prim in Blender → it moves in Unreal within a frame. Move a prim in
Unreal's USD Stage editor → Blender sees it.

---

## Requirements

| Requirement | Why |
|-------------|-----|
| **Unreal Engine 5.5+** (source or Launcher) | The plugin uses the pxr USD C++ SDK behind the `USDImporter` plugin's wrappers. Launcher builds of 5.8 ship the full USD SDK (headers + libs under the `USDCore` plugin's ThirdParty), so a source checkout is no longer required. Tested against 5.7 (source) and 5.8 (Launcher). |
| **`USDImporter` plugin enabled** | Provides `AUsdStageActor` and the pxr stage handle. |
| **`USDCore` plugin enabled** | Provides `UnrealUSDWrapper` (pxr linkage + `USE_USD_SDK`). |
| **Python OpenUSDConnect server** | The hub all clients connect to. See repo root for the server. |
| FlatBuffers headers | Header-only. Source-engine checkouts ship them at `Engine/Source/ThirdParty/flatbuffers` and the Build.cs picks up whatever version the engine carries. Launcher builds ship only the license stub: run `python setup_flatbuffers.py --engine <engine root>` once from the plugin folder — it detects the version the engine declares and downloads it into the plugin's ThirdParty directory. |

---

## Installation

1. **Copy the plugin folder** into your project:
   ```
   <YourProject>/Plugins/OpenUSDConnect/
   ```

   On a **Launcher-installed engine**, also fetch the FlatBuffers headers once
   (source checkouts skip this — the engine ships them):
   ```
   cd <YourProject>/Plugins/OpenUSDConnect
   python setup_flatbuffers.py --engine "D:/UE_5.8"
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

3. **Regenerate project files** — right-click the `.uproject` → *Generate Visual Studio project files*.

4. **Build** the editor target (Development Editor) from Visual Studio or via
   `Engine/Build/BatchFiles/Build.bat <ProjectName>Editor Win64 Development -Project=<path-to-.uproject>`.

---

## Configuration

**Edit → Project Settings → Plugins → OpenUSD Connect**

| Setting | Default | Description |
|---------|---------|-------------|
| Server Host | `127.0.0.1` | Hostname/IP of the OpenUSDConnect server |
| Server Port | `7200` | TCP port |
| Department | *(empty)* | Per-department layer name (match the server's `--department` arg if set) |
| Auto Connect on World Start | `true` | Connect on first tick after world initialization |
| Reconnect Delay (s) | `3.0` | Wait time between reconnect attempts |

---

## Usage

### 1. Start the OpenUSDConnect server

From the repo root:
```bash
cd <repo>/OpenUSDConnect
.\.venv\Scripts\activate          # or `source .venv/bin/activate`
python -m openusdconnect.server --port 7200 --base my_scene.usda --dashboard 8080
```
The optional `--dashboard 8080` enables a web UI at <http://localhost:8080> useful for verifying clients and event traffic.

### 2. Open the USD stage in Unreal

The **standard** workflow:

1. **Window → Virtual Production → USD Stage** (or search the menu for "USD Stage")
2. In the USD Stage panel: **File → Open** → pick the same `.usda` the server is hosting.
3. This spawns an `AUsdStageActor` in the level. Select it in the World Outliner.
4. In its **Details** panel set **Stage State → `OpenedAndLoaded`**.
   > ⚠ This is required. With `Opened`, the prims appear in the panel tree but
   > **no scene components are generated**, so live edits have nothing to apply to.

### 3. Connect Blender (or any other client)

Install the OpenUSDConnect Blender addon, open the same `.usda`, enable Emit + Receive.

### 4. It just works

Without pressing Play — the subsystem ticks in the editor:

- Move a cube in Blender → it moves in Unreal.
- Drag a prim in Unreal's USD Stage tree (or the level viewport) → Blender follows.

The **Output Log** will show:
```
LogUSDConnect:            Connected to OpenUSDConnect server at 127.0.0.1:7200 (receiver)
LogUSDConnect:            HELLO_OK received — entering receive loop
LogUSDEmit:               Emitter connected to 127.0.0.1:7200
LogUSDEmit:               Emitter HELLO_OK — ready to send
LogUSDConnectSubsystem:   Attached to AUsdStageActor (UsdStageActor_0)
```
Per-event chatter is at `Verbose` level — enable it via `LogUSDConnect Verbose` in the console if you need to debug traffic.

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

- **Receiver** — gets `BroadcastEvent` frames and applies them to the pxr stage that `AUsdStageActor` is holding. The stage actor's USD notice listener then refreshes the Unreal scene components.
- **Emitter** — subscribes to `AUsdStageActor::OnPrimChanged`; when a prim is edited locally, reads the prim's TRS/visibility from the pxr stage and sends a `Txn` frame.

An echo guard (origin matching + an in-flight `bSuppressEmit` flag) prevents the receiver from re-emitting events it just applied.

Full architecture and protocol notes: see [`PLUGIN_DEV.md`](PLUGIN_DEV.md).

---

## Troubleshooting

| Symptom | Cause / Fix |
|---------|-------------|
| `Could not connect to 127.0.0.1:7200 — retrying` | Server isn't running, or port mismatch. Start the server. |
| `LogPluginManager: ... incompatible with engine version` | The `.uplugin` declares `5.7.0` and the plugin uses 5.5+ APIs (e.g. `EAllowShrinking`). If you're on an older UE, source-port those call sites first. |
| Connection works but edits don't reflect in Unreal viewport | The stage actor's `Stage State` is `Opened` instead of `OpenedAndLoaded`. Change it in the Details panel. |
| Geometry imports but renders default gray | The asset's materials only author a MaterialX context (`outputs:mtlx:surface`, e.g. the usd-wg OpenChessSet), and the stage actor defaults to the `universal` (UsdPreviewSurface) context. Set **Render Context → `mtlx`** on the `AUsdStageActor` Details panel. |
| `LogUsd: ... failed to find a valid referenced MaterialX file. Reverting to parsing the generated Material prims` | Expected for wire-authored inline shader networks (there is no `.mtlx` file behind them) — but note the fallback prim parse **does not translate MaterialX values**: the material asset is created, the surface renders default gray/black. |
| Inline MaterialX materials render default gray or black | UE 5.8 translates MaterialX only from **referenced `.mtlx` documents** (via Interchange); inline `ND_*` prim networks are recognized structurally but their values are not translated. Reference a document instead (`set_reference` with a `.mtlx` asset path), **name the USD Material prim exactly like the material inside the document** (UE maps the generated material back by name), and consider **Substrate Adaptive GBuffer** (Project Settings → Rendering) for full standard_surface fidelity. UsdPreviewSurface inline networks are unaffected — they translate through the universal context. |
| Generated meshes are named after a **container** prim instead of the individual objects (e.g. `SM_World1`, `SM_World2`, … for a root prim called `World`), every synced edit rebuilds them, and materials jump between objects on visibility changes | That container prim is being **collapsed**: it has no `kind`, and UE collapses kind-less subtrees by default (`USD.CollapsePrimsWithoutKind` is true), folding the whole subtree into **one** static mesh whose sections and material slots re-index on every rebuild. Author `kind = "group"` on scene-root Xforms (correct USD model hierarchy), or set `USD.CollapsePrimsWithoutKind 0`, or uncheck **Use Prim Kinds For Collapsing** on the stage actor. |
| Generated assets churn constantly (new transient packages per edit); appearance drifts until a full stage reload | No persistent asset cache: each stage actor defaults to a throwaway transient cache. Create a **USD Asset Cache** asset and assign it on the stage actor (or Project Settings → USDCore → Default Asset Cache). Consider also disabling **Share Assets for Identical Prims**, so prims with identical geometry but different materials don't share one mesh asset. |
| Edits in Unreal don't reach Blender | Confirm the **Emitter HELLO_OK** line appears in the log; if not, the emitter socket failed. Check the dashboard's *Clients* tab. |
| `Plugin requires engine version '5.7.0'` warning on UE 5.5/5.6 | Source-compatible if you replace `EAllowShrinking::No` with `false` in `USDConnectSubsystem.cpp`. |

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
| EnsurePrim (create prim) | ✅ | — |
| EnsureXformOps | ✅ | — |
| SetXformTrs (T/R/S) | ✅ | ✅ |
| DeletePrim | ✅ | — |
| DeactivatePrim | ✅ | — |
| RenamePrim | ✅ | — |
| SetVisibility | ✅ | ✅ |
| SetStageMetadata (FPS, timecodes, metersPerUnit) | ✅ | — |
| SetReference / SetPayload | ✅ | — |
| LoadPayload / UnloadPayload | ✅ | — |
| SetVariantSelections | ✅ | — |
| SetMaterialBinding | ✅ | — |
| SetGprimAttrs (mesh points/normals/primvars, camera params) | ✅ | — |
| SetInstanceable / SetPointInstancer | ✅ | — |
| SetConnectableInput / SetConnectableConnection (shaders) | ✅ | — |

`SetConnectableInput` / `SetConnectableConnection` author the shader network onto
the pxr stage (typed values, `info:id`, connection edges); what renders from it
is up to UE's USD material translation and the stage actor's **Render Context**
(see Troubleshooting).

See [`PLUGIN_DEV.md`](PLUGIN_DEV.md) for the protocol details and a list of remaining work.
