# Layered material zoo

`scene.usda` composes a stronger `shot.usda` over `lookdev.usda`. The lookdev
layer defines four bound materials:

- UsdPreviewSurface matte and metallic materials
- A textured UsdPreviewSurface network
- A MaterialX standard surface

The shot layer overrides only three shader inputs. It intentionally contains no
copies of the weaker material definitions or texture graph.

Open the composed fixture directly with:

```bash
usdview tests/fixtures/layered_material_zoo/scene.usda
```

`test_layered_material_zoo_preserves_graphs_and_overrides` in
`tests/integration/test_logical_layer_replay.py` sends the two authored layers
through a live OpenUSDConnect server. It verifies receiver-local layer
ownership, composed values, shader connections, material bindings, uncommon
ports, property metadata, and mute state.

The integration test substitutes an absolute path for the texture before
transmission. Resolver-anchor mapping for relative asset paths replicated into
anonymous receiver layers remains separate future work.
