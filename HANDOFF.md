# OpenUSDConnect — Handoff Brief

## What it is
A real-time USD sync framework for DCC livelink: a Python server holds an authoritative `pxr.Usd.Stage`, and DCC clients (Blender, Unreal, usdview) send/receive incremental edits over TCP so multiple apps stay in sync against one USD scene.

## Current live-open status
- The server supports a WebDAV virtual USD directory via `--vfs-port`, defaulting to `/usd/`.
- `/usd/scene.usd` is the flattened fallback; `/usd/scene.live.usda` is the composition-aware root; `/usd/_layers/*.usda` exposes live layer exports; `/usd/openusdconnect.json` is the manifest.
- Virtual USD roots embed `customLayerData["openusdconnect"]` metadata for host, port, scene id, epoch, and `snapshot_seq`.
- Blender can import the WebDAV/UNC file or a live URL, read that metadata, and auto-connect emitter/receiver from `snapshot_seq + 1`.
- The VFS is read-only by default; `--vfs-write-mode drop` accepts/discards PUT writes, and `--vfs-write-mode translate` parses complete USD saves into live server events.
- `scripts/mount_vfs_share.py` maps the WebDAV share to a Windows drive letter, so artists can pick `O:\scene.usd` from normal file dialogs instead of pasting URLs.
- `scripts/local_vfs_drive_bridge.py` is the no-admin fallback when WebClient cannot start; it maps `O:` with `subst`, mirrors `scene.usd`, and PUTs local saves back to the VFS.
- Live URL imports use a local ETag-keyed cache rather than one-off temp files.
- VFS prewarm is enabled by default; `scripts/bench_vfs_snapshot.py` measures cold/cached snapshot generation.
- `scripts/check_windows_unc_webdav.py` validates Windows WebClient/UNC workstation setup.
- Snapshot `epoch` invalidates VFS cache entries for compaction, purge, and visible layer/proposal state changes that do not advance the event log sequence.
- Token-required servers now reject missing `client_id`; Blender receiver/emitter use the stable addon client id and TOFU token persistence.
- Production-facing docs live in `docs/live-open-quickstart.md` and `docs/live-open-production-guide.md`.

## Repo & running
- Root: `D:\Workspace\Git\OpenUSDConnect` (git; main branch `master`).
- Activate venv: `.\.venv\Scripts\activate`.
- Run server: `python -m openusdconnect.server --port 7200 --base test_scene.usda --dashboard 8080` (dashboard = NiceGUI on the given port; optional).
- Tests: `pytest` (`tests/unit` run an in-process `UsdSyncServer`; `tests/integration` are heavier). `tests/conftest.py` has a `free_port` fixture.

## Core sync library (`openusdconnect/`)
- `emitter.py` — `NoticeEmitter` watches a `Usd.Stage` via `Usd.Notice.ObjectsChanged`, diffs, and builds events.
- `event_apply.py` / `events.py` — appliers (one per event kind) that apply events to a stage; `apply_events`, `atomic_apply`.
- `receiver.py` — `ReceiverThread`: background TCP client (server→DCC), auto-reconnect, bounded queue, callbacks (`on_stage_metadata`, playback).
- `sender.py` — `EventSender`: TCP client (DCC→server), `connect()` does hello handshake, `send_events()`.
- `dispatcher.py` — `EventDispatcher.drain_and_apply()`: parse → stage commit → DCC adapter dispatch → emitter cache invalidation (feedback-loop guard).
- `codec.py` / `framing.py` — FlatBuffers encode/decode + 4-byte big-endian length-prefixed frames. **Custom codec on purpose**: `flatc --cpp` is avoided (allocator/Unity-build/`dynamic_cast`-macro conflicts under Unreal); a Python generator emits constants for the C++ side.
- `protocol_constants.py` — event-kind constants, `PROTOCOL_VERSION = 1`.

