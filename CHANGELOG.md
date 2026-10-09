# Changelog

Notable changes to OpenUSDConnect are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, a minor
release may contain breaking changes; each such release starts with a
**Migrating** section.

The release version covers the Python package and the Blender add-on
(`openusdconnect/_version.py`). The Unreal plugin carries its own version in
`OpenUSDConnect.uplugin`. Each entry states wire compatibility separately: the
protocol version (`PROTOCOL_VERSION`) and wire schema version
(`SCHEMA_VERSION`) decide whether clients and servers from different releases
can connect.

## [0.5.0] - Unreleased

The high-level clients share one lifecycle, report all state through
`client.status`, and deliver host notifications through one `ClientObserver`.
Code written against 0.4 needs the changes in **Migrating from 0.4**.

**Wire compatibility:** protocol 13 and wire schema 10, unchanged. New
handshake and transaction-result fields are optional: 0.4.0 peers still
connect but get no replay-identity validation or checkpoints.

### Migrating from 0.4

Callbacks become one `observer=ClientObserver`:

- `on_applied`, `on_applied_events`, and `on_imported` become
  `on_applied(batch)`; read `batch.prim_paths`, `batch.events`, and
  `batch.imported_paths`.
- `on_playback_claimed` and `on_playback_rejected` become
  `on_playback_claim(result)`.
- Other `on_*` arguments become the observer method of the same name, which
  receives a typed value instead of a dict: `StageMetadata`, `PlaybackState`,
  or `PlaybackClaim` (a rejection's `current_leader_client_id` is its
  `leader_client_id`).
- For `UsdReceiver.applying_seq`, use `batch.seq` inside `on_applied`.

State moves to `client.status`:

- Properties keep their names there, except the event counts:
  `status.pending_events`, `prepared_events`, `deferred_events`, and
  `acknowledged_events_total` replace the `*_event_count` properties.
- `transaction_failure` and `recovery_incident` become `status.failure` and
  `status.recovery`; use `str(status.failure)` and
  `status.failure.disposition` for `transaction_error` and
  `recovery_disposition`.
- `recovery_required`, `native_scene_rebuild_required`, and
  `connection_rejected` are phases: check `status.phase` (and
  `status.auth_rejected` to tell rejections apart).
- `UsdReceiver.layered_replay_active` is removed. A connected receiver always
  has layered replay; the server's handshake is rejected otherwise.

Return values and defaults:

- `UsdReceiver.update()` and `UsdPublisher.update()` return `SyncUpdate`
  instead of `int`; read `applied_events` or `submitted_events`.
- `client.stage_metadata` returns `StageMetadata` instead of `dict`.
- `connect()` and `flush()` wait 10 seconds instead of indefinitely; pass
  `timeout=None` for no limit.

Behavior:

- Token, metadata, and playback notifications run during `update()` or
  `close()` on the calling thread instead of on network threads.
- Stage edits made in `on_resync` are no longer published, matching
  `on_applied`.
- `UsdPublisher.update()` raises before `start()`. While disconnected it
  reconnects in the background; `disconnect()` pauses that until `connect()`.
- `publish_current_edit_target()` captures its snapshot while disconnected and
  sends it from a later `update()`, instead of returning 0 without capturing.
- `ManagedClient.rebind_stage()` refuses unacknowledged work (finish with
  `submit_and_wait()`) and unsent edits unless `discard_unsent=True` drops
  them.
- `ManagedClient.flush()` raises when the server rejects the connection,
  like `UsdPublisher.flush()`, instead of returning `False`.
- `UsdReceiver.status` reports `CONNECTING` instead of `READY` while it
  reconnects, like the other clients.
- `ReceiverThread` is `EventReceiver` and no longer a `threading.Thread`:
  call `close(timeout)` instead of `stop()` and `join()`, and read `running`
  instead of `is_alive()`. On it and on `EventSender`, settings and state are
  read-only properties (`token`, and the receiver's `reconnect`, stay
  assignable) and `sock` is gone; read `connected`. Both close on leaving a
  `with` block, and a collected one stops its thread, so keep the handle
  while it should run.

The low-level `EventSender`, `EventReceiver`, and `EventDispatcher` keep their
callable arguments and properties.

### Added

- `ClientObserver` and its typed payloads: `AppliedBatch`, `StageMetadata`,
  `PlaybackState`, `PlaybackClaim`.
- `wait_until_ready()` and `submit_and_wait()`. They return `False` only on
  timeout and raise for states that more updates cannot fix.
