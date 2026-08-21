# Building and selecting OpenUSD

OpenUSDConnect targets the OpenUSD release recorded in
[`openusd.lock.json`](../openusd.lock.json). Use that release for the server,
usdview, and USD-native clients that share a plugin environment. Blender and
Unreal use the OpenUSD runtime supplied by the host application.

## Requirements

The source build requires Git, [uv](https://docs.astral.sh/uv/), the Python
version in `.python-version`, CMake 3.27 or newer, and a 64-bit C++17 toolchain.
The supported compiler path matches OpenUSD: MSVC on Windows, Apple Clang on
macOS, and GCC or Clang on Linux.

On Windows, install the Desktop development with C++ workload from Visual
Studio or Build Tools. An x64 Native Tools command prompt or x64 Developer
PowerShell already exposes the compiler. From an ordinary shell, the wrapper
uses `vswhere` to locate Visual Studio and initializes its x64 developer
environment automatically.

The Python interpreter running the build must have the same major and minor
version as the bindings OpenUSD produces. The project `.venv` satisfies that
requirement when it was created from `.python-version`.

## Managed source build

The default profile builds a headless runtime with Python bindings and OpenUSD
command-line tools:

```bash
uv run python scripts/build_openusd.py
```

The wrapper clones the exact tag and commit in `openusd.lock.json`, then calls
OpenUSD's upstream `build_usd.py`. It owns these paths by default:

| Purpose | Path |
| --- | --- |
| OpenUSD checkout | `.openusd/<version>/<platform>/checkout` |
| Downloaded dependency sources | `.openusd/<version>/<platform>/sources` |
| Build tree | `.openusd/<version>/<platform>/build` |
| Installation | `.openusd/<version>/<platform>/install` |

Use `--root` to relocate all four paths. `--checkout-dir`,
`--dependency-source-dir`, `--build-dir`, and `--install-dir` override them
individually.

The wrapper verifies an existing checkout before using it. It does not update,
reset, or run code from a locally modified checkout. `--dry-run` prints the
clone and upstream build commands without creating directories.

## Build profiles and features

| Selection | Result |
| --- | --- |
| `--profile runtime` | Python bindings and command-line tools without imaging |
| `--profile usdview` | Imaging, usdview, Python bindings, tools, and MaterialX |
| `--embree` | Adds the Embree Hydra renderer |
| `--renderman PATH` | Enables usdview/imaging and builds hdPrman |

Install the `usdview` dependency group when building or running usdview. It
provides the matching PySide6 and PyOpenGL packages:

```bash
uv run --group usdview python scripts/build_openusd.py --profile usdview
```

Embree is an optional addition:

```bash
uv run --group usdview python scripts/build_openusd.py \
  --profile usdview --embree
```

RenderMan must already be installed. Pass its RenderManProServer prefix;
`--renderman` is a usdview/imaging option and builds hdPrman:

```bash
uv run --group usdview python scripts/build_openusd.py \
  --renderman /opt/pixar/RenderManProServer-27.0
```

Boolean feature switches also have `--no-*` forms. For example,
`--no-materialx` disables MaterialX when starting from the usdview profile.
usdview cannot be combined with `--no-python`.

Builds use at most eight parallel jobs by default to avoid exhausting memory
on machines with many logical cores. Override that limit with `--jobs`. Use
`--variant` for `release`, `debug`, or `relwithdebuginfo`, and pass a CMake
`--generator` or `--toolset` when the platform needs an explicit selection.
`--force NAME` rebuilds one upstream dependency and may be repeated;
`--force-all` requests a complete upstream rebuild.

## Managed runtime selection

After a successful Python-enabled build, the wrapper writes a platform-specific
record such as `.openusd/active-windows-x86_64.json`. OpenUSDConnect reads this
project-local file before the optional `usd-core` fallback and configures the
Python bindings, native library paths, and recorded RenderMan installation
automatically. Platform-specific roots and records keep Windows, WSL/Linux, and
macOS artifacts separate when they share a checkout. Normal project commands
need no shell activation:

```bash
uv run openusdconnect-server --base scene.usda
```

OpenUSD executables need to be resolved before their process starts, so launch
them through the child-command wrapper:

```bash
uv run python scripts/run_with_openusd.py -- usdview scene.usda
```

An explicit `OPENUSDCONNECT_USD_ROOT` or `--usd-root` takes precedence over the
managed selection.

## External OpenUSD installations

The shell adapters are for OpenUSD builds created outside the managed workflow.
They configure the current shell without writing persistent user or system
environment variables.

PowerShell:

```powershell
. .\scripts\openusd_env.ps1 "D:\OpenUSDInstall"
```

Bash or Zsh:

```bash
source scripts/openusd_env.sh /opt/OpenUSDInstall
```

For CI or another shell, configure only one child command:

```bash
uv run python scripts/run_with_openusd.py --usd-root /opt/OpenUSDInstall -- \
  openusdconnect-server --base scene.usda
```

The adapters search Windows `Lib/site-packages`, Unix
`lib/pythonX.Y/site-packages` and `dist-packages`, the legacy `lib/python`
layout, and the selected interpreter's environment. Use `-PythonPath` or
`--python-path` only when the bindings are elsewhere.

The repository `.venv` is the default interpreter. If the external OpenUSD
build targets another interpreter, select it explicitly:

```powershell
. .\scripts\openusd_env.ps1 "E:\OpenUSDInstall" `
    -PythonExecutable "E:\OpenUSD-venv\Scripts\python.exe"
```

For Bash/Zsh, set `OPENUSDCONNECT_ENV_PYTHON`; for the child-command wrapper,
use `--python-executable`.

Use `-RenderManRoot` or `--renderman-root` to configure hdPrman. Additional USD
plugin paths use `-PluginPath` or repeatable `--plugin-path`; native dependency
directories use `-DllDir` or repeatable `--dll-dir`. The underlying environment
maps these to `PXR_PLUGINPATH_NAME` and the platform loader path.

## Verify the selected runtime

For a managed build:

```bash
uv run python -c \
  "import pxr; from pxr import Usd; print(pxr.__file__); print(Usd.GetVersion())"
```

For an external build, run the same probe after sourcing the adapter, or place
it after `--` in `run_with_openusd.py`. A Python ABI mismatch is reported while
the runtime is configured instead of failing on the first later USD import.

The [command-line reference](cli-reference.md) covers server and integration
commands after the runtime has been selected.
