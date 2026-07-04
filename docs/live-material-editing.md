# Live Material Editing

Real-time bidirectional material and shader sync between DCC applications through the OpenUSDConnect protocol.

Three event kinds carry all material state:

- `set_material_binding` binds or unbinds a `UsdShade.Material` to a prim. An optional `material_purpose` (`"preview"` or `"full"`) selects the per-purpose binding slot; empty means allPurpose.
- `set_connectable_input` sets typed input values on any `UsdShade.ConnectableAPI` container: Shader, NodeGraph, Material, or UsdLux light. `info_id` carries the shader's `info:id` (its Sdr identifier) and is empty for non-Shader containers.
- `set_connectable_connection` authors or clears connection edges (`connections` plus optional `disconnections`), covering both input-to-output edges and the input-to-input edges NodeGraph interface forwarding produces.

The protocol layer is shader-agnostic: any `info:id` and any input set replicate. What a given DCC can *display* is decided by its shader mapper registry (see the Blender section below).

## Architecture

Material edits flow through the emitter's USD stage, the bridge between DCC node graphs and the network protocol:

```
Blender node edit
  -> depsgraph reports a Material update
  -> BlenderStageAuthor reads the mapped node sockets via the shader
     mapper and diffs against its per-shader baseline
  -> changed values are written as UsdShade inputs on the emitter's stage
  -> NoticeEmitter picks up the stage change and diffs against its
     per-prim cache
  -> set_connectable_input event -> server -> broadcast to other clients
  -> BlenderAdapter routes the values through the shader mapper registry
     onto Blender node sockets
```

The same `ShaderMapperRegistry` serves both directions: the mapper that applies incoming values to native nodes also reads them back for outgoing events, so values stay in USD input space throughout the roundtrip.

### Two diff layers on emit

`BlenderStageAuthor` keeps a per-shader baseline of `{usd_input_name: value}`. On each material depsgraph tick it:

1. reads every mapped socket from the node (for multi-node networks, from the socket map cached when the network was built)
2. compares against the baseline and keeps only actual changes
3. authors the changed values to the emitter's USD stage
4. the `NoticeEmitter` then diffs that stage change against its own per-prim cache before building events

First encounter seeds the baseline without authoring, so importing a material does not broadcast its defaults. The receive pipeline also seeds baselines right after applying shader events, reading the freshly applied node state, so the first local edit to a synced shader diffs against the synced values instead of being swallowed as a seed.

On the stage side, the emitter reads the whole connectable interface once per cycle (`read_usdshade_connectable`) and fans the diff out into `set_connectable_input` and `set_connectable_connection` events. An input that carries a connection is excluded from value events, since its value comes from the source node.

### Echo suppression

The server does not echo events back to the origin that sent them, and the receive pipeline suppresses the emitter while applying incoming events, so an edit never bounces back to its author.

## Blender shader support

The Blender integration's default registry maps these USD shader ids to Blender nodes.

### UsdPreviewSurface (single node)

Maps to Blender's Principled BSDF: one node, direct socket mapping. The MaterialX wrapper id `ND_UsdPreviewSurface_surfaceshader` uses the same mapping.

| USD input | Blender socket |
|-----------|----------------|
| `diffuseColor` | Base Color |
| `metallic` | Metallic |
| `roughness` | Roughness |
| `emissiveColor` | Emission Color |
| `clearcoat` | Coat Weight |
| `clearcoatRoughness` | Coat Roughness |
| `opacity` | Alpha |
| `ior` | IOR |
| `specularColor` | Specular Tint |

USD's `emissiveColor` is the final emission value with no separate strength, while Blender defaults Emission Strength to 0. When a non-zero emissive color arrives, Emission Strength is set to 1.0.

Bidirectional: edits to any of these sockets in Blender are emitted as `set_connectable_input`; incoming events apply to the Principled BSDF.

### MaterialX standard_surface (multi-node)

`ND_standard_surface_surfaceshader` builds a 5-node network: a Principled BSDF plus two HueSat -> Mix chains that combine `base`/`base_color` into Base Color and `specular`/`specular_color` into Specular Tint. Every other standard_surface input (roughness, metalness, transmission, subsurface, sheen, coat, thin film, emission, anisotropy, normal, tangent) maps directly onto a Principled socket.

