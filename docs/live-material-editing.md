# Live Material Editing

Real-time bidirectional material sync between DCC applications through the OpenUSDConnect protocol.

## Architecture

Material edits flow through the emitter's USD stage — the bridge between DCC node graphs and the network protocol:

```
Blender node edit
  → depsgraph detects Material update
  → BlenderStageAuthor.on_material_update reads socket values
  → _author_shader_inputs writes diff to emitter's USD stage
  → NoticeEmitter detects stage change
  → _build_dirty_prim_events diffs against cache
  → set_shader_input event sent to server
  → server broadcasts to other receivers
  → BlenderAdapter.set_shader_input applies via ShaderMapperRegistry
```

The emitter and receiver use the same `ShaderMapperRegistry` — the mapper that applies incoming values also reads them back for outgoing events.

### Baseline Diffing

The `BlenderStageAuthor` tracks `_last_shader_values` per shader prim — a dict of `{usd_name: value}` representing the last-known state. On each material depsgraph tick:

1. `read_all_inputs` reads ALL mapped sockets from the node (or multi-node network)
2. Values are compared against the baseline
3. Only changed values are authored to the USD stage
4. The `NoticeEmitter` provides a second diff layer against its own cache

First encounter seeds the baseline without authoring — prevents Blender defaults from being broadcast.

## Supported Shader Types

### UsdPreviewSurface (single-node)

Maps to Blender's Principled BSDF. One node, direct socket mapping.

| USD Input | Blender Socket |
|-----------|---------------|
| `diffuseColor` | Base Color |
| `metallic` | Metallic |
| `roughness` | Roughness |
| `emissiveColor` | Emission Color |
| `clearcoat` | Coat Weight |
| `clearcoatRoughness` | Coat Roughness |
| `opacity` | Alpha |
| `ior` | IOR |
| `specularColor` | Specular Tint |

**Bidirectional**: Change any of these in Blender → emitted as `set_shader_input`. Receive `set_shader_input` → applied to Principled BSDF.

### MaterialX Standard Surface (multi-node)

Maps to a 5-node Blender network: Principled BSDF + 2x HueSat + 2x Mix nodes.

| USD Input | Blender Target |
|-----------|---------------|
| `base_color` | Mix_Base.B input |
| `base` | HueSat_Base.Value |
| `metalness` | BSDF.Metallic |
| `specular_roughness` | BSDF.Roughness |
| `coat` | BSDF.Coat Weight |
| `coat_roughness` | BSDF.Coat Roughness |
| `emission_color` | BSDF.Emission Color |
| ... | (28 inputs total) |

**Bidirectional**: The `input_map` from `create_network` maps every USD input to its exact Blender socket. The same map is used for both applying values (forward) and reading them back (reverse). Values stay in MaterialX input space throughout the roundtrip.

### MaterialX via ActivisionMtlxMapper

Delegates to the vendored `io_blender_mtlx` library for shader-specific node creation. Supports OpenPBR, Standard Surface, and 23+ MaterialX utility node types. Same `input_map` mechanism enables bidirectional editing.

### Texture Connections (forward only)

`UsdUVTexture` and `UsdPrimvarReader` nodes are created and connected via `set_shader_connection` events. Texture file loading (`_fix_missing_textures`) resolves relative paths from the USD stage. Reverse sync for texture changes (swapping images) is not yet implemented.

## Example Workflows

### 1. UsdPreviewSurface roughness sync

```bash
# Terminal: start server
uv run python -m openusdconnect.server --port 7200 --base test_scene.usda --log events.db

# Terminal: create a Teapot with payload
uv run python -c "
  import socket
  from openusdconnect.protocol import make_hello
  from openusdconnect.transport import send_line
  s = socket.create_connection(('127.0.0.1', 7200), timeout=5)
  send_line(s, make_hello('emitter', client_id='cli'))
  send_line(s, {'type': 'txn', 'client_id': 'cli', 'events': [
    {'k': 'ensure_prim', 'prim': '/World/Teapot', 'typeName': 'Xform'},
    {'k': 'ensure_xform_ops', 'prim': '/World/Teapot'},
    {'k': 'set_xform_trs', 'prim': '/World/Teapot', 'fields': ['t','r','s'],
     't': [0,3,0], 'r': [1,0,0,0], 's': [1,1,1]},
    {'k': 'set_payload', 'prim': '/World/Teapot',
     'payloads': [{'asset_path': './assets/full_assets/Teapot/Teapot.usd',
                   'prim_path': '/Teapot'}]},
    {'k': 'load_payload', 'prim': '/World/Teapot'},
  ]})
  s.close()
"
```

