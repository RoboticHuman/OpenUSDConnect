# Examples

These examples exercise the same public clients and event protocol used by the
integrations. They require Git, Python 3.13+, and `uv`; Blender and Unreal Engine
are not required. From the repository root, install the standard OpenUSD runtime:

```text
uv sync --group bundled-usd
```

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
| [Fourier waves](fourier_waves/README.md#quickstart) | A compute client expands a procedural into live mesh geometry; runs until `Ctrl+C` | [run.py](fourier_waves/run.py) |
| [Instancing dance](instancing_dance/README.md#quickstart) | Scenegraph instances, references, and animated transforms; runs until `Ctrl+C` | [run.py](instancing_dance/run.py) |

The launchers coordinate their own server and participating processes. They use
temporary event logs and stop their child processes on normal exit or
`Ctrl+C`. Add `--no-usdview` when no usdview executable is installed.

## Multi-process demo

[Shared-stage client](shared_stage_client/README.md) shows two processes editing an
equivalent file-backed layer graph through `SharedStageClient`. Its guide starts the
shared-stage server separately, then runs observer and author roles with
[demo.py](shared_stage_client/demo.py).

## Authoring recipe

[Referenced MaterialX document](mtlx_document/README.md) is a focused CLI recipe,
not a standalone launcher. It authors a reference to the included
[glass.mtlx](mtlx_document/glass.mtlx) document and binds that material to a scene
prim for consumers with a native MaterialX pipeline.
