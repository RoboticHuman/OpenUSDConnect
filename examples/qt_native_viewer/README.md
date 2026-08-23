# Qt native-scene integration

This example receives layered USD changes into an application-owned scene.
`UsdReceiver` maintains a USD stage for composition and sends composed changes
to `NativeSceneAdapter`. The Qt viewport renders the adapter-owned scene and
never reads the USD stage.

`NativeSceneAdapter` implements `DCCAdapter` by updating a small Python scene
graph. Its methods show where a host would create objects, apply transforms and
geometry state, update materials, or remove objects. The renderer uses the
object lifecycle, transform, and display-color state needed by this demo.

Run the complete example from the repository root:

```text
uv run --group bundled-usd --group qt-example python examples/qt_native_viewer/run.py
```

## Network flow

The launcher starts a real OpenUSDConnect server with a temporary event log and
connects three TCP clients: one viewer and two authors for the `animation` and
`layout` departments. The server orders and persists their events, and the
viewer receives the same layered replay used by other managed clients. There
is no in-process shortcut between the authors and the adapter.

`animation` is stronger than `layout`. After the connections are ready, the
launcher runs four authored changes:

1. Layout creates a blue cube at `x = -4`.
2. Animation overrides it with an orange cube at `x = +4`.
3. Layout changes its weaker opinion to `x = -1`. The canvas does not move and
   no transform reaches the adapter because the composed value remains `+4`.
4. Animation removes its prim spec. The layout value is revealed and projection
   moves the native cube to `x = -1`.

Use **Run masking demo** to repeat the sequence. Close the window to stop the
temporary server and remove its event log.

Drag with the left mouse button to orbit, drag with the right or middle button
to pan, and use the wheel to zoom.

For an unattended smoke run, use `--exit-after 6`. The last line should report
`final native translation=[-1.0, 0.0, 0.0]`.

## Integration boundary

The receive-side integration is in `viewer.py`. `demo_driver.py` only authors
the exact layer opinions used by the masking sequence.

Connect the viewer to an existing server whose department priority includes
`animation,layout`:

```text
uv run --group bundled-usd --group qt-example python examples/qt_native_viewer/viewer.py --port 7340
```

The reusable wiring in `MainWindow.__init__` is:

```python
mirror_stage = Usd.Stage.CreateInMemory()
adapter = NativeSceneAdapter(native_scene)
client = UsdReceiver(
    mirror_stage,
    app_name="my-native-host",
    adapter=adapter,
    on_resync=adapter.reset,
)
client.start()
```

Call `client.update()` from the thread that owns the host scene. Socket reads
remain on the receiver's background thread; replay, USD composition, and
projection happen when `update()` runs. `NativeSceneAdapter` requests one UI
refresh after each applied batch rather than redrawing for every event.

This example covers the receive path. A bidirectional host must also observe
changes to its native document and publish equivalent USD edits. Use
`UsdPublisher` when the integration maintains an authoring stage; otherwise,
use the lower-level sending API to publish translated native events.