- `update(max_messages=)` on `ManagedClient` and `SharedStageClient` spreads a
  reconnect backlog over frames; local edits wait only for the messages queued
  before them.
- `ClientStatus` fields `auth_rejected`, `has_unsent_changes`,
  `deferred_events`, `deferred_layer_keys`, `edit_target_is_published`, and
  `recovery_stage_pending`; the `can_author` property; `ClientPhase.PARKED`.
- `SharedStageClient.resume_recovery()` (error code
  `no_pending_recovery_stage`).
- `claim_playback()` and `send_playback_control()` on `SharedStageClient` and
  `UsdPublisher`.
- `token_provider=` on `EventSender` and `EventReceiver` supplies the token
  for each connection attempt.
- `EventDispatcher.drained_message_count`, `EventReceiver.stopped`, and
  `NoticeEmitter.has_local_changes`.
- Receiver replay identity and optional post-commit transaction checkpoints.

### Changed

- `EventDispatcher` starts its cursor at `receiver.sync_from - 1`, so
  integrations no longer seed `last_seq` for continuation.
- `EventSender` and `EventReceiver` run their connections on native threads
  in the client core: transaction writes leave the calling thread and need no
  GIL, callbacks and the token provider run on the connection thread, and
  `close()` interrupts a pending connect or read at once.
- `EventSender.connect()` waits for an attempt already in flight before
  making its own; a token provider that raises is logged and fails that
  attempt instead of raising. While recovery is required, `rejection_reason`
  names the failure.
- Building the native extension fetches the pinned FlatBuffers headers on the
  first configure, which needs network access unless
  `FETCHCONTENT_SOURCE_DIR_FLATBUFFERS` names a local copy.

### Fixed

- A receiver continuing from a live-open snapshot replayed the full history
  over it when the integration did not seed the dispatcher cursor.
- MCP writes were confirmed before the mirror applied them.
- `EventSender.send_events()` returns `False` for a transaction above the
  16 MiB frame limit instead of queueing one that made the server close the
  connection on every replay.
- Replay completion markers were lost when a resync reset applied progress.
- The emitter dropped property edits absorbed by a prim resync.
- Bidirectional clients read the token file on every `update()` while their
  sender reconnected.
- Retries against an unreachable server logged a traceback each time.
- `NoticeEmitter.rebind_stage()` compared stage metadata against the previous
  stage, and `cleanup()` kept pending stage-metadata changes.
- A `ManagedClient` constructed with invalid options left the stage modified.

## [0.4.0] - 2026-09-18

**Wire compatibility:** protocol 13 (from 12), wire schema 10. Clients and
servers must both be 0.4.0 or later.

### Added

- `ServerRuntime`, `ServerConfig`, and `start_server()` embed the server in an
  application without signal handlers; `run_server()` remains the blocking
  command-line runner.
- `ClientPhase`, `ClientStatus`, and `SyncUpdate` in
  `openusdconnect.client_types`, `client.status`, and `client.client_id`.
- `EventSender.request_connect()` and `cancel_connect()` for nonblocking
  handshakes; bidirectional clients reconnect their sender from `update()`.
- `openusdconnect.shader_mapping` for shader mapper interfaces and
  `openusdconnect.usd_authoring` helpers (`set_connectable_input_value`,
  `resolve_shader_port_type`).
- Distributable server packages and configurable OpenUSD runtimes.
- Targeted time-sample erasure with native Sdf tracking.

### Changed

- The native Sdf notice bridge ABI is version 2; rebuild it with
  `openusdconnect-build-sdf-notice-bridge`.

### Fixed

- Time-sample deletions keep their order through application and log
  compaction.

## [0.3.0] - 2026-08-30

**Wire compatibility:** protocol 12, wire schema 10.

### Added

- Shared native C++ client core (framing, producer outbox, receiver inbox)
  used by the Python client through nanobind and by the Unreal plugin.
- `UsdReceiver(adapter=...)` projects composed changes into non-USD
  native scenes.
- Cross-platform OpenUSD runtime configuration and managed OpenUSD source
  builds.

### Changed

- `uv sync` builds the native client extension, which requires CMake and a
  64-bit C++17 toolchain. The Blender add-on vendors a build for Blender's
  Python.

## [0.2.0] - 2026-08-18

First versioned release. Protocol 12, wire schema 10. The release version is
defined once in `openusdconnect/_version.py` and mirrored by the Blender
add-on.
