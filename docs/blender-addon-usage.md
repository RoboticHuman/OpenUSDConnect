# Blender Addon Usage

The USD Connect addon enables real-time transform sync between Blender instances (or any DCC with an OpenUSDConnect adapter) via a networked event protocol.

## Installation

1. Build the addon zip:
   ```bash
   uv run python scripts/build_blender_addon.py
   ```
   This produces `dist/usd_connect_blender.zip`.

2. In Blender: **Edit > Preferences > Add-ons > Install from Disk** and select the zip.

3. Enable the "USD Connect" addon (category: Import-Export).

The zip bundles the `openusdconnect` core library, so no separate Python package installation is needed.

For development, see [Testing Setup](testing-setup.md) for the debug launcher (`start_usdconnect_debug.py`) which supports hot-reloading the addon into running Blender instances without restarting.

## UI Overview

The addon adds a **USD Connect** tab in the 3D Viewport sidebar (press `N` to toggle). It has five sections:

### Import
- **Import USD (with prim tagging)** — Opens a file browser to import a `.usda`/`.usd`/`.usdc` file. All imported objects are tagged with their USD prim paths (`usd_prim_path` and `usd_type_name` custom properties).
- **Skip Leaf /Geom Prim Paths** — When enabled (default), skips tagging the leaf `/Geom` child prims to reduce noise.

### Local Capture
Captures transform edits into a USD session layer delta (local only, no network). Useful for offline diffing.

- **Base USD File** — Path to the reference USD file.
- **Start/Stop Capture** — Toggles the depsgraph listener.
- **Emit Diff** — Exports the session layer delta as `.usda` text.
- **Clear Diff** — Discards all buffered changes.
- **Coalesce (sec)** — Batching window for change events (default: 0.15s).

### Network Emitter
Sends live transform events to a sync server.

- **Server Host / Port** — Address of the OpenUSDConnect server (default: `127.0.0.1:7200`).
- **Send Rate (Hz)** — How often to poll for changes (default: 60 Hz).
- **Auto-track New Objects** — When enabled, any manipulated object without a `usd_prim_path` tag is automatically assigned one.
- **Root Prim** — Parent path for auto-tracked objects (default: `/World`).
- **Connect/Disconnect Emitter** — Toggles the network connection.

### Network Receiver
Listens to the sync server and applies incoming events to Blender objects.

- **Host / Port** — Server address (default: `127.0.0.1:7200`).
- **Start/Stop Receiver** — Toggles the background listener thread.
- **Last seq** — Shows the highest event sequence number processed.
- **Reset Seq** — Clears the sequence counter to force a full replay on next connect.

## Two-Blender Live Sync Walkthrough

This is the manual workflow: one Blender instance emits changes, another receives them in real time.
For the newer live-open workflow, see [Live-Open Quickstart](live-open-quickstart.md).

## Live-Open Via Virtual USD File

The addon can import an OpenUSDConnect virtual USD file and auto-connect to
the live server when the file contains `customLayerData["openusdconnect"]`.
This is the preferred workflow when the server is started with `--vfs-port`.

### Server

```bash
uv run openusdconnect-server --port 7200 --base scene.usda --vfs-port 7280
```

This exposes:

| Path | Use |
|------|-----|
| `http://127.0.0.1:7280/usd/scene.usd` | Backing WebDAV URL and Blender live URL import. |
| `\\127.0.0.1@7280\usd\scene.usd` | Windows file-picker path through WebDAV/UNC. |

### Blender

Use either entry point:

- Click **Import USD (with prim tagging)** and choose the UNC path.
- Paste the HTTP path into the live URL field and click the URL import button.

The addon imports the snapshot first. If live metadata is present, it sets
the receiver and emitter host/port, seeds the receiver from `snapshot_seq`,
starts the emitter, and starts the receiver from `snapshot_seq + 1`.
HTTP live URL imports are cached locally by URL and ETag so unchanged imports
reuse the same base USD path instead of leaking temp files. If a live URL
points at `scene.live.usda`, the addon follows the embedded flattened fallback
URL because the one-file local cache cannot resolve the remote `_layers/`
directory.

The virtual share is read-only by default. Server operators can opt into
compatibility drop mode with `--vfs-write-mode drop`, but direct file writes
still do not become live sync edits.

If metadata is not present, the import behaves like the existing manual
workflow. If auto-connect fails, the imported snapshot remains open and the
addon reports the connection error.

When the server is running with `--require-token`, the USD file only says
that a token is required. It does not contain the token. Blender uses the
existing TOFU token store for the live emitter and receiver connections.

