# Command-line reference

## First server session

With a managed OpenUSD build registered, or an external build configured,
start a persistent local server from the repository root:

```bash
uv sync --group dashboard
uv run openusdconnect-server --base test_scene.usda --event-log session.db --export-diff session-changes.usda --port 7200 --dashboard-port 8080
```

When no managed or external runtime is selected, a renderer-neutral session
can replace the first command with
`uv sync --group bundled-usd --group dashboard`.

The sync endpoint is `127.0.0.1:7200` and the optional dashboard is
<http://127.0.0.1:8080>. Stop the server with Ctrl+C. `session.db` retains the
ordered event history for the next run, while `session-changes.usda` is written
on clean shutdown as a portable managed-layer export. Relative paths are
resolved from the shell's current directory.

In a second terminal, this sends one transaction and should print
`Sent 1 event(s)`:

```bash
uv run openusdconnect-send '{"k":"ensure_prim","prim":"/World/FirstEdit","typeName":"Xform"}'
```

The dashboard currently listens on all network interfaces even though the
local URL above uses `127.0.0.1`. Keep it behind a trusted firewall or omit
`--dashboard-port` when the host network is not trusted.

For a bounded test that creates and cleans up its own server, run the
[headless first run](../README.md#get-started). For a file-picker workflow,
use the separate [server-provided USD file](live-open.md) path.

## Common options

Endpoint options use the following names:

| Setting | Canonical option | Default |
| --- | --- | --- |
| Sync server host | `--host` | `127.0.0.1` |
| Sync server port | `--port` | `7200` |
| WebDAV bind host | `--vfs-host` in combined tools, `--host` in the native mount tool | sync host / `127.0.0.1` |
| WebDAV port | `--vfs-port` in combined tools, `--port` in the native mount tool | disabled on the server; `7280` elsewhere |
| WebDAV collection | `--vfs-share` in combined tools, `--share` in the native mount tool | `usd` |
| Virtual snapshot file | `--vfs-name` in combined tools, `--name` in the native mount tool | `scene.usd` |
| Complete virtual-file URL | `--vfs-url` | `http://127.0.0.1:7280/usd/scene.usd` |
| SQLite event log | `--event-log` | `usd_events.db` |
| Dashboard listener | `--dashboard-port` | disabled |
| Windows USD plugin dependency directory | `--plugin-dll-dir` (repeatable) | `OPENUSDCONNECT_DLL_DIRS` |

Durations state their role in the option name, such as `--poll-interval`,
`--settle-time`, and `--startup-timeout`. Invalid ports, counts, and durations
are rejected during argument parsing.

`--durability strict` is the default and persists server-authored records
before live publication. `realtime` may defer persistence for server-internal
writes that do not carry producer progress. Client `Txn` acknowledgements keep
their exactly-once contract in either mode: an acknowledged transaction and
its cumulative producer high-water mark are durable.

## Primary commands

| Command | Purpose | Dependency groups |
| --- | --- | --- |
| `uv run openusdconnect-server --help` | Run the TCP sync server and optional VFS/dashboard services. | OpenUSD runtime; add `vfs` or `dashboard` as needed |
| `uv run python scripts/start_live_open.py --help` | Start the local server, WebDAV file endpoint, write-capable mirror, and optional Windows drive. | OpenUSD runtime, `vfs`; add `dashboard` when using `--dashboard-port` |
| `uv run python scripts/start_live_open.py stop --help` | Stop the processes recorded by the live-open launcher. | OpenUSD runtime, `vfs` |
| `uv run python scripts/local_vfs_bridge.py --help` | Mirror one virtual USD file locally and upload stable saves with ETag conflict protection. | base installation |
| `uv run python scripts/local_vfs_bridge.py status --help` | Print bridge health and recovery state. | base installation |
| `uv run python scripts/local_vfs_bridge.py stop --help` | Stop a bridge and release its optional Windows drive. | base installation |
| `uv run openusdconnect-mount-vfs --help` | Use the native Windows or macOS WebDAV filesystem client. | base installation |
| `uv run openusdconnect-send --help` | Send JSON events or protocol control messages. | base installation |
| `uv run python -m integrations.mcp --help` | Expose the live scene through the MCP stdio server. | OpenUSD runtime, `mcp` |
| `uv run python scripts/start_usdview.py --help` | Start a server and open usdview already connected. | OpenUSD/usdview runtime |
| `uv run python -m integrations.usdview.launcher --help` | Open a stage in usdview with automatic receiver wiring. | OpenUSD/usdview runtime |
| `uv run --group dashboard python scripts/demo_layer_dashboard.py` | Start and populate a temporary departmental dashboard demo. | OpenUSD runtime, `dashboard` |

`scripts/mount_vfs_share.py` remains a compatibility wrapper for
`openusdconnect-mount-vfs`.

## Server layer modes

`openusdconnect-server --layer-mode managed` is the default. It provides
receiver-owned collaboration layers, department policy, and VFS live-open
workflows.

`--layer-mode shared_stage` instead routes exact authored changes to equivalent
portable root and sublayers opened by every process. It requires `--base`
and cannot be combined with departments, VFS, `--export-diff`, or purge.
Managed and shared-stage clients are rejected when they connect to a
server running the other mode.

## OpenUSD runtime and custom plugins

Build profiles, compiler setup, directory layout, and external installation
configuration are documented in
[Building and selecting OpenUSD](building-openusd.md). A managed build is
selected automatically. Use the command wrapper when launching an OpenUSD
executable:

```bash
uv run python scripts/run_with_openusd.py -- usdview scene.usda
```

For an external build, provide its install prefix:

```bash
uv run python scripts/run_with_openusd.py --usd-root /path/to/OpenUSD -- \
  openusdconnect-server --base scene.usda
```

Use `--python-path /external/site-packages` for Python bindings outside the
prefix and `--python-executable` to select the matching interpreter. Use
repeatable `--plugin-path` and `--dll-dir` options for project additions, or
`--renderman-root /path/to/RenderManProServer` to configure or override
hdPrman. An inherited valid `RMANTREE` is also configured.

The server initializes Sdr before replay and accepts the active environment.
Use the same plugin discovery and renderer variables as clients that interpret
the scene. All source-tree launchers inherit that environment. RenderMan is
discovered automatically when `RMANTREE` is set.

On Windows, `openusdconnect-server` also accepts repeatable
`--plugin-dll-dir` options. `OPENUSDCONNECT_DLL_DIRS` provides the same setting
through the environment. Source-tree tools support this option where shown by
their `--help` output.

Programmatic hosts can set `ServerConfig.plugin_dll_dirs` or call
`prepare_usd_plugin_environment()` before accessing Sdr.

### Bundled runtime fallback

The `bundled-usd` group installs `usd-core`: a renderer-neutral and
custom-plugin-free runtime. Use it only when the session does not need
MaterialX or custom renderer, resolver, file-format, or shader plugins:

```bash
uv run --group bundled-usd openusdconnect-server --base scene.usda
```

Do not combine this group with a managed or explicitly configured project
runtime.

## Development commands

| Command | Purpose |
| --- | --- |
| `scripts/build_blender_addon.py` | Package the Blender addon zip. |
| `scripts/build_distribution.py` | Build release artifacts; see [Packaging and distribution](distribution.md) for components and USD profiles. |
| `openusdconnect-build-sdf-notice-bridge` | Build exact Sdf change tracking against the active OpenUSD install. |
| `scripts/setup_blender_test.py` | Download/configure a portable Blender test runtime. |
| `scripts/start_usdconnect_debug.py` | Start the server and one or two debug-enabled Blender sessions. |
| `scripts/start_usdview.py` | Start a temporary server and connected usdview session. |
| `scripts/run_unreal_tests.py` | Discover Unreal, package the plugin, and run the real editor integration scenario. |
| `scripts/run_material_zoo.py` | Stream the MaterialX/OpenPBR material zoo into Blender, usdview, and/or Unreal. |
| `scripts/demo_layer_dashboard.py` | Start a populated temporary dashboard; use `--exit-after` for smoke tests. |
| `scripts/stress_test_departments.py` | Stress managed transactions and optionally capture a py-spy profile. |
| `scripts/check_windows_unc_webdav.py` | Diagnose a Windows WebClient UNC read and parse the result as USD. |
| `scripts/bench_vfs_snapshot.py` | Measure VFS snapshot generation and cache behavior. |
| `scripts/dump_events.py` | Decode an event-log database for diagnostics. |
| `scripts/create_test_asset.py` | Generate a small test USD asset. |

Benchmark, stress, and DCC bootstrap scripts under `scripts/` are developer
helpers used by these workflows or their tests; they are not additional server
entry points.
