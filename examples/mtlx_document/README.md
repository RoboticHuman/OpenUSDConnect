# Referenced MaterialX document

Authors a material from an external `.mtlx` document over the sync protocol,
instead of wiring an inline USD shader network. Consumers that have a native
MaterialX pipeline (Unreal's Interchange, usdMtlx-aware Hydra renderers) take
the document path directly, which covers lobes their USD-prim parsers may not
(Unreal renders `glass.mtlx`'s transmission through Interchange, while the
same values authored as inline prims draw opaque).

The USD Material prim must be named exactly like the material inside the
document (`GlassMtlx` here) Unreal maps the generated material back to the
prim by name.

```bash
uv run python -m openusdconnect.send \
  '{"k":"ensure_prim","prim":"/World/Looks/GlassMtlx","typeName":""}' \
  '{"k":"set_reference","prim":"/World/Looks/GlassMtlx","refs":[{"asset_path":"<repo>/examples/mtlx_document/glass.mtlx","prim_path":"/MaterialX/Materials/GlassMtlx"}]}' \
  '{"k":"set_material_binding","prim":"/World/MyPrim","material_path":"/World/Looks/GlassMtlx"}'
```