When `transmission` is above zero the network is rewired for glass: `specular_IOR` drives the IOR socket and `transmission_color` drives Base Color, bypassing the diffuse base chain (Blender's Principled BSDF tints transmitted light with Base Color and has a single IOR for reflection and refraction).

Bidirectional: `create_network` returns an `input_map` of USD input name to Blender socket. The same map applies incoming values and reads them back for reverse sync.

### OpenPBR surface (multi-node)

`ND_open_pbr_surface_surfaceshader` translates to standard_surface per the official MaterialX translation graph, then builds the standard_surface network. Most inputs are passthrough renames (`base_weight` to `base`, `fuzz_weight` to `sheen`, and so on). The computed channels become real Blender node subgraphs only when active, so a connected texture stays correct per-pixel: coat darkening of the base color, the coat/specular roughness mix, and the fuzz roughness power curve.

### MaterialX utility nodes

Math (`ND_multiply_*`, `ND_divide_vector2`, `ND_subtract_vector2`, `ND_distance_vector3`), mix and multiply (`ND_mix_color3`, `ND_mix_vector3`, `ND_multiply_color3`), conversions (`ND_convert_*`), `ND_ifequal_*`, `ND_extract_color4`, `ND_texcoord_vector2`, `ND_surfacematerial`, and `ND_normalmap_*` each map to one or two configured Blender nodes with fixed port maps. `set_connectable_connection` routes edges through those maps, so multi-node MaterialX graphs (texture chains, channel math) rebuild as native Blender node networks.

### Textures and primvar readers

`UsdUVTexture` and the MaterialX image nodes (`ND_image_*`, `ND_tiledimage_*`) map to Image Texture nodes. Color-typed variants load as sRGB; float and vector variants load as Non-Color, so normal maps and roughness data skip gamma correction. Relative file paths resolve against the scene's asset root (or the imported base USD's directory), and image datablocks already loaded from the same absolute path are reused rather than duplicated.

Texture sync is bidirectional on tracked nodes: swapping the image on a framework-created Image Texture node emits the new file path as an asset-typed `file` input, which other receivers load through the same resolution path. Changing the UV map or attribute name on a tracked reader node emits `varname` the same way.

`UsdPrimvarReader_float2` maps to a UV Map node; the other `UsdPrimvarReader_*` variants map to Attribute nodes that read the named primvar (vertex colors, custom data) at render time.

### Material binding in Blender

Incoming `set_material_binding` events assign path-stable Blender materials: two references to the same asset get separate materials, each tagged with its composed USD path. Per-purpose bindings resolve like `ComputeBoundMaterial(purpose="preview")`: preview overrides allPurpose, and full is the last-resort fallback. A binding on an Xform propagates to descendant meshes that have no binding of their own, mirroring USD binding inheritance.

## Example workflows

### 1. Load an asset and edit its material live

Set up the server and two Blender instances per the walkthrough in [Blender Addon Usage](blender-addon-usage.md), then reference an asset from the command line:

```bash
# Terminal: start the server on the repo's test scene
uv run python -m openusdconnect.server --port 7200 --base test_scene.usda --log events.db

# Terminal: create a Teapot with a payload, one atomic transaction
uv run python -m openusdconnect.send \
  '{"k":"ensure_prim","prim":"/World/Teapot","typeName":"Xform"}' \
  '{"k":"ensure_xform_ops","prim":"/World/Teapot"}' \
  '{"k":"set_xform_trs","prim":"/World/Teapot","fields":["t"],"t":[0,3,0]}' \
  '{"k":"set_payload","prim":"/World/Teapot","payloads":[{"asset_path":"./assets/full_assets/Teapot/Teapot.usd","prim_path":"/Teapot"}]}' \
  '{"k":"load_payload","prim":"/World/Teapot"}'
```

Connected Blender receivers import the Teapot with its Ceramic material. In the emitter Blender, change Roughness on the Ceramic Principled BSDF. The wire carries one event:

