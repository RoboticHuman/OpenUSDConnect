# Blender addon

The Blender addon imports USD with stable prim-path tags, publishes Blender
edits through a local USD authoring stage, and applies authoritative composed
changes from a separate USD mirror back into Blender.

## Install

```bash
uv run python scripts/build_blender_addon.py
```

Install `dist/usd_connect_blender.zip` through **Edit > Preferences > Add-ons >
Install from Disk**, then enable **USD Connect**. The zip includes the
`openusdconnect` Python package; Blender supplies its own `pxr` bindings.

For a development install and hot reload, see
[Testing](testing-setup.md#interactive-blender-debugging).

## UI

Open the **USD Connect** tab in the 3D Viewport sidebar (`N`).

### Import

**Import USD (with prim tagging)** uses Blender's USD importer and records the
source paths as `usd_prim_path` and `usd_type_name` custom properties. Those
tags are the stable identity used by emit and receive.

When the imported file contains OpenUSDConnect live metadata, the import panel
can auto-start the emitter and receiver. **Skip Leaf /Geom Prim Paths** avoids
tagging common importer-generated leaf geometry objects when their parent is
the useful scene identity.

### Local Capture

Local Capture observes Blender changes and writes them into a USD session-layer
delta without networking. Use **Emit Diff** to export the authored delta and
**Clear Diff** to discard it. **Coalesce (sec)** controls the local batching
window.

### Network Emitter

The emitter connects to the sync server and publishes changes detected from
Blender. Configure host, port, optional department, and send rate. Department
ordering applies only when the server was started with `--departments`.
**Auto-track New Objects** assigns untagged objects a path below **Root Prim**
when they are first edited.

### Network Receiver

The receiver reads the authoritative stream in a background thread and applies
it from Blender's timer loop. **Rebuild Replay** discards retained receive state
and reconstructs it from the configured base or live snapshot sequence.

### Playback Sync

One connected client may claim playback leadership. The leader can play,
pause, or push the current frame; followers update their Blender timelines from
the server's playback state.

## Normal layered workflow

Use this workflow when collaboration-layer order, muting, or departments must
be preserved.

1. Start a managed server on the original base scene:

   ```bash
   uv run openusdconnect-server \
     --base scene.usda \
     --event-log events.db \
     --port 7200
   ```

2. In each Blender instance, import the same original base through **Import USD
   (with prim tagging)**.
3. Start the receiver. It reconstructs server collaboration layers in a
   separate USD mirror and projects their composed result into Blender.
4. Start the emitter on instances that should author changes.

The authoring stage and receive mirror are intentionally separate. An authored
opinion stays in its logical layer, while Blender displays the value composed
from the complete authoritative layer stack.

## Live-open workflow

For a simple single-layer workstation session:

```bash
uv sync --group bundled-usd --group vfs
uv run python scripts/start_live_open.py --base scene.usda --open
```

Import the reported local `scene.usd` path with prim tagging. Embedded metadata
sets the server endpoint and `snapshot_seq`; the addon continues at
`snapshot_seq + 1` when auto-start is enabled.

Live-open continuation is flat replay because the snapshot contains composed
state rather than historical collaboration-layer identities. It therefore
requires one unmuted collaboration layer and no department policy. A server
that requires layered replay rejects the receiver without closing the imported
scene. Use the original base workflow in that case.

The [getting started guide](getting-started.md) covers the first local session. The
[Live-open and VFS guide](live-open.md) covers local mirrors,
Windows/macOS mounts, write modes, and token behavior.

## What synchronizes

The Blender integration currently handles these groups:

- Prim creation, deletion, deactivation, rename, visibility, and type changes
- Local transforms using USD translate/orient/scale, including quaternion
  rotation and stage-axis/unit conversion
- Mesh and parametric geometry attributes, normals, primvars, purpose, cameras,
  and selected light types
- References, payload load state, variants, and native scenegraph instancing
- Material bindings, UsdPreviewSurface, MaterialX shader values, NodeGraphs,
  textures, and connection changes
- Point-instancer authored state in the USD mirror; Blender records that the
  prim is a point instancer but does not map its prototypes or instances into a
  native Blender representation
- Stage units, timeline metadata, and shared playback state

Unsupported or not directly representable USD opinions remain correct in the
receive mirror even when Blender has no native equivalent. Examples include
some UsdLux types and inputs, arbitrary Sdf-only fields, and API-schema removal.

See [Live material editing](live-material-editing.md) for the shader mappings
and nested referenced-material behavior.

## Composition and native import

Blender's USD importer owns the initial realization of referenced assets,
loaded payloads, and variant replacements. OpenUSDConnect sends the root
composition operation and preserves explicit descendant edits, but does not
replay a second synthesized copy of the imported subtree over Blender's result.
This avoids placeholder geometry, duplicate material graphs, and lost
`GeomSubset` material assignments.

Subsequent explicit edits to imported descendants still synchronize normally.
Referenced Material and NodeGraph interface values are resolved through the
mirror stage and forwarded to existing Blender shader sockets without
rebuilding the imported graph.

## Materials and lights

`set_connectable_input` covers Shader, Material, NodeGraph, and UsdLux inputs.
`set_connectable_connection` carries connection and disconnection edges.
Blender maps supported shader identifiers through its shader-mapper registry;
unknown networks remain in USD.

Directly mapped lights include Distant, Sphere, Rect, and Disk lights.
DomeLight maps to the Blender World, so the most recently applied dome wins.
Other light types or inputs with no clean Blender equivalent remain mirror-only.

## Cameras and units

Camera focal length, aperture, offsets, clipping, focus distance, f-stop, and
projection are converted using the stage's `metersPerUnit`. Transform and
distance conversion also account for USD Y-up versus Blender Z-up.

The receiver authors incoming time samples to the USD mirror. Native Blender
F-curve creation for arbitrary received animation is not complete; static
camera and transform opinions map directly.

## Auto-tracking

With **Auto-track New Objects** enabled, the addon assigns an untagged edited
object a path below the configured root. Primitive type inference is
best-effort: recognizable Blender primitives map to their USD schema types,
other meshes use `Mesh`, and other objects use `Xform`.

Auto-tracking creates collaboration identity for the object. It is not a
general USD exporter and does not infer every Blender modifier, constraint, or
material graph.

## Replay and feedback behavior

The receiver retains its mirror when stopped and resumes from the next sequence
when the base, endpoint, and replay mode are unchanged. **Rebuild Replay** is
required after intentionally changing those inputs.

The server broadcasts an author's committed records to every receiver,
including the author's receive role in layered sessions. During application,
the addon suppresses its emitter and updates emitter baselines so authoritative
records do not bounce back as new edits.

Compaction sends a resync boundary. The addon clears the affected replay state
and rebuilds from the compacted authoritative log automatically.