### 1. Start the sync server

In a terminal:

```bash
uv run python -m openusdconnect.server --port 7200 --base scene.usda --log events.db
```

The server is an authoritative event sequencer. It maintains a replay log so receivers that connect late (or reconnect) get all prior events. Use `--compact` on startup to compact the event log, or send a `compact` message from any connected client to trigger compaction at runtime.

### 2. Set up the Emitter (Blender instance A)

1. Open the **USD Connect** sidebar panel.
2. Click **Import USD** and select your base scene file. This tags all objects with their USD prim paths.
3. In the **Network Emitter** section:
   - Set **Server Host** and **Port** to match the server.
   - Optionally enable **Auto-track New Objects** if you want to sync objects created during the session.
4. Click **Connect Emitter**. The status should show "Emitter connected".

Now any transform, visibility, or object creation changes are sent to the server as events.

### 3. Set up the Receiver (Blender instance B)

1. Open the **USD Connect** sidebar panel.
2. Click **Import USD** and select the **same** base scene file. Objects must have matching prim path tags.
3. In the **Network Receiver** section:
   - Set **Host** and **Port** to match the server.
4. Click **Start Receiver**. The status should show "Receiver running".

Incoming events are applied automatically. Move an object in instance A and it updates in instance B.

### 4. Test it

In the emitter Blender:
- Select an object and move/rotate/scale it — the receiver updates in real time.
- Toggle object visibility (eye icon in outliner) — the receiver shows/hides accordingly.
- With auto-tracking enabled, add a new primitive (e.g., Add > Mesh > Cube) and move it — the receiver creates and positions it.

## Synced Event Types

| Event | What triggers it | What happens on the receiver |
|-------|-----------------|------------------------------|
| `ensure_prim` | First encounter of an object | Creates the prim (Xform, Sphere, Cube, SphereLight, etc.) and applies any API schemas in `api_schemas` (e.g. `ShapingAPI` for spot lights) |
| `ensure_xform_ops` | First encounter of an object | Establishes translate/orient/scale ops |
| `set_xform_trs` | Object moved/rotated/scaled | Applies the transform delta |
| `set_visibility` | Visibility toggled | Shows/hides the object |
| `set_gprim_attrs` | Attribute change | Updates parametric attrs (sphere radius, etc.), mesh topology, primvars (UVs, vertex colors), normals, purpose, and `UsdGeomCamera` typed attrs (focal length, aperture, clipping, projection, DoF) |
| `set_reference` | USD reference set on a prim | Imports the referenced asset |
| `set_payload` | USD payload arc set on a prim | Stores payload info (unloaded by default) |
| `load_payload` | Payload load requested | Imports the payload asset |
| `unload_payload` | Payload unload requested | Removes imported payload children |
| `set_variant_selections` | Variant selection changed | Updates the active variant |
| `set_material_binding` | Material bound/unbound | Updates the material:binding relationship |
| `set_connectable_input` | Shader, NodeGraph, Material, or UsdLux light input value changed | Writes the typed input via `UsdShade.ConnectableAPI` |
| `set_connectable_connection` | Shader/NodeGraph/Material/light input or output connection authored or cleared | Updates the connection edge |
| `set_instanceable` | Native scenegraph instancing toggled on a prim that has a reference/payload arc | Sets the `instanceable` flag; composition rebuilds the instance locally. The Blender adapter toggles collection-instance Empties best-effort |
| `set_point_instancer` | UsdGeomPointInstancer prototypes or per-instance arrays changed (including animated samples) | Authors the prototypes relationship and typed arrays at the event's time code. The Blender adapter records paths only |
| `deactivate_prim` | Object deleted | Deactivates the prim |
| `delete_prim` | Prim removed from stage | Removes the prim |
| `rename_prim` | Object renamed | Renames the prim path |

Rotation is transmitted as quaternion `[w, x, y, z]` (USD convention). Only changed fields are sent (partial diffs).

`set_gprim_attrs` events include `primvar_meta` (USD type name and interpolation) for
primvar attributes and `attr_interp` for non-primvar attributes with interpolation metadata
(e.g., normals).

### UsdLux lights

Light prims (`DistantLight`, `SphereLight`, `RectLight`, `DiskLight`, `DomeLight`) replicate through the same machinery as shaders and materials — they're `UsdShade.ConnectableAPI` containers and their parameters (`intensity`, `color`, `radius`, `shaping:cone:angle`, `texture:file`, etc.) ride on `set_connectable_input` events with empty `info_id`. Applied API schemas (`ShapingAPI` for spot/cone lights, `ShadowAPI` for shadow controls, `MeshLightAPI`/`VolumeLightAPI` for mesh-as-light) flow via the optional `api_schemas` field on `ensure_prim` (additive only — removing an API schema is out of scope for v1).

