# Live-Open Quickstart

This guide covers the current live-open workflow: start one OpenUSDConnect
server, expose the live scene as a normal-looking USD file, import that file
in Blender, and let the addon configure live sync from metadata.

## What You Get

- A live sync server on the TCP protocol, usually `127.0.0.1:7200`.
- A WebDAV virtual USD directory, usually `http://127.0.0.1:7280/usd/`.
- A flattened USD fallback file: `scene.usd`.
- A composition-aware USD root: `scene.live.usda`.
- Exported live layers under `_layers/`.
- A native filesystem mount on Windows or macOS.
- A write-capable local mirror. It is a normal directory on macOS and Linux,
  and may also use a drive alias such as `O:\scene.usd` on Windows.
- A flattened snapshot for non-integrated tools.
- Blender and Unreal metadata discovery with configurable auto-start for
  receiver/emitter.

The WebDAV directory is read-only by default. Direct writes return `403`.
For compatibility with tools that require a successful save, use
`--vfs-write-mode drop`; writes are accepted and discarded. For fallback
editing from non-integrated tools, use `--vfs-write-mode translate`; a saved
USD snapshot is parsed, translated into live events, and broadcast through the
normal sync server. Translate mode validates uploaded content as a readable USD
file by default. Invalid USD, stale live snapshots, and obviously incomplete
destructive saves are rejected instead of silently replacing newer live state.
Drop mode does not validate because it never mutates server state.

## Prerequisites

- Python environment with OpenUSD `pxr` bindings available.
- Server dependencies:

```powershell
uv sync --group server --group vfs
```

- Blender addon installed if you want automatic live sync:

```powershell
uv run python scripts/build_blender_addon.py
```

Then install `dist/usd_connect_blender.zip` from Blender:
`Edit > Preferences > Add-ons > Install from Disk`.

Windows native mounts require the WebClient service. macOS native mounts use
the system `mount_webdav` client and need no additional package.

## Start A Live-Open Server

For a complete local workstation session, use the workstation launcher:

```bash
uv run python scripts/start_live_open.py \
  --base /path/to/scene.usda \
  --open
```

This starts the sync server, the VFS endpoint, and a local mirror. On Windows,
the mirror uses `O:` by default; pass `--drive` to choose another letter. On
macOS and Linux, the file defaults to `.ouc_live_mount/usd/scene.usd`.
Stop the recorded session with:

```bash
uv run python scripts/start_live_open.py stop
```

The launcher keeps translate write validation enabled by default. For
compatibility testing only, pass `--vfs-bypass-write-validation`; invalid USD
writes are then accepted and dropped because they cannot be translated.

To run the pieces manually:

```bash
uv run openusdconnect-server \
  --host 127.0.0.1 \
  --port 7200 \
  --base /path/to/scene.usda \
  --event-log /path/to/usd_events.db \
  --vfs-port 7280
```

The sync protocol listens on `--port`. The virtual file listens on
`--vfs-port`.

Useful VFS flags:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--vfs-port PORT` | disabled | Starts the WebDAV virtual file endpoint. |
| `--vfs-host HOST` | same as `--host` | Bind host/interface for WebDAV. |
| `--vfs-share NAME` | `usd` | WebDAV collection/share name. |
| `--vfs-name FILE` | `scene.usd` | Flattened fallback file name. |
| `--vfs-live-name FILE` | `scene.live.usda` | Composition-aware root file name. |
| `--vfs-layer-dir NAME` | `_layers` | Directory containing exported live layers. |
| `--vfs-manifest-name FILE` | `openusdconnect.json` | Machine-readable VFS manifest. |
| `--vfs-write-mode MODE` | `forbid` | `forbid` returns 403; `drop` accepts and discards PUT bodies; `translate` turns full-file USD saves into live events. |
| `--vfs-bypass-write-validation` | translate validation enabled | Accept invalid USD writes in `translate` mode instead of rejecting them. Invalid bypassed writes are dropped because they cannot be translated. |
| `--no-vfs-prewarm` | prewarm enabled | Disables background snapshot pre-generation. |
| `--advertise-host HOST` | bind host, or `127.0.0.1` for all interfaces | Host embedded in live metadata. |

If you bind to all interfaces, set `--advertise-host` to the hostname or IP
that Blender clients should use:

```powershell
uv run openusdconnect-server `
  --host 0.0.0.0 `
  --port 7200 `
  --base D:\show\shot010\scene.usda `
  --vfs-port 7280 `
  --advertise-host 10.10.20.15
```

## Supported Open Paths

### HTTP/WebDAV URL

