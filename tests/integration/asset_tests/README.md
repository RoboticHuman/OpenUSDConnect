# Blender asset integration tests

These opt-in tests launch Blender and a real OpenUSDConnect server against
assets from the USD Working Group submodule. They cover behavior that mocks and
small synthetic stages cannot establish reliably, including native USD import,
MaterialX enrichment, references and payloads, variants, texture resolution,
material identity, and bindings.

## Run

Initialize the assets and configure Blender first:

```bash
git submodule update --init --recursive
uv run python scripts/setup_blender_test.py --version 5.0.1
```

Run the tier:

```bash
uv run pytest tests/integration/asset_tests/ --asset-tests -v
```

The Blender executable can also come from `--blender`, `BLENDER_EXE`, or
`blender.test.cfg`; see [Testing](../../../docs/testing-setup.md#blender).

Scope an iteration to one wrapper test:

```bash
uv run pytest \
  tests/integration/asset_tests/test_assets.py::test_bishop_materialx \
  --asset-tests -v
```

The pytest wrapper builds the current addon, starts and stops its own server,
launches Blender with repo-local user resources, and reports the Blender-side
assertion output. Do not launch the internal Blender scripts directly unless
you are debugging the harness itself.

## Material Zoo inspection

```bash
uv run python scripts/run_material_zoo.py --show --renderman --exit-after 0
```

The runner opens the unchanged `test_scene.usda` in Blender and usdview, then
streams the committed semantic fixture through a temporary server. Use
`--viewers blender` or `--viewers usdview` for one client and
`--no-presentation` to omit the shared camera and IBL.

## Add coverage

The public pytest cases live in `test_assets.py`; Blender-side scenarios live
beside it and use `helpers.TestHarness` for setup, event submission, timed
steps, and assertions.

When adding a scenario:

1. Prefer an existing asset with a specific regression-relevant feature.
2. Assert exact USD paths, material identity, node/link counts, texture paths,
   or geometry assignments where possible. Minimum-count checks alone can miss
   duplicate graph or placeholder-object regressions.
3. Exercise edits through the real server rather than calling the adapter
   directly.
4. Leave cleanup to the pytest wrapper and `TestHarness.done()`.
5. Add the public wrapper to `test_assets.py` so the scenario is collected only
   under `--asset-tests`.

Read `helpers.py` and a neighboring scenario for the current harness API. This
avoids duplicating a method inventory here that can drift from the code.
