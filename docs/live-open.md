# Server-Provided USD Files

The server-provided file service, also called live-open, publishes the current
server scene as USD files through HTTP/WebDAV and a local filesystem mirror. An
application without an OpenUSDConnect integration can open the current scene as
a snapshot. With write fallback enabled, supported changes saved to that file
can be sent to the server, but the application does not receive incremental
updates. An integrated DCC also reads the embedded server address, then
continues synchronization over the normal TCP connection after the last event
included in the file.

Live continuation from a flattened `scene.usd` is intended for the default
managed session with one unmuted collaboration layer and no department policy.
Use the original base scene for managed layered replay, or the original portable
layer graph for `shared_stage` mode.

For the primary server and Blender setup, begin with
[Getting Started](getting-started.md). Its server-provided file section introduces
this option; this page documents resource formats, mounting, write fallback,
metadata, and server configuration.

## Start The Live-Open Services

```bash
uv sync --group vfs --group dashboard
uv run python scripts/start_live_open.py --base test_scene.usda --dashboard-port 8080 --open
```

This command assumes the project OpenUSD runtime is active. Use the
`bundled-usd` group only for a renderer-neutral session that needs neither
MaterialX nor custom renderer, resolver, file-format, or shader plugins; in
that case, include `--group bundled-usd` in the `uv sync` command.

The workstation launcher starts the sync server, WebDAV endpoint, and a
write-capable local mirror. On Windows it also exposes the mirror through `O:`
by default. Stop the recorded processes with:

```bash
uv run python scripts/start_live_open.py stop
```

Run both commands from the repository root. The launcher prints and records its
process IDs in `.ouc_live_mount/live_open_session.json`; `stop` reads that same
state file and releases the optional Windows drive alias. It stops processes but
keeps the event database, logs, and generated mirror for inspection or restart.
Use a distinct `--state-file`, `--log-dir`, `--mirror-dir`, port set, and drive
alias for each concurrent session, and pass the matching `--state-file` to
`stop`.

Unlike the standalone server, the workstation launcher defaults to
`--vfs-write-mode translate` so applications without a plug-in can save through
the local mirror.

When enabled, the dashboard listener binds to all network interfaces. The sync
and WebDAV services retain their configured hosts. Keep the dashboard behind a
trusted firewall or leave `--dashboard-port` at `0` on an untrusted network.

## Virtual Resources

The WebDAV collection defaults to <http://127.0.0.1:7280/usd/>.

| Resource | Purpose |
| --- | --- |
| `scene.usd` | Flattened snapshot with no sibling-file dependencies; used for DCC import and live-open continuation. |
| `scene.live.usda` | Composition root that preserves the exported base and live override layers. |
| `_layers/` | File-backed layers referenced by `scene.live.usda`. |
| `openusdconnect.json` | Machine-readable manifest for launchers and diagnostics. |

All generated resources are cached by the server's `(epoch, sequence)` token.
Their contents and ETags do not change merely because time passes; they change
when an edit, compaction, purge, or other visible stage-state transition changes
that token.

### `scene.usd`: Flattened Snapshot

Use `scene.usd` for DCC import, non-integrated viewers, file downloads,
and the Blender or Unreal live-open workflow. It contains the composed stage at
one server sequence and does not depend on sibling files.

Flattening intentionally discards the identity of individual contributing
layers. An integrated client therefore continues from `snapshot_seq + 1` in
flat replay rather than reconstructing the historical collaboration-layer
stack.

### `scene.live.usda`: Composition Root

Use `scene.live.usda` when inspecting the server's exported composition or when
a USD runtime can resolve the companion `_layers` directory. It keeps the base
and server-authored overrides separate, making layer strength and authored
opinions visible.

It is less portable than `scene.usd`: copying only the root file breaks its
sublayer references. It is not the default live-open target for DCC import.

## Choose A File Path