In Blender A (emitter + receiver running): the Teapot imports with the Ceramic material. Change `Roughness` on the Principled BSDF. The server log shows:

```json
{"k": "set_shader_input", "prim": "/World/Teapot/Materials/Ceramic/UsdPreview/usdpreviewsurface",
 "shader_id": "UsdPreviewSurface", "inputs": {"roughness": 0.8}}
```

Blender B (receiver): the Ceramic material's roughness updates to 0.8.

### 2. CLI shader override

```bash
# Override roughness on a running scene
uv run python -c "
  import socket
  from openusdconnect.protocol import make_hello
  from openusdconnect.transport import send_line
  s = socket.create_connection(('127.0.0.1', 7200), timeout=5)
  send_line(s, make_hello('emitter', client_id='cli'))
  send_line(s, {'type': 'txn', 'client_id': 'cli', 'events': [
    {'k': 'set_shader_input',
     'prim': '/World/Teapot/Materials/Ceramic/UsdPreview/usdpreviewsurface',
     'shader_id': 'UsdPreviewSurface',
     'inputs': {'roughness': 0.1, 'ior': 2.0},
     'input_types': {'roughness': 'float', 'ior': 'float'}},
  ]})
  s.close()
"
```

All connected Blender receivers update the Ceramic material's roughness to 0.1 and IOR to 2.0.

## Contributor Guide: Adding Material Sync to a New DCC

### 1. Implement `DCCAdapter` methods

```python
class MyDCCAdapter(DCCAdapter):
    def set_material_binding(self, prim_path, material_path):
        # Assign material to geometry in your DCC
        ...

    def set_shader_input(self, prim_path, shader_id, inputs, input_types):
        # Apply shader values to your DCC's shader nodes
        mapper = self._shader_registry.get(shader_id)
        mapper.apply_value(node, usd_name, value)
        ...

    def set_shader_connection(self, prim_path, connections, disconnections=None):
        # Wire shader nodes together in your DCC
        ...
```

### 2. Register `ShaderMapper` subclasses

```python
class MyPBRMapper(ShaderMapper):
    def apply_value(self, node, usd_name, value, **kwargs):
        native_name = self.get_native_input(usd_name)
        # Set the value on your DCC's shader node

    def read_all_inputs(self, node):
        # Read all mapped values from the node for reverse sync
        result = {}
        for usd_name in self._input_map:
            result[usd_name] = read_from_native_node(node, usd_name)
        return result

registry = ShaderMapperRegistry()
registry.register(MyPBRMapper("UsdPreviewSurface", "my_pbr_node", {
    "diffuseColor": "base_color",
    "roughness": "roughness",
    ...
}))
```

### 3. For multi-node shaders

```python
class MyMtlxMapper(MultiNodeShaderMapper):
    def create_network(self, tree, inputs, **kwargs):
        # Build your DCC's node network
        # Return (nodes, input_map, output_map)
        # input_map: {usd_name: socket_object} — enables bidirectional sync
        ...
```

The `input_map` is the key to bidirectional editing. The same map is used by:
- Forward: `_apply_multi_node_shader` writes to sockets
- Reverse: `MultiNodeShaderMapper.read_all_inputs(input_map=...)` reads from sockets

### 4. The core handles the rest

`openusdconnect/` manages:
- Protocol events (`set_shader_input`, `set_shader_connection`, `set_material_binding`)
- Diff detection (`NoticeEmitter` compares against cache)
- Network transport (TCP JSON Lines)
- Event storage and compaction (SQLite)
- Origin-based echo suppression

## Current Limitations

- **Texture connections**: forward only (receive). Swapping an image file in Blender's Image Texture node is not emitted.
- **Locally-created multi-node materials**: reverse sync requires the `input_map` from `create_network`, which is populated during import. Materials created from scratch in Blender (not from a USD import) won't have this map.
- **MaterialX reverse for ActivisionMtlxMapper**: depends on the vendored `io_blender_mtlx` handler being idempotent for re-creating networks on existing node trees.
