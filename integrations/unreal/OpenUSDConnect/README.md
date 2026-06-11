# OpenUSD Connect — Unreal Engine Plugin

Live two-way sync of an OpenUSD stage between Unreal Engine and any other DCC
(Blender today, others tomorrow) via the **OpenUSDConnect** server.

Edit a prim in Blender → it moves in Unreal within a frame. Move a prim in
Unreal's USD Stage editor → Blender sees it.

---

## Requirements

| Requirement | Why |
|-------------|-----|
| **Unreal Engine 5.5+ source build** | The plugin uses the pxr USD C++ SDK behind the `USDImporter` plugin's wrappers. Launcher builds do not link the USD SDK. Tested against 5.7. |
| **`USDImporter` plugin enabled** | Provides `AUsdStageActor` and the pxr stage handle. |
| **`USDCore` plugin enabled** | Provides `UnrealUSDWrapper` (pxr linkage + `USE_USD_SDK`). |
| **Python OpenUSDConnect server** | The hub all clients connect to. See repo root for the server. |
| FlatBuffers 24.3.25 | Already shipped with UE5 at `Engine/Source/ThirdParty/flatbuffers/flatbuffers-24.3.25/`. The Build.cs references it directly — no separate install. |

---

## Installation

1. **Copy the plugin folder** into your project:
   ```
   <YourProject>/Plugins/OpenUSDConnect/
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
| RenamePrim | ⚠ stub | — |
| SetVisibility | ✅ | ✅ |
| SetStageMetadata (FPS, timecodes, metersPerUnit) | ✅ | — |
| SetReference / SetPayload | ✅ | — |
| LoadPayload / UnloadPayload | ✅ | — |
| SetVariantSelections | ✅ | — |
| SetMaterialBinding | ✅ | — |
| SetGprimAttrs (mesh points/normals/primvars) | ❌ | ❌ |
| SetConnectableInput / SetConnectableConnection (shaders) | ❌ | ❌ |

See [`PLUGIN_DEV.md`](PLUGIN_DEV.md) for the protocol details and a list of remaining work.
