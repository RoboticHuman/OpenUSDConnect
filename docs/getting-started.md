# Getting started with Blender

## Requirements

- Windows, macOS, or Linux
- [Git](https://git-scm.com/)
- Python 3.13 or newer
- [uv](https://docs.astral.sh/uv/)
- Blender 4.4 or newer

Python and `uv` run the server and build the add-on. Blender supplies its own
Python and OpenUSD runtime. Unless marked otherwise, the commands work in
PowerShell and POSIX shells.

## Install

```bash
git clone https://github.com/RoboticHuman/OpenUSDConnect.git
cd OpenUSDConnect
```

If you used `scripts/build_openusd.py`, its completed installation is selected
automatically and this step is unnecessary. For an OpenUSD installation built
outside the repository workflow, configure it in the current shell.

PowerShell:

```powershell
.\scripts\openusd_env.ps1 "D:\OpenUSDInstall"
```

Bash or Zsh:

```bash
source scripts/openusd_env.sh /opt/OpenUSDInstall
```

The [OpenUSD build guide](building-openusd.md) covers the managed builder,
custom plugin paths, and renderer setup.
Install the dashboard dependency and build the add-on:

```bash
uv sync --group dashboard
uv run python scripts/build_blender_addon.py
```

For a renderer-neutral session without MaterialX or custom plugins, run
`uv sync --group bundled-usd --group dashboard` instead.

The build creates `dist/usd_connect_blender.zip`. In Blender:

1. Open **Edit > Preferences > Add-ons**.
2. Choose **Install from Disk**.
3. Select `dist/usd_connect_blender.zip`.
4. Enable **USD Connect** if Blender does not enable it automatically.

The add-on contains the OpenUSDConnect Python client. Blender does not need the
repository's Python environment. The product and server are named
**OpenUSDConnect**; the shorter **USD Connect** label is the Blender sidebar and
add-on name.

## Start the server

Run this from the repository root:

```bash
uv run openusdconnect-server --base test_scene.usda --event-log getting-started.db --export-diff getting-started-changes.usda --port 7200 --dashboard-port 8080
```

The sync server listens on `127.0.0.1:7200`; the dashboard is at
<http://127.0.0.1:8080>. Keep the terminal running. The walkthrough is local and
does not require an external service. Startup is complete when the terminal
reports `Server listening on 127.0.0.1:7200` and
`Dashboard running on http://localhost:8080`.

The dashboard listener currently binds to all network interfaces. Keep it
behind a trusted firewall, or omit `--dashboard-port` when dashboard access is
not needed.

## Connect Blender

In the first Blender instance:

1. Remove Blender's default cube, camera, and light so they cannot be mistaken
   for imported scene objects.
2. Press `N` in the 3D viewport and open the **USD Connect** tab.
3. In the **USD Connect** tab, choose **Import USD (with prim tagging)** and
   select `test_scene.usda`. Do not use Blender's standard **File > Import**
   command. This add-on command records the USD path represented by each
   imported Blender object so later edits can be matched correctly.
4. Under **Network Receiver**, confirm the host and port are
   `127.0.0.1:7200`, then choose
   **Start Receiver**.
5. Under **Network Emitter**, confirm the same host and port, then choose
   **Connect Emitter**.
6. Confirm that the panel shows **Receiver running** and **Emitter connected**.

**Connect Emitter** also starts monitoring supported Blender edits. Open a
second Blender process and repeat the same steps. On macOS, use
`open -n -a Blender` if the Dock reuses the first process. Select the imported
`Sphere`, `Cone`, or `Cylinder` in either instance and move, rotate, or scale
it; the matching object in the other instance should update shortly.

The emitter publishes local edits; the receiver applies committed server
events. The original USD file is only the common baseline and is never
overwritten. The server stores edits in its event log and collaboration
layers.

## Alternative: open a server-provided USD file

Live-open creates a local `scene.usd` containing a snapshot and the sync server
address. An integrated application can open that file and continue from its
embedded sequence number, avoiding separate base-file and endpoint setup.

Stop the standard server with `Ctrl+C`, then run:

```bash
uv sync --group vfs --group dashboard
uv run python scripts/start_live_open.py --base test_scene.usda --dashboard-port 8080 --open
```

Bundled fallback users must also include `--group bundled-usd` in the `uv sync`
command.

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

The launcher records its processes in
`<repo>/.ouc_live_mount/live_open_session.json`. Run the matching `stop`
command from the same repository directory. For concurrent sessions, give each
one a different `--state-file`, `--log-dir`, ports, mirror directory, and drive
alias, then pass the same `--state-file` to its `stop` command.

In Blender, leave **Auto-start Emitter** and **Auto-start Receiver** enabled,
choose **Import USD (with prim tagging)**, and select the reported `scene.usd`.
The add-on reads the `openusdconnect` metadata, configures the server address,
and starts after the last event represented in the file. Repeat in a second
Blender instance to test bidirectional editing.

The manual connect, disconnect, start, and stop controls remain available.
Disable **Auto-start Emitter** for receive-only use, or disable **Auto-start
Receiver** for send-only use.

On Windows, the drive is a local `subst` alias and does not require the
WebClient service or administrator access. Run the launcher and Blender under
the same Windows user and elevation level because drive aliases can differ
between elevated and unelevated sessions. The alias does not survive a reboot.
If `O:` is occupied, use `--drive P:` or `--no-drive` and open the printed
mirror path.

## Inspect the session

Open <http://127.0.0.1:8080>. After moving an object, confirm that:

- Two emitter and two receiver connections appear in the client list.
- The sequence number at the top of the dashboard advances.
- A transform transaction appears in the event list.
- The server and both Blender instances agree on the final transform.

For a live-open session, the served snapshot is also available at
<http://127.0.0.1:7280/usd/scene.usd>. DCC file pickers should use the reported
local path or a native mount rather than the diagnostic HTTP URL.

## Stop the session

Stop a standard server with `Ctrl+C` in its terminal.

The event log remains on disk after the server stops. Starting the server again
with `--event-log getting-started.db` resumes that session. On shutdown,
`getting-started-changes.usda` contains the server-authored collaboration
changes and can be composed over the unchanged `test_scene.usda`. Saving a
`.blend` file does not replace either server artifact.

For an independent test, choose a new `--event-log` and `--export-diff` name.
To reset this walkthrough completely, stop the server before deleting
`getting-started.db`, its `-wal`/`-shm` companions if present, and
`getting-started-changes.usda`.

Stop every process recorded by the live-open launcher and release its Windows
drive alias with:

```bash
uv run python scripts/start_live_open.py stop
```

## Common problems

### A port is already in use

First stop the earlier server terminal. For a live-open session, also run
`uv run python scripts/start_live_open.py stop` from the repository directory
that contains its state file. Use alternate ports only after confirming the
previous session is intentional or has stopped.

For the standard server, choose unused sync and dashboard ports:

```bash
uv run openusdconnect-server --base test_scene.usda --port 7210 --dashboard-port 8090
```

Set both Blender endpoint controls to port `7210`.

For live-open, choose a matching set of unused ports:

```bash
uv run python scripts/start_live_open.py --base test_scene.usda --port 7210 --vfs-port 7290 --dashboard-port 8090 --state-file .ouc_live_mount/session-7210.json --open
```

The live-open ports are embedded into `scene.usd` and discovered during import.
Stop this alternate session with:

```bash
uv run python scripts/start_live_open.py stop --state-file .ouc_live_mount/session-7210.json
```

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

The local mirror handles DCC save patterns without administrator access. Use a
native Windows UNC or macOS WebDAV mount only when a normal local path is not
suitable; see [server-provided USD files](live-open.md).

## Next steps

- [Documentation index](README.md)
- [Blender add-on controls and layered workflows](blender-addon-usage.md)
- [Unreal Engine integration](../integrations/unreal/OpenUSDConnect/README.md)
- [Server-provided file paths and write fallback](live-open.md)
- [Run the material and instancing demos](../examples/README.md)
- [Connect an MCP client](mcp-server-usage.md)
- [Build a USD-native integration](usd-native-integration.md)
