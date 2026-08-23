# Examples

These examples use the public client APIs and the same protocol as the DCC
integrations. They require Git, Python 3.13+, and `uv`, but not Blender or
Unreal Engine.

## Runtime setup

Configure the project OpenUSD runtime as described in the
[runtime guide](../docs/cli-reference.md#openusd-runtime-and-custom-plugins),
then install the base environment from the repository root:

```text
uv sync
```

Use `uv sync --group bundled-usd` only when the examples need neither MaterialX
nor custom renderer, resolver, file-format, or shader plugins.

Start with the bounded headless test:

```text
uv run python examples/usd_native_client/run.py --no-usdview --seconds 3
```

Success is reported as `local_valid=True` and `peer_valid=True`. The launcher
stops its temporary server and clients and removes its temporary event log.

## One-command demos

| Example | What it demonstrates | Launcher |
| --- | --- | --- |
| [USD-native clients](usd_native_client/README.md) | Bounded headless publish-and-receive test; usdview is optional | [run.py](usd_native_client/run.py) |
| [Qt native-scene integration](qt_native_viewer/README.md) | Receive composed changes through `DCCAdapter` and render an application-owned scene | [run.py](qt_native_viewer/run.py) |
| [Fourier waves](fourier_waves/README.md#quickstart) | A compute client expands a procedural into live mesh geometry; runs until `Ctrl+C` | [run.py](fourier_waves/run.py) |
| [Instancing dance](instancing_dance/README.md#quickstart) | Scenegraph instances, references, and animated transforms; runs until `Ctrl+C` | [run.py](instancing_dance/run.py) |

Each launcher owns its server and child processes, uses a temporary event log,
and cleans up on normal exit or `Ctrl+C`. Pass `--no-usdview` if usdview is not
installed.

The Instancing Dance default Pyramid comes from the `assets` submodule. Before
running it, use `git submodule update --init --recursive`.

## Multi-process demo

[Shared-stage client](shared_stage_client/README.md) runs two processes against
equivalent file-backed layer graphs. Start the shared-stage server separately,
then run observer and author roles with
[demo.py](shared_stage_client/demo.py).

## Authoring recipe

[Referenced MaterialX document](mtlx_document/README.md) is a CLI recipe rather
than a standalone launcher. It authors a reference to the included
[glass.mtlx](mtlx_document/glass.mtlx) document and binds that material to a scene
prim for consumers with a native MaterialX pipeline.