```text
http://127.0.0.1:7280/usd/scene.usd
```

This is the flattened fallback file. Browsers, `curl`, and
`Invoke-WebRequest` can download it for diagnostics or custom launcher flows.
Do not assume every stock USD runtime can open raw HTTP directly. Use a native
mount or local mirror when an application expects a normal file path.

The directory also contains:

```text
http://127.0.0.1:7280/usd/scene.live.usda
http://127.0.0.1:7280/usd/_layers/
http://127.0.0.1:7280/usd/openusdconnect.json
```

`scene.live.usda` preserves the live overlay stack and uses the original base
layer path when the server has one. Use it from a native mount when the target
USD runtime can resolve the companion layer files.

### macOS Native Mount

The native helper mounts the complete generated VFS tree read-only by default:

```bash
uv run python scripts/mount_vfs_share.py --port 7280 --open
```

The default live file is:

```text
~/.openusdconnect/mounts/usd/scene.usd
```

Choose a different directory with `--mount-point`. Unmount it with the same
option:

```bash
uv run python scripts/mount_vfs_share.py unmount --mount-point /path/to/mount
```

`--read-write` is available for filesystem diagnostics, but it is not the
recommended DCC save path. The VFS has a fixed resource tree, while OpenUSD
normally saves by creating a temporary sibling and renaming it over the target.
Use the local bridge for saves. Native filesystem caching can also delay a
reread; integrated clients use live sync after the initial open.

### Windows UNC Path

```text
\\127.0.0.1@7280\usd\scene.usd
```

Use this when you want a normal file-picker experience on Windows. The OS
WebDAV redirector maps the UNC path to the WebDAV endpoint.

For a friendlier browseable drive letter, mount the share:

```powershell
uv run python scripts/mount_vfs_share.py --port 7280 --drive O: --open
```

If the helper reports `WebClient: STOPPED` and `Access is denied`, start the
Windows **WebClient** service from an elevated PowerShell or from
`services.msc`, then run the mount command again:

```powershell
Start-Service WebClient
```

Unmount a Windows native mapping when done:

```powershell
uv run python scripts/mount_vfs_share.py unmount --drive O:
```

### Write-Capable Local Bridge

The local bridge keeps `scene.usd` refreshed in a normal local directory and
uploads completed saves back through HTTP `PUT`:

```bash
uv run python scripts/local_vfs_bridge.py \
  --vfs-url http://127.0.0.1:7280/usd/scene.usd \
  --mirror-dir .ouc_live_mount/usd
```

Useful bridge options:

```bash
uv run python scripts/local_vfs_bridge.py \
  --vfs-url http://127.0.0.1:7280/usd/scene.usd \
  --mirror-dir .ouc_live_mount/usd \
  --background \
  --open

uv run python scripts/local_vfs_bridge.py status \
  --status-file .ouc_live_mount/bridge/openusdconnect_bridge_status.json

uv run python scripts/local_vfs_bridge.py stop \
  --status-file .ouc_live_mount/bridge/openusdconnect_bridge_status.json \
  --stop-process
```

On Windows, add `--drive O:` to expose the mirror through `subst`; this remains
the default when no drive option is given. Pass `--no-drive` to use only the
directory. The bridge keeps status and logs outside the mirror.

Open the reported `Live USD file`, for example:

```text
.ouc_live_mount/usd/scene.usd
```

Notes:

- The file appears as a normal `.usd` file.
- The server serves USDA text bytes under the `.usd` name.
- The virtual share contains the flattened snapshot, a live composition root,
  a manifest, and exported layer files.
- Native VFS paths reject creation, deletion, and rename operations. The local
  bridge accommodates safe-save patterns locally and uploads only the completed
  managed file.
- The bridge waits for a save to stabilize, uploads with the last observed
  `ETag`, and enters a visible recovery/conflict state instead of overwriting
  concurrent local or remote edits.
- Direct `PUT` writes are forbidden by default, dropped only when
  `--vfs-write-mode drop` is explicitly enabled, or translated when
  `--vfs-write-mode translate` is explicitly enabled.
- Translate mode records a write summary on the server and requires the
  embedded `scene_id`, `epoch`, and `snapshot_seq` to match the current server
  snapshot exactly.
- Translate mode also rejects authored USD properties outside the supported
  event subset, and it is disabled while any non-default collaboration layer
  or proposal is active.

## Blender Live-Open

The Blender addon supports one live-open entry point: import a normal file
path with prim tagging, then let the embedded metadata configure live sync.

