# Live-open and VFS

Live-open lets an application import a normal-looking USD file containing the
current managed-server scene, then continue receiving live events from the
snapshot sequence embedded in that file.

The VFS exposes:

- `scene.usd`: a flattened continuation snapshot for broad compatibility
- `scene.live.usda`: a composition root preserving the exported live layer
  stack
- `_layers/`: generated live layers used by the composition root
- `openusdconnect.json`: machine-readable endpoint and snapshot metadata

The filenames are configurable. The snapshot is an initial-open mechanism;
interactive synchronization still uses the TCP protocol.

## Recommended workstation workflow

Install the server and VFS dependencies:

```bash
uv sync --group bundled-usd --group vfs
```

Start a complete local session:

```bash
uv run python scripts/start_live_open.py --base /path/to/scene.usda --open
```

The launcher starts:

1. the sync server, normally on `127.0.0.1:7200`
2. the WebDAV VFS, normally on `127.0.0.1:7280`
3. a write-capable local mirror of `scene.usd`
4. on Windows, an optional `O:` alias for that mirror

It prints the file path to open. On macOS and Linux the default is below
`.ouc_live_mount`; on Windows the default is `O:\scene.usd`.

Stop only the processes recorded by that launcher with:

```bash
uv run python scripts/start_live_open.py stop
```

The workstation launcher uses validated `translate` writes by default. The
standalone server's VFS remains read-only by default.

## Open in Blender

1. Build and install the addon if needed:

   ```bash
   uv run python scripts/build_blender_addon.py
   ```

2. Open the **USD Connect** sidebar.
3. Choose **Import USD (with prim tagging)**.
4. Select the local `scene.usd` reported by the launcher.
5. Enable **Auto-start Emitter** and **Auto-start Receiver** in the import
   options if this Blender instance should join immediately.

The addon imports the snapshot, reads `customLayerData["openusdconnect"]`,
configures host and port, and starts receiving at `snapshot_seq + 1`.

Snapshot continuation uses flat replay. It is valid only when the server has
one unmuted collaboration layer and no department policy. If layer ordering or
muting is required, import the original base file and use Blender's normal
layered replay workflow instead.

If auto-start fails, the imported scene stays open and the addon reports the
connection error.

## Run the server manually

```bash
uv run openusdconnect-server \
  --host 127.0.0.1 \
  --port 7200 \
  --base /path/to/scene.usda \
  --event-log /path/to/usd_events.db \
  --vfs-port 7280
```

The sync protocol listens on `--port`; WebDAV listens on `--vfs-port`.

Important VFS options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--vfs-host` | sync bind host | WebDAV bind interface |
| `--vfs-share` | `usd` | WebDAV collection name |
| `--vfs-name` | `scene.usd` | flattened snapshot name |
| `--vfs-live-name` | derived `.live.usda` name | composition-root name |
| `--vfs-layer-dir` | `_layers` | generated layer directory |
| `--vfs-write-mode` | `forbid` | `forbid`, `drop`, or `translate` |
| `--advertise-host` | bind host, or loopback for `0.0.0.0` | host embedded in metadata |
| `--no-vfs-prewarm` | prewarm enabled | disable background snapshot generation |

When binding to all interfaces, set an address that remote clients can reach:

```bash
uv run openusdconnect-server \
  --host 0.0.0.0 \
  --advertise-host 10.10.20.15 \
  --base scene.usda \
  --vfs-port 7280
```

## Ways to access the virtual file

The local mirror is recommended for DCC saves because it supports temporary
files and rename-based safe-save patterns.

| Access | Example | Use |
| --- | --- | --- |
| Local mirror | `.ouc_live_mount/usd/scene.usd` or `O:\scene.usd` | recommended read/write DCC path |
| HTTP/WebDAV | `http://127.0.0.1:7280/usd/scene.usd` | diagnostics and custom launchers |
| Windows WebDAV UNC | `\\127.0.0.1@7280\usd\scene.usd` | native file picker, usually read-oriented |
| macOS native mount | `~/.openusdconnect/mounts/usd/scene.usd` | native read-oriented mount |

