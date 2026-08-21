# Live material editing

Material events represent `UsdShade.ConnectableAPI` values, connections, and
bindings. The protocol does not encode renderer-specific or DCC-specific node
types.

## Protocol model

Three event kinds carry material state:

- `set_material_binding` authors or clears a material binding. An optional
  `material_purpose` selects all-purpose, preview, or full bindings.
- `set_connectable_input` authors typed values on a Shader, NodeGraph,
  Material, or UsdLux connectable. `info_id` is the Shader's Sdr identifier and
  is empty for non-Shader containers.
- `set_connectable_connection` authors or clears input and output connection
  edges, including interface forwarding through Material and NodeGraph inputs.

The core accepts valid shader identifiers and typed inputs. A DCC adapter maps
the networks it understands; all other authored state remains in its USD
mirror.

## Data flow

For Blender, a local node edit follows this path:

```text
Blender node edit
  -> BlenderStageAuthor writes the changed USD input
  -> NoticeEmitter builds typed events from the edit-target opinion
  -> server validates, commits, and broadcasts the transaction
  -> receiving mirror composes the authoritative layer stack
  -> BlenderAdapter updates existing native nodes and sockets
```

The server sends committed records to every receiver, including a layered
receiver associated with the author. Receive application suppresses local
capture and advances its baselines, so authoritative records are not emitted a
second time. Same-origin composed correction is still applied when a stronger
layer masks the author's opinion.

`BlenderStageAuthor` also keeps a native-node baseline so an import does not
broadcast shader defaults as user edits. The stage-side emitter performs a
second authored-opinion diff before creating protocol events.

## Blender shader support

The default Blender registry includes:

- `UsdPreviewSurface` and `ND_UsdPreviewSurface_surfaceshader`, mapped to
  Principled BSDF
- MaterialX `standard_surface`, translated to a Principled-based network
- OpenPBR surface, translated through the MaterialX standard-surface mapping
- Common MaterialX math, mix, conversion, conditional, extract, texcoord,
  image, tiled-image, and normal-map nodes
- `UsdUVTexture` and `UsdPrimvarReader_*`

UsdPreviewSurface covers diffuse/base color, metallic, roughness, emission,
coat, opacity, IOR, and specular tint. Blender has no separate strength for
USD's final emissive color, so a non-zero incoming color enables emission
strength.

MaterialX standard-surface inputs that do not have a direct Principled socket
are represented with supporting nodes. Transmission uses Blender's available
base-color and IOR controls. OpenPBR inputs are translated to the corresponding
standard-surface behavior, including computed coat and fuzz channels when
needed.

These mappings approximate the source graph in Blender; they do not promise
renderer-identical output. The USD mirror remains authoritative.

## Referenced and nested interfaces

Referenced MaterialX frequently exposes values on a Material or NodeGraph
interface rather than directly on the final Shader. Initial import enrichment
uses public OpenUSD value-producer queries to flatten those interface values
and shader-output connections onto Blender's existing imported graph.

For live edits, the adapter asks the mirror's composed connectable for its
recursive interface consumers and updates the corresponding existing shader
sockets. This supports:

- Material interface values driving a Shader
- NodeGraph inputs forwarding to nested NodeGraphs or Shaders
- externally referenced materials containing internal references
- connection rewiring and disconnection through those interfaces

The adapter does not build a persistent manual composition cache and does not
recreate the material graph for an interface value change. It relies on the
current composed USD stage and updates the nodes already owned by Blender's
importer.

## Textures and primvars

`UsdUVTexture` and MaterialX image nodes map to Blender Image Texture nodes.
Color variants load as sRGB; scalar and vector data load as Non-Color. Relative
asset paths resolve against the owning USD document or configured asset root,
and already-loaded images with the same absolute path are reused.

Swapping an image on a framework-created texture node publishes the new asset
path. Changing a tracked UV map or primvar reader publishes its `varname`.
Inputs driven by a connection are not simultaneously emitted as scalar values.

Packed texture channel networks are preserved by native import ownership. The
adapter does not replace an imported channel-separation graph with a simplified
reconstruction.

## Material binding behavior

Blender materials are identified by their composed USD paths, not only by leaf
names. Two references to the same asset therefore receive distinct native
materials when their composed paths differ.

Per-purpose resolution follows USD binding intent: preview overrides
all-purpose for preview consumers, and full is the fallback where applicable.
A binding on an Xform is inherited by descendant meshes unless a descendant
authors a stronger binding. Native import preserves `GeomSubset` assignments
and polygon material indices.

## Send a material transaction

Use one transaction for the material, terminal connection, and binding:

```bash
uv run openusdconnect-send '{"k":"ensure_prim","prim":"/World/Looks/Brass","typeName":"Material"}' '{"k":"ensure_prim","prim":"/World/Looks/Brass/Surface","typeName":"Shader"}' '{"k":"set_connectable_input","prim":"/World/Looks/Brass/Surface","info_id":"UsdPreviewSurface","inputs":{"diffuseColor":[0.71,0.65,0.26],"roughness":0.3,"metallic":1.0},"input_types":{"diffuseColor":"color3f","roughness":"float","metallic":"float"}}' '{"k":"set_connectable_connection","prim":"/World/Looks/Brass","connections":{"outputs:surface":{"source_prim":"/World/Looks/Brass/Surface","source_attr":"outputs:surface"}}}' '{"k":"set_material_binding","prim":"/World/Model","material_path":"/World/Looks/Brass"}'
```

The CLI validates the complete event batch and sends it atomically. The MCP
server exposes the same operation through `usd_send_events` and provides Sdr
shader discovery; see [MCP server](mcp-server-usage.md).

## Add material support to another DCC

A DCC adapter implements the material methods from `DCCAdapter`:

```python
class MyAdapter(DCCAdapter):
    def set_material_binding(
        self,
        prim_path,
        material_path,
        material_purpose="",
    ):
        ...

    def set_connectable_input(
        self,
        prim_path,
        info_id,
        inputs,
        input_types,
        time=None,
    ):
        ...

    def set_connectable_connection(
        self,
        prim_path,
        connections,
        disconnections=None,
    ):
        ...
```

`ShaderMapper`, `MultiNodeShaderMapper`, and `ShaderMapperRegistry` in
`openusdconnect.adapters` are optional DCC-independent helpers. A
`ShaderMapper` translates one USD shader ID to a native node type and input
map. A `MultiNodeShaderMapper` returns `(nodes, input_map, output_map)` from
`create_network()` so the adapter can apply values, read them back, and resolve
connection endpoints consistently.

The core handles event validation, serialization, sequencing, replay,
authored-opinion detection, and receive dispatch. The integration remains
responsible for native node creation, socket mapping, asset loading, and
host-thread scheduling.

## Integration constraints

- Preserve the authored `input_types`. When reverse-authoring a previously
  unknown input, prefer the existing USD input type, then the Sdr definition,
  and only then a value-shape fallback.
- Do not emit a socket value while that input is connection-driven.
- Reverse sync for a multi-node native network requires the `input_map` created
  during import or receive. An unrelated graph authored from scratch in the DCC
  has no USD identity or map unless the integration creates one.
- Match shaders and materials by stable USD paths or explicit tags, not leaf
  node names.
- Material or NodeGraph output events may be represented by composed interface
  forwarding rather than a one-to-one native node. Query the USD mirror for
  producers and consumers instead of duplicating composition rules.
- Time-sampled connectable values remain time samples in USD. A DCC adapter
  needs an explicit animation policy to turn them into native keys.
