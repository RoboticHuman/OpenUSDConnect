# Asset Integration Tests

Heavy E2E tests that run inside Blender. They verify material enrichment,
texture connections, variant switching, and material identity across real
USD assets.

**Skipped by default** in `uv run pytest`. Enable with `--asset-tests`.
Each test launches Blender as a subprocess (~60 seconds per test).

## Running via pytest (recommended)

```bash
# Build addon first
uv run python scripts/build_blender_addon.py

# Run all asset tests
uv run pytest tests/integration/asset_tests/ --asset-tests \
    --blender .blender/blender-5.0.1-windows-x64/blender.exe -v

# Run a single test
uv run pytest tests/integration/asset_tests/test_assets.py::test_bishop_materialx \
    --asset-tests --blender .blender/blender-5.0.1-windows-x64/blender.exe -v
```

The pytest wrapper handles server start/stop automatically.

Alternatively, set `BLENDER_EXE` env var or create `blender.test.cfg` to avoid
`--blender` on every invocation (see `tests/conftest.py`).

## Running manually (for debugging)

Start a server, then run Blender directly with the test script:

```bash
# Terminal 1: start server
uv run python -m openusdconnect.server --port 7202 --log test_asset.db

# Terminal 2: run test (Blender auto-quits when done)
BLENDER_USER_RESOURCES=".blender/user_data" \
  .blender/blender-5.0.1-windows-x64/blender.exe \
  --python tests/integration/asset_tests/test_bishop.py

# Clean up
taskkill /F /IM python.exe   # Windows
rm -f test_asset.db*
```

## Test Inventory

| Test | Asset | What it verifies |
|------|-------|-----------------|
| `test_bishop.py` | OpenChessSet Bishop | MaterialX multi-node network, texture loading via NodeGraph, diffuse connection chain (Mix←HueSat←Texture), dual materials (B/W), material binding, shader map seeding |
| `test_teapot_variants.py` | Teapot | Payload loading, default variant material (Ceramic), primvar-connected Base Color, variant switch Utah↔Fancy with material rebinding, **interleaved live editing with value retention across variant round-trips** |
| `test_two_teapots.py` | Teapot ×2 | Path-based material identity (two Ceramics with different `usd_material_path`), node tree integrity, parent-context object naming |
| `test_vehicles.py` | Vehicles 4WD | 6 material bindings across mesh parts, external material file references |

## Adding a New Test

1. Create `test_<name>.py` in this folder
2. Import the shared harness:
   ```python
   import sys, os
   sys.path.insert(0, os.path.dirname(__file__))
   from helpers import TestHarness, PROJECT_ROOT
   ```
3. Use the harness API:
   ```python
   harness = TestHarness("LABEL")

   _step = 0
   def _run():
       global _step
       if _step == 0:
           harness.setup()            # install addon, connect
           _step = 1
           return 2.0
       elif _step == 1:
           harness.send_reference(     # or send_payload
               "/World/Asset", ASSET_PATH, "/AssetRoot")
           _step = 2
           return 8.0                  # wait for import
       elif _step == 2:
           # Assertions
           harness.check_material("MatName", path_contains="Asset",
                                  min_nodes=3, base_color_linked=True)
           harness.check_texture("MatName", "diffuse", loaded=True)
           harness.check_binding("MeshObj", "MatName")
           harness.check_shader_maps_seeded("Asset")
           harness.done()
           return None

   import bpy
   bpy.app.timers.register(_run, first_interval=2.0)
   ```

## Harness API

| Method | Description |
|--------|-------------|
| `setup()` | Install addon, connect emitter + receiver |
| `send_reference(prim, asset, ref)` | Send ensure_prim + set_reference |
| `send_payload(prim, asset, ref)` | Send ensure_prim + set_payload + load_payload |
| `send_variant(prim, selections)` | Send set_variant_selections |
| `check_material(name, ...)` | Assert material exists with properties |
| `check_texture(mat, node, loaded)` | Assert texture node has image loaded |
| `check_connection(mat, input, linked)` | Assert BSDF input connection state |
| `check_binding(obj, mat)` | Assert object has material assigned |
| `check_shader_maps_seeded(path)` | Assert reverse-path maps populated |
| `done()` | Print results and quit Blender |