| Path | Recommended use |
| --- | --- |
| Local mirror | DCC open and save through a filesystem directory; recommended on every platform. |
| Windows drive alias | Explorer and file-picker access to the local mirror without administrator rights. |
| Windows UNC/WebDAV | Read/open diagnostics through the native WebClient redirector. |
| macOS WebDAV mount | Native read-oriented access to the complete virtual tree. |
| HTTP URL | Download, scripting, health checks, and custom launchers. |

Raw HTTP is not a universal OpenUSD asset resolver. Prefer the local mirror or
a native filesystem path when an application expects a file.

## Local Mirror

The bridge mirrors the managed `scene.usd` into a normal directory and uploads
completed saves with ETag conflict protection. It accommodates applications
that save through a temporary sibling followed by rename.

```text
uv run python scripts/local_vfs_bridge.py --vfs-url http://127.0.0.1:7280/usd/scene.usd --mirror-dir .ouc_live_mount/usd --background --open
```

On Windows, add `--drive O:` to expose the mirror as `O:\scene.usd`. The drive
is a local alias, not a WebDAV mapping, and normally contains only the managed
live file. Bridge status and logs remain outside the exposed directory.

```text
uv run python scripts/local_vfs_bridge.py status --status-file .ouc_live_mount/bridge/openusdconnect_bridge_status.json
uv run python scripts/local_vfs_bridge.py stop --status-file .ouc_live_mount/bridge/openusdconnect_bridge_status.json --stop-process
```

The bridge waits for a save to stabilize and uploads against the last observed
ETag. A concurrent remote edit places it in an explicit conflict/recovery state
instead of overwriting newer work.

## Native WebDAV Paths

### Windows

Windows commonly accepts either of these UNC forms:

```text
\\127.0.0.1@7280\usd\scene.usd
\\127.0.0.1@7280\DavWWWRoot\usd\scene.usd
```

The Windows WebDAV redirector requires the **WebClient** service. Starting that
service may require an elevated session. Diagnose the path without mounting it:

```powershell
uv run python scripts/check_windows_unc_webdav.py --port 7280
```

Map it through the native client when the complete WebDAV tree is required. The
helper tries the URL, `DavWWWRoot`, and short UNC forms because Windows WebClient
behavior differs by configuration:

```powershell
uv run openusdconnect-mount-vfs --port 7280 --drive P: --open
uv run openusdconnect-mount-vfs unmount --drive P:
```

For local DCC use, prefer the launcher's no-admin local drive alias.

### macOS

```bash
uv run openusdconnect-mount-vfs --port 7280 --open
uv run openusdconnect-mount-vfs unmount
```

The native mount is read-only by default. The system WebDAV client can cache
reads, so integrated clients use TCP live sync after the initial open.

### Linux

Use the local mirror by default. The mount helper does not install or configure
a system FUSE/WebDAV client.

## Write Fallback

The server controls direct `PUT` behavior with `--vfs-write-mode`:

| Mode | Behavior |
| --- | --- |
| `forbid` | Default standalone-server mode. Reject writes with `403`. |
| `drop` | Accept and discard the body. No validation is performed because server state cannot change. |
| `translate` | Validate the saved USD, compute supported differences, and publish them as normal live transactions. |

Translate mode checks that the upload is readable USD and that its embedded
`scene_id`, `epoch`, and `snapshot_seq` still match the authoritative snapshot.
It rejects stale saves, incomplete destructive saves, unsupported authored
properties, and sublayer-topology changes rather than silently replacing newer
state.

Saving bytes identical to the current snapshot is accepted as a no-op: it does
not add events or advance the snapshot token.

Use `--vfs-bypass-write-validation` only for compatibility experiments. Invalid
USD accepted through the bypass cannot be translated and is dropped.

Translate mode is available only for the default managed collaboration layer.
Department-layer and shared-stage sessions preserve composition through their
native protocols and do not accept flattened write translation.

## Embedded Metadata

Both live-open USD roots contain `customLayerData["openusdconnect"]`.