1. Start the server with `--vfs-port`.
2. In Blender, open the USD Connect sidebar.
3. Click `Import USD (with prim tagging)`.
4. Select the local mirror, native mount, or Windows UNC path. For example:

```text
.ouc_live_mount/usd/scene.usd
```

The addon imports the snapshot normally, reads the embedded metadata, sets
the receiver/emitter host and port, and seeds the receiver from
`snapshot_seq`. When the Import panel's **Auto-start Emitter** and
**Auto-start Receiver** checkboxes are enabled, it also starts live emit and
receive from `snapshot_seq + 1`.

This snapshot continuation uses flat replay. The snapshot contains the
composed scene at `snapshot_seq`, but not the logical identity of every
historical collaboration-layer opinion. Layered replay remains available when
Blender is configured from the original base USD rather than a live snapshot.

If live auto-start fails, the imported snapshot stays open and Blender
reports the connection error. When either checkbox is disabled, the manual
start/stop buttons remain available and use the metadata-populated host,
port, and sequence values.

## Metadata Contract

The virtual file embeds live metadata in the root layer:

```python
customLayerData["openusdconnect"]
```

Current fields:

| Field | Meaning |
| --- | --- |
| `live` | `true` when this is an OpenUSDConnect live snapshot. |
| `host` | Sync TCP host for receiver/emitter connections. |
| `port` | Sync TCP port for receiver/emitter connections. |
| `protocol_version` | OpenUSDConnect protocol version. |
| `scene_id` | Stable server scene identifier for this base scene. |
| `snapshot_seq` | Highest sequence number covered by the snapshot token. |
| `epoch` | Snapshot epoch, bumped when visible non-transaction stage state changes or sequence ids are recycled. |
| `vfs_url` | HTTP/WebDAV URL for this virtual file. |
| `department` | Reserved department hint; currently empty by default. |
| `requires_token` | Whether the TCP sync server requires TOFU tokens. |
| `generated_at` | UTC timestamp for snapshot generation. |

Plugin-enabled clients should import the snapshot first, then connect a
receiver with `sync_from = snapshot_seq + 1`.

## Token Behavior

`--require-token` applies to the TCP live sync protocol, not to the WebDAV
snapshot endpoint. The virtual file never embeds an auth token.

The WebDAV snapshot endpoint is intentionally unauthenticated. Keep
`--vfs-host` bound to `127.0.0.1` for local workflows, or put remote VFS access
behind a trusted network boundary or proxy.

Blender and the Unreal Python bridge use the existing TOFU token store:

- On first connect, the server issues a token.
- The client saves the token for that host and port.
- Later receiver/emitter connections present the saved token.
- If authentication is rejected, the client reports the failure.

The native Unreal plugin stores issued tokens in the user's Unreal config
when **Persist Auth Tokens** is enabled. If a token-required live-open file is
opened and no token is saved yet, Unreal starts the receiver first, keeps the
issued token in memory, optionally saves it, then starts the emitter on the
next tick with the same token.
`GetStatus()` reports endpoint, metadata source, snapshot sequence,
receiver/emitter state, and auth state.

When a virtual file says `requires_token = true`, plugin-enabled clients
must already support the server's token flow. Non-integrated tools can still
open the snapshot, but they cannot join live sync.

## Quick Verification

Fetch the snapshot:

```bash
curl -o ouc_scene.usd http://127.0.0.1:7280/usd/scene.usd
```

Open it with OpenUSD:

```bash
uv run python -c "from pxr import Usd; stage = Usd.Stage.Open('ouc_scene.usd'); print(bool(stage), stage.GetRootLayer().customLayerData.get('openusdconnect'))"
```

Validate Windows UNC/WebClient on a workstation:

```powershell
uv run python scripts/check_windows_unc_webdav.py --port 7280
```

Preview or create the native mount on Windows or macOS:

```bash
uv run python scripts/mount_vfs_share.py --port 7280 --print-only
uv run python scripts/mount_vfs_share.py --port 7280 --open
```

Measure snapshot cost on a real or synthetic scene:

```powershell
uv run python scripts/bench_vfs_snapshot.py --base D:\path\to\scene.usda
uv run python scripts/bench_vfs_snapshot.py --synthetic-prims 10000
```

Run the focused tests:

```bash
uv run pytest \
  tests/unit/test_vfs.py \
  tests/unit/test_vfs_mount.py \
  tests/unit/test_local_vfs_bridge.py \
  tests/unit/test_start_live_open.py \
  tests/integration/test_vfs_webdav.py \
  tests/integration/test_start_live_open.py \
  -q
uv run pytest tests/integration/test_live_discovery.py tests/integration/test_live_open_blender.py -q
```
