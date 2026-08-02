# Command-Line Reference

OpenUSDConnect keeps endpoint names consistent across its user-facing tools:

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

Durations state their role in the option name, such as `--poll-interval`,
`--settle-time`, and `--startup-timeout`. Invalid ports, counts, and durations
are rejected during argument parsing.

## Primary Commands

| Command | Purpose | Dependency groups |
| --- | --- | --- |
| `uv run openusdconnect-server --help` | Run the TCP sync server and optional VFS/dashboard services. | `server`; add `vfs` or `dashboard` as needed |
| `uv run python scripts/start_live_open.py --help` | Start a complete local server, WebDAV VFS, write-capable mirror, and optional Windows drive. | `server`, `vfs` |
| `uv run python scripts/start_live_open.py stop --help` | Stop the processes recorded by the live-open launcher. | `server`, `vfs` |
| `uv run python scripts/local_vfs_bridge.py --help` | Mirror one virtual USD file locally and upload stable saves with ETag conflict protection. | base installation |
| `uv run python scripts/local_vfs_bridge.py status --help` | Print bridge health and recovery state. | base installation |
| `uv run python scripts/local_vfs_bridge.py stop --help` | Stop a bridge and release its optional Windows drive. | base installation |
| `uv run openusdconnect-mount-vfs --help` | Use the native Windows or macOS WebDAV filesystem client. | base installation |
| `uv run openusdconnect-send --help` | Send JSON events or protocol control messages. | base installation |
| `uv run python -m integrations.mcp --help` | Expose the live scene through the MCP stdio server. | `server`, `mcp` |
| `uv run python -m integrations.usdview.launcher --help` | Open a stage in usdview with automatic receiver wiring. | OpenUSD/usdview runtime |

`scripts/mount_vfs_share.py` remains a compatibility wrapper for
`openusdconnect-mount-vfs`.

## Development Commands

| Command | Purpose |
| --- | --- |
| `scripts/build_blender_addon.py` | Package the Blender addon zip. |
| `scripts/setup_blender_test.py` | Download/configure a portable Blender test runtime. |
| `scripts/start_usdconnect_debug.py` | Start the server and one or two debug-enabled Blender sessions. |
| `scripts/run_unreal_tests.py` | Discover Unreal, package the plugin, and run the real editor integration scenario. |
| `scripts/run_material_zoo.py` | Stream the MaterialX/OpenPBR material zoo into Blender and/or usdview. |
| `scripts/check_windows_unc_webdav.py` | Diagnose a Windows WebClient UNC read and parse the result as USD. |
| `scripts/bench_vfs_snapshot.py` | Measure VFS snapshot generation and cache behavior. |
| `scripts/dump_events.py` | Decode an event-log database for diagnostics. |
| `scripts/create_test_asset.py` | Generate a small test USD asset. |

Benchmark, stress, and DCC bootstrap scripts under `scripts/` are developer
helpers used by these workflows or their tests; they are not additional server
entry points.

## Compatibility Aliases

Older spellings remain accepted but are hidden from `--help`:

| Tool | Compatibility aliases |
| --- | --- |
| Server | `--log`, `--dashboard` |
| Live-open launcher | `--write-mode`, `--bypass-write-validation`, `--dashboard`, `--wait` |
| Local bridge | `--url`, `--poll`, `--settle` |
| Blender debug launcher | `--server-host`, `--server-port`, `--base-usd`, `--log-path` |

New examples and automation should use the canonical options.