| Field | Meaning |
| --- | --- |
| `live` | Marks an OpenUSDConnect live resource. |
| `host`, `port` | TCP endpoint advertised to integrated clients. |
| `protocol_version` | Wire protocol version. |
| `scene_id` | Stable identity of the authoritative scene. |
| `snapshot_seq` | Highest event sequence represented by this snapshot. |
| `epoch` | Snapshot generation epoch. |
| `vfs_url` | Canonical HTTP/WebDAV URL for the resource. |
| `department` | Optional collaboration-layer hint. |
| `requires_token` | Whether TCP connections require a TOFU token. |
| `generated_at` | UTC snapshot generation time. |

An integrated client imports the snapshot first, seeds its applied cursor from
`snapshot_seq`, and connects with `sync_from = snapshot_seq + 1`.

Flat snapshot continuation requires one unmuted collaboration layer and no
department policy. A server that must preserve managed layer ordering rejects
flat replay with a clear reason. Open the original base stage with a
layer-capable integration for that workflow.

## Authentication And Exposure

`--require-token` protects the TCP synchronization protocol. Authentication
tokens are never embedded in USD files or the manifest.

The WebDAV endpoint itself is unauthenticated. Keep `--vfs-host` on
`127.0.0.1` for a local workstation or place remote access behind a trusted
network boundary or authenticated proxy.

Blender and Unreal persist TOFU tokens according to their integration settings
and reuse them for receiver and emitter reconnects.

## Run The Server Endpoint Manually

The following command starts the TCP server, WebDAV endpoint, and dashboard. It
does not create the local mirror or Windows drive alias provided by
`start_live_open.py`.

```text
uv run openusdconnect-server --host 127.0.0.1 --port 7200 --base test_scene.usda --event-log usd_events.db --vfs-port 7280 --vfs-write-mode translate --dashboard-port 8080
```

Run the [local mirror](#local-mirror) command separately if a filesystem path is
required. Stop the foreground server with `Ctrl+C`, then stop a background
bridge with its documented `stop` command. Relative event-log and mirror paths
resolve from the current working directory, so run from the repository root or
use absolute paths.

Important VFS options:

| Option | Default | Purpose |
| --- | --- | --- |
| `--vfs-port` | Disabled | Start the WebDAV endpoint. |
| `--vfs-host` | Sync bind host | WebDAV bind host or interface. |
| `--vfs-share` | `usd` | Collection name. |
| `--vfs-name` | `scene.usd` | Flattened snapshot name. |
| `--vfs-live-name` | `scene.live.usda` | Composition-root name. |
| `--vfs-layer-dir` | `_layers` | Exported-layer directory. |
| `--vfs-manifest-name` | `openusdconnect.json` | Manifest name. |
| `--advertise-host` | Bind host or loopback | Host written into live metadata. |
| `--no-vfs-prewarm` | Prewarm enabled | Disable background snapshot pre-generation. |

When binding the server to `0.0.0.0`, set `--advertise-host` to the hostname or
address that remote DCCs can reach.

## Verify And Diagnose

Download and parse the flattened snapshot:

macOS or Linux:

```bash
curl -o ouc_scene.usd http://127.0.0.1:7280/usd/scene.usd
```

PowerShell:

```powershell
Invoke-WebRequest http://127.0.0.1:7280/usd/scene.usd -OutFile ouc_scene.usd
```

Then parse the downloaded file:

```text
uv run python -c "from pxr import Usd; s=Usd.Stage.Open('ouc_scene.usd'); print(bool(s), s.GetRootLayer().customLayerData.get('openusdconnect'))"
```

Open <http://127.0.0.1:7280/usd/openusdconnect.json> to inspect the
machine-readable manifest.

For server and mount options, see the [command-line reference](cli-reference.md).
For integration behavior, continue with the [Blender guide](blender-addon-usage.md)
or [Unreal guide](../integrations/unreal/OpenUSDConnect/README.md).
