# Examples

These examples exercise the same public clients and event protocol used by the
integrations. Complete the [getting-started requirements](../docs/getting-started.md#what-you-need), then
follow the linked example guide for its authoritative run instructions.

## One-command demos

| Example | What it demonstrates | Launcher |
| --- | --- | --- |
| [USD-native clients](usd_native_client/README.md) | Layered publish and receive with separate author and mirror stages, plus a second peer process | [run.py](usd_native_client/run.py) |
| [Fourier waves](fourier_waves/README.md#quickstart) | A network compute client that expands a generative procedural into live mesh geometry | [run.py](fourier_waves/run.py) |
| [Instancing dance](instancing_dance/README.md#quickstart) | Scenegraph instances, references, and animated transforms shared with connected receivers | [run.py](instancing_dance/run.py) |

The launchers coordinate their own server and participating processes. Their guides
document viewer behavior, headless options where available, and customization flags.

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
