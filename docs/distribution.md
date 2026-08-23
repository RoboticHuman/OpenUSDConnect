# Packaging and distribution

`scripts/build_distribution.py` creates installable artifacts for each
OpenUSDConnect consumer. Build only the components needed for a release; a
Blender or Unreal package is always tied to the host version used to build it.

| Artifact | Consumer setup | Compatibility boundary |
| --- | --- | --- |
| Self-contained server archive | Extract and run the included server or MCP launcher | Operating system and CPU architecture |
| Python wheel and source archive | Install with `pip`, `uv pip`, or `uv tool` | Python 3.13+ and supported wheel platform |
| Blender add-on ZIP | Install from Blender's Add-ons preferences | Operating system, CPU architecture, and Blender's embedded Python ABI |
| Unreal Engine plugin ZIP | Extract to `<Project>/Plugins/OpenUSDConnect` | Unreal Engine version and target platform |
| C++ client SDK ZIP | Add with CMake and link an exported target | C++17 compiler and target platform |
| Docker image | Run with Docker or Compose | Container platform |

Every file artifact is recorded in `release-manifest.json` and
`SHA256SUMS`. Build metadata inside the server, Blender, and Unreal packages
identifies the release and target used to create it.

## Build release artifacts

Install the repository environment first:

```bash
uv sync --frozen
```

Build any combination by repeating `--component`:

```bash
uv run python scripts/build_distribution.py \
  --component python \
  --component server \
  --component cpp-sdk
```

The default output is `dist/release/<version>`. A subsequent build refuses to
mix files into that release directory; pass `--clean-output` to replace it.

The `all` component also requires Blender, Unreal Engine, and Docker:

```powershell
uv run python scripts/build_distribution.py `
  --component all `
  --blender D:\path\to\blender.exe `
  --unreal-engine D:\path\to\UnrealEngine `
  --clean-output
```

Each component can be built independently on its target machine. This is the
expected way to produce a platform matrix; do not copy a native DCC package to
a different operating system, architecture, Python ABI, or engine version.
Maintainers can run the dispatch-only `Build release packages` workflow to
build and smoke-test core packages across Windows, Linux, and macOS and Blender
packages across Windows and Linux. It uploads workflow artifacts but does not
publish a release. Unreal packages remain engine-local builds.

## Self-contained server and MCP

The server archive contains its own Python runtime, OpenUSD, VFS and dashboard
dependencies, and MCP server. Windows releases use ZIP; Linux and macOS use
`tar.gz` so launcher permissions are preserved. The destination machine does
not need Python or the repository.

On Windows:

```powershell
.\openusdconnect-server.cmd --base C:\scenes\scene.usda --vfs-port 7280 --dashboard-port 8080
.\openusdconnect-mcp.cmd --host 127.0.0.1 --port 7200
```

On Linux or macOS, use `./openusdconnect-server` and
`./openusdconnect-mcp`.

## Python installation profiles

The wheel exposes installation profiles for consumers that prefer a managed
Python environment:

```bash
uv tool install "openusdconnect[server]"
uv tool install "openusdconnect[mcp]"
uv tool install "openusdconnect[complete]"
```

`server` includes OpenUSD, WebDAV, and the dashboard. `mcp` includes OpenUSD
and the MCP runtime. `complete` includes both. The base package remains
suitable for a host that already supplies its own OpenUSD runtime.

## Blender packages

Pass the exact Blender executable being targeted:

```powershell
uv run python scripts/build_distribution.py `
  --component blender `
  --blender D:\path\to\blender.exe
```

The builder queries Blender for its embedded Python version and extension
suffix, compiles the native client for that ABI, creates a platform-tagged
add-on ZIP, and installs it into a clean temporary Blender profile as a smoke
test. The builder provisions a matching uv-managed Python development runtime
for compilation. `--blender-python-sdk` is available for build environments
that maintain an explicit matching SDK.

## Unreal Engine packages

BuildPlugin must run against each supported engine and target platform:

```powershell
uv run python scripts/build_distribution.py `
  --component unreal `
  --unreal-engine D:\path\to\UnrealEngine
```

The output contains the staged native client and pinned FlatBuffers headers,
then removes build logs, intermediate files, and debug symbols from the
customer archive. With smoke tests enabled, the cleaned package is installed
into a temporary Unreal project and exercised by the integration harness.

## C++ SDK and Docker

The C++ SDK includes the native client core, generated protocol bindings, and
the matching header-only FlatBuffers runtime. Consumers can use
`add_subdirectory()` and link `OpenUSDConnect::ClientCore` or
`OpenUSDConnect::ClientProtocol`.

Build the container directly or through the release command:

```bash
docker build -t openusdconnect-server .
uv run python scripts/build_distribution.py --component docker
```

The runtime image contains the complete server profile and runs as a non-root
user. `docker compose up server` exposes sync, VFS, and dashboard ports.
Use `--docker-command` when the Docker CLI requires a command prefix, such as a
specific WSL distribution or a remote Docker context.
