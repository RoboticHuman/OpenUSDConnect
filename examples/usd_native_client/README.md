# USD-native client demo

This example runs two independent USD-native clients:

- `demo.py` publishes a moving sphere while receiving authoritative layered
  replay into a separate mirror stage.
- `peer.py` pre-authors a cube in another stage and publishes its current edit
  target from a separate process.

The first client receives both its own authoritative sphere and the other
client's cube. No application code authors protocol event dictionaries.

Run the server, both clients, and usdview with one command:

```bash
uv run python examples/usd_native_client/run.py
```

Use `--no-usdview --seconds 3` for a short headless run. Successful output
reports `local_valid=True` and `peer_valid=True`.

The important ownership rule is visible in `demo.py`: its publisher observes
the author stage's session layer, while `UsdReceiver` owns different session
layers on the mirror stage. All three stages share the same read-only base
layer.
See the [USD-native integration contract](../../docs/usd-native-integration.md)
for the corresponding host integration rules.
