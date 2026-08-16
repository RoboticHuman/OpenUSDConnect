# Getting Started With Blender

## Requirements

- Windows, macOS, or Linux
- [Git](https://git-scm.com/)
- Python 3.13 or newer
- [uv](https://docs.astral.sh/uv/)
- Blender 4.4 or newer

Python and `uv` run the server and build the add-on. Blender uses its own Python
and OpenUSD runtime. The commands in this guide work in PowerShell and
POSIX-compatible shells.

## Install

```bash
git clone https://github.com/RoboticHuman/OpenUSDConnect.git
cd OpenUSDConnect
uv sync --group server --group dashboard
uv run python scripts/build_blender_addon.py
```

The build creates `dist/usd_connect_blender.zip`. In Blender:

1. Open **Edit > Preferences > Add-ons**.
2. Choose **Install from Disk**.
3. Select `dist/usd_connect_blender.zip`.
4. Enable **USD Connect** if Blender does not enable it automatically.

The add-on contains the OpenUSDConnect Python client. Blender does not need the
repository's Python environment.

## Start The Server

Run this from the repository root:

```bash
uv run openusdconnect-server --base test_scene.usda --port 7200 --dashboard-port 8080
```

This starts the sync server on `127.0.0.1:7200` and the dashboard at
<http://127.0.0.1:8080>. Keep the terminal running. The walkthrough is local and
does not require an external service. Startup is complete when the terminal
reports `Server listening on 127.0.0.1:7200` and
`Dashboard running on http://localhost:8080`.

## Connect Blender

In the first Blender instance:

1. Press `N` in the 3D viewport and open the **USD Connect** tab.
2. In the **USD Connect** tab, choose **Import USD (with prim tagging)** and
   select `test_scene.usda`. Do not use Blender's standard **File > Import**
   command. This add-on command records the USD path represented by each
   imported Blender object so later edits can be matched correctly.
3. Under **Network Emitter**, confirm the host and port are
   `127.0.0.1:7200`, then choose **Connect Emitter**.
4. Under **Network Receiver**, confirm the same host and port, then choose
   **Start Receiver**.
5. Confirm that the panel shows **Emitter connected** and **Receiver running**.

**Connect Emitter** also begins monitoring supported Blender edits. Open a
second Blender instance and repeat the same steps. Move, rotate, or scale any
imported object in either instance; the matching object in the other instance
should update shortly.

The emitter sends local edits to the server. The receiver applies server edits
in Blender. The original USD file is the common starting point and is not
overwritten; the server stores synchronized changes in its event history and
collaboration layers.

## Alternative: Open A Server-Provided USD File

The server-provided file workflow is an optional alternative to opening the base
file and entering the connection fields manually. It creates a local
`scene.usd` containing the current scene state and sync server address. An
integrated application opens that file, then sends and receives live changes
through the same TCP sync server used above.

Stop the standard server with `Ctrl+C`, then run:

```bash
uv sync --group server --group vfs --group dashboard
uv run python scripts/start_live_open.py --base test_scene.usda --dashboard-port 8080 --open
```

The launcher starts:

- The TCP sync server on `127.0.0.1:7200`
- The WebDAV endpoint on `127.0.0.1:7280`
- A write-capable local mirror containing `scene.usd`
- A Windows drive alias when running on Windows
- The dashboard at <http://127.0.0.1:8080>

It prints the exact file to open:

| Platform | Live USD file |
| --- | --- |
| Windows | `O:\scene.usd` |
| macOS | `<repo>/.ouc_live_mount/usd/scene.usd` |
| Linux | `<repo>/.ouc_live_mount/usd/scene.usd` |

`<repo>` means the cloned OpenUSDConnect directory. The `--open` option opens
the generated file's folder in Explorer, Finder, or the platform file manager;
it does not start Blender or import the file.

In Blender, leave **Auto-start Emitter** and **Auto-start Receiver** enabled,
choose **Import USD (with prim tagging)**, and select the reported `scene.usd`.
The add-on reads the `openusdconnect` metadata, configures the server address,
and starts after the last event represented in the file. Repeat in a second
Blender instance to test bidirectional editing.

The manual connect, disconnect, start, and stop controls remain available.
Disable **Auto-start Emitter** for receive-only use, or disable **Auto-start
Receiver** for send-only use.

On Windows, the drive is a local `subst` alias and does not require the
WebClient service or administrator access. If `O:` is occupied, use `--drive
P:` or `--no-drive` and open the printed mirror path.

## Inspect The Session

Open <http://127.0.0.1:8080>. After moving an object, confirm that:

- Both Blender clients appear in the client list.
- The sequence number at the top of the dashboard advances.
- A transform transaction appears in the event list.
- The server and both Blender instances agree on the final transform.

For a live-open session, the served snapshot is also available at
<http://127.0.0.1:7280/usd/scene.usd>. DCC file pickers should use the reported
local path or a native mount rather than the diagnostic HTTP URL.

## Stop The Session

Stop a standard server with `Ctrl+C` in its terminal.

The event log remains on disk after the server stops. Starting the server again
with the same event log resumes that session. Use a different `--event-log`
path when you need an independent test session.

Stop every process recorded by the live-open launcher and release its Windows
drive alias with:

```bash
uv run python scripts/start_live_open.py stop
```

## Common Problems

### A port is already in use

For the standard server, choose unused sync and dashboard ports:

```bash
uv run openusdconnect-server --base test_scene.usda --port 7210 --dashboard-port 8090
```

Set both Blender endpoint controls to port `7210`.

For live-open, choose a matching set of unused ports:

```bash
uv run python scripts/start_live_open.py --base test_scene.usda --port 7210 --vfs-port 7290 --dashboard-port 8090 --open
```

The live-open ports are embedded into `scene.usd` and discovered during import.

### The server reports an unsupported schema version

An event log from an older, pre-release protocol may not be replayable. Preserve
the old log and select a new one for a standard server:

```bash
uv run openusdconnect-server --base test_scene.usda --event-log current_events.db --port 7200 --dashboard-port 8080
```

For live-open, select a new log directory:

```bash
uv run python scripts/start_live_open.py --base test_scene.usda --log-dir .ouc_live_mount/current --dashboard-port 8080 --open
```

Do not overwrite the old database when its events still need inspection or
migration.

### Blender imports the scene but does not connect

- For the standard workflow, confirm both endpoint controls match the server
  and choose **Connect Emitter** and **Start Receiver**.
- For live-open, confirm that you imported the generated `scene.usd`, not the
  original base file, and inspect the emitter and receiver status messages.
- Confirm that the server terminal is still running.
- Reinstall the freshly built add-on ZIP if Blender is using an older version.

A live-open import remains usable as a static snapshot if its connection fails.

### The live-open drive letter is unavailable

Choose a different drive or skip the alias:

```powershell
uv run python scripts/start_live_open.py --base test_scene.usda --dashboard-port 8080 --drive P: --open
```

```powershell
uv run python scripts/start_live_open.py --base test_scene.usda --dashboard-port 8080 --no-drive --open
```

### You need a native WebDAV mount

The local mirror is recommended because it supports DCC save patterns without
administrator access. Native Windows UNC and macOS WebDAV mounts are available
for environments that require them; see [server-provided USD files](live-open.md).

## Next Steps

- [Documentation index](README.md)
- [Blender add-on controls and layered workflows](blender-addon-usage.md)
- [Unreal Engine integration](../integrations/unreal/OpenUSDConnect/README.md)
- [Server-provided file paths and write fallback](live-open.md)
- [Run the material and instancing demos](../examples/README.md)
- [Connect an MCP client](mcp-server-usage.md)
- [Build a USD-native integration](usd-native-integration.md)
