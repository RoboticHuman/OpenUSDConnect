# Referenced MaterialX Document

This recipe authors a reference to the included `glass.mtlx` document and binds
its material to the sphere in `test_scene.usda`. Consumers with a native
MaterialX pipeline can resolve the document directly instead of receiving an
inline USD shader graph.

From the repository root, install the standard runtime and dashboard:

```text
uv sync --group bundled-usd --group dashboard
```

Start the server in the first terminal:

```text
uv run openusdconnect-server --base test_scene.usda --event-log mtlx-example.db --export-diff mtlx-example-changes.usda --port 7200 --dashboard-port 8080
```

Wait for `Server listening on 127.0.0.1:7200`. In a second terminal, also from
the repository root, send one transaction:

```text
uv run openusdconnect-send '{"k":"ensure_prim","prim":"/World/Looks/GlassMtlx","typeName":"Material"}' '{"k":"set_reference","prim":"/World/Looks/GlassMtlx","refs":[{"asset_path":"examples/mtlx_document/glass.mtlx","prim_path":"/MaterialX/Materials/GlassMtlx"}]}' '{"k":"set_material_binding","prim":"/World/Sphere/Geom","material_path":"/World/Looks/GlassMtlx"}'
```

The command reports `Sent 3 event(s)`. Open <http://127.0.0.1:8080> to inspect
the transaction. Stop the server with `Ctrl+C`; this writes
`mtlx-example-changes.usda`. Verify the unresolved reference and binding were
exported:

```text
uv run python -c "from pxr import Sdf; l=Sdf.Layer.FindOrOpen('mtlx-example-changes.usda'); print(bool(l.GetPrimAtPath('/World/Looks/GlassMtlx')), bool(l.GetPropertyAtPath('/World/Sphere/Geom.material:binding')))"
```

Successful output is `True True`. The event database and exported layer remain
in the repository root. Use different names for an independent run.

The bundled `usd-core` runtime can author and preserve the `.mtlx` reference,
but it does not include the `usdMtlx` file-format plugin. The server may
therefore warn that it cannot compose the referenced document. This does not
reject the transaction or remove the reference. To compose and inspect the
material on the server, run with an OpenUSD installation that includes
MaterialX support as described in the
[runtime guide](../../docs/cli-reference.md#openusd-runtime-and-custom-plugins).

The USD Material prim is named `GlassMtlx` because the material inside the
document has that name. Visual output depends on the receiving application's
MaterialX file-format support, renderer, and supported nodes.
