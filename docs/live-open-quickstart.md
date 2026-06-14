# Live-Open Quickstart

This guide covers the current live-open workflow: start one OpenUSDConnect
server, expose the live scene as a normal-looking USD file, import that file
in Blender, and let the addon auto-connect to the live sync sockets.

## What You Get

- A live sync server on the TCP protocol, usually `127.0.0.1:7200`.
- A WebDAV virtual USD directory, usually `http://127.0.0.1:7280/usd/`.
- A flattened USD fallback file: `scene.usd`.
- A composition-aware USD root: `scene.live.usda`.
- Exported live layers under `_layers/`.
- A Windows UNC path for file browsers: `\\127.0.0.1@7280\usd\scene.usd`.
- A flattened snapshot for non-integrated tools.
- Blender auto-connect when the imported file contains OpenUSDConnect metadata.

The WebDAV directory is read-only by default. Direct writes return `403`.
For compatibility with tools that require a successful save, start the server
with `--vfs-write-mode drop`; writes are then accepted and discarded.

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

For Windows UNC paths, the Windows WebClient service must be available and
allowed to connect to the selected host and port.

## Start A Live-Open Server

```powershell
uv run openusdconnect-server `
  --host 127.0.0.1 `
  --port 7200 `
  --base D:\path\to\scene.usda `
  --log D:\path\to\usd_events.db `
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
| `--vfs-write-mode MODE` | `forbid` | `forbid` returns 403; `drop` accepts and discards PUT bodies. |
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

This is the flattened fallback file. Browsers, `curl`,
`Invoke-WebRequest`, and the Blender live URL operator can download it.
Do not assume every stock USD runtime can open raw HTTP directly. Use the UNC
path when an application expects a normal file path.

The directory also contains:

```text
http://127.0.0.1:7280/usd/scene.live.usda
http://127.0.0.1:7280/usd/_layers/
http://127.0.0.1:7280/usd/openusdconnect.json
```

`scene.live.usda` preserves the live overlay stack and uses the original base
layer path when the server has one. Use it from UNC when the target USD runtime
can resolve the companion layer files.

### Windows UNC Path

```text
\\127.0.0.1@7280\usd\scene.usd
```

Use this when you want a normal file-picker experience on Windows. The OS
WebDAV redirector maps the UNC path to the WebDAV endpoint.

Notes:

- The file appears as a normal `.usd` file.
- The server serves USDA text bytes under the `.usd` name.
- The virtual share contains the flattened snapshot, a live composition root,
  a manifest, and exported layer files.
- Safe-save patterns that create temp files or rename files are forbidden.
- Direct `PUT` writes are forbidden by default, or dropped only when
  `--vfs-write-mode drop` is explicitly enabled.

## Blender Live-Open

The Blender addon supports two live-open entry points.

### Option A: Import The UNC File

1. Start the server with `--vfs-port`.
2. In Blender, open the USD Connect sidebar.
3. Click `Import USD (with prim tagging)`.
4. Select or paste the UNC path:

```text
\\127.0.0.1@7280\usd\scene.usd
```

The addon imports the snapshot normally, reads the embedded metadata, sets
the receiver/emitter host and port, seeds the receiver from
`snapshot_seq + 1`, then starts live receive and emit.

### Option B: Import A Live URL

1. Paste one of these into the live URL field in the USD Connect sidebar:

```text
http://127.0.0.1:7280/usd/scene.usd
```

2. Click the URL import button.

The addon downloads the snapshot to an ETag-keyed local `.usd` cache, imports
that local file, reads the live metadata, and auto-connects. If the URL points
at `scene.live.usda`, the addon follows its `flattened_fallback` metadata
because local temp-file imports cannot resolve the remote `_layers/` directory.

If live auto-connect fails, the imported snapshot stays open and Blender
reports the connection error.

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

Blender uses the existing TOFU token store:

- On first connect, the server issues a token.
- Blender saves the token for that host and port.
- Later receiver/emitter connections present the saved token.
- If authentication is rejected, Blender deletes the stale token and reports
  the failure.

When a virtual file says `requires_token = true`, plugin-enabled clients
must already support the server's token flow. Non-integrated tools can still
open the snapshot, but they cannot join live sync.

## Quick Verification

Fetch the snapshot:

```powershell
Invoke-WebRequest `
  -Uri http://127.0.0.1:7280/usd/scene.usd `
  -OutFile $env:TEMP\ouc_scene.usd
```

Open it with OpenUSD:

```powershell
uv run python -c "from pxr import Usd; stage = Usd.Stage.Open(r'$env:TEMP\ouc_scene.usd'); print(bool(stage), stage.GetRootLayer().customLayerData.get('openusdconnect'))"
```

Validate Windows UNC/WebClient on a workstation:

```powershell
uv run python scripts/check_windows_unc_webdav.py --port 7280
```

Measure snapshot cost on a real or synthetic scene:

```powershell
uv run python scripts/bench_vfs_snapshot.py --base D:\path\to\scene.usda
uv run python scripts/bench_vfs_snapshot.py --synthetic-prims 10000
```

Run the focused tests:

```powershell
uv run pytest tests/unit/test_vfs.py tests/integration/test_vfs_webdav.py -q
uv run pytest tests/integration/test_live_discovery.py tests/integration/test_live_open_blender.py -q
```