## Server (`openusdconnect/server/`)
- `state.py` — `UsdSyncServer`: the authoritative `self.stage` (opened from `--base` or in-memory `/Root`), an anonymous edit layer in the session layer, optional per-department layers, a SQLite `EventStore` (`self.store`), monotonic seq via `assign_seq()` under `_seq_lock`, a broadcast queue/thread, `process_txn` (apply→seq→persist→returns records to broadcast), `replay_from(handler, seq)`, and `compact_log()`/`purge()` (both **reset seq to 1** and trigger a resync). `_txn_barrier.py` is a shared/exclusive barrier (shared = txns, exclusive = compaction/purge); lock order is barrier → `stage_lock`/`_seq_lock`.
- `connection.py` — `ConnectionHandler` (per-client thread): hello handshake (role `emitter`/`receiver`, `client_id`, `origin`, `department`, `token`), TOFU auth, replay on connect, read loop dispatching Txn/playback/compact.
- `cli.py` — argparse → `run_server()` → `UsdSyncServer`. Flags: `--host --port --base --log --dashboard --departments --require-token --durability {strict,realtime} --max-connections --txn-rate --txn-burst`. New flags follow this pattern (plumb through `run_server` + `main`).
- Schema: `openusdconnect/schema/{messages,events}.fbs`. `Hello` carries role/client_id/origin/department/token/sync_from; `HelloOk` carries an issued token + stage metadata.

## Sync model (important)
- A client opens **two** connections (receiver + emitter) correlated by shared `client_id` + `origin`; the server uses `origin` to suppress echoing a client's own edits back to it.
- **Initial scene state is delivered by event replay**, not file transfer: a receiver sends `sync_from=N` and the server replays the log from seq N. Reconnect resumes at `last_seq + 1`. Events are idempotent/latest-wins (at-least-once safe).

## Integrations (`integrations/`)
- **`blender/`** — addon: `receiver_addon.py`, `capture.py` (emitter + `BlenderStageAuthor`), `ui.py`, `blender_adapter.py`. Blender **flattens USD to native objects on import** (no persistent live stage). Manual host/port still works, and live-open now auto-connects when imported snapshot metadata is present. Build the installable zip with `scripts/build_blender_addon.py` (copies all `*.py` from `integrations/blender/` + vendors `openusdconnect`). Unit tests mock `bpy`.
- **`unreal/OpenUSDConnect/`** — C++ UE5 plugin (`UTickableWorldSubsystem`, bridges `AUsdStageActor`). Uses the current **direct host/port connection**. **Must keep working** — any change has to stay backward-compatible with this.
- **`usdview/`**, **`dashboard/`** (NiceGUI) — receive-only / admin.

## Environment gotchas (verified, will bite)
- Windows 11 **Home**, PowerShell. MSVC 2022 + CMake on PATH (user builds Unreal from source).
- `import pxr` resolves through a user-level `PYTHONPATH=D:\Workspace\Git\OpenUSDInstall\lib\python` — a **Debug** OpenUSD build, version **0.26.2**. Anything compiled against it must be built `--config Debug` (/MDd) or it crashes at the DLL boundary. A pip `usd-core==26.3` wheel exists in the venv but is **shadowed** by that PYTHONPATH; tests that depend on the install should guard on `Usd.GetVersion()`.
- `uv sync` strips manually-installed packages (PySide6/PyOpenGL used by usdview) — restore with `uv pip install pyside6 pyopengl pyopengl-accelerate`.
- A few unit tests (`test_usdlux.py`, `test_usdgeomcamera.py`) fail/error due to **missing `assets/` files on disk** — pre-existing and unrelated to any code change.

## The task to (re)execute
Make a live, server-hosted USD scene openable from DCCs **as transparently as a normal USD file**: the user should not have to type a host/port or run a separate "connect" step, and opening the scene should establish live two-way sync automatically. Ideally the scene(s) the server hosts are browsable like normal files. Hard constraints:
1. Everything currently working must keep working — **especially the Unreal C++ plugin's existing direct host/port connection**.
2. Provide a graceful fallback: an app without the plugin should still be able to open a plain, readable USD snapshot of the current state.

Design the approach fresh (transport, how the connection details are discovered rather than typed, how DCCs open/browse it). Validate against a throwaway client/`test_scene.usda` before wiring into a real DCC.