Do not assume a stock USD runtime can open an HTTP URL directly. Use a local
mirror or OS mount when the application expects a filesystem path.

### Native mount helper

The native arguments are platform-specific.

On Windows, choose a drive letter. The WebClient service must be running:

```powershell
uv run python scripts/mount_vfs_share.py --port 7280 --drive O: --open
uv run python scripts/mount_vfs_share.py unmount --drive O:
```

On macOS, use the macOS-only mount arguments. The mount is read-only by
default:

```bash
uv run python scripts/mount_vfs_share.py \
  --port 7280 \
  --mount-point /path/to/mount \
  --volume-name OpenUSDConnect \
  --open

uv run python scripts/mount_vfs_share.py unmount \
  --mount-point /path/to/mount
```

Omit `--mount-point` to use
`~/.openusdconnect/mounts/<share>`. `--read-write` is available on macOS for
filesystem diagnostics, but native WebDAV mounts do not reliably support the
temporary-file and rename sequence used by USD safe saves. Use the local
bridge for DCC saves.

### Local bridge by itself

To mirror an already-running VFS:

```bash
uv run python scripts/local_vfs_bridge.py \
  --vfs-url http://127.0.0.1:7280/usd/scene.usd \
  --mirror-dir .ouc_live_mount/usd \
  --background \
  --open
```

The bridge waits for saves to stabilize and uploads them with the last observed
ETag. A concurrent or stale save enters a visible recovery state instead of
overwriting newer server state. Use its `status` and `stop` subcommands with
the status-file path printed at startup.

## Write modes

The WebDAV tree has a fixed set of server-generated resources. Creating,
renaming, or deleting arbitrary files is not supported.

- `forbid` returns HTTP 403 for writes. This is the standalone server default.
- `drop` accepts and discards writes. Use it only for applications that demand
  a successful save even though live edits arrive through the plugin.
- `translate` parses a completed USD save and converts supported differences to
  normal live transactions.

Translate mode checks the embedded `scene_id`, `epoch`, and `snapshot_seq` to
reject stale saves. It validates the USD file, rejects obviously incomplete
destructive saves, and rejects authored operations that cannot be mapped safely
into managed collaboration layers, including arbitrary sublayer topology.
Custom properties, relationships, prim/layer metadata, and local variant
definitions are supported through exact Sdf field events.

`--vfs-bypass-write-validation` is a compatibility escape hatch. Invalid bytes
cannot be translated and are dropped; do not use it as a data-preserving mode.

## Metadata contract

Both snapshot forms embed `customLayerData["openusdconnect"]` on the root
layer. Important fields are:

| Field | Meaning |
| --- | --- |
| `live` | identifies an OpenUSDConnect continuation file |
| `host`, `port` | TCP sync endpoint |
| `protocol_version` | protocol compatibility value |
| `scene_id` | server scene identity |
| `snapshot_seq` | highest event included in the snapshot |
| `epoch` | invalidates stale snapshots when visible non-transaction state changes or sequence IDs are recycled |
| `vfs_url` | backing WebDAV URL |
| `requires_token` | whether TCP clients must authenticate |
| `generated_at` | snapshot generation timestamp |

An integrated client imports the snapshot first and then requests events from
`snapshot_seq + 1`.

## Authentication and exposure

`--require-token` protects the TCP sync protocol. Tokens are never embedded in
the USD file and the WebDAV endpoint is intentionally unauthenticated.

Keep `--vfs-host` on loopback for local workflows. Remote VFS access should be
placed behind a trusted network boundary or authenticated proxy. Metadata-aware
Blender and Unreal integrations use their TOFU token stores when joining TCP
sync.

## Verify the setup

```bash
curl -o ouc_scene.usd http://127.0.0.1:7280/usd/scene.usd
uv run python -c "from pxr import Usd; s=Usd.Stage.Open('ouc_scene.usd'); print(bool(s), s.GetRootLayer().customLayerData.get('openusdconnect'))"
```

Windows WebClient diagnostic:

```powershell
uv run python scripts/check_windows_unc_webdav.py --port 7280
```

Focused automated tests are listed under [Live-open and VFS tests](testing-setup.md#live-open-and-vfs-tests).