```json
{"k": "set_connectable_input",
 "prim": "/World/Teapot/Materials/Ceramic/UsdPreview/usdpreviewsurface",
 "info_id": "UsdPreviewSurface",
 "inputs": {"roughness": 0.8},
 "input_types": {"roughness": "float"}}
```

and every other receiver updates the Ceramic material.

### 2. CLI shader override

```bash
uv run python -m openusdconnect.send \
  '{"k":"set_connectable_input","prim":"/World/Teapot/Materials/Ceramic/UsdPreview/usdpreviewsurface","info_id":"UsdPreviewSurface","inputs":{"roughness":0.1,"ior":2.0},"input_types":{"roughness":"float","ior":"float"}}'
```

All connected receivers update the Ceramic material's roughness and IOR.

The MCP server exposes these same events as validated LLM authoring tools, including full shader-network recipes; see [MCP Server Usage](mcp-server-usage.md).

## Adding material sync to a new DCC

`ShaderMapper`, `MultiNodeShaderMapper`, and `ShaderMapperRegistry` live in `openusdconnect.adapters` and are DCC-agnostic (the `node` parameter is untyped; each mapper knows its own node object type).

### 1. Implement the DCCAdapter methods

```python
class MyDCCAdapter(DCCAdapter):
    def set_material_binding(self, prim_path, material_path, material_purpose=""):
        # Assign the material to geometry in your DCC
        ...

    def set_connectable_input(self, prim_path, info_id, inputs, input_types, time=None):
        # info_id is empty for NodeGraph / Material / light containers
        mapper = self._shader_registry.get(info_id)
        ...

    def set_connectable_connection(self, prim_path, connections, disconnections=None):
        # Wire shader nodes together in your DCC
        ...
```

### 2. Register ShaderMapper subclasses

```python
class MyPBRMapper(ShaderMapper):
    def apply_value(self, node, usd_name, value, **kwargs):
        native_name = self.get_native_input(usd_name)
        # Set the value on your DCC's shader node

    def read_all_inputs(self, node):
        # Read every mapped value back for reverse sync
        return {usd_name: read_native(node, usd_name)
                for usd_name in self._input_map}

registry = ShaderMapperRegistry()
registry.register(MyPBRMapper("UsdPreviewSurface", "my_pbr_node", {
    "diffuseColor": "base_color",
    "roughness": "roughness",
}))
```

### 3. Multi-node shaders

```python
class MyMtlxMapper(MultiNodeShaderMapper):
    def create_network(self, tree, inputs, **kwargs):
        # Build your DCC's node network.
        # Return (nodes, input_map, output_map).
        ...
```

The `input_map` (USD input name -> native socket) is the key to bidirectional editing: forward application writes values through it, and `read_all_inputs(input_map=...)` reads them back through it. The `output_map` lets `set_connectable_connection` resolve this shader's output sockets when another node connects to it.

### 4. The core handles the rest

- event encoding, transport, and sequencing (FlatBuffers over TCP through the sync server)
- change detection on the emitter's stage (`NoticeEmitter` diffs the connectable interface per prim)
- receive-side dispatch (`EventDispatcher` decodes, commits to the mirror stage, and calls your adapter)
- echo suppression, event storage, replay, and compaction

## Behavior notes and limitations

- Reverse authoring preserves USD types: the type authored on the synced prim wins, then the shader's Sdr node definition, then a value-shape heuristic for inputs the stage has never seen. A texture swap round-trips as `asset`, a subsurface radius as `float3`, exactly as the source authored them.
- Reverse sync reads unlinked value sockets only. An input driven by a connection is skipped when reading back, so a value edit never fights a texture link.
- Multi-node reverse sync requires the socket map produced by `create_network`, which exists once the network has been built by the receive/import path. A multi-node material authored from scratch inside Blender has no map and does not emit.
- On the emit side, Blender nodes are matched to USD shader prims through the `usd_shader_path` / `usd_shader_id` tags set during import and receive. An untagged Principled BSDF inside a tagged material falls back to matching a UsdPreviewSurface shader found under the material prim on the stage; other untagged nodes are not tracked.
- Connections targeting Material or NodeGraph *output* ports are skipped by the Blender adapter: the Material surface terminal is wired automatically when a surface-shader network is built, and NodeGraph ports are flattened at import.