The Blender adapter's UsdLux→Blender light translation (intensity↔energy conversion, mapping ShapingAPI to Blender's spot data) is not yet wired up — the receive path lands the schema on the mirror USD stage but doesn't currently create a Blender light object.

### UsdGeomCamera

Camera prims (`UsdGeomCamera`) replicate through `ensure_prim` (typeName=`"Camera"`) plus `set_gprim_attrs` carrying the typed camera attrs. The Blender adapter routes those attrs onto a `bpy.types.Camera` data block. Conversion follows Blender's own USD importer:

| USD attr | Blender field | Note |
|---|---|---|
| `focalLength` | `camera.lens` | scaled by `100 * stage_meters_per_unit` (USD's tenths-of-unit → mm) |
| `horizontalAperture` / `verticalAperture` | `camera.sensor_width` / `sensor_height` | same scaling, drives `sensor_fit` |
| `horizontalApertureOffset` / `verticalApertureOffset` | `camera.shift_x` / `shift_y` | normalised by sensor size |
| `clippingRange` | `camera.clip_start` / `clip_end` | scaled by `scene_scale` |
| `focusDistance` | `camera.dof.focus_distance` | scaled by `scene_scale` |
| `fStop` | `camera.dof.aperture_fstop` + `use_dof` | `use_dof` = (fStop > 0) |
| `projection` | `camera.type` | `"perspective"` → `PERSP`, `"orthographic"` → `ORTHO` |

Camera attributes participate in the time-sample flow: when a value is authored at a USD time sample, it replicates with the `time` field set on `set_gprim_attrs`. Blender-side F-curve authoring (cameras + transforms) is a follow-up — current capture writes the static opinion. Time samples authored on the mirror stage from any source (manual scripting, scrubbing in another DCC) are picked up automatically by the emitter.

## Playback synchronization

A single client at a time drives the shared playhead. The flow is:

1. **Claim** — that client clicks **Claim Playback** in the panel. The server grants the leader role if no one currently holds it; otherwise the request is rejected with the current leader's id.
2. **Drive** — the leader uses **Play / Pause / Push Frame** to advance the playhead. Each command updates the server's playback state and broadcasts `PlaybackState` to every other client.
3. **Follow** — non-leader clients receive `PlaybackState` and the addon calls `scene.frame_set(...)` on the next idle tick, mapping `time` (USD timecode) to a Blender frame via the scene FPS. The same feedback guard that suppresses incoming-event re-emission also wraps the `frame_set` call so the depsgraph evaluation doesn't bounce back to the server.
4. **Release** — on leader disconnect, the server clears the role and broadcasts a vacant-leader `PlaybackState`. Another client can then claim.

Stage metadata (`timeCodesPerSecond`, `framesPerSecond`, `startTimeCode`, `endTimeCode`, `metersPerUnit`, `upAxis`) is delivered in `hello_ok` for new clients and broadcast as a `set_stage_metadata` event when it changes mid-session. The Blender receiver maps `framesPerSecond` to `scene.render.fps`, `metersPerUnit` to `scene.unit_settings.scale_length`, and the start/end timecodes to `scene.frame_start` / `scene.frame_end`.

## Auto-track Mode

When **Auto-track New Objects** is enabled in the Network Emitter:

- Any object you manipulate (even without an existing `usd_prim_path`) is automatically assigned a path under the configured **Root Prim** (e.g., `/World/Cube`).
- The USD type is inferred from the Blender mesh data: Sphere, Cube, Cylinder, Cone map to their USD equivalents. Other meshes become `Mesh`, non-mesh objects become `Xform`.
- The inferred type is stored as a `usd_type_name` custom property on the Blender object.

## Sequence Persistence

The receiver remembers the last sequence number it processed. On disconnect and reconnect, it resumes from where it left off — no duplicate events. To force a full replay from the beginning, click **Reset Seq** while the receiver is stopped.

If the server compacts its event log, it broadcasts a `resync` message. Receivers must handle this by resetting their sequence counter — the server then replays the compacted log. The Blender addon handles this. Other DCC integrations must implement `resync` support (see `protocol.py` for the message spec).

## Feedback Loop Guard

The addon prevents feedback loops: when the receiver applies incoming events, the capture/emitter module is temporarily suppressed so those changes aren't re-emitted back to the server.
