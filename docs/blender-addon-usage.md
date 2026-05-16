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

This is the primary workflow: one Blender instance emits changes, another receives them in real time.

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
| `set_gprim_attrs` | Attribute change | Updates parametric attrs, mesh topology, primvars (UVs, vertex colors), normals, purpose |
| `set_reference` | USD reference set on a prim | Imports the referenced asset |
| `set_payload` | USD payload arc set on a prim | Stores payload info (unloaded by default) |
| `load_payload` | Payload load requested | Imports the payload asset |
| `unload_payload` | Payload unload requested | Removes imported payload children |
| `set_variant_selections` | Variant selection changed | Updates the active variant |
| `set_material_binding` | Material bound/unbound | Updates the material:binding relationship |
| `set_connectable_input` | Shader, NodeGraph, Material, or UsdLux light input value changed | Writes the typed input via `UsdShade.ConnectableAPI` |
| `set_connectable_connection` | Shader/NodeGraph/Material/light input or output connection authored or cleared | Updates the connection edge |
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
