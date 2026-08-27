# Packaging and distribution

`scripts/build_distribution.py` builds Python, standalone server/MCP, Blender,
Unreal, C++ SDK, Docker, and Linux artifacts. Use the target platform's toolchain.

## Build

```bash
uv sync --frozen
uv run python scripts/build_distribution.py \
  --component python --component server --component cpp-sdk --usd-profile full
```

Repeat `--component` to select outputs; its default, `all`, also requires
Blender, Unreal Engine, and Docker. `linux-packages` is opt-in.
Output defaults to `dist/release/<version>`; use `--output-dir PATH` to change it.
Choose a new output directory for another build, or `--clean-output` to replace it.

The builder writes `release-manifest.json` and file checksums in `SHA256SUMS`.
Smoke tests run by default, including unattended Blender and Unreal checks in
temporary profiles/projects. No manual UI interaction is required.
These checks do not establish rendering support on every platform.

## USD profiles

`--usd-profile core|full|external` defaults to `full`. It selects USD for standalone
server/MCP, Docker, and Linux artifacts, not Blender or Unreal's bundled USD.

| Profile | Runtime |
| --- | --- |
| `full` | Pinned OpenUSD plus MaterialX, built from source without renderers, usdview, or Qt. |
| `core` | Pinned `usd-core`, without MaterialX or custom plugins. |
| `external` | Provided installation, validated for version, ABI, and capabilities before bundling. |

`external` requires `--usd-root PATH`. Optional `--usd-plugin-path PATH` is
repeatable; `--allow-unpinned-usd` permits a version outside the pin.
All three options are external-only.
The supplied runtime and plugins must match the package's OS, architecture, and
Python version (`--server-python`, default `3.13`). The builder loads them before
packaging; it does not rebuild external plugins or supply missing dependencies.

```bash
uv run python scripts/build_distribution.py \
  --component server --usd-profile external --usd-root /path/to/OpenUSD \
  --usd-plugin-path /path/to/plugins
```

Server archive names end in `-usd-core`, `-usd-full`, or `-usd-external` before
the extension: `.zip` on Windows, `.tar.gz` on Linux/macOS.
`usd-runtime.json` records the USD version, file formats, and plugin capabilities.
Smoke tests check a relocated package and, when available, an external `.mtlx`
reference through the server and VFS snapshot.

## Install

Extract a server archive and run its launchers without installing Python:

```bash
./openusdconnect-server --base scene.usda
./openusdconnect-mcp --host 127.0.0.1 --port 7200
```

On Windows use `.\openusdconnect-server.cmd` and `.\openusdconnect-mcp.cmd`.

Install a built wheel in a managed Python environment:

```bash
uv pip install "path/to/openusdconnect-<version>-<tags>.whl"
```

The base wheel expects an existing USD runtime. Python extras `server`, `mcp`,
and `complete` supply `usd-core` and service dependencies; they are separate from
the distribution builder's USD profiles.

The C++ SDK supports `add_subdirectory()` and linking
`OpenUSDConnect::ClientCore` or
`OpenUSDConnect::ClientProtocol`.

## DCC builds

Unreal smoke tests use the asset submodule: initialize it with
`git submodule update --init assets`. Their Python dependencies are provisioned
in an isolated environment by the builder.

```bash
uv run python scripts/build_distribution.py \
  --component blender --blender /path/to/blender --output-dir dist/blender
uv run python scripts/build_distribution.py \
  --component unreal --unreal-engine /path/to/UnrealEngine --output-dir dist/unreal
```

Blender packages target the executable's platform and embedded Python ABI.
The builder provisions a matching Python development runtime; use
`--blender-python-sdk PATH` for an explicit matching SDK.
Install the add-on ZIP in the matching Blender version. Unreal packages target
the selected engine/platform; extract the plugin into `<Project>/Plugins/OpenUSDConnect`.

## Docker and Linux artifacts

Docker runtime targets are `server` (default TCP service), `live-open` (adds
WebDAV), `complete` (adds dashboard and MCP dependencies), and `mcp` (stdio MCP).
For Docker, use the `USD_PROFILE` build argument (default `full`).

```bash
docker build --build-arg USD_PROFILE=full -t openusdconnect-server .
docker build --target mcp --build-arg USD_PROFILE=core -t openusdconnect-mcp .
uv run python scripts/build_distribution.py \
  --component docker --docker-target complete --usd-profile full --output-dir dist/docker
uv run python scripts/build_distribution.py \
  --component linux-packages --usd-profile core --output-dir dist/linux
```

`linux-packages` exports Linux Python, server, and C++ SDK artifacts via Docker.
Blender and Unreal remain native host builds.
Linux archives require system libraries compatible with the build environment;
Docker images include their own userspace runtime.
`--docker-command` accepts a Docker CLI command or prefix.
For an external Linux runtime, the distribution script accepts the same
`--usd-root` and repeatable `--usd-plugin-path` options. With Docker directly:

```bash
docker build --build-arg USD_PROFILE=external \
  --build-context usd_runtime=/path/to/linux/openusd \
  --build-context usd_plugins=/path/to/plugin-installations \
  -t openusdconnect-server .
```

The optional `usd_plugins` context contains one installation per child directory.
Use `--build-arg ALLOW_UNPINNED_USD=1` to permit a non-pinned external USD version.
